"""FastAPI app — thin HTTP layer over the main agent."""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import uuid
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..agent.main_agent import run_analysis_phase, run_generate_phase
from ..shared.browser import BrowserManager
from ..shared.events import EventBus
from ..shared.schemas import AnalysisResult, DownloadResult, GenerateRequest
from ..shared.storage import JobStorage
from ..shared.trace import TraceCollector, reset_collector, set_collector, setup_tracing

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
setup_tracing()
logger = logging.getLogger(__name__)


app = FastAPI(title="Web Style Reference Tool")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── In-memory job registry ──────────────────────────────────────────────
#
# One entry per active job. We persist analysis results in-memory so the
# generate endpoint can reach back to them by job_id.

class Job:
    def __init__(self, job_id: str):
        self.job_id = job_id
        # One bus per phase so the SSE stream can close cleanly on `done`.
        self.analyze_bus = EventBus()
        self.generate_bus: EventBus | None = None
        self.status: str = "running"
        self.error: str | None = None
        self.analysis: AnalysisResult | None = None
        self.download: DownloadResult | None = None
        self.storage: JobStorage | None = None
        self.generated_html: str | None = None
        self.trace = TraceCollector()

    def bus_for(self, phase: str) -> EventBus | None:
        if phase == "analyze":
            return self.analyze_bus
        if phase == "generate":
            return self.generate_bus
        return None


_jobs: dict[str, Job] = {}


# ── Request models ──────────────────────────────────────────────────────

class AnalyzeReq(BaseModel):
    url: str


class GenerateReq(BaseModel):
    job_id: str
    site_type: str
    pages: list[str] = []
    extra_instructions: str = ""


# ── Background workers ──────────────────────────────────────────────────

async def _run_analyze_job(job_id: str, url: str) -> None:
    job = _jobs[job_id]
    bus = job.analyze_bus
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    browser = BrowserManager()
    tok = set_collector(job.trace)
    try:
        await bus.publish("status", message=f"▶️  Starting analysis of {url}")
        storage = JobStorage(url, timestamp)
        job.storage = storage
        # Stream trace events to disk so the full log is recoverable after
        # the backend restarts or the request completes.
        job.trace.attach_log_file(storage.base_dir / "trace.jsonl")
        logger.info("Trace log → %s", storage.base_dir / "trace.jsonl")
        await browser.launch()
        download, analysis = await run_analysis_phase(url, browser, storage, bus)
        job.download = download
        job.analysis = analysis
        job.status = "done"
        await bus.publish(
            "done",
            phase="analysis",
            job_id=job_id,
            download=download.model_dump(),
            analysis=analysis.model_dump(),
        )
    except Exception as exc:
        logger.exception("Analysis job %s failed", job_id)
        job.status = "error"
        job.error = str(exc)
        await bus.publish("error", message=str(exc))
    finally:
        await browser.close()
        try:
            if job.storage is not None:
                job.trace.dump(job.storage.base_dir / "trace.json")
        except Exception:
            logger.exception("Failed to dump trace.json")
        reset_collector(tok)


async def _run_generate_job(job_id: str, req: GenerateReq) -> None:
    job = _jobs[job_id]
    bus = job.generate_bus
    if bus is None or not job.analysis or not job.storage:
        return
    tok = set_collector(job.trace)
    try:
        await bus.publish(
            "status",
            message=f"▶️  Starting generation: {req.site_type} — {', '.join(req.pages) or 'single-page'}",
        )
        request = GenerateRequest(
            site_type=req.site_type,
            pages=req.pages,
            extra_instructions=req.extra_instructions,
        )
        site = await run_generate_phase(request, job.analysis, job.download, job.storage, bus)
        job.generated_html = site.html
        await bus.publish("done", phase="generate", job_id=job_id, html=site.html)
    except Exception as exc:
        logger.exception("Generate job %s failed", job_id)
        await bus.publish("error", message=str(exc))
    finally:
        try:
            if job.storage is not None:
                job.trace.dump(job.storage.base_dir / "trace.json")
        except Exception:
            logger.exception("Failed to dump trace.json")
        reset_collector(tok)


# ── Routes ──────────────────────────────────────────────────────────────

@app.post("/api/analyze")
async def start_analyze(req: AnalyzeReq):
    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = Job(job_id)
    asyncio.create_task(_run_analyze_job(job_id, req.url))
    return {"job_id": job_id, "status": "running"}


@app.post("/api/generate")
async def start_generate(req: GenerateReq):
    job = _jobs.get(req.job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.analysis:
        raise HTTPException(409, "Analysis not yet complete")
    job.generate_bus = EventBus()  # fresh bus for the generate phase
    asyncio.create_task(_run_generate_job(req.job_id, req))
    return {"status": "started", "job_id": req.job_id}


@app.get("/api/stream/{job_id}")
async def stream(job_id: str, phase: str = "analyze"):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    bus = job.bus_for(phase)
    if bus is None:
        raise HTTPException(404, f"No active {phase} stream for this job")

    async def _gen():
        while True:
            event = await bus.get()
            yield {"event": event.event, "data": json.dumps(event.data, default=str)}
            if event.event in ("done", "error"):
                break

    return EventSourceResponse(_gen())


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": job_id,
        "status": job.status,
        "error": job.error,
        "analysis": job.analysis.model_dump() if job.analysis else None,
        "download": job.download.model_dump() if job.download else None,
        "generated_html": job.generated_html,
    }


# ── Static: serve saved screenshots & generated sites ──────────────────
_OUTPUT_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "output"
_OUTPUT_DIR.mkdir(exist_ok=True)
app.mount("/output", StaticFiles(directory=str(_OUTPUT_DIR)), name="output")


@app.get("/api/trace/{job_id}")
async def get_trace(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"job_id": job_id, "events": job.trace.events}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "jobs_in_memory": len(_jobs)}
