import { useEffect, useMemo, useState } from "react";

export default function TraceViewer({ jobId }) {
  const [events, setEvents] = useState([]);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selected, setSelected] = useState(null);
  const [filter, setFilter] = useState("all"); // all | llm | errors

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch(`/api/trace/${jobId}`);
        if (!r.ok) return;
        const d = await r.json();
        if (!cancelled) setEvents(d.events || []);
      } catch {}
    };
    load();
    if (!autoRefresh) return;
    const iv = setInterval(load, 1500);
    return () => { cancelled = true; clearInterval(iv); };
  }, [jobId, autoRefresh]);

  const filtered = useMemo(() => {
    if (filter === "all") return events;
    if (filter === "llm") return events.filter(e => e.type === "llm_call" || e.type === "llm_response");
    if (filter === "tools") return events.filter(e => e.type === "tool_call" || e.type === "tool_result");
    if (filter === "errors") return events.filter(e => e.type === "error");
    return events;
  }, [events, filter]);

  if (!jobId) {
    return <div style={s.empty}>Start an analysis to see the execution trace.</div>;
  }

  return (
    <div style={s.wrap}>
      <div style={s.toolbar}>
        <strong style={{ fontSize: 13 }}>Execution Trace</strong>
        <span style={s.count}>{events.length} events</span>
        <div style={{ flex: 1 }} />
        {["all", "llm", "tools", "errors"].map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            style={{ ...s.tab, ...(filter === f ? s.tabActive : {}) }}
          >{f}</button>
        ))}
        <label style={s.refresh}>
          <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
          auto-refresh
        </label>
      </div>

      <div style={s.split}>
        <div style={s.list}>
          {filtered.map(ev => (
            <TraceRow
              key={ev.seq}
              ev={ev}
              active={selected?.seq === ev.seq}
              onClick={() => setSelected(ev)}
            />
          ))}
          {filtered.length === 0 && <div style={s.empty}>No events yet.</div>}
        </div>
        <div style={s.detail}>
          {selected ? <TraceDetail ev={selected} allEvents={events} /> : (
            <div style={s.empty}>Click an event to inspect it.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function TraceRow({ ev, active, onClick }) {
  const indent = (ev.depth || 0) * 14;
  const { icon, color, label, hint } = rowMeta(ev);
  return (
    <div
      onClick={onClick}
      style={{
        ...s.row,
        paddingLeft: 10 + indent,
        background: active ? "#eef2ff" : "transparent",
        borderLeft: active ? "3px solid #4f46e5" : "3px solid transparent",
      }}
    >
      <span style={{ ...s.icon, color }}>{icon}</span>
      <span style={s.rowLabel}>{label}</span>
      {hint && <span style={s.rowHint}>{hint}</span>}
      {ev.ms != null && <span style={s.rowMs}>{Math.round(ev.ms)}ms</span>}
    </div>
  );
}

function rowMeta(ev) {
  switch (ev.type) {
    case "enter":
      return { icon: "▸", color: "#4f46e5", label: ev.label, hint: ev.args };
    case "exit":
      return { icon: "◂", color: "#059669", label: ev.label, hint: `→ ${ev.result}` };
    case "error":
      return { icon: "✗", color: "#dc2626", label: ev.label, hint: ev.err };
    case "metric":
      return {
        icon: "·", color: "#6b7280", label: "metric",
        hint: Object.entries(ev.kv || {}).map(([k, v]) => `${k}=${v}`).join("  "),
      };
    case "note":
      return { icon: "·", color: "#6b7280", label: "note", hint: ev.msg };
    case "llm_call":
      return {
        icon: "⟶", color: "#b45309",
        label: `${ev.role} call · ${ev.model}`,
        hint: `max_tokens=${ev.max_tokens}`,
      };
    case "llm_response":
      return {
        icon: "⟵", color: "#0369a1",
        label: `response`,
        hint: `${ev.stop_reason || ""}  in=${ev.input_tokens ?? "?"} out=${ev.output_tokens ?? "?"}`,
      };
    case "tool_call":
      return {
        icon: "🔧", color: "#7c3aed",
        label: `tool · ${ev.name}`,
        hint: Object.entries(ev.args || {}).map(([k, v]) =>
          `${k}=${typeof v === "string" ? JSON.stringify(v).slice(0, 40) : JSON.stringify(v)}`
        ).join(" "),
      };
    case "tool_result":
      return {
        icon: ev.is_error ? "⚠" : "✓",
        color: ev.is_error ? "#dc2626" : "#16a34a",
        label: `tool result · ${ev.name}`,
        hint: `${(ev.content || "").length} chars`,
      };
    default:
      return { icon: "·", color: "#6b7280", label: ev.type, hint: "" };
  }
}

function TraceDetail({ ev, allEvents }) {
  if (ev.type === "llm_call") {
    const resp = allEvents.find(e => e.type === "llm_response" && e.call_seq === ev.seq);
    return <LlmPanel call={ev} resp={resp} />;
  }
  if (ev.type === "llm_response") {
    const call = allEvents.find(e => e.seq === ev.call_seq);
    return <LlmPanel call={call} resp={ev} />;
  }
  if (ev.type === "tool_call") {
    const result = allEvents.find(e => e.type === "tool_result" && e.call_seq === ev.seq);
    return <ToolPanel call={ev} result={result} />;
  }
  if (ev.type === "tool_result") {
    const call = allEvents.find(e => e.seq === ev.call_seq);
    return <ToolPanel call={call} result={ev} />;
  }
  return (
    <div style={s.detailBody}>
      <div style={s.detailTitle}>{ev.type}</div>
      <pre style={s.pre}>{JSON.stringify(ev, null, 2)}</pre>
    </div>
  );
}

function LlmPanel({ call, resp }) {
  return (
    <div style={s.detailBody}>
      <div style={s.detailTitle}>
        LLM call {call ? `· ${call.role} · ${call.model}` : ""}
      </div>
      {resp && (
        <div style={s.meta}>
          stop_reason: <b>{resp.stop_reason || "—"}</b> &nbsp;·&nbsp;
          input: <b>{resp.input_tokens ?? "?"}</b> tok &nbsp;·&nbsp;
          output: <b>{resp.output_tokens ?? "?"}</b> tok &nbsp;·&nbsp;
          {Math.round(resp.ms || 0)} ms
        </div>
      )}

      {call && (
        <>
          <Section title="System prompt" chars={call.system?.length}>
            <pre style={s.pre}>{call.system}</pre>
          </Section>
          <Section title="User content">
            {(call.user_content || []).map((b, i) => (
              <div key={i} style={s.block}>
                {b.type === "text" ? (
                  <>
                    <div style={s.blockLabel}>text · {b.text?.length ?? 0} chars</div>
                    <pre style={s.pre}>{b.text}</pre>
                  </>
                ) : b.type === "image" ? (
                  <div style={s.blockLabel}>🖼  image · {b.media_type} · ~{Math.round((b.bytes || 0) / 1024)} KB</div>
                ) : (
                  <div style={s.blockLabel}>block: {b.type}</div>
                )}
              </div>
            ))}
          </Section>
        </>
      )}

      {resp && (
        <Section title="Response" chars={resp.text?.length}>
          <pre style={s.pre}>{resp.text}</pre>
        </Section>
      )}
    </div>
  );
}

function ToolPanel({ call, result }) {
  return (
    <div style={s.detailBody}>
      <div style={s.detailTitle}>
        🔧 Tool · {call?.name || result?.name}
      </div>
      {result && (
        <div style={s.meta}>
          {result.is_error ? <span style={{ color: "#dc2626" }}>ERROR</span> : "ok"}
          &nbsp;·&nbsp; {Math.round(result.ms || 0)} ms
          &nbsp;·&nbsp; {(result.content || "").length.toLocaleString()} chars returned
        </div>
      )}
      {call && (
        <Section title="Tool arguments (from main agent)">
          <pre style={s.pre}>{JSON.stringify(call.args || {}, null, 2)}</pre>
        </Section>
      )}
      {result && (
        <Section title="Tool result (returned to main agent)" chars={(result.content || "").length}>
          <pre style={s.pre}>{tryPretty(result.content)}</pre>
        </Section>
      )}
    </div>
  );
}

function tryPretty(s) {
  if (!s) return "";
  try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; }
}

function Section({ title, chars, children }) {
  const [open, setOpen] = useState(true);
  return (
    <div style={s.section}>
      <div style={s.sectionHead} onClick={() => setOpen(!open)}>
        <span>{open ? "▾" : "▸"}</span>
        <span>{title}</span>
        {chars != null && <span style={s.sectionMeta}>{chars.toLocaleString()} chars</span>}
      </div>
      {open && <div style={s.sectionBody}>{children}</div>}
    </div>
  );
}

const s = {
  wrap: {
    marginTop: 16, border: "1px solid #e5e7eb", borderRadius: 10,
    overflow: "hidden", background: "#fff",
    display: "flex", flexDirection: "column", height: 640,
  },
  toolbar: {
    display: "flex", alignItems: "center", gap: 8, padding: "10px 14px",
    borderBottom: "1px solid #e5e7eb", background: "#f9fafb", flexShrink: 0,
  },
  count: { fontSize: 12, color: "#6b7280" },
  tab: {
    border: "1px solid #d1d5db", background: "#fff", borderRadius: 6,
    padding: "4px 10px", fontSize: 12, cursor: "pointer",
  },
  tabActive: { background: "#4f46e5", color: "#fff", borderColor: "#4f46e5" },
  refresh: { fontSize: 12, color: "#6b7280", display: "flex", gap: 4, alignItems: "center" },
  split: {
    display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
    flex: 1, minHeight: 0, overflow: "hidden",
  },
  list: {
    overflowY: "auto", overflowX: "hidden",
    borderRight: "1px solid #e5e7eb",
    fontFamily: "ui-monospace, Menlo, monospace", fontSize: 12,
  },
  row: {
    display: "flex", alignItems: "center", gap: 8, padding: "5px 10px",
    cursor: "pointer", borderBottom: "1px solid #f3f4f6",
    minWidth: 0,
  },
  icon: { fontWeight: 700, width: 16, textAlign: "center", flexShrink: 0 },
  rowLabel: { color: "#111827", fontWeight: 500, flexShrink: 0, whiteSpace: "nowrap" },
  rowHint: {
    color: "#6b7280", flex: 1, minWidth: 0,
    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
  },
  rowMs: { color: "#9ca3af", fontSize: 11, flexShrink: 0 },
  detail: { overflowY: "auto", overflowX: "hidden", padding: 14 },
  detailBody: { fontSize: 13 },
  detailTitle: { fontSize: 14, fontWeight: 600, marginBottom: 8, color: "#111827" },
  meta: { fontSize: 12, color: "#374151", marginBottom: 12 },
  section: { marginBottom: 12, border: "1px solid #e5e7eb", borderRadius: 8 },
  sectionHead: {
    display: "flex", gap: 8, alignItems: "center", padding: "8px 10px",
    background: "#f9fafb", cursor: "pointer", borderRadius: "8px 8px 0 0", fontSize: 13, fontWeight: 600,
  },
  sectionMeta: { marginLeft: "auto", fontSize: 11, color: "#6b7280", fontWeight: 400 },
  sectionBody: { padding: 10 },
  block: { marginBottom: 10 },
  blockLabel: { fontSize: 11, color: "#6b7280", marginBottom: 4 },
  pre: {
    margin: 0, padding: 10, background: "#0b1020", color: "#e5e7eb",
    borderRadius: 6, fontSize: 11.5, maxHeight: 320, overflow: "auto",
    whiteSpace: "pre-wrap", wordBreak: "break-word",
  },
  empty: { padding: 24, color: "#9ca3af", fontSize: 13, textAlign: "center" },
};
