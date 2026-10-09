/* ctxgraph UI: vanilla JS, hash routing, fetches the JSON API on demand. */
(() => {
  const app = document.getElementById("app");
  const TYPES = ["symbol", "file", "object", "field", "flow", "component", "service", "team", "person", "rule", "recordtype", "permissionset", "layout"];

  // ---------- helpers ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => (n == null ? "–" : Number(n).toLocaleString());
  const api = async (path, opts) => {
    const r = await fetch(path, opts);
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || r.statusText);
    return j;
  };
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const typeBadge = (t) => `<span class="badge type t-${esc(t)}">${esc(t)}</span>`;
  const entityLink = (e, label) => `<a class="chip" href="#/entity/${encodeURIComponent(e.id)}" title="${esc(e.type)}">${esc(label || e.name)}</a>`;
  const fileLink = (path, line) => `<a href="#/file/${encodeURIComponent(path)}${line ? "#L" + line : ""}">${esc(path)}</a>`;
  const setActive = (route) => document.querySelectorAll("header nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === route));
  const render = (html) => { app.innerHTML = html; };
  const fail = (e) => render(`<div class="card err">${esc(e.message || e)}</div>`);
  const qsParam = (name) => new URLSearchParams(location.hash.split("?")[1] || "").get(name);

  // ---------- overview ----------
  async function overview() {
    setActive("overview");
    const o = await api("/api/overview");
    document.getElementById("repo-name").textContent = "· " + o.repo;
    const src = o.sources.map((s) => `
      <tr><td><b>${esc(s.id)}</b><div class="small muted mono">${esc(s.paths.join("  "))}</div></td>
      <td>${esc(s.type)}</td><td><span class="badge">${esc(s.bucket)}</span></td>
      <td class="num">${num(s.files)}</td><td class="num">${num(s.chunks)}</td><td class="num">${num(s.tokens)}</td>
      <td class="mono small">${esc((s.ingested_commit || "").slice(0, 8))}<div class="muted">${esc((s.ingested_at || "").slice(0, 16))}</div></td></tr>`).join("");
    const ents = TYPES.filter((t) => o.entities[t]).map((t) => `<a class="chip" href="#/entities?type=${t}">${typeBadge(t)} ${num(o.entities[t])}</a>`).join(" ");
    const edges = Object.entries(o.edges).map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="num">${num(v)}</td></tr>`).join("");
    const langs = Object.entries(o.chunks.by_language).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${num(v)}</td></tr>`).join("");
    const kinds = Object.entries(o.chunks.by_kind).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${num(v)}</td></tr>`).join("");
    const findings = o.findings.length ? o.findings.map((f) => `<div class="finding"><b>${esc(f.key)}</b> — ${esc(f.message)}<div class="small muted">remedy: ${esc(f.remedy)}${f.items && f.items.length ? " · " + esc(f.items.slice(0, 6).join(", ")) : ""}</div></div>`).join("") : `<div class="muted">no findings</div>`;
    const emb = o.embeddings.count ? `${num(o.embeddings.count)} vectors` : (o.embeddings.provider === "none" ? "off" : "none yet");
    render(`
      <h1>What is stored for <span class="mono">${esc(o.repo)}</span></h1>
      <div class="explain">Context is created in stages. Sources (globs in <code>ctxgraph.yaml</code>) are resolved against <code>git ls-files</code>; each file is split into chunks by heading or declaration; code and metadata are parsed into a graph of entities and edges; chunks get vectors; a query retrieves candidates, ranks them, and compiles a token-budgeted pack with graph facts on top. Click a stage to inspect it.</div>
      <div class="pipeline">
        <div class="stage"><div class="n">${num(o.sources.length)}</div><div class="t">sources · ${num(o.sources.reduce((a, s) => a + s.files, 0))} files</div></div><div class="arrow">→</div>
        <a class="stage" href="#/files"><div class="n">${num(o.chunks.total)}</div><div class="t">chunks · ${num(o.chunks.tokens)} tokens · p50 ${num(o.chunks.p50)}, p90 ${num(o.chunks.p90)}</div></a><div class="arrow">→</div>
        <a class="stage" href="#/entities"><div class="n">${num(o.entities_total)}</div><div class="t">entities · ${num(o.edges_total)} edges · ${num(o.mentions)} doc mentions</div></a><div class="arrow">→</div>
        <div class="stage"><div class="n">${esc(emb)}</div><div class="t">embeddings · ${esc(o.embeddings.model || o.embeddings.provider)}</div></div><div class="arrow">→</div>
        <a class="stage" href="#/query"><div class="n">${num(o.queries_logged)}</div><div class="t">queries logged · try one</div></a>
      </div>
      <div class="row">
        <div class="col" style="flex:2 1 560px">
          <h2>Sources</h2>
          <div class="card"><table><thead><tr><th>source</th><th>type</th><th>bucket</th><th class="num">files</th><th class="num">chunks</th><th class="num">tokens</th><th>ingested</th></tr></thead><tbody>${src}</tbody></table></div>
          <h2>Graph</h2>
          <div class="card"><div class="chips">${ents || '<span class="muted">no entities</span>'}</div>
            <h3>edges by kind</h3><table><tbody>${edges || '<tr><td class="muted">none</td></tr>'}</tbody></table></div>
        </div>
        <div class="col">
          <h2>Chunks</h2>
          <div class="card"><h3>by language</h3><table><tbody>${langs || '<tr><td class="muted">none</td></tr>'}</tbody></table>
            <h3>by kind</h3><table><tbody>${kinds}</tbody></table>
            <div class="small muted" style="margin-top:8px">largest chunk ${num(o.chunks.max)} tokens · target ${num(o.config.chunking.target_tokens)}, max ${num(o.config.chunking.max_tokens)} · parser: ${esc(o.config.parser)}</div></div>
          <h2>Retrieval knobs</h2>
          <div class="card kv">
            <span class="k">budget</span><span>${num(o.config.budget_tokens_default)} tokens</span>
            <span class="k">candidates</span><span>${num(o.config.retrieval.candidates)}</span>
            <span class="k">test path penalty</span><span>×${o.config.retrieval.test_path_penalty}</span>
            <span class="k">importance boost</span><span>${o.config.retrieval.importance_boost}</span>
            <span class="k">chunks per file</span><span>${o.config.retrieval.max_chunks_per_file}</span>
            <span class="k">near-dup cosine</span><span>${o.config.retrieval.near_duplicate_cosine}</span>
            <span class="k">rerank</span><span>${o.config.rerank_enabled ? "on" : "off (toggle per query)"}</span>
            <span class="k">index</span><span class="mono small">${esc(o.db_path)} · ${(o.db_bytes / 1048576).toFixed(1)} MB</span>
          </div>
          <h2>Findings</h2>${findings}
        </div>
      </div>`);
  }

  // ---------- entities ----------
  async function entities() {
    setActive("entities");
    const type = qsParam("type") || "";
    const q = qsParam("q") || "";
    const data = await api(`/api/entities?type=${encodeURIComponent(type)}&q=${encodeURIComponent(q)}&limit=200`);
    const opts = ['<option value="">all types</option>'].concat(TYPES.filter((t) => data.types[t]).map((t) => `<option value="${t}" ${t === type ? "selected" : ""}>${t} (${num(data.types[t])})</option>`)).join("");
    const rows = data.items.map((e) => `
      <tr class="click" onclick="location.hash='#/entity/${encodeURIComponent(e.id)}'">
        <td>${typeBadge(e.type)}</td><td><b>${esc(e.name)}</b>${e.label ? ` <span class="muted">${esc(e.label)}</span>` : ""}${e.test ? ' <span class="badge test">test</span>' : ""}</td>
        <td class="muted">${esc(e.kind || "")}</td><td class="mono small muted">${esc(e.path || "")}</td>
        <td class="num">${num(e.in)}</td><td class="num">${num(e.out)}</td></tr>`).join("");
    render(`
      <h1>Entities <span class="muted small">${num(data.total)} match</span></h1>
      <div class="toolbar">
        <select id="etype">${opts}</select>
        <input type="search" id="eq" placeholder="search names…" value="${esc(q)}" style="min-width:260px">
        <button class="ghost" id="ego">Search</button>
        <span class="muted small">sorted by incoming edges (how much the codebase leans on it)</span>
      </div>
      <div class="card"><table><thead><tr><th>type</th><th>name</th><th>kind</th><th>path</th><th class="num">in</th><th class="num">out</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="empty">nothing matches</td></tr>'}</tbody></table></div>`);
    const go = () => { location.hash = `#/entities?type=${encodeURIComponent(document.getElementById("etype").value)}&q=${encodeURIComponent(document.getElementById("eq").value)}`; };
    document.getElementById("ego").onclick = go;
    document.getElementById("etype").onchange = go;
    document.getElementById("eq").onkeydown = (ev) => { if (ev.key === "Enter") go(); };
  }

  async function entity(id) {
    setActive("entities");
    const [d, g] = await Promise.all([api(`/api/entity?id=${encodeURIComponent(id)}`), api(`/api/neighborhood?id=${encodeURIComponent(id)}`)]);
    const attrs = Object.entries(d.attrs || {}).filter(([k]) => !["path", "start_line", "end_line", "simple"].includes(k))
      .map(([k, v]) => `<span class="k">${esc(k)}</span><span>${esc(Array.isArray(v) ? v.join(", ") : v)}</span>`).join("");
    const group = (obj, dir) => Object.entries(obj).map(([kind, items]) => `<h3>${dir} ${esc(kind)} <span class="muted">(${items.length})</span></h3><div class="chips">${items.slice(0, 60).map((i) => entityLink(i, i.name) + (i.count ? `<span class="muted small"> ×${i.count}</span>` : "")).join(" ")}${items.length > 60 ? `<span class="muted">… +${items.length - 60}</span>` : ""}</div>`).join("");
    const chunkList = (list) => list.map((c) => `<div class="item" onclick="location.hash='#/file/${encodeURIComponent(c.path)}#L${c.start_line}'" title="${esc(c.path)}"><span class="mono">${esc(c.path.split("/").pop())}<span class="muted">#L${c.start_line}-${c.end_line}</span></span> <span class="muted small">${esc(c.heading || "")} · ${num(c.tokens)} tok · ${esc(c.bucket)}</span></div>`).join("");
    render(`
      <div class="toolbar"><a href="#/entities">← entities</a></div>
      <h1>${typeBadge(d.type)} ${esc(d.name)} ${d.is_test ? '<span class="badge test">test code</span>' : ""}</h1>
      <div class="summary">${esc(d.summary)}</div>
      ${d.path ? `<div class="small">source: ${fileLink(d.path, d.attrs.start_line)}</div>` : ""}
      <div class="row" style="margin-top:12px">
        <div class="col" style="flex:2 1 600px">
          <h2>Neighbourhood <span class="muted small">(semantic edges, two hops, ${num(g.nodes.length)} nodes${g.truncated ? ", truncated to the most referenced" : ""}; members and declarations are listed below)</span></h2>
          <svg class="graph" id="graph"></svg>
          <div class="legend" id="legend"></div>
          <div class="legend"><span style="--c: var(--t-symbol)">calls</span><span style="--c: var(--muted)">references / other</span><span class="muted">· click a node to open it, hover for details</span></div>
          ${group(d.outgoing, "→")}${group(d.incoming, "←")}
        </div>
        <div class="col">
          ${attrs ? `<h2>Attributes</h2><div class="card kv">${attrs}</div>` : ""}
          ${d.chunks.length ? `<h2>Chunks carrying it <span class="muted small">(${d.chunks.length})</span></h2><div class="card side">${chunkList(d.chunks)}</div>` : ""}
          ${d.mention_chunks.length ? `<h2>Mentioned in docs</h2><div class="card side">${chunkList(d.mention_chunks)}</div>` : ""}
          ${d.mentioned_in && d.mentioned_in.length ? `<div class="small muted" style="margin-top:6px">${d.mentioned_in.map((p) => fileLink(p)).join(", ")}</div>` : ""}
        </div>
      </div>`);
    drawGraph(document.getElementById("graph"), g);
    document.getElementById("legend").innerHTML = TYPES.filter((t) => g.nodes.some((n) => n.type === t)).map((t) => `<span style="--c: var(--t-${t})">${t}</span>`).join("");
  }

  // ---------- neighbourhood graph: radial, dependency-free ----------
  function drawGraph(svg, g) {
    const W = svg.clientWidth || 900, H = svg.clientHeight || 520;
    const cx = W / 2, cy = H / 2;
    const byId = Object.fromEntries(g.nodes.map((n) => [n.id, { ...n }]));
    const nodes = Object.values(byId);
    const root = byId[g.root];
    if (root) { root.x = cx; root.y = cy; }
    // first hop on a ring, ordered by how referenced they are so big nodes spread out
    const ring1 = nodes.filter((n) => n.depth === 1).sort((a, b) => b.indeg - a.indeg);
    // rings are ellipses so a wide canvas is used fully
    const rx1 = W * 0.26, ry1 = H * 0.3;
    ring1.forEach((n, i) => { n.angle = (i / Math.max(ring1.length, 1)) * 2 * Math.PI - Math.PI / 2; n.x = cx + Math.cos(n.angle) * rx1; n.y = cy + Math.sin(n.angle) * ry1; });
    // second hop fans out from its parent on an outer ring
    const rx2 = W * 0.42, ry2 = H * 0.46;
    const kids = {};
    nodes.filter((n) => n.depth === 2).forEach((n) => { (kids[n.parent] = kids[n.parent] || []).push(n); });
    for (const [pid, list] of Object.entries(kids)) {
      const p = byId[pid]; if (!p) continue;
      const spread = Math.min(0.9, 0.22 * list.length);
      list.forEach((n, i) => { const a = (p.angle ?? 0) + (list.length > 1 ? (i / (list.length - 1) - 0.5) * spread : 0); n.angle = a; n.x = cx + Math.cos(a) * rx2; n.y = cy + Math.sin(a) * ry2; });
    }
    // a few relaxation passes so nothing sits on top of anything else
    for (let it = 0; it < 60; it++) {
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j]; if (a.id === g.root || b.id === g.root) continue;
        let dx = b.x - a.x, dy = b.y - a.y; const d = Math.sqrt(dx * dx + dy * dy) || 0.01; const min = 26;
        if (d < min) { const push = (min - d) / 2; dx /= d; dy /= d; a.x -= dx * push; a.y -= dy * push; b.x += dx * push; b.y += dy * push; }
      }
      for (const n of nodes) { n.x = Math.max(20, Math.min(W - 20, n.x)); n.y = Math.max(16, Math.min(H - 16, n.y)); }
    }
    const edges = g.edges.filter((e) => byId[e.src] && byId[e.dst]);
    const r = (n) => n.id === g.root ? 11 : 5 + Math.min(7, Math.log2(1 + (n.indeg || 0)));
    const short = (n) => (n.type === "file" ? n.name.split("/").pop() : n.name.split(".").slice(-2).join("."));
    const label = (n) => {
      if (n.id === g.root) return `<text x="${(n.x + 14).toFixed(1)}" y="${(n.y + 4).toFixed(1)}" font-weight="600">${esc(short(n))}</text>`;
      const left = Math.cos(n.angle ?? 0) < -0.1;
      return `<text x="${(n.x + (left ? -1 : 1) * (r(n) + 4)).toFixed(1)}" y="${(n.y + 4).toFixed(1)}" text-anchor="${left ? "end" : "start"}">${esc(short(n))}</text>`;
    };
    const labelled = new Set(nodes.filter((n) => n.depth <= 1).map((n) => n.id));
    // label second-hop nodes only where there is room: the most referenced, none closer than 18px vertically to a labelled neighbour
    const placed = nodes.filter((n) => n.depth <= 1);
    nodes.filter((n) => n.depth === 2).sort((a, b) => b.indeg - a.indeg).forEach((n) => {
      const side = Math.cos(n.angle ?? 0) < -0.1 ? -1 : 1;
      if (!placed.some((m) => Math.abs(m.y - n.y) < 18 && Math.abs(m.x - n.x) < 220 && (Math.cos(m.angle ?? 0) < -0.1 ? -1 : 1) === side)) { labelled.add(n.id); placed.push(n); }
    });
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = edges.map((e) => `<line class="${esc(e.kind)}" x1="${byId[e.src].x.toFixed(1)}" y1="${byId[e.src].y.toFixed(1)}" x2="${byId[e.dst].x.toFixed(1)}" y2="${byId[e.dst].y.toFixed(1)}"><title>${esc(byId[e.src].name)} ${esc(e.kind)} ${esc(byId[e.dst].name)}</title></line>`).join("")
      + nodes.map((n) => `<g><circle class="${n.id === g.root ? "root" : ""}" cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r(n)}" fill="var(--t-${esc(n.type)})" data-id="${esc(n.id)}"><title>${esc(n.type)}: ${esc(n.name)}${n.kind ? " (" + esc(n.kind) + ")" : ""} · ${n.indeg} incoming</title></circle>${labelled.has(n.id) ? label(n) : ""}</g>`).join("");
    svg.querySelectorAll("circle").forEach((c) => c.addEventListener("click", () => { location.hash = `#/entity/${encodeURIComponent(c.dataset.id)}`; }));
  }

  // ---------- files ----------
  async function files() {
    setActive("files");
    const q = qsParam("q") || "";
    const data = await api(`/api/files?q=${encodeURIComponent(q)}&limit=300`);
    const rows = data.items.map((f) => `
      <tr class="click" onclick="location.hash='#/file/${encodeURIComponent(f.path)}'">
        <td class="mono">${esc(f.path)}${f.test ? ' <span class="badge test">test</span>' : ""}</td><td>${esc(f.source)}</td><td class="muted">${esc(f.language || f.kind || "")}</td>
        <td class="num">${num(f.chunks)}</td><td class="num">${num(f.tokens)}</td><td>${esc(f.owner || f.service || "")}</td>
        <td class="small muted">${esc(f.last_changed || "")}${f.last_author ? " · " + esc(f.last_author) : ""}${f.commits ? " · " + f.commits + " commits" : ""}</td></tr>`).join("");
    render(`
      <h1>Files <span class="muted small">${num(data.total)} indexed</span></h1>
      <div class="toolbar"><input type="search" id="fq" placeholder="filter paths…" value="${esc(q)}" style="min-width:320px"><button class="ghost" id="fgo">Filter</button>
        <span class="muted small">open a file to see where it was cut into chunks</span></div>
      <div class="card"><table><thead><tr><th>path</th><th>source</th><th>language</th><th class="num">chunks</th><th class="num">tokens</th><th>owner</th><th>last change</th></tr></thead><tbody>${rows || '<tr><td colspan="7" class="empty">no files</td></tr>'}</tbody></table>
      ${data.total > data.items.length ? `<div class="muted small" style="padding:8px">showing ${data.items.length} of ${num(data.total)}; narrow the filter</div>` : ""}</div>`);
    const go = () => { location.hash = `#/files?q=${encodeURIComponent(document.getElementById("fq").value)}`; };
    document.getElementById("fgo").onclick = go;
    document.getElementById("fq").onkeydown = (ev) => { if (ev.key === "Enter") go(); };
  }

  async function file(path, targetLine) {
    setActive("files");
    const d = await api(`/api/file?path=${encodeURIComponent(path)}`);
    const lines = d.text.split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    const starts = new Map(d.chunks.map((c, i) => [c.start_line, { c, i }]));
    const inChunk = new Array(lines.length + 2).fill(-1);
    d.chunks.forEach((c, i) => { for (let l = c.start_line; l <= c.end_line; l++) inChunk[l] = i; });
    let html = "";
    for (let i = 0; i < lines.length; i++) {
      const ln = i + 1;
      const s = starts.get(ln);
      if (s) html += `<tr class="chunkhead" id="C${s.i}"><td class="ln">▸</td><td>chunk ${s.i + 1}/${d.chunks.length} · L${s.c.start_line}-${s.c.end_line} · ${num(s.c.tokens)} tokens · ${esc(s.c.heading || "(no heading)")} · ${esc(s.c.bucket)}${s.c.embedded ? " · vector ✓" : ""}</td></tr>`;
      const ci = inChunk[ln];
      const band = ci < 0 ? "" : (ci % 2 ? "b" : "a");
      html += `<tr class="${band} ${targetLine === ln ? "target" : ""}" id="L${ln}"><td class="ln">${ln}</td><td>${esc(lines[i]) || " "}</td></tr>`;
    }
    const uncovered = lines.length - inChunk.slice(1, lines.length + 1).filter((x) => x >= 0).length;
    const chunks = d.chunks.map((c, i) => `<div class="item" onclick="document.getElementById('C${i}').scrollIntoView({block:'start'})"><b>${i + 1}</b> <span class="mono">L${c.start_line}-${c.end_line}</span> <span class="muted small">${num(c.tokens)} tok</span><div class="small muted">${esc(c.heading || "")}</div></div>`).join("");
    const symbols = d.symbols.map((s) => `<div class="item" onclick="location.hash='#/entity/${encodeURIComponent(s.id)}'"><span class="muted small">${esc(s.kind || "")}</span> ${esc(s.name)} <span class="mono small muted">L${s.start_line}-${s.end_line}</span></div>`).join("");
    render(`
      <div class="toolbar"><a href="#/files">← files</a></div>
      <h1 class="mono" style="font-size:16px">${esc(d.path)} ${d.test ? '<span class="badge test">test</span>' : ""}</h1>
      ${d.entity ? `<div class="summary">${esc(d.entity.summary)}</div>` : ""}
      <div class="small muted" style="margin-bottom:10px">${num(lines.length)} lines → ${num(d.chunks.length)} chunks (bands alternate per chunk${uncovered ? `; ${uncovered} blank/uncovered lines` : ""}). Each chunk is what retrieval returns as a unit; its heading line is the breadcrumb an agent sees.</div>
      <div class="row">
        <div class="col" style="flex:3 1 700px"><div class="source"><table><tbody>${html}</tbody></table></div></div>
        <div class="col" style="flex:1 1 260px">
          <div class="side"><h3>chunks</h3><div class="card" style="padding:0">${chunks}</div>
          ${symbols ? `<h3>declarations</h3><div class="card" style="padding:0">${symbols}</div>` : ""}</div>
        </div>
      </div>`);
    if (targetLine) { const el = document.getElementById("L" + targetLine); if (el) el.scrollIntoView({ block: "center" }); }
  }

  // ---------- query ----------
  const DECISION = {
    included: ["ok", "in pack"], over_budget: ["warn", "cut: budget"], file_cap: ["warn", "cut: per-file cap"],
    duplicate: ["", "duplicate text"], not_reached: ["", "not reached"],
  };
  async function queryView() {
    setActive("query");
    const last = sessionStorage.getItem("ctxgraph.q") || "";
    render(`
      <h1>Query playground</h1>
      <div class="explain">Ask what an agent would ask. Left: the pack exactly as the agent receives it. Right: every candidate retrieval produced, with the decision the compiler made about it and why. Scores are BM25 (or fused rank scores in hybrid mode) after test demotion and the importance boost.</div>
      <div class="card" style="margin-top:12px">
        <textarea id="qtext" placeholder="e.g. what starts a pipeline execution when it is triggered">${esc(last)}</textarea>
        <div class="toolbar" style="margin-top:8px">
          <label>budget <input type="text" id="qbudget" value="3000" style="width:80px"></label>
          <label>retriever <select id="qret"><option value="auto">auto</option><option value="bm25">bm25</option><option value="hybrid">hybrid</option><option value="vector">vector</option></select></label>
          <label><input type="checkbox" id="qrerank"> rerank</label>
          <label><input type="checkbox" id="qfacts" checked> facts</label>
          <label>bucket <select id="qbucket"><option value="">any</option><option>knowledge</option><option>expertise</option><option>norms</option></select></label>
          <button id="qgo">Run</button>
          <span id="qstatus" class="muted small"></span>
        </div>
      </div>
      <div id="qout"></div>`);
    const run = async () => {
      const text = document.getElementById("qtext").value.trim();
      if (!text) return;
      sessionStorage.setItem("ctxgraph.q", text);
      document.getElementById("qstatus").textContent = "running…";
      try {
        const bucket = document.getElementById("qbucket").value;
        const r = await post("/api/query", {
          text, budget: Number(document.getElementById("qbudget").value) || 3000, retriever: document.getElementById("qret").value,
          rerank: document.getElementById("qrerank").checked, facts: document.getElementById("qfacts").checked, buckets: bucket ? [bucket] : [],
        });
        document.getElementById("qstatus").textContent = `${r.retriever} · match ${r.mode} · search ${r.search_ms} ms, total ${r.total_ms} ms`;
        renderQuery(r);
      } catch (e) { document.getElementById("qstatus").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    };
    document.getElementById("qgo").onclick = run;
    document.getElementById("qtext").onkeydown = (ev) => { if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey)) run(); };
  }

  function renderQuery(r) {
    const p = r.pack;
    const facts = p.facts.map((f) => `<div class="fact">${esc(f)}</div>`).join("");
    const chunks = p.chunks.map((c, i) => `
      <div class="chunkcard"><div class="head" onclick="this.nextElementSibling.style.display = this.nextElementSibling.style.display === 'none' ? '' : 'none'">
        <span><span class="badge">${esc(c.bucket)}</span> <span class="mono">${esc(c.citation)}</span> <span class="muted small">${esc(c.heading || "")}</span></span>
        <span class="muted small">#${c.rank} · ${num(c.token_count)} tok</span></div><pre>${esc(c.text)}</pre></div>`).join("");
    const cands = r.candidates.map((c) => {
      const [cls, label] = DECISION[c.decision] || ["", c.decision];
      return `<tr class="click" onclick="location.hash='#/file/${encodeURIComponent(c.path)}#L${c.start_line}'">
        <td class="num">${c.rank}</td><td><span class="badge ${cls}">${esc(label)}</span></td><td class="num">${c.score}</td>
        <td class="mono small">${esc(c.path)}<span class="muted">#L${c.start_line}-${c.end_line}</span>${c.test ? ' <span class="badge test">test</span>' : ""}<div class="muted">${esc(c.heading || "")}</div></td>
        <td class="num">${num(c.tokens)}</td><td class="num muted">${num(c.importance)}</td></tr>`;
    }).join("");
    const factTokens = p.used_tokens - p.chunks.reduce((a, c) => a + c.token_count, 0);
    document.getElementById("qout").innerHTML = `
      <div class="row" style="margin-top:14px">
        <div class="col" style="flex:1 1 520px">
          <h2>Pack <span class="muted small">${num(p.used_tokens)} / ${num(p.budget_tokens)} tokens · ${p.chunks.length} chunks · ${num(factTokens)} tokens of facts</span></h2>
          ${facts ? `<div class="card"><h3>facts (from the graph, no model)</h3>${facts}</div>` : ""}
          ${chunks || '<div class="empty">no chunks fit</div>'}
          <details style="margin-top:8px"><summary class="muted small">markdown as served</summary><pre>${esc(r.markdown)}</pre></details>
        </div>
        <div class="col" style="flex:1 1 520px">
          <h2>Candidates <span class="muted small">${r.candidates.length} retrieved · ${p.dropped_over_budget} cut by budget · ${p.dropped_file_cap} by per-file cap · ${p.dropped_duplicates} duplicates${r.near_duplicates_dropped ? ` · ${r.near_duplicates_dropped} near-duplicates` : ""}</span></h2>
          <div class="card" style="padding:0"><table><thead><tr><th class="num">#</th><th>decision</th><th class="num">score</th><th>chunk</th><th class="num">tok</th><th class="num" title="incoming references to classes in this file">imp</th></tr></thead><tbody>${cands || '<tr><td colspan="6" class="empty">nothing retrieved</td></tr>'}</tbody></table></div>
          <div class="small muted" style="margin-top:8px">Budget ${num(r.knobs.budget)}, 10% reserved for facts and citations; at most ${r.knobs.max_chunks_per_file} chunks per file; test paths ×${r.knobs.test_path_penalty}; importance boost ${r.knobs.importance_boost}.</div>
        </div>
      </div>`;
  }

  // ---------- log ----------
  async function log() {
    setActive("log");
    const rows = await api("/api/log?limit=100");
    render(`
      <h1>Query log <span class="muted small">${rows.length} most recent</span></h1>
      <div class="card"><table><thead><tr><th>when</th><th>query</th><th>match</th><th class="num">cands</th><th class="num">in pack</th><th class="num">tokens</th><th class="num">ms</th><th>sources hit</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td class="small muted mono">${esc(r.ts.slice(0, 16))}</td><td><a href="#/query" onclick="sessionStorage.setItem('ctxgraph.q', ${JSON.stringify(r.text).replace(/"/g, "&quot;")})">${esc(r.text)}</a></td>
        <td><span class="badge ${r.mode === "none" ? "bad" : r.mode.startsWith("any") ? "warn" : "ok"}">${esc(r.mode)}</span></td>
        <td class="num">${r.candidates}</td><td class="num">${r.pack_chunks}</td><td class="num">${num(r.used_tokens)}/${num(r.budget)}</td><td class="num">${r.duration_ms}</td><td class="small muted">${esc(JSON.parse(r.sources_json).join(", "))}</td></tr>`).join("") || '<tr><td colspan="8" class="empty">no queries yet</td></tr>'}
      </tbody></table></div>`);
  }

  // ---------- router ----------
  async function route() {
    const h = location.hash || "#/";
    const path = h.slice(1).split("?")[0];
    try {
      if (path === "/" || path === "") return await overview();
      if (path === "/entities") return await entities();
      if (path.startsWith("/entity/")) return await entity(decodeURIComponent(path.slice(8)));
      if (path === "/files") return await files();
      if (path.startsWith("/file/")) {
        const rest = path.slice(6); const m = rest.match(/^(.*?)(?:#L(\d+))?$/);
        return await file(decodeURIComponent(m[1]), m[2] ? Number(m[2]) : null);
      }
      if (path === "/query") return await queryView();
      if (path === "/log") return await log();
      render(`<div class="empty">unknown route</div>`);
    } catch (e) { fail(e); }
  }
  window.addEventListener("hashchange", route);
  route();
})();
