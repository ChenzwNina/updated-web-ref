import { useState } from "react";

export default function UrlInput({ onSubmit, disabled }) {
  const [url, setUrl] = useState("");

  const submit = (e) => {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
  };

  return (
    <form onSubmit={submit} style={styles.form}>
      <input
        type="text"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="https://stripe.com"
        disabled={disabled}
        style={styles.input}
      />
      <button type="submit" disabled={disabled || !url.trim()} style={styles.btn}>
        {disabled ? "Analyzing…" : "Analyze Style"}
      </button>
    </form>
  );
}

const styles = {
  form: {
    display: "flex",
    gap: 10,
    marginBottom: 20,
  },
  input: {
    flex: 1,
    padding: "14px 16px",
    fontSize: 15,
    border: "1px solid #e5e7eb",
    borderRadius: 10,
    outline: "none",
    background: "#fff",
  },
  btn: {
    padding: "14px 24px",
    fontSize: 15,
    fontWeight: 600,
    border: "none",
    borderRadius: 10,
    background: "linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%)",
    color: "#fff",
    cursor: "pointer",
  },
};
