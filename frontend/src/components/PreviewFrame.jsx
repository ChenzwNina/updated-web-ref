import { useMemo } from "react";

export default function PreviewFrame({ html, onClose }) {
  const srcDoc = useMemo(() => html, [html]);

  const download = () => {
    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "generated-site.html";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section style={styles.card}>
      <header style={styles.head}>
        <h2 style={styles.title}>Generated site</h2>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={styles.secondary} onClick={download}>
            Download .html
          </button>
          <button style={styles.secondary} onClick={onClose}>
            Close
          </button>
        </div>
      </header>
      <iframe
        title="Generated site"
        srcDoc={srcDoc}
        style={styles.iframe}
        sandbox="allow-scripts allow-same-origin"
      />
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
  title: { fontSize: 18, fontWeight: 700, margin: 0 },
  secondary: {
    background: "#fff",
    border: "1px solid #e5e7eb",
    padding: "6px 12px",
    borderRadius: 8,
    fontSize: 13,
    cursor: "pointer",
    color: "#374151",
  },
  iframe: {
    width: "100%",
    height: 720,
    border: "1px solid #e5e7eb",
    borderRadius: 8,
    background: "#fff",
  },
};
