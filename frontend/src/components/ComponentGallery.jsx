import { useEffect, useRef, useState } from "react";

export default function ComponentGallery({ analysis, download }) {
  const [activeCat, setActiveCat] = useState(
    analysis.groups[0]?.category ?? null
  );

  useEffect(() => {
    if (analysis.groups.length > 0 && !analysis.groups.find((g) => g.category === activeCat)) {
      setActiveCat(analysis.groups[0].category);
    }
  }, [analysis]);

  const active = analysis.groups.find((g) => g.category === activeCat);

  return (
    <section style={styles.wrap}>
      <aside style={styles.sidebar}>
        <div style={styles.sidebarHead}>
          <div style={styles.sidebarTitle}>Components</div>
          <div style={styles.sidebarSub}>{analysis.summary}</div>
        </div>

        {download?.pages?.length > 0 && (
          <div style={styles.pagesCol}>
            {download.pages.map((p) => (
              <a
                key={p.url}
                href={`/${p.screenshot_path}`}
                target="_blank"
                rel="noreferrer"
                style={styles.pageCard}
                title={p.url}
              >
                <img src={`/${p.screenshot_path}`} alt="" style={styles.thumb} />
                <div style={styles.pageTitle}>{p.title || p.url}</div>
              </a>
            ))}
          </div>
        )}

        {Array.isArray(analysis.design_tokens?.palette) &&
          analysis.design_tokens.palette.length > 0 && (
          <PaletteSwatches
            primary={analysis.design_tokens.palette_primary}
            secondary={analysis.design_tokens.palette_secondary || []}
            accent={analysis.design_tokens.palette_accent || []}
            fallback={analysis.design_tokens.palette}
          />
        )}

        {analysis.design_tokens?.typography &&
          Object.keys(analysis.design_tokens.typography).length > 0 && (
          <TypographyPanel typography={analysis.design_tokens.typography} />
        )}

        <nav style={styles.cats}>
          {analysis.groups.map((g) => (
            <button
              key={g.category}
              onClick={() => setActiveCat(g.category)}
              style={{
                ...styles.cat,
                ...(g.category === activeCat ? styles.catActive : {}),
              }}
            >
              <span style={styles.catLabel}>{g.category.replace(/_/g, " ")}</span>
              <span style={styles.badge}>{g.components.length}</span>
            </button>
          ))}
        </nav>

        {Object.keys(analysis.design_tokens || {}).length > 0 && (
          <details style={styles.tokens}>
            <summary style={styles.tokensSummary}>
              Design tokens ({Object.keys(analysis.design_tokens).length})
            </summary>
            <div style={styles.tokensList}>
              {Object.entries(analysis.design_tokens)
                .slice(0, 80)
                .map(([name, values]) => (
                  <div key={name} style={styles.token}>
                    <code style={styles.tokenName}>{name}</code>
                    <span style={styles.tokenVal}>
                      {Array.isArray(values)
                        ? values.join(", ")
                        : values && typeof values === "object"
                          ? Object.entries(values)
                              .map(([k, v]) => `${k}=${v}`)
                              .join(", ")
                          : String(values ?? "")}
                    </span>
                  </div>
                ))}
            </div>
          </details>
        )}
      </aside>

      <main style={styles.main}>
        {active ? (
          <>
            <header style={styles.mainHead}>
              <h2 style={styles.mainTitle}>{active.category.replace(/_/g, " ")}</h2>
              <span style={styles.mainCount}>
                {active.components.length} component{active.components.length === 1 ? "" : "s"}
              </span>
            </header>
            <div style={styles.compList}>
              {active.components.map((c) => (
                <ComponentCard key={c.id} comp={c} download={download} />
              ))}
            </div>
          </>
        ) : analysis.groups.length === 0 ? (
          <div style={styles.emptyState}>
            <div style={styles.emptyIcon}>🔍</div>
            <div style={styles.emptyTitle}>No components extracted</div>
            <div style={styles.emptyDesc}>
              The analysis completed but couldn't extract UI components from this site.
              This can happen with heavily JavaScript-rendered pages or unusual page structures.
              Try running the analysis again.
            </div>
          </div>
        ) : (
          <div style={styles.empty}>Select a category from the sidebar.</div>
        )}
      </main>
    </section>
  );
}

function ComponentCard({ comp, download }) {
  const s = comp.styles || {};
  const page = download?.pages?.find((p) => p.url === comp.source_url)
    || download?.pages?.[0];
  return (
    <article style={styles.compCard}>
      <div style={styles.compHead}>
        <div style={styles.compHeadLeft}>
          <strong style={styles.compName}>{comp.name}</strong>
          <span style={styles.compType}>{comp.type}</span>
          {comp.count > 1 && <span style={styles.compCount}>×{comp.count}</span>}
          <ValidationBadge status={comp.validation_status} note={comp.validation_note} />
        </div>
        {comp.source_url && (
          <a href={comp.source_url} target="_blank" rel="noreferrer" style={styles.sourceLink}>
            source ↗
          </a>
        )}
      </div>
      {comp.description && <p style={styles.compDesc}>{comp.description}</p>}
      <LivePreview snippet={comp.html_snippet} sourceUrl={comp.source_url} page={page} fallbackStyles={s} name={comp.name} />
      {comp.html_snippet && <CodeBlock snippet={comp.html_snippet} />}
      <details style={styles.stylesDetails}>
        <summary style={styles.stylesSummary}>styles</summary>
        <div style={styles.stylesList}>
          {Object.entries(s)
            .filter(([k, v]) => v && k !== "extra")
            .map(([k, v]) => (
              <div key={k}>
                <code>{k}</code>: <span style={{ color: "#475569" }}>{v}</span>
              </div>
            ))}
        </div>
      </details>
    </article>
  );
}

// Pretty-printed + copyable code block showing the self-contained generated
// snippet. This is the snippet users paste directly into their project —
// NOT raw page HTML. After the validate_components skill runs, this is
// either the LLM's inline-styled extraction or its regenerated replacement.
function prettyPrintHtml(src) {
  if (!src) return "";
  // Very small formatter: newline before open tags, indent by depth.
  // Works well for the short self-contained snippets we produce.
  let out = "";
  let depth = 0;
  const VOID = new Set(["area", "base", "br", "col", "embed", "hr", "img",
    "input", "link", "meta", "param", "source", "track", "wbr"]);
  const tokens = src.replace(/>\s+</g, "><").split(/(<[^>]+>)/).filter(Boolean);
  for (const tok of tokens) {
    if (tok.startsWith("</")) {
      depth = Math.max(0, depth - 1);
      out += (out ? "\n" : "") + "  ".repeat(depth) + tok;
    } else if (tok.startsWith("<") && !tok.startsWith("<!")) {
      const tagMatch = tok.match(/^<\s*([a-zA-Z0-9-]+)/);
      const tag = tagMatch ? tagMatch[1].toLowerCase() : "";
      const selfClosing = tok.endsWith("/>") || VOID.has(tag);
      out += (out ? "\n" : "") + "  ".repeat(depth) + tok;
      if (!selfClosing) depth += 1;
    } else {
      // text node
      const t = tok.trim();
      if (t) out += (out ? "\n" : "") + "  ".repeat(depth) + t;
    }
  }
  return out;
}

function CodeBlock({ snippet }) {
  const [copied, setCopied] = useState(false);
  const pretty = prettyPrintHtml(snippet);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(pretty);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Fallback: select + execCommand
      const ta = document.createElement("textarea");
      ta.value = pretty;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch {}
      document.body.removeChild(ta);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <div style={styles.codeBlock}>
      <div style={styles.codeHeader}>
        <span style={styles.codeLabel}>Component code</span>
        <button onClick={onCopy} style={styles.copyBtn} title="Copy to clipboard">
          {copied ? (
            <>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"
                   strokeLinejoin="round" aria-hidden>
                <polyline points="20 6 9 17 4 12" />
              </svg>
              Copied
            </>
          ) : (
            <>
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="2" strokeLinecap="round"
                   strokeLinejoin="round" aria-hidden>
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
              </svg>
              Copy
            </>
          )}
        </button>
      </div>
      <pre style={styles.codePre}><code>{pretty}</code></pre>
    </div>
  );
}

function PaletteSwatches({ primary, secondary, accent, fallback }) {
  const [copied, setCopied] = useState(null);
  const onCopy = async (hex) => {
    try { await navigator.clipboard.writeText(hex); } catch {}
    setCopied(hex);
    setTimeout(() => setCopied((c) => (c === hex ? null : c)), 1000);
  };

  // If the backend didn't emit tiers (older job), fall back to showing the
  // flat `palette` list as "Brand palette" — no tier breakdown available.
  const hasTiers = Boolean(primary) || (secondary || []).length > 0 || (accent || []).length > 0;
  const tiers = hasTiers
    ? [
        { label: "Primary", colors: primary ? [primary] : [] },
        { label: "Secondary", colors: secondary || [] },
        { label: "Accent", colors: accent || [] },
      ].filter((t) => t.colors.length > 0)
    : [{ label: "Brand palette", colors: fallback || [] }];

  const renderSwatch = (hex) => (
    <button
      key={hex}
      onClick={() => onCopy(hex)}
      title={`Click to copy ${hex}`}
      style={{ ...styles.swatch, background: hex }}
    >
      <span style={styles.swatchLabel}>
        {copied === hex ? "copied" : hex}
      </span>
    </button>
  );

  return (
    <div style={styles.paletteWrap}>
      <div style={styles.paletteTitle}>Color palette</div>
      {tiers.map((t) => (
        <div key={t.label} style={styles.paletteTier}>
          <div style={styles.paletteTierLabel}>{t.label}</div>
          <div style={styles.paletteGrid}>{t.colors.map(renderSwatch)}</div>
        </div>
      ))}
    </div>
  );
}

function TypographyPanel({ typography }) {
  // Show the rendered font families per role so the user can see what the
  // reference site uses; generator prompt instructs the model to match
  // these (or pick the closest Google Font).
  const roles = [
    { key: "heading", label: "Headings" },
    { key: "body", label: "Body" },
    { key: "button", label: "Buttons" },
  ];
  const entries = roles
    .map((r) => ({
      ...r,
      family: typography[`${r.key}_family`],
      weight: typography[`${r.key}_weight`],
      size: typography[`${r.key}_size`],
    }))
    .filter((r) => r.family);
  if (!entries.length) return null;
  return (
    <div style={styles.typoWrap}>
      <div style={styles.paletteTitle}>Typography</div>
      {entries.map((r) => (
        <div key={r.key} style={styles.typoRow}>
          <div style={styles.typoLabel}>{r.label}</div>
          <div
            style={{
              ...styles.typoSample,
              fontFamily: `${r.family}, system-ui, sans-serif`,
              fontWeight: r.weight || 400,
            }}
          >
            {r.label === "Headings" ? "The quick brown fox" : "The quick brown fox jumps"}
          </div>
          <div style={styles.typoMeta}>
            {r.family}
            {r.weight ? ` · ${r.weight}` : ""}
            {r.size ? ` · ${r.size}px` : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

function ValidationBadge({ status, note }) {
  if (!status || status === "unchecked") return null;
  const palette = {
    ok:             { bg: "#ecfdf5", fg: "#047857", label: "✓ rendered" },
    regenerated:    { bg: "#eff6ff", fg: "#1d4ed8", label: "↻ regenerated" },
    unrecoverable:  { bg: "#fef2f2", fg: "#b91c1c", label: "⚠ preview unreliable" },
  };
  const p = palette[status] || { bg: "#f3f4f6", fg: "#374151", label: status };
  return (
    <span
      title={note || status}
      style={{
        background: p.bg,
        color: p.fg,
        fontSize: 10,
        fontWeight: 600,
        padding: "2px 8px",
        borderRadius: 999,
        letterSpacing: 0.2,
      }}
    >
      {p.label}
    </span>
  );
}

// Cache the parsed full saved HTML per page so we can look up ancestor
// chains for components lifted from that page.
const _docCache = new Map();

async function fetchPageDoc(pageHtmlPath) {
  if (!pageHtmlPath) return null;
  if (_docCache.has(pageHtmlPath)) return _docCache.get(pageHtmlPath);
  const pr = fetch(`/${pageHtmlPath}`)
    .then((r) => (r.ok ? r.text() : ""))
    .then((html) => {
      if (!html) return null;
      try {
        return new DOMParser().parseFromString(html, "text/html");
      } catch {
        return null;
      }
    })
    .catch(() => null);
  _docCache.set(pageHtmlPath, pr);
  return pr;
}

// Find the snippet's root element in the saved page DOM so we can embed
// the real element with its ancestor chain. Falls back to null if no match.
function locateInDoc(doc, snippet) {
  if (!doc || !snippet) return null;
  let root;
  try {
    const tmp = new DOMParser().parseFromString(
      `<!DOCTYPE html><html><body>${snippet}</body></html>`, "text/html"
    );
    root = tmp.body.firstElementChild;
  } catch { return null; }
  if (!root) return null;

  const tag = root.tagName.toLowerCase();
  const classList = Array.from(root.classList);
  const href = root.getAttribute("href");

  let candidates = [];

  // Strategy 1: full class match
  if (classList.length > 0) {
    const sel = tag + "." + classList.map((c) => CSS.escape(c)).join(".");
    candidates = Array.from(doc.querySelectorAll(sel));
    if (candidates.length === 0 && classList.length > 1) {
      const loose = tag + "." + CSS.escape(classList[0]);
      candidates = Array.from(doc.querySelectorAll(loose));
    }
  }

  // Strategy 2: href-based match for links/buttons
  if (candidates.length === 0 && href) {
    const sel = tag + '[href="' + CSS.escape(href) + '"]';
    candidates = Array.from(doc.querySelectorAll(sel));
  }

  // Strategy 3: match by inline style substrings (useful for enriched snippets
  // that carry computed styles as inline attributes)
  if (candidates.length === 0) {
    const inlineStyle = root.getAttribute("style") || "";
    const bgMatch = inlineStyle.match(/background-color:\s*([^;]+)/);
    const fsMatch = inlineStyle.match(/font-size:\s*([^;]+)/);
    if (bgMatch || fsMatch) {
      const all = Array.from(doc.querySelectorAll(tag)).slice(0, 100);
      candidates = all.filter((el) => {
        const cs = el.ownerDocument.defaultView?.getComputedStyle(el);
        if (!cs) return false;
        if (bgMatch && cs.backgroundColor !== bgMatch[1].trim()) return false;
        if (fsMatch && cs.fontSize !== fsMatch[1].trim()) return false;
        return true;
      });
    }
  }

  // Strategy 4: tag-only fallback
  if (candidates.length === 0) {
    candidates = Array.from(doc.querySelectorAll(tag));
  }
  if (candidates.length === 0) return null;

  const snippetText = (root.textContent || "").trim().slice(0, 120).toLowerCase();
  let best = candidates[0];
  let bestScore = -1;
  for (const el of candidates) {
    const t = (el.textContent || "").trim().slice(0, 120).toLowerCase();
    let score = 0;
    if (snippetText && t.includes(snippetText.slice(0, 30))) score += 2;
    if (t === snippetText) score += 3;
    if (el.children.length > 0 && root.children.length > 0) score += 1;
    // Bonus for href match
    if (href && el.getAttribute("href") === href) score += 4;
    if (score > bestScore) { bestScore = score; best = el; }
  }
  return best;
}

function serializeAttrs(el) {
  return Array.from(el.attributes)
    .map((a) => `${a.name}="${a.value.replace(/"/g, "&quot;")}"`)
    .join(" ");
}

// Build an HTML document that embeds `element` at the correct position in
// its original ancestor chain (no siblings), so descendant CSS selectors
// and inherited custom properties match the real page.
function buildContextualDoc(doc, element, base) {
  const headHtml = doc.head ? doc.head.innerHTML : "";
  const bodyAttrs = doc.body ? serializeAttrs(doc.body) : "";

  const ancestors = [];
  let cur = element.parentElement;
  while (cur && cur.tagName !== "BODY" && cur.tagName !== "HTML") {
    ancestors.unshift(cur);
    cur = cur.parentElement;
  }

  let openTags = "";
  let closeTags = "";
  for (const a of ancestors) {
    const tag = a.tagName.toLowerCase();
    openTags += `<${tag} ${serializeAttrs(a)}>`;
    closeTags = `</${tag}>` + closeTags;
  }

  const override = `
    <style id="__preview_overrides">
      html, body { margin: 0 !important; padding: 0 !important; background: #fff; overflow-x: hidden; }
      body { padding: 20px !important; }
      body > * { max-width: 100%; }
      a { pointer-events: none; }
      * { animation: none !important; transition: none !important; }
      /* Neutralize fixed/sticky positioning so the component renders in flow. */
      [style*="position: fixed"], [style*="position:fixed"],
      [style*="position: sticky"], [style*="position:sticky"] { position: static !important; }
      .fixed, .sticky, [class*="--fixed"], [class*="--sticky"],
      [class*="w-nav"][style*="position"] { position: static !important; }
    </style>
  `;

  return `<!DOCTYPE html><html><head>
    <base href="${base}">
    ${headHtml}
    ${override}
  </head><body ${bodyAttrs}>${openTags}${element.outerHTML}${closeTags}</body></html>`;
}

function buildSimpleDoc(snippet, headHtml, base) {
  return `<!DOCTYPE html><html><head>
<base href="${base}">
${headHtml || ""}
<style id="__preview_overrides">
html,body { margin:0; padding:20px; background:#fff; overflow-x:hidden; }
body > * { max-width: 100%; }
a { pointer-events: none; }
* { animation: none !important; transition: none !important; }
</style>
</head><body>${snippet}</body></html>`;
}

function StyleFallback({ s, name, sourceUrl }) {
  const previewStyle = {
    background: s.background_color || "#fff",
    color: s.text_color || "#111",
    borderRadius: s.border_radius || 8,
    padding: s.padding || "12px 16px",
    fontWeight: s.font_weight || 500,
    fontSize: s.font_size || 14,
    fontFamily: s.font_family || "inherit",
    border: s.border || "1px solid #e5e7eb",
    boxShadow: s.box_shadow || "none",
    display: "inline-block",
    maxWidth: "100%",
  };
  return (
    <div style={styles.fallbackPreview}>
      <div>
        <div style={previewStyle}>{name}</div>
        <div style={styles.fallbackCaption}>
          Rendered from extracted styles.{" "}
          {sourceUrl && (
            <a href={sourceUrl} target="_blank" rel="noreferrer" style={{ color: "#6366f1" }}>
              View on source page ↗
            </a>
          )}
        </div>
      </div>
    </div>
  );
}

// Snippets emitted by the screenshot-based analyzer carry their visual
// identity in an inline `style` attribute and have no source-page classes
// or hrefs. Trying to locate those in the saved HTML is actively harmful:
// we'd end up wrapping some unrelated tag in Bouqs's full <head> + Chakra
// ancestor chain and render a broken clone of the real page. Detect that
// case and render the snippet standalone.
function isSelfContainedSnippet(snippet) {
  try {
    const tmp = new DOMParser().parseFromString(
      `<!DOCTYPE html><html><body>${snippet}</body></html>`, "text/html"
    );
    const root = tmp.body.firstElementChild;
    if (!root) return false;
    const hasClass = (root.getAttribute("class") || "").trim().length > 0;
    const hasHref = !!root.getAttribute("href");
    const hasInlineStyle = (root.getAttribute("style") || "").trim().length > 0;
    // Also treat snippets that contain any inline-styled descendant as
    // self-contained (e.g. wrapper + styled child).
    const anyInlineStyled = hasInlineStyle || !!tmp.body.querySelector("[style]");
    return anyInlineStyled && !hasClass && !hasHref;
  } catch {
    return false;
  }
}

function LivePreview({ snippet, sourceUrl, page, fallbackStyles, name }) {
  const [srcDoc, setSrcDoc] = useState(null);
  const [blank, setBlank] = useState(false);
  const iframeRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    if (!snippet) { setSrcDoc(null); return; }
    (async () => {
      const base = sourceUrl || page?.url || "about:blank";

      // Fast path: snippet is already self-contained with inline styles —
      // render it standalone, don't try to look it up in the saved HTML.
      if (isSelfContainedSnippet(snippet)) {
        setSrcDoc(buildSimpleDoc(snippet, "", base));
        return;
      }

      if (!page) {
        setSrcDoc(buildSimpleDoc(snippet, "", base));
        return;
      }
      const doc = await fetchPageDoc(page.html_path);
      if (cancelled) return;
      if (doc) {
        const el = locateInDoc(doc, snippet);
        if (el) {
          setSrcDoc(buildContextualDoc(doc, el, base));
          return;
        }
        setSrcDoc(buildSimpleDoc(snippet, doc.head?.innerHTML || "", base));
        return;
      }
      setSrcDoc(buildSimpleDoc(snippet, "", base));
    })();
    return () => { cancelled = true; };
  }, [snippet, sourceUrl, page]);

  useEffect(() => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const onLoad = () => {
      try {
        const doc = iframe.contentDocument;
        if (!doc) return;
        const body = doc.body;
        const scrollH = body?.scrollHeight || 0;
        const hasVisual =
          (body?.innerText || "").trim().length > 0 ||
          body?.querySelector("img, svg, video, canvas, picture");
        if (scrollH < 8 && !hasVisual) {
          setBlank(true);
          iframe.style.height = "0px";
          return;
        }
        setBlank(false);
        iframe.style.height = Math.min(Math.max(scrollH, 40), 600) + "px";
      } catch {}
    };
    iframe.addEventListener("load", onLoad);
    return () => iframe.removeEventListener("load", onLoad);
  }, [srcDoc]);

  if (!snippet) {
    return <StyleFallback s={fallbackStyles || {}} name={name} sourceUrl={sourceUrl} />;
  }

  return (
    <div style={styles.livePreview}>
      {srcDoc ? (
        <>
          <iframe
            ref={iframeRef}
            srcDoc={srcDoc}
            sandbox="allow-same-origin allow-scripts"
            title={name}
            style={{
              width: "100%",
              height: blank ? 0 : 180,
              border: 0,
              display: blank ? "none" : "block",
              background: "#fff",
            }}
          />
          {blank && (
            <StyleFallback s={fallbackStyles || {}} name={name} sourceUrl={sourceUrl} />
          )}
        </>
      ) : (
        <div style={{ padding: 16, fontSize: 12, color: "#9ca3af" }}>loading preview…</div>
      )}
    </div>
  );
}

const styles = {
  wrap: {
    display: "grid",
    gridTemplateColumns: "240px minmax(0, 1fr)",
    gap: 20,
    marginBottom: 20,
    alignItems: "flex-start",
  },
  sidebar: {
    position: "sticky",
    top: 20,
    background: "#fff",
    borderRadius: 12,
    padding: 16,
    border: "1px solid #e5e7eb",
    maxHeight: "calc(100vh - 40px)",
    overflowY: "auto",
  },
  sidebarHead: { marginBottom: 14, paddingBottom: 12, borderBottom: "1px solid #f3f4f6" },
  sidebarTitle: { fontSize: 16, fontWeight: 700, color: "#111827" },
  sidebarSub: { fontSize: 11, color: "#6b7280", marginTop: 4, lineHeight: 1.4 },
  pagesCol: { display: "flex", flexDirection: "column", gap: 6, marginBottom: 14 },
  pageCard: {
    display: "flex", gap: 8, alignItems: "center",
    borderRadius: 6, overflow: "hidden", border: "1px solid #e5e7eb",
    background: "#fafafa", textDecoration: "none", color: "inherit",
    padding: 4,
  },
  thumb: { width: 46, height: 32, objectFit: "cover", objectPosition: "top", borderRadius: 4, flexShrink: 0 },
  pageTitle: {
    fontSize: 11, color: "#475569",
    whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
  },
  paletteWrap: { marginBottom: 14 },
  paletteTitle: {
    fontSize: 11,
    fontWeight: 600,
    color: "#6b7280",
    textTransform: "uppercase",
    letterSpacing: 0.6,
    marginBottom: 6,
  },
  paletteGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, 1fr)",
    gap: 4,
  },
  paletteTier: { marginBottom: 8 },
  paletteTierLabel: {
    fontSize: 10,
    fontWeight: 600,
    color: "#9ca3af",
    marginBottom: 3,
    letterSpacing: 0.3,
  },
  typoWrap: { marginBottom: 14 },
  typoRow: {
    padding: "6px 8px",
    borderRadius: 6,
    background: "#fafafa",
    border: "1px solid #eef0f3",
    marginBottom: 4,
  },
  typoLabel: {
    fontSize: 10,
    fontWeight: 600,
    color: "#9ca3af",
    textTransform: "uppercase",
    letterSpacing: 0.4,
    marginBottom: 2,
  },
  typoSample: {
    fontSize: 15,
    color: "#111827",
    lineHeight: 1.2,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  typoMeta: {
    fontSize: 10,
    color: "#6b7280",
    fontFamily: "ui-monospace, Menlo, monospace",
    marginTop: 2,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
  },
  swatch: {
    position: "relative",
    height: 40,
    borderRadius: 6,
    border: "1px solid rgba(0,0,0,0.08)",
    cursor: "pointer",
    padding: 0,
    overflow: "hidden",
  },
  swatchLabel: {
    position: "absolute",
    left: 0,
    right: 0,
    bottom: 0,
    fontSize: 9,
    fontWeight: 600,
    color: "#fff",
    padding: "1px 3px",
    background: "rgba(0,0,0,0.45)",
    textAlign: "center",
    fontFamily: "ui-monospace, Menlo, monospace",
  },

  cats: { display: "flex", flexDirection: "column", gap: 2, marginBottom: 14 },
  cat: {
    border: "none",
    background: "transparent",
    padding: "8px 10px",
    borderRadius: 8,
    fontSize: 13,
    cursor: "pointer",
    color: "#374151",
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
    textAlign: "left",
    fontWeight: 500,
  },
  catActive: {
    background: "#eef2ff",
    color: "#4338ca",
  },
  catLabel: { textTransform: "capitalize" },
  badge: {
    fontSize: 11,
    background: "rgba(0,0,0,0.06)",
    padding: "1px 8px",
    borderRadius: 999,
    fontWeight: 600,
  },
  tokens: {
    background: "#f8fafc",
    padding: 10,
    borderRadius: 8,
  },
  tokensSummary: { cursor: "pointer", fontSize: 12, fontWeight: 600 },
  tokensList: {
    marginTop: 8,
    fontSize: 11,
    fontFamily: "ui-monospace, Menlo, monospace",
    maxHeight: 240,
    overflowY: "auto",
    display: "flex",
    flexDirection: "column",
    gap: 3,
  },
  token: { display: "flex", gap: 6, flexWrap: "wrap" },
  tokenName: { color: "#7c3aed", fontWeight: 600 },
  tokenVal: { color: "#334155" },

  main: { minWidth: 0 },
  mainHead: {
    display: "flex",
    alignItems: "baseline",
    gap: 12,
    marginBottom: 14,
  },
  mainTitle: {
    fontSize: 22, fontWeight: 700, margin: 0,
    textTransform: "capitalize", color: "#111827",
  },
  mainCount: { fontSize: 13, color: "#6b7280" },
  empty: { padding: 40, textAlign: "center", color: "#9ca3af" },
  emptyState: {
    padding: "60px 32px",
    textAlign: "center",
    background: "#fafafa",
    borderRadius: 12,
    border: "1px dashed #d1d5db",
  },
  emptyIcon: { fontSize: 36, marginBottom: 12 },
  emptyTitle: { fontSize: 16, fontWeight: 600, color: "#374151", marginBottom: 8 },
  emptyDesc: { fontSize: 13, color: "#6b7280", lineHeight: 1.6, maxWidth: 400, margin: "0 auto" },

  compList: { display: "flex", flexDirection: "column", gap: 16 },
  compCard: {
    border: "1px solid #e5e7eb",
    borderRadius: 12,
    padding: 16,
    background: "#fff",
  },
  compHead: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 12,
    marginBottom: 6,
  },
  compHeadLeft: { display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" },
  compName: { fontSize: 15, color: "#111827" },
  compType: {
    fontSize: 10,
    padding: "2px 8px",
    borderRadius: 4,
    background: "#eef2ff",
    color: "#4338ca",
    textTransform: "uppercase",
    letterSpacing: 0.4,
    fontWeight: 600,
  },
  compCount: { fontSize: 12, color: "#6b7280" },
  sourceLink: { fontSize: 11, color: "#6366f1", textDecoration: "none" },
  compDesc: { fontSize: 13, color: "#4b5563", lineHeight: 1.5, margin: "4px 0 12px" },

  fallbackPreview: {
    padding: 16,
    background: "#fafafa",
    borderRadius: 8,
    border: "1px dashed #d1d5db",
    display: "flex",
    justifyContent: "center",
    alignItems: "center",
    minHeight: 60,
  },
  fallbackCaption: {
    fontSize: 11,
    color: "#9ca3af",
    marginTop: 8,
    textAlign: "center",
  },
  livePreview: {
    background: "#fff",
    borderRadius: 8,
    border: "1px solid #e5e7eb",
    overflow: "hidden",
  },
  blankNote: {
    padding: "14px 16px",
    fontSize: 12,
    color: "#6b7280",
    textAlign: "center",
    background: "#fafafa",
  },

  codeBlock: {
    marginTop: 12,
    borderRadius: 8,
    background: "#0b1020",
    border: "1px solid #1f2937",
    overflow: "hidden",
  },
  codeHeader: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "6px 10px 6px 12px",
    background: "#111827",
    borderBottom: "1px solid #1f2937",
  },
  codeLabel: {
    fontSize: 11,
    fontWeight: 600,
    color: "#9ca3af",
    textTransform: "uppercase",
    letterSpacing: 0.6,
  },
  copyBtn: {
    display: "inline-flex",
    alignItems: "center",
    gap: 5,
    background: "transparent",
    color: "#d1d5db",
    border: "1px solid #374151",
    padding: "4px 10px",
    borderRadius: 6,
    fontSize: 11,
    fontWeight: 600,
    cursor: "pointer",
    lineHeight: 1,
  },
  codePre: {
    margin: 0,
    padding: 12,
    fontSize: 11,
    lineHeight: 1.55,
    color: "#e5e7eb",
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    overflow: "auto",
    maxHeight: 260,
    whiteSpace: "pre",
  },

  stylesDetails: { marginTop: 10 },
  stylesSummary: { cursor: "pointer", fontSize: 11, color: "#6b7280" },
  stylesList: {
    fontSize: 11,
    fontFamily: "ui-monospace, Menlo, monospace",
    marginTop: 6,
    lineHeight: 1.6,
  },
};
