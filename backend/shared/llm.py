"""Thin wrappers around the Anthropic SDK for main-agent and subagent calls.

- `agent_loop()` drives a tool-use loop until the model stops or hits max turns.
- `subagent_call()` is a one-shot Sonnet call used by skills for focused subtasks.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic

from .trace import _depth, get_collector, metric, note, traced

logger = logging.getLogger(__name__)


MAIN_AGENT_MODEL = os.environ.get("MAIN_AGENT_MODEL", "claude-opus-4-6")
SUBAGENT_MODEL = os.environ.get("SUBAGENT_MODEL", "claude-sonnet-4-6")


def _client() -> AsyncAnthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return AsyncAnthropic(api_key=key)


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]


def _tool_defs(tools: list[ToolSpec]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


@traced
async def agent_loop(
    *,
    model: str,
    system: str,
    user_prompt: str,
    tools: list[ToolSpec],
    max_tokens: int = 4096,
    max_turns: int = 20,
    on_event: Callable[[str, dict], Awaitable[None]] | None = None,
) -> str:
    """Run a tool-use loop. Returns the final assistant text.

    `on_event` is an optional async callback: on_event("tool_use", {...}).
    """
    client = _client()
    by_name = {t.name: t for t in tools}
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    import time as _time
    for turn in range(max_turns):
        col = get_collector()
        call_seq = None
        if col:
            call_seq = col.llm_call(
                _depth.get(),
                role="main_agent",
                model=model,
                system=system,
                user_content=messages[-1].get("content") if messages else "",
                max_tokens=max_tokens,
            )
        _t0 = _time.perf_counter()
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=_tool_defs(tools) if tools else None,  # type: ignore[arg-type]
            messages=messages,
        )
        _dt = (_time.perf_counter() - _t0) * 1000
        _text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if col and call_seq is not None:
            col.llm_response(
                _depth.get(),
                call_seq=call_seq,
                text=_text,
                stop_reason=resp.stop_reason,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                ms=_dt,
            )
        metric(
            turn=turn,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        # Record assistant reply
        assistant_blocks = [_block_to_dict(b) for b in resp.content]
        messages.append({"role": "assistant", "content": assistant_blocks})

        if resp.stop_reason != "tool_use":
            # Done — concatenate text blocks
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            return text.strip()

        # Collect tool_use blocks, execute each, append tool_result message
        tool_results: list[dict] = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            name = block.name
            args = block.input or {}
            if on_event:
                await on_event("tool_use", {"name": name, "args": args})
            spec = by_name.get(name)
            if spec is None:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Unknown tool: {name}",
                    "is_error": True,
                })
                continue
            col2 = get_collector()
            tool_seq = None
            if col2:
                tool_seq = col2.tool_call(_depth.get(), name=name, args=args, tool_use_id=block.id)
            _tt0 = _time.perf_counter()
            try:
                result = await spec.handler(args)
                content = result if isinstance(result, str) else json.dumps(result, default=str)
                if col2 and tool_seq is not None:
                    col2.tool_result(
                        _depth.get(), call_seq=tool_seq, name=name,
                        content=content, is_error=False,
                        ms=(_time.perf_counter() - _tt0) * 1000,
                    )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                })
            except Exception as exc:
                logger.exception("Tool %s failed", name)
                if col2 and tool_seq is not None:
                    col2.tool_result(
                        _depth.get(), call_seq=tool_seq, name=name,
                        content=f"Tool error: {exc}", is_error=True,
                        ms=(_time.perf_counter() - _tt0) * 1000,
                    )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": f"Tool error: {exc}",
                    "is_error": True,
                })

        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"agent_loop exceeded max_turns={max_turns}")


def _block_to_dict(block: Any) -> dict:
    t = getattr(block, "type", "")
    if t == "text":
        return {"type": "text", "text": block.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {"type": t}


@traced
async def subagent_call(
    *,
    system: str,
    user_content: str | list[dict],
    max_tokens: int = 4096,
    model: str = SUBAGENT_MODEL,
) -> str:
    """One-shot Sonnet call for focused subagent work. Returns text."""
    import time as _time
    client = _client()
    messages = [{"role": "user", "content": user_content}]
    col = get_collector()
    call_seq = None
    if col:
        call_seq = col.llm_call(
            _depth.get(), role="subagent", model=model, system=system,
            user_content=user_content, max_tokens=max_tokens,
        )
    _t0 = _time.perf_counter()
    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    _dt = (_time.perf_counter() - _t0) * 1000
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    if col and call_seq is not None:
        col.llm_response(
            _depth.get(), call_seq=call_seq, text=text,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            ms=_dt,
        )
    metric(
        stop_reason=resp.stop_reason,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
        response_chars=len(text),
    )
    if resp.stop_reason == "max_tokens":
        note(f"⚠️  subagent hit max_tokens={max_tokens} — response likely truncated")
    return text


def extract_json(text: str) -> str:
    """Pull a JSON object out of an LLM response, tolerating fences/prose.

    Handles truncated JSON from max_tokens by repairing unclosed brackets.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    t = t.strip()

    start = t.find("{")
    if start == -1:
        return t

    candidate = t[start:]

    # Try parsing as-is first
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    # Truncated JSON repair: close any unclosed brackets/braces and strip
    # trailing partial tokens (e.g. a key without a value).
    repaired = _repair_truncated_json(candidate)
    if repaired:
        return repaired

    # Last resort: naive extraction
    end = t.rfind("}")
    if end > start:
        return t[start:end + 1]
    return t


def _repair_truncated_json(text: str) -> str | None:
    """Attempt to repair JSON truncated mid-stream (e.g. from max_tokens)."""
    import re

    # Strip trailing incomplete string/key (unmatched quote)
    t = text.rstrip()
    # Remove trailing comma or colon (partial key-value)
    t = re.sub(r'[,:\s]+$', '', t)
    # If we're inside an unclosed string, close it
    quote_count = t.count('"') - t.count('\\"')
    if quote_count % 2 == 1:
        t += '"'
        t = re.sub(r'[,:\s]+$', '', t)

    # Count unclosed brackets
    opens = []
    in_string = False
    escape = False
    for ch in t:
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ('{', '['):
            opens.append(ch)
        elif ch == '}' and opens and opens[-1] == '{':
            opens.pop()
        elif ch == ']' and opens and opens[-1] == '[':
            opens.pop()

    # Close all unclosed brackets in reverse order
    for bracket in reversed(opens):
        t += ']' if bracket == '[' else '}'

    try:
        json.loads(t)
        return t
    except json.JSONDecodeError:
        return None
