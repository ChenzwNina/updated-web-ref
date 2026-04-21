import { useState } from "react";

const PRESETS = [
  "Personal portfolio",
  "SaaS landing page",
  "Coffee shop",
  "Photography studio",
  "Online magazine",
  "Restaurant",
];

export default function GenerateForm({ onGenerate, disabled }) {
  const [siteType, setSiteType] = useState("");
  const [pagesText, setPagesText] = useState("home, about, contact");
  const [extra, setExtra] = useState("");

  const submit = (e) => {
    e.preventDefault();
    const st = siteType.trim();
    if (!st) return;
    const pages = pagesText
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
    onGenerate({ site_type: st, pages, extra_instructions: extra.trim() });
  };

  return (
    <section style={styles.card}>
      <h2 style={styles.title}>Generate a new site in this style</h2>
      <p style={styles.sub}>
        Analysis is done. Tell the agent what site you want built using the
        extracted design language.
      </p>

      <form onSubmit={submit}>
        <label style={styles.label}>Website type</label>
        <input
          type="text"
          value={siteType}
          onChange={(e) => setSiteType(e.target.value)}
          placeholder="e.g. Personal portfolio"
          style={styles.input}
          disabled={disabled}
        />
        <div style={styles.presets}>
          {PRESETS.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setSiteType(p)}
              style={styles.preset}
              disabled={disabled}
            >
              {p}
            </button>
          ))}
        </div>

        <label style={styles.label}>Pages / sections (comma-separated)</label>
        <input
          type="text"
          value={pagesText}
          onChange={(e) => setPagesText(e.target.value)}
          placeholder="home, about, contact"
          style={styles.input}
          disabled={disabled}
        />

        <label style={styles.label}>Extra instructions (optional)</label>
        <textarea
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
          placeholder="Any specific content, tone, or design tweaks…"
          rows={3}
          style={{ ...styles.input, resize: "vertical", fontFamily: "inherit" }}
          disabled={disabled}
        />

        <button type="submit" disabled={disabled || !siteType.trim()} style={styles.btn}>
          {disabled ? "Generating…" : "Generate"}
        </button>
      </form>
    </section>
  );
}

const styles = {
  card: {
    background: "#fff",
    borderRadius: 12,
    padding: 20,
    border: "1px solid #e5e7eb",
    marginBottom: 20,
  },
  title: { fontSize: 20, fontWeight: 700, margin: 0 },
  sub: { fontSize: 13, color: "#6b7280", marginTop: 4, marginBottom: 16 },
  label: {
    display: "block",
    fontSize: 12,
    fontWeight: 600,
    color: "#374151",
    marginTop: 10,
    marginBottom: 4,
  },
  input: {
    width: "100%",
    padding: "10px 12px",
    fontSize: 14,
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    outline: "none",
    background: "#fff",
  },
  presets: { display: "flex", gap: 6, flexWrap: "wrap", marginTop: 6 },
  preset: {
    border: "1px solid #e5e7eb",
    background: "#f8fafc",
    padding: "4px 10px",
    borderRadius: 999,
    fontSize: 11,
    cursor: "pointer",
    color: "#475569",
  },
  btn: {
    marginTop: 14,
    padding: "12px 22px",
    border: "none",
    borderRadius: 10,
    background: "linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)",
    color: "#fff",
    fontSize: 14,
    fontWeight: 600,
    cursor: "pointer",
  },
};
