import { useEffect, useRef } from "react";

const PHASE_LABELS = {
  analyzing: "Analyzing site…",
  generating: "Generating site…",
  ready: "Analysis ready",
  done: "Generation complete",
  error: "Error",
};

export default function StatusPanel({ logs, phase }) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [logs.length]);

  return (
    <section style={styles.card}>
      <header style={styles.head}>
        <strong>Activity</strong>
        <span style={{ ...styles.pill, ...(styles[phase] || {}) }}>
          {PHASE_LABELS[phase] || phase}
        </span>
      </header>
      <div ref={ref} style={styles.logs}>
        {logs.map((l, i) => (
          <div key={i} style={styles.line}>
            <span style={styles.ts}>{l.ts.slice(11, 19)}</span>
            <span>{l.msg}</span>
          </div>
        ))}
        {logs.length === 0 && (
          <div style={{ color: "#9ca3af" }}>Waiting for events…</div>
        )}
      </div>
    </section>
  );
}

const styles = {
  card: {
    background: "#fff",
    borderRadius: 12,
    padding: 16,
    border: "1px solid #e5e7eb",
    marginBottom: 20,
  },
  head: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 10,
  },
  pill: {
    fontSize: 12,
    fontWeight: 600,
    padding: "4px 10px",
    borderRadius: 999,
    background: "#eef2ff",
    color: "#4338ca",
  },
  analyzing: { background: "#eef2ff", color: "#4338ca" },
  generating: { background: "#fef3c7", color: "#92400e" },
  ready: { background: "#d1fae5", color: "#065f46" },
  done: { background: "#d1fae5", color: "#065f46" },
  error: { background: "#fee2e2", color: "#991b1b" },
  logs: {
    maxHeight: 240,
    overflowY: "auto",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 12.5,
    lineHeight: 1.7,
    background: "#0f172a",
    color: "#e2e8f0",
    borderRadius: 8,
    padding: "10px 12px",
    whiteSpace: "pre-wrap",
  },
  line: { display: "flex", gap: 10 },
  ts: { color: "#64748b", flexShrink: 0 },
};
