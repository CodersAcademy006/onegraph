"""Smoke test. Only the free (no-API-cost) parts run by default — build_graph,
affected, query_graph are local graphify calls, $0. summarize() is NOT called
here automatically: it burns real Gemini quota, so it only runs when you pass
--with-gemini explicitly.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402


def demo():
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("--") \
        else Path(__file__).parent.parent / "testrepos" / "flask"
    with_gemini = "--with-gemini" in sys.argv

    print(f"[1/3] build_graph({repo})")
    graph_path = core.build_graph(repo)
    data = json.loads(graph_path.read_text())
    node_count = len(data.get("nodes", data)) if isinstance(data, dict) else len(data)
    assert node_count > 0, "graph.json has no nodes"
    print(f"      ok: {graph_path}, {node_count} nodes")

    print("[2/3] query_graph('how does the app handle routing')")
    answer = core.query_graph(repo, "how does the app handle routing")
    assert answer.strip(), "query_graph returned empty"
    print(f"      ok: {len(answer)} chars back")

    if with_gemini:
        print("[3/3] analyze_risk (REAL Gemini API call, costs quota)")
        # pick any node name as the test symbol rather than guessing a project-specific one
        sample_symbol = str(data.get("nodes", data)[0].get("label", data.get("nodes", data)[0].get("id", "")))
        result = core.analyze_risk(repo, [sample_symbol])
        assert result.strip(), "analyze_risk returned empty"
        print(f"      ok: {result[:200]}...")
    else:
        print("[3/3] skipped (pass --with-gemini to actually call Gemma 4)")

    print("ALL OK")


if __name__ == "__main__":
    demo()
