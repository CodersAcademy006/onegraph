"""A/B falsifiability proof: same question, same repo, same Gemma 4 model,
two contexts only -- baseline (raw file tree + naive keyword-matched
snippets, what RAG/diagram tools typically feed a model) vs OneGraph
(graphify's real dependency-graph query). Every file:Lnn citation in both
answers is checked against the actual repo on disk. Pure core.py, no
deepwiki/pr-agent boot needed.

Usage:
    python3 ab_proof.py [repo_url_or_local_path] ["question"]

Defaults to pallets/flask and "how does Flask dispatch an incoming request
to a view function" if no args given.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

DEFAULT_REPO = "https://github.com/pallets/flask"
DEFAULT_QUESTION = (
    "how does Flask dispatch an incoming request to the right view function"
)
CITE_RE = re.compile(r"([\w\-./]+\.\w+):L?(\d+)")
INSTRUCTION = (
    "Answer this question about the repository: {question}\n\n"
    "Cite every factual claim with the exact format path:Lnn (the file's path "
    "relative to the repo root, a colon, capital L, then the line number)."
)


def resolve_repo(arg: str | None) -> Path:
    if not arg:
        arg = DEFAULT_REPO
    if arg.startswith("http"):
        print(f"cloning {arg} ...")
        return core.clone_repo(arg)
    return Path(arg)


def file_tree(repo_path: Path, limit: int = 400) -> list[str]:
    paths = [
        str(p.relative_to(repo_path))
        for p in repo_path.rglob("*.py")
        if ".git" not in p.parts
    ]
    paths.sort()
    return paths[:limit]


def keyword_snippets(repo_path: Path, question: str, files: list[str],
                      top_n: int = 3, window: int = 15) -> str:
    """Naive substring-match retrieval -- no embeddings, no graph. This is
    the baseline a diagram/RAG tool without real indexing tends to give."""
    words = [w.lower() for w in re.findall(r"[a-zA-Z]{4,}", question)]
    scored = []
    for rel in files:
        text = (repo_path / rel).read_text(errors="ignore")
        score = sum(text.lower().count(w) for w in words)
        if score:
            scored.append((score, rel, text))
    scored.sort(key=lambda t: t[0], reverse=True)

    parts = []
    for _, rel, text in scored[:top_n]:
        lines = text.splitlines()
        # Best window = the `window`-line span with the most keyword hits,
        # not just the first occurrence -- a fairer naive-retrieval baseline.
        best_start, best_hits = 0, -1
        for start in range(0, max(1, len(lines) - window + 1)):
            span = " ".join(lines[start:start + window]).lower()
            hits = sum(span.count(w) for w in words)
            if hits > best_hits:
                best_start, best_hits = start, hits
        end = min(len(lines), best_start + window)
        snippet = "\n".join(f"{i + 1}: {lines[i]}" for i in range(best_start, end))
        parts.append(f"## {rel}\n{snippet}")
    return "\n\n".join(parts)


def verify_citations(repo_path: Path, answer: str) -> list[tuple[str, int, bool]]:
    seen, results = set(), []
    for path_str, line_str in CITE_RE.findall(answer):
        line_num = int(line_str)
        if (path_str, line_num) in seen:
            continue
        seen.add((path_str, line_num))
        f = repo_path / path_str
        ok = f.is_file()
        if ok:
            total_lines = sum(1 for _ in f.open(errors="ignore"))
            ok = 1 <= line_num <= total_lines
        results.append((path_str, line_num, ok))
    return results


def run(repo_path: Path, question: str) -> str:
    graph_path = repo_path / "graphify-out" / "graph.json"
    if not graph_path.exists():
        print(f"building graph for {repo_path} ...")
        core.build_graph(repo_path)

    instruction = INSTRUCTION.format(question=question)

    tree = file_tree(repo_path)
    baseline_context = (
        "FILE TREE:\n" + "\n".join(tree) + "\n\n" +
        "RELEVANT FILE SNIPPETS:\n" +
        keyword_snippets(repo_path, question, tree)
    )
    print("[1/2] asking Gemma 4 with baseline (file tree + keyword snippets) context...")
    baseline_answer = core.summarize(baseline_context, instruction)
    baseline_cites = verify_citations(repo_path, baseline_answer)

    print("[2/2] asking Gemma 4 with OneGraph (graphify query) context...")
    graph_context = core.query_graph(repo_path, question)
    graph_answer = core.summarize(graph_context, instruction)
    graph_cites = verify_citations(repo_path, graph_answer)

    def row(name, cites):
        ok = sum(1 for *_, v in cites if v)
        total = len(cites)
        pct = f"{100 * ok // total}%" if total else "n/a"
        return f"{name:<9} {total} citations, {ok} verified -> {pct} real"

    table = "\n".join([
        row("baseline", baseline_cites),
        row("OneGraph", graph_cites),
    ])

    report = [
        f"# A/B falsifiability proof\n",
        f"**Repo:** {repo_path.name}  **Question:** {question}\n",
        "## Result\n```\n" + table + "\n```\n",
        "## Baseline answer (file tree + keyword snippets)\n",
        baseline_answer, "\n",
        "**Citations checked:**\n",
        *(f"- `{p}:L{l}` -> {'OK' if v else 'DOES NOT EXIST'}" for p, l, v in baseline_cites),
        "\n## OneGraph answer (graphify query)\n",
        graph_answer, "\n",
        "**Citations checked:**\n",
        *(f"- `{p}:L{l}` -> {'OK' if v else 'DOES NOT EXIST'}" for p, l, v in graph_cites),
    ]
    print("\n" + table)
    return "\n".join(report)


if __name__ == "__main__":
    repo_arg = sys.argv[1] if len(sys.argv) > 1 else None
    question_arg = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_QUESTION
    out = run(resolve_repo(repo_arg), question_arg)
    out_path = Path(__file__).parent / "AB_PROOF_RESULTS.md"
    out_path.write_text(out)
    print(f"\nwritten to {out_path}")
