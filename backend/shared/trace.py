"""Simple call-trace logger.

Apply `@traced` to any sync or async function. Entry/exit lines print to the
"trace" logger with depth-based indentation, file path + function name, a
short arg summary, and elapsed time. Use `metric()` to log freeform
diagnostic numbers (e.g. HTML sizes, counts before/after dedupe).

Example output:
    ▸ backend/agent/main_agent.py::run_analysis_phase(url='https://stripe.com')
      ▸ backend/skills/download_website/skill.py::run_download_skill(root_url='...')
        ▸ backend/shared/browser.py::find_nav_links(url='...', max_links=20)
        ◂ find_nav_links → list[NavLink] len=14  [621ms]
        ▸ backend/skills/download_website/subagent.py::pick_subpages(...)
        ◂ pick_subpages → list[NavLink] len=3  [1810ms]
        · metric  download.pages=4 total_html_chars=420810
      ◂ run_download_skill → DownloadResult  [8142ms]
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import pathlib
import time
from contextvars import ContextVar
from typing import Any, Callable, TypeVar

logger = logging.getLogger("trace")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
_depth: ContextVar[int] = ContextVar("_trace_depth", default=0)


class TraceCollector:
    """In-memory structured trace buffer for a single job.

    Events are flat dicts — the frontend reconstructs a tree from `depth`.
    """

    def __init__(self, log_path: pathlib.Path | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self._seq = 0
        self._log_path = log_path
        self._log_fh = None
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log_fh = log_path.open("a", encoding="utf-8", buffering=1)
            except Exception as exc:  # pragma: no cover — best-effort logging
                logger.warning(f"TraceCollector: cannot open {log_path}: {exc}")

    def attach_log_file(self, log_path: pathlib.Path) -> None:
        """Start streaming events to a JSONL file (idempotent)."""
        if self._log_fh is not None:
            return
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = log_path.open("a", encoding="utf-8", buffering=1)
            self._log_path = log_path
            # Backfill events we already buffered before the file was attached.
            for ev in self.events:
                self._write(ev)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"TraceCollector: cannot open {log_path}: {exc}")

    def _write(self, ev: dict) -> None:
        fh = self._log_fh
        if fh is None:
            return
        try:
            fh.write(json.dumps(ev, default=str) + "\n")
        except Exception:
            pass

    def close(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    def dump(self, path: pathlib.Path) -> None:
        """Write the full event list as one pretty JSON document."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"events": self.events}, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"TraceCollector: cannot write {path}: {exc}")

    def _add(self, **kw: Any) -> None:
        self._seq += 1
        kw.setdefault("seq", self._seq)
        kw.setdefault("t", time.time())
        self.events.append(kw)
        self._write(kw)

    def enter(self, depth: int, label: str, args: str) -> None:
        self._add(type="enter", depth=depth, label=label, args=args)

    def exit(self, depth: int, label: str, result: str, ms: float) -> None:
        self._add(type="exit", depth=depth, label=label, result=result, ms=round(ms, 1))

    def error(self, depth: int, label: str, err: str, ms: float) -> None:
        self._add(type="error", depth=depth, label=label, err=err, ms=round(ms, 1))

    def metric(self, depth: int, kv: dict[str, Any]) -> None:
        self._add(type="metric", depth=depth, kv={k: _short(v) for k, v in kv.items()})

    def note(self, depth: int, msg: str) -> None:
        self._add(type="note", depth=depth, msg=msg)

    def llm_call(
        self,
        depth: int,
        *,
        role: str,
        model: str,
        system: str,
        user_content: Any,
        max_tokens: int,
    ) -> int:
        """Record an outgoing LLM request. Returns the event seq so the
        matching response can back-reference it."""
        self._seq += 1
        seq = self._seq
        ev = {
            "seq": seq,
            "t": time.time(),
            "type": "llm_call",
            "depth": depth,
            "role": role,  # "main_agent" | "subagent"
            "model": model,
            "system": system,
            "user_content": _serialize_user_content(user_content),
            "max_tokens": max_tokens,
        }
        self.events.append(ev)
        self._write(ev)
        return seq

    def tool_call(self, depth: int, *, name: str, args: dict, tool_use_id: str) -> int:
        self._seq += 1
        seq = self._seq
        ev = {
            "seq": seq, "t": time.time(),
            "type": "tool_call", "depth": depth,
            "name": name, "args": args, "tool_use_id": tool_use_id,
        }
        self.events.append(ev)
        self._write(ev)
        return seq

    def tool_result(self, depth: int, *, call_seq: int, name: str, content: str, is_error: bool, ms: float) -> None:
        self._add(
            type="tool_result", depth=depth, call_seq=call_seq, name=name,
            content=content[:20000], is_error=is_error, ms=round(ms, 1),
        )

    def llm_response(
        self,
        depth: int,
        *,
        call_seq: int,
        text: str,
        stop_reason: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        ms: float,
    ) -> None:
        self._add(
            type="llm_response",
            depth=depth,
            call_seq=call_seq,
            text=text,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ms=round(ms, 1),
        )


def _serialize_user_content(content: Any) -> list[dict]:
    """Flatten multimodal content into a compact serializable form."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for block in content:
            if not isinstance(block, dict):
                out.append({"type": "unknown", "repr": str(block)[:200]})
                continue
            t = block.get("type")
            if t == "text":
                out.append({"type": "text", "text": block.get("text", "")})
            elif t == "image":
                src = block.get("source") or {}
                out.append({
                    "type": "image",
                    "media_type": src.get("media_type", "image/png"),
                    "bytes": len(src.get("data", "")) * 3 // 4,
                })
            else:
                out.append({"type": t or "unknown"})
        return out
    return [{"type": "unknown", "repr": str(content)[:200]}]


_collector: ContextVar[TraceCollector | None] = ContextVar("_trace_collector", default=None)


def set_collector(c: TraceCollector | None) -> Any:
    """Attach a collector to the current async context. Returns the reset token."""
    return _collector.set(c)


def reset_collector(token: Any) -> None:
    _collector.reset(token)


def get_collector() -> TraceCollector | None:
    return _collector.get()

F = TypeVar("F", bound=Callable[..., Any])

_ENABLED = os.environ.get("TRACE", "1") != "0"


def setup_tracing(level: int = logging.INFO) -> None:
    """Configure the `trace` logger once at app startup."""
    t = logging.getLogger("trace")
    t.setLevel(level)
    t.propagate = False
    if t.handlers:
        return
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S.%f"[:-3]))
    t.addHandler(h)


def _rel(path: str) -> str:
    try:
        return str(pathlib.Path(path).resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return path


def _short(val: Any, limit: int = 80) -> str:
    """One-line summary of a value — type name + size hint, never the full body."""
    if val is None:
        return "None"
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        if len(val) <= limit:
            return repr(val)
        return repr(val[:limit]) + f"…({len(val)} chars)"
    if isinstance(val, bytes):
        return f"bytes[{len(val)}]"
    if isinstance(val, (list, tuple)):
        return f"{type(val).__name__}[{len(val)}]"
    if isinstance(val, dict):
        return f"dict[{len(val)} keys]"
    # Pydantic / dataclass: show class name + best-effort field names
    cls = type(val).__name__
    if hasattr(val, "model_fields"):
        try:
            return f"{cls}(" + ", ".join(f"{k}=…" for k in list(val.model_fields.keys())[:4]) + ")"
        except Exception:
            return cls
    return cls


def _summarize_args(func: Callable[..., Any], args: tuple, kwargs: dict, limit: int = 80) -> str:
    try:
        sig = inspect.signature(func)
        bound = sig.bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        pieces = [_short(a, limit) for a in args]
        pieces.extend(f"{k}={_short(v, limit)}" for k, v in kwargs.items())
        return ", ".join(pieces)
    pieces: list[str] = []
    for name, val in bound.arguments.items():
        if name in ("self", "cls"):
            continue
        pieces.append(f"{name}={_short(val, limit)}")
    return ", ".join(pieces)


def traced(fn: F | None = None, *, name: str | None = None, arg_limit: int = 80) -> F:
    """Decorator — logs entry, exit, and elapsed time of the function.

    Works on both sync and async callables.
    """
    def deco(func: F) -> F:
        if not _ENABLED:
            return func

        is_async = inspect.iscoroutinefunction(func)
        file = _rel(inspect.getfile(func))
        label = name or f"{file}::{func.__qualname__}"

        if is_async:
            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                depth = _depth.get()
                indent = "  " * depth
                arg_repr = _summarize_args(func, args, kwargs, arg_limit)
                logger.info(f"{indent}▸ {label}({arg_repr})")
                c = _collector.get()
                if c: c.enter(depth, label, arg_repr)
                token = _depth.set(depth + 1)
                t0 = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    dt = time.perf_counter() - t0
                    logger.info(f"{indent}◂ {func.__qualname__} → {_short(result)}  [{dt*1000:.0f}ms]")
                    if c: c.exit(depth, func.__qualname__, _short(result), dt * 1000)
                    return result
                except Exception as exc:
                    dt = time.perf_counter() - t0
                    logger.info(f"{indent}✗ {func.__qualname__} raised {type(exc).__name__}: {exc}  [{dt*1000:.0f}ms]")
                    if c: c.error(depth, func.__qualname__, f"{type(exc).__name__}: {exc}", dt * 1000)
                    raise
                finally:
                    _depth.reset(token)
            return wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            depth = _depth.get()
            indent = "  " * depth
            arg_repr = _summarize_args(func, args, kwargs, arg_limit)
            logger.info(f"{indent}▸ {label}({arg_repr})")
            c = _collector.get()
            if c: c.enter(depth, label, arg_repr)
            token = _depth.set(depth + 1)
            t0 = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                dt = time.perf_counter() - t0
                logger.info(f"{indent}◂ {func.__qualname__} → {_short(result)}  [{dt*1000:.0f}ms]")
                if c: c.exit(depth, func.__qualname__, _short(result), dt * 1000)
                return result
            except Exception as exc:
                dt = time.perf_counter() - t0
                logger.info(f"{indent}✗ {func.__qualname__} raised {type(exc).__name__}: {exc}  [{dt*1000:.0f}ms]")
                if c: c.error(depth, func.__qualname__, f"{type(exc).__name__}: {exc}", dt * 1000)
                raise
            finally:
                _depth.reset(token)
        return sync_wrapper  # type: ignore[return-value]

    if fn is not None:
        return deco(fn)
    return deco  # type: ignore[return-value]


def metric(**kv: Any) -> None:
    """Log a one-line diagnostic at current indent. Use for key counts/sizes.

        metric(cat="button", input=12, after_dedupe=3)
    """
    if not _ENABLED:
        return
    depth = _depth.get()
    indent = "  " * depth
    parts = " ".join(f"{k}={_short(v)}" for k, v in kv.items())
    logger.info(f"{indent}· {parts}")
    c = _collector.get()
    if c: c.metric(depth, dict(kv))


def note(msg: str) -> None:
    """Log a free-form note at current indent."""
    if not _ENABLED:
        return
    depth = _depth.get()
    indent = "  " * depth
    logger.info(f"{indent}· {msg}")
    c = _collector.get()
    if c: c.note(depth, msg)
