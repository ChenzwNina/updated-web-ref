export default function ProgressIndicator({ stage, pct, stats }) {
  return (
    <section style={s.card}>
      <div style={s.stageRow}>
        <span style={s.stageLabel}>{stage}</span>
        <Dots />
      </div>
      <div style={s.barOuter}>
        <div style={{ ...s.barInner, width: `${Math.min(100, pct || 0)}%` }} />
      </div>
      {stats && stats.length > 0 && (
        <div style={s.statsRow}>
          {stats.map((it) => (
            <div key={it.label} style={s.stat}>
              <span style={s.statValue}>{it.value}</span>
              <span style={s.statLabel}>{it.label}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Dots() {
  return (
    <span style={s.dots}>
      <span style={{ ...s.dot, animationDelay: "0s" }} />
      <span style={{ ...s.dot, animationDelay: "0.2s" }} />
      <span style={{ ...s.dot, animationDelay: "0.4s" }} />
      <style>{`
        @keyframes pi-bounce { 0%,80%,100% { transform: translateY(0); opacity: 0.4 } 40% { transform: translateY(-4px); opacity: 1 } }
      `}</style>
    </span>
  );
}

const s = {
  card: {
    background: "#fff",
    borderRadius: 16,
    padding: 28,
    border: "1px solid #e5e7eb",
    boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
    marginBottom: 20,
  },
  stageRow: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    marginBottom: 16,
  },
  stageLabel: { fontSize: 16, fontWeight: 600, color: "#111827" },
  dots: { display: "inline-flex", gap: 4 },
  dot: {
    width: 6, height: 6, borderRadius: "50%", background: "#6366f1",
    animation: "pi-bounce 1.4s ease-in-out infinite",
    display: "inline-block",
  },
  barOuter: {
    height: 6, borderRadius: 999, background: "#eef2ff",
    overflow: "hidden",
  },
  barInner: {
    height: "100%",
    background: "linear-gradient(90deg, #6366f1, #8b5cf6)",
    transition: "width 0.4s ease",
  },
  statsRow: {
    display: "flex",
    gap: 28,
    marginTop: 18,
    paddingTop: 14,
    borderTop: "1px solid #f3f4f6",
  },
  stat: { display: "flex", flexDirection: "column", gap: 2 },
  statValue: { fontSize: 20, fontWeight: 700, color: "#111827" },
  statLabel: { fontSize: 11, color: "#6b7280", textTransform: "uppercase", letterSpacing: 0.5 },
};
