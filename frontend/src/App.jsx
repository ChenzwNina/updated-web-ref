import { useState, useRef } from "react";
import UrlInput from "./components/UrlInput.jsx";
import ProgressIndicator from "./components/ProgressIndicator.jsx";
import ComponentGallery from "./components/ComponentGallery.jsx";
import GenerateForm from "./components/GenerateForm.jsx";
import PreviewFrame from "./components/PreviewFrame.jsx";
import TraceViewer from "./components/TraceViewer.jsx";

const STAGES = {
  idle: { label: "Ready", pct: 0 },
  starting: { label: "Starting…", pct: 5 },
  scanning: { label: "Scanning navigation links", pct: 15 },
  subpages: { label: "Picking representative subpages", pct: 25 },
  downloading: { label: "Downloading pages", pct: 40 },
  analyzing: { label: "Analyzing components", pct: 65 },
  validating: { label: "Validating & fixing previews", pct: 85 },
  ready: { label: "Analysis complete", pct: 100 },
  generating: { label: "Generating your site", pct: 60 },
  done: { label: "Site generated", pct: 100 },
  error: { label: "Error", pct: 100 },
};

export default function App() {
  const [jobId, setJobId] = useState(null);
  const [phase, setPhase] = useState("idle");
  const [stage, setStage] = useState("idle");
  const [pagesDone, setPagesDone] = useState(0);
  const [componentsFound, setComponentsFound] = useState(0);
  const [analysis, setAnalysis] = useState(null);
  const [download, setDownload] = useState(null);
  const [generatedHtml, setGeneratedHtml] = useState(null);
  const [showTrace, setShowTrace] = useState(false);
  const [errorMsg, setErrorMsg] = useState(null);
  const streamRef = useRef(null);

  const attachStream = (id, phaseName, onDone) => {
    const es = new EventSource(`/api/stream/${id}?phase=${phaseName}`);
    streamRef.current = es;

    es.addEventListener("status", (e) => {
      try {
        const d = JSON.parse(e.data);
        const m = (d.message || "").toLowerCase();
        if (m.includes("scanning")) setStage("scanning");
        else if (m.includes("picked subpages") || m.includes("asking subagent")) setStage("subpages");
        else if (m.includes("downloaded") && phaseName === "analyze") {
          setStage("downloading");
          setPagesDone((n) => n + 1);
        } else if (m.includes("analyzing") || m.includes("preparing") || m.includes("prepared") || m.includes("single sonnet call") || m.includes("extracted") || m.includes("visual analysis")) {
          setStage("analyzing");
        } else if (m.includes("render test") || m.includes("regenerating") || m.includes("validation complete")) {
          setStage("validating");
        }
      } catch {}
    });

    es.addEventListener("skill_start", (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.skill === "download_website") setStage("downloading");
        if (d.skill === "analyze_components" || d.skill === "analyze_screenshots") setStage("analyzing");
        if (d.skill === "validate_components") setStage("validating");
        if (d.skill === "generate_website") setStage("generating");
      } catch {}
    });

    es.addEventListener("skill_end", (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.skill === "analyze_components" && d.components != null) {
          setComponentsFound(d.components);
        }
      } catch {}
    });

    es.addEventListener("error", (e) => {
      let msg = "Something went wrong.";
      try {
        const d = JSON.parse(e.data);
        if (d.message) msg = d.message;
      } catch {}
      setErrorMsg(msg);
      setPhase("error");
      setStage("error");
      es.close();
    });

    es.addEventListener("done", (e) => {
      let d;
      try { d = JSON.parse(e.data); } catch { return; }
      onDone?.(d);
      es.close();
    });
  };

  const handleAnalyze = async (url) => {
    setAnalysis(null);
    setDownload(null);
    setGeneratedHtml(null);
    setErrorMsg(null);
    setPagesDone(0);
    setComponentsFound(0);
    setStage("starting");
    setPhase("analyzing");

    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const { job_id } = await res.json();
      setJobId(job_id);

      attachStream(job_id, "analyze", (d) => {
        setDownload(d.download);
        setAnalysis(d.analysis);
        setStage("ready");
        setPhase("ready");
      });
    } catch (e) {
      setErrorMsg(String(e));
      setPhase("error");
      setStage("error");
    }
  };

  const handleGenerate = async ({ site_type, pages, extra_instructions }) => {
    if (!jobId) return;
    setPhase("generating");
    setStage("generating");
    setGeneratedHtml(null);

    const res = await fetch("/api/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, site_type, pages, extra_instructions }),
    });
    if (!res.ok) {
      setErrorMsg(
        res.status === 404
          ? "Job expired (the backend may have restarted). Please run a new analysis first."
          : `Failed to start generation (${res.status})`
      );
      setPhase("error");
      return;
    }
    attachStream(jobId, "generate", (d) => {
      setGeneratedHtml(d.html);
      setPhase("done");
      setStage("done");
    });
  };

  const restart = () => {
    setJobId(null);
    setPhase("idle");
    setStage("idle");
    setAnalysis(null);
    setDownload(null);
    setGeneratedHtml(null);
    setErrorMsg(null);
    setPagesDone(0);
    setComponentsFound(0);
  };

  const isStep1 = !analysis; // no analysis yet → progress view
  const stageInfo = STAGES[stage] || STAGES.idle;

  return (
    <div style={styles.page}>
      <header style={styles.header}>
        <h1 style={styles.title}>Web Style Reference</h1>
        <p style={styles.subtitle}>
          Paste a URL. We'll capture its visual design and let you generate a new site in the same style.
        </p>
      </header>

      {isStep1 && (
        <>
          <UrlInput
            onSubmit={handleAnalyze}
            disabled={phase === "analyzing"}
          />
          {phase !== "idle" && (
            <ProgressIndicator
              stage={stageInfo.label}
              pct={stageInfo.pct}
              stats={[
                { label: "Pages", value: pagesDone },
                { label: "Components", value: componentsFound },
              ]}
            />
          )}
          {errorMsg && <div style={styles.error}>{errorMsg}</div>}
          {jobId && (
            <TraceToggle showTrace={showTrace} setShowTrace={setShowTrace} jobId={jobId} />
          )}
        </>
      )}

      {!isStep1 && (
        <>
          <div style={styles.crumb}>
            <button onClick={restart} style={styles.crumbBtn}>← New analysis</button>
            <span style={styles.crumbText}>Analyzed {download?.root_url}</span>
          </div>

          <ComponentGallery analysis={analysis} download={download} />

          <GenerateForm
            onGenerate={handleGenerate}
            disabled={phase === "generating"}
          />

          {(phase === "generating" || phase === "done") && (
            <ProgressIndicator
              stage={stageInfo.label}
              pct={stageInfo.pct}
              stats={phase === "done" && generatedHtml ? [
                { label: "Chars", value: generatedHtml.length.toLocaleString() },
              ] : []}
            />
          )}

          {errorMsg && <div style={styles.error}>{errorMsg}</div>}

          {generatedHtml && (
            <PreviewFrame html={generatedHtml} onClose={() => setGeneratedHtml(null)} />
          )}

          <TraceToggle showTrace={showTrace} setShowTrace={setShowTrace} jobId={jobId} />
        </>
      )}
    </div>
  );
}

function TraceToggle({ showTrace, setShowTrace, jobId }) {
  return (
    <div style={{ marginTop: 16 }}>
      <button
        onClick={() => setShowTrace((v) => !v)}
        style={{
          background: showTrace ? "#4f46e5" : "#fff",
          color: showTrace ? "#fff" : "#4f46e5",
          border: "1px solid #4f46e5",
          borderRadius: 8,
          padding: "6px 14px",
          fontSize: 13,
          cursor: "pointer",
          fontWeight: 500,
        }}
      >
        {showTrace ? "▾ Hide" : "▸ Show"} execution trace
      </button>
      {showTrace && <TraceViewer jobId={jobId} />}
    </div>
  );
}

const styles = {
  page: {
    maxWidth: 1280,
    margin: "0 auto",
    padding: "32px 24px 80px",
  },
  header: { textAlign: "center", marginBottom: 28 },
  title: {
    fontSize: 32, fontWeight: 700, letterSpacing: "-0.02em",
    color: "#111827", margin: 0,
  },
  subtitle: {
    marginTop: 8,
    fontSize: 14,
    color: "#6b7280",
    maxWidth: 560,
    margin: "8px auto 0",
  },
  crumb: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    marginBottom: 18,
    fontSize: 13,
  },
  crumbBtn: {
    background: "#fff",
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    padding: "6px 12px",
    color: "#4b5563",
    cursor: "pointer",
    fontSize: 12,
  },
  crumbText: { color: "#6b7280" },
  error: {
    background: "#fef2f2",
    border: "1px solid #fecaca",
    color: "#991b1b",
    padding: "10px 14px",
    borderRadius: 8,
    fontSize: 13,
    marginBottom: 16,
  },
};
