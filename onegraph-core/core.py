"""OneGraph core: Graphify builds the graph, Gemma 4 narrates it.

No DB, no web framework. Three front ends (PR-Agent, gitdiagram, deepwiki-open)
import this directly or shell out to it. All subprocess calls, no graphify
python bindings (none exist) — ponytail: shell out, don't wrap a client lib
that isn't there.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path

GEMINI_KEY_FILE = os.path.expanduser("~/.scoreone_gemini_keys")
GEMMA_MODEL = "gemma-4-26b-a4b-it"  # cloud (Gemini API), set ONEGRAPH_GEMMA_BACKEND=cloud
GEMMA_MODEL_LOCAL = "gemma4:12b-it-q4_K_M"  # default: local Ollama, no quota/outage risk
OLLAMA_URL = "http://localhost:11434/api/generate"
_GEMINI_KEY_RE = re.compile(r"AIza[A-Za-z0-9_-]{35}")
GRAPHS_SITE_URL = "https://graphs-site-omega.vercel.app"


def _gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    if os.path.exists(GEMINI_KEY_FILE):
        # Not one-key-per-line: this file is a single-line rotation pool of
        # several keys (scoreone's quota setup), so pull out the first real
        # token by shape rather than assuming line structure.
        match = _GEMINI_KEY_RE.search(Path(GEMINI_KEY_FILE).read_text())
        if match:
            return match.group(0)
    raise RuntimeError(f"set GEMINI_API_KEY or put a key in {GEMINI_KEY_FILE}")


def clone_repo(url: str) -> Path:
    dest = Path(tempfile.gettempdir()) / f"onegraph-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True, capture_output=True, text=True,
    )
    return dest


def build_graph(path: Path) -> Path:
    """graphify extract --code-only: tree-sitter AST only, no LLM call, $0 cost."""
    subprocess.run(
        ["graphify", "extract", str(path), "--code-only"],
        check=True, capture_output=True, text=True,
    )
    graph_path = path / "graphify-out" / "graph.json"
    if not graph_path.exists():
        raise RuntimeError(f"graphify extract ran but {graph_path} missing")
    return graph_path


def affected(path: Path, symbol: str, depth: int = 2) -> str:
    """Reverse traversal: what breaks if `symbol` changes. Real blast radius,
    not an LLM guess."""
    r = subprocess.run(
        ["graphify", "affected", symbol, "--depth", str(depth),
         "--graph", str(path / "graphify-out" / "graph.json")],
        capture_output=True, text=True,
    )
    return r.stdout


def blast_radius_summary(affected_text: str, limit: int = 4) -> tuple[int, list[str]]:
    """Parse `affected()`'s raw '- symbol() [relation] file:Lxx' lines into a
    count + a few example names, for inline PR comments. Pure data, no LLM:
    the line-level marker is a graph fact, not a narration."""
    lines = [l for l in affected_text.splitlines() if l.startswith("- ")]
    examples = [l[2:].split(" [", 1)[0] for l in lines[:limit]]
    return len(lines), examples


def risk_level(blast_radius_counts: list[int]) -> str:
    """High/Medium/Low from the real total blast radius, not an LLM's
    self-rating — a count is a fact, an LLM's opinion of its own severity
    isn't. Thresholds are a judgment call, not graphify output."""
    total = sum(blast_radius_counts)
    if total >= 15:
        return "High"
    if total >= 5:
        return "Medium"
    if total > 0:
        return "Low"
    return "None"


def query_graph(path: Path, question: str) -> str:
    r = subprocess.run(
        ["graphify", "query", question,
         "--graph", str(path / "graphify-out" / "graph.json")],
        capture_output=True, text=True,
    )
    return r.stdout


def summarize(context: str, instruction: str) -> str:
    """One Gemma 4 call. Local Ollama by default — no API key, no quota, no
    outage risk (the hosted Gemini API's gemma-4-* models were down mid-event
    on 2026-10-22; the local gemma4:12b-it quant isn't affected). Set
    ONEGRAPH_GEMMA_BACKEND=cloud to use the Gemini API instead. Context must
    come from graphify output (affected/query/graph.json), never raw file
    content — that's the whole point, citations trace back to a real graph
    node, not a hallucination."""
    prompt = f"{instruction}\n\nCONTEXT:\n{context}"

    if os.environ.get("ONEGRAPH_GEMMA_BACKEND") == "cloud":
        key = _gemini_key()
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{GEMMA_MODEL}:generateContent?key={key}")
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        return data["candidates"][0]["content"]["parts"][0]["text"]

    body = json.dumps({
        "model": GEMMA_MODEL_LOCAL, "prompt": prompt, "stream": False,
    }).encode()
    req = urllib.request.Request(OLLAMA_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    return data["response"]


def graph_stats(path: Path) -> tuple[int, int]:
    """Total (nodes, edges) Graphify actually extracted for this repo — the
    real scale number for judges, not the trimmed PR-subgraph count."""
    data = json.loads((path / "graphify-out" / "graph.json").read_text())
    return len(data["nodes"]), len(data["links"])


def _mermaid_id(node_id: str) -> str:
    return "n" + "".join(c if c.isalnum() else "_" for c in node_id)


def graph_mermaid(path: Path, changed_symbols: list[str], max_edges: int = 40) -> str:
    """1-hop subgraph around the PR's changed symbols, as GitHub-renderable
    Mermaid — no hosting, GitHub renders ```mermaid fences natively. Real
    edges from graph.json, not a guess."""
    data = json.loads((path / "graphify-out" / "graph.json").read_text())
    nodes = data["nodes"]
    labels = {n["id"]: n.get("label", n["id"]) for n in nodes}
    # changed_symbols are diff file paths (e.g. "src/flask/helpers.py"), not node
    # ids, so match the same way `graphify affected` resolves a file -> every
    # symbol defined in it, not an exact id lookup (which would always miss).
    changed_set = set(changed_symbols)
    changed_ids = {n["id"] for n in nodes if n["id"] in changed_set or n.get("source_file") in changed_set}
    if not changed_ids:
        return ""

    relevant = [e for e in data["links"] if e["source"] in changed_ids or e["target"] in changed_ids]
    lines = ["flowchart LR"]
    for edge in relevant[:max_edges]:
        src, dst = edge["source"], edge["target"]
        src_label = labels.get(src, src).replace('"', "'")
        dst_label = labels.get(dst, dst).replace('"', "'")
        rel = edge.get("relation", "")
        lines.append(f'  {_mermaid_id(src)}["{src_label}"] -->|{rel}| {_mermaid_id(dst)}["{dst_label}"]')
    if len(relevant) > max_edges:
        lines.append(f"  %% +{len(relevant) - max_edges} more edges omitted")
    return "\n".join(lines)


def export_graph_html(path: Path) -> Path:
    """graphify label + export html: names every community (sponsor's own
    LLM-backed labeler) before exporting the sponsor's own interactive
    viewer (force layout, color-coded clusters, clickable side panel) — the
    real product output, not a reimplementation of it. Plain `extract`
    leaves communities unlabeled (empty legend), so label is a required
    step here, not optional polish. Labeling is best-effort: a failed call
    still leaves graphify's own heuristic community names, never a hard
    failure for the whole run."""
    label_env = {**os.environ, "GEMINI_API_KEY": _gemini_key()}
    try:
        subprocess.run(
            ["graphify", "label", ".", "--backend", "gemini"],
            capture_output=True, text=True, cwd=str(path), env=label_env, timeout=300,
        )
    except subprocess.TimeoutExpired:
        pass  # huge repo, many communities: keep graphify's own heuristic names, don't block export
    subprocess.run(
        ["graphify", "export", "html", "--graph", str(path / "graphify-out" / "graph.json"), "--node-limit", "6000"],
        check=True, capture_output=True, text=True, cwd=str(path),
    )
    html_path = path / "graphify-out" / "graph.html"
    if not html_path.exists():
        raise RuntimeError(f"graphify export html ran but {html_path} missing")
    _boost_node_labels(html_path)
    _add_ui_polish(html_path)
    return html_path


_LABEL_SCALING_SNIPPET = """<script>
// onegraph: zoom-scaled node labels — vis-network's own scaling option, not
// a custom overlay. Hub/high-degree nodes already have the biggest dot size
// (graphify sets it from degree), so they read their function/file name even
// fully zoomed out; smaller leaf nodes reveal their name as you zoom in,
// which keeps a 2000+ node graph legible instead of a wall of text.
network.setOptions({ nodes: { scaling: { label: { enabled: true, min: 12, max: 34 } } } });
</script>
</body>"""


def _boost_node_labels(html_path: Path) -> None:
    """Patch graphify's exported graph.html so node names are visible on
    the graph itself at a readable zoom, not only on click."""
    text = html_path.read_text()
    if "</body>" in text and "onegraph: zoom-scaled node labels" not in text:
        html_path.write_text(text.replace("</body>", _LABEL_SCALING_SNIPPET, 1))


_UI_POLISH_STYLE = """<style id="onegraph-ui-polish">
/* onegraph: materials + motion (apple-design) on top of graphify's flat,
   static sidebar — translucent surfaces instead of solid fills, hover/press
   feedback, and real transitions instead of every state snapping instantly. */
#sidebar {
  background: color-mix(in srgb, var(--panel) 72%, transparent);
  backdrop-filter: blur(20px) saturate(160%);
  box-shadow: -8px 0 30px rgba(0, 0, 0, 0.25);
}
html, body, #sidebar { transition: background-color 220ms ease, color 220ms ease; }
#theme-wrap button, .search-item, .neighbor-link, .legend-item, #search {
  transition: background-color 160ms ease, color 160ms ease, transform 120ms ease, border-color 160ms ease;
}
#theme-wrap button:active { transform: scale(0.96); }
.legend-item:hover, .neighbor-link:hover, .search-item:hover { transform: translateX(2px); }
.legend-item.dimmed { transition: opacity 200ms ease; }
#info-content { transition: opacity 160ms ease; }
#info-content.og-swap { opacity: 0; }
#theme-wrap button svg, .og-stat svg, .og-search-icon { width: 13px; height: 13px; vertical-align: -2px; opacity: 0.8; }
#theme-wrap button svg { margin-right: 3px; }
#search-wrap { position: relative; }
#search { padding-left: 28px; }
.og-search-icon { position: absolute; left: 22px; top: 22px; pointer-events: none; }
#stats { display: flex; gap: 12px; flex-wrap: wrap; }
.og-stat { display: inline-flex; align-items: center; gap: 4px; }
.og-stat b { font-variant-numeric: tabular-nums; color: var(--text); }
@media (prefers-reduced-motion: reduce) {
  #theme-wrap button, .search-item, .neighbor-link, .legend-item, #search,
  #info-content, html, body, #sidebar {
    transition: opacity 150ms ease !important; transform: none !important;
  }
}
</style>
</head>"""

_UI_POLISH_SCRIPT = """<script id="onegraph-ui-polish-js">
(async function () {
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let animate = null;
  try {
    ({ animate } = await import("https://cdn.jsdelivr.net/npm/motion@14.6.0/+esm"));
  } catch (e) { /* offline or blocked CDN: everything below degrades to instant, no motion */ }

  const lucideScript = document.createElement('script');
  lucideScript.src = "https://cdn.jsdelivr.net/npm/lucide@1.61.0/dist/umd/lucide.js";
  lucideScript.onload = function () {
    var light = document.querySelector('[data-theme-choice="light"]');
    var dark = document.querySelector('[data-theme-choice="dark"]');
    var system = document.querySelector('[data-theme-choice="system"]');
    if (light) light.insertAdjacentHTML('afterbegin', '<svg data-lucide="sun"></svg>');
    if (dark) dark.insertAdjacentHTML('afterbegin', '<svg data-lucide="moon"></svg>');
    if (system) system.insertAdjacentHTML('afterbegin', '<svg data-lucide="monitor"></svg>');
    var searchWrap = document.getElementById('search-wrap');
    if (searchWrap) searchWrap.insertAdjacentHTML('beforeend', '<svg class="og-search-icon" data-lucide="search"></svg>');

    var statsEl = document.getElementById('stats');
    var m = statsEl && statsEl.textContent.match(/(\\d+)\\s*nodes.*?(\\d+)\\s*edges.*?(\\d+)\\s*communities/i);
    if (statsEl && m) {
      var counts = { nodes: Number(m[1]), edges: Number(m[2]), communities: Number(m[3]) };
      statsEl.innerHTML =
        '<span class="og-stat"><svg data-lucide="circle-dot"></svg><b data-count="' + counts.nodes + '">0</b> nodes</span>' +
        '<span class="og-stat"><svg data-lucide="share-2"></svg><b data-count="' + counts.edges + '">0</b> edges</span>' +
        '<span class="og-stat"><svg data-lucide="layers"></svg><b data-count="' + counts.communities + '">0</b> communities</span>';
    }
    window.lucide.createIcons();
    if (statsEl) {
      statsEl.querySelectorAll('b[data-count]').forEach(function (el) {
        var target = Number(el.dataset.count);
        if (reduceMotion || !animate || !target) { el.textContent = target; return; }
        animate(0, target, {
          duration: 0.9, easing: [0.16, 1, 0.3, 1],
          onUpdate: function (latest) { el.textContent = Math.round(latest); },
        });
      });
    }
  };
  document.head.appendChild(lucideScript);

  // Critically-damped focus (apple-design: no overshoot on a direct click,
  // not a flick) instead of vis-network's default easeInOutQuad.
  if (typeof focusNode === 'function' && typeof network !== 'undefined') {
    window.focusNode = function (nodeId) {
      var n = nodesDS.get(nodeId);
      if (!n) return;
      network.focus(nodeId, {
        scale: 1.4,
        animation: reduceMotion ? false : { duration: 450, easingFunction: 'easeOutCubic' },
      });
      network.selectNodes([nodeId]);
      showInfo(nodeId);
    };
  }

  // Fade the info panel on content swap instead of an instant snap.
  var infoContent = document.getElementById('info-content');
  if (infoContent && window.MutationObserver && !reduceMotion) {
    var fading = false;
    new MutationObserver(function () {
      if (fading) return;
      fading = true;
      infoContent.classList.add('og-swap');
      requestAnimationFrame(function () {
        infoContent.classList.remove('og-swap');
        fading = false;
      });
    }).observe(infoContent, { childList: true });
  }

  // Sidebar arrives with a soft slide-fade instead of appearing instantly.
  var sidebar = document.getElementById('sidebar');
  if (sidebar && animate && !reduceMotion) {
    animate(sidebar, { opacity: [0, 1], x: [16, 0] }, { duration: 0.5, easing: [0.16, 1, 0.3, 1] });
  }
})();
</script>
</body>"""


def _add_ui_polish(html_path: Path) -> None:
    """Patch graphify's exported graph.html with real motion + materials —
    translucent sidebar, hover/press feedback, counting stat line, a
    critically-damped node-focus animation, and a fading info panel —
    instead of flat panels where every state change snaps instantly. Uses
    Motion (springs/easing) and Lucide (icons) from CDN, both already in use
    elsewhere on this project; degrades to no-motion if the CDN is blocked or
    the viewer has prefers-reduced-motion set."""
    text = html_path.read_text()
    if "</head>" in text and "onegraph-ui-polish" not in text:
        text = text.replace("</head>", _UI_POLISH_STYLE, 1)
    if "</body>" in text and "onegraph-ui-polish-js" not in text:
        text = text.replace("</body>", _UI_POLISH_SCRIPT, 1)
    html_path.write_text(text)


def deploy_graph_html(html_path: Path, slug: str, site_dir: Path) -> str:
    """Copy this PR's graph.html into the static Vercel site and push a prod
    deploy. Returns the public URL the PR comment links to."""
    dest = site_dir / "graphs" / f"{slug}.html"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(html_path, dest)
    subprocess.run(
        ["vercel", "deploy", "--prod", "--yes", "--cwd", str(site_dir)],
        check=True, capture_output=True, text=True,
    )
    return f"{GRAPHS_SITE_URL}/graphs/{slug}.html"


def analyze_risk(repo_path: Path, changed_symbols: list[str]) -> str:
    """The one function PR-Agent's /graph_risk tool actually calls."""
    blast_radius = "\n\n".join(
        f"--- affected by change to {sym} ---\n{affected(repo_path, sym)}"
        for sym in changed_symbols
    )
    reply = summarize(
        blast_radius,
        "You are reviewing a pull request using real dependency-graph data "
        "(not guesses). Think it through if you need to, but the LAST line "
        "of your reply must be exactly: FINAL: <the risk review>, a short "
        "plain-English risk review, 2-4 sentences, citing the specific "
        "modules/functions named in the context. If the context is empty "
        "for a symbol, say the graph found no downstream dependents, do "
        "not invent any.",
    )
    return _extract_final(reply)


def _extract_final(reply: str) -> str:
    """Model reasoning (drafts, checklists, self-review) sometimes precedes
    OR follows the real answer despite instructions, and the model doesn't
    always honor the FINAL: marker. Try, in order: the FINAL: marker, the
    last quoted sentence (where the model usually puts its chosen answer
    when skipping the marker), then the last paragraph."""
    if "FINAL:" in reply:
        tail = reply.rsplit("FINAL:", 1)[-1].strip()
        if tail:
            return tail
    quotes = re.findall(r'"([^"\n]{20,})"', reply)
    if quotes:
        return quotes[-1].strip()
    paragraphs = [p.strip() for p in reply.split("\n\n") if p.strip()]
    return paragraphs[-1] if paragraphs else reply.strip()
