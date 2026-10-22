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
GEMMA_MODEL = "gemma-4-26b-a4b-it"
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


def query_graph(path: Path, question: str) -> str:
    r = subprocess.run(
        ["graphify", "query", question,
         "--graph", str(path / "graphify-out" / "graph.json")],
        capture_output=True, text=True,
    )
    return r.stdout


def summarize(context: str, instruction: str) -> str:
    """One Gemma 4 call via Gemini API. Context must come from graphify output
    (affected/query/graph.json), never raw file content — that's the whole
    point, citations trace back to a real graph node, not a hallucination."""
    key = _gemini_key()
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMMA_MODEL}:generateContent?key={key}")
    body = json.dumps({
        "contents": [{"parts": [{"text": f"{instruction}\n\nCONTEXT:\n{context}"}]}]
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return data["candidates"][0]["content"]["parts"][0]["text"]


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
    subprocess.run(
        ["graphify", "label", ".", "--backend", "gemini"],
        capture_output=True, text=True, cwd=str(path), env=label_env,
    )
    subprocess.run(
        ["graphify", "export", "html", "--graph", str(path / "graphify-out" / "graph.json")],
        check=True, capture_output=True, text=True, cwd=str(path),
    )
    html_path = path / "graphify-out" / "graph.html"
    if not html_path.exists():
        raise RuntimeError(f"graphify export html ran but {html_path} missing")
    _boost_node_labels(html_path)
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
