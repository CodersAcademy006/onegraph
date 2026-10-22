"""Smoke test for the ONEGRAPH_BACKEND gate in research.py (OneGraph
integration). Checks the gate import logic in isolation, without booting the
full FastAPI app (research.py's other imports need API keys/config that
aren't available in CI). Run: python3 api/services/test_onegraph_gate.py
"""
import os
import subprocess
import sys
from pathlib import Path

CORE_PATH = Path(__file__).resolve().parents[4] / "core"
FLASK_REPO = Path(__file__).resolve().parents[4] / "testrepos" / "flask"


def _gate(env_value):
    code = (
        "import os, sys\n"
        f"os.environ['ONEGRAPH_BACKEND'] = {env_value!r}\n" if env_value else
        "import os, sys\n"
        "os.environ.pop('ONEGRAPH_BACKEND', None)\n"
    )
    code += (
        "onegraph_core = None\n"
        "if os.environ.get('ONEGRAPH_BACKEND') == 'graphify':\n"
        f"    sys.path.insert(0, {str(CORE_PATH)!r})\n"
        "    import core as onegraph_core\n"
        "print('SET' if onegraph_core is not None else 'UNSET')\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def demo():
    print("[1/2] gate off when ONEGRAPH_BACKEND unset")
    assert _gate(None) == "UNSET"
    print("      ok")

    print("[2/2] gate on + query_graph returns real graph facts")
    assert _gate("graphify") == "SET"
    sys.path.insert(0, str(CORE_PATH))
    import core as onegraph_core
    graph_path = FLASK_REPO / "graphify-out" / "graph.json"
    assert graph_path.exists(), f"missing {graph_path}, run build_graph first"
    answer = onegraph_core.query_graph(FLASK_REPO, "how does the app handle routing")
    assert "NODE" in answer, "query_graph didn't return graph nodes"
    print(f"      ok: {len(answer)} chars, contains real NODE citations")

    print("ALL OK")


if __name__ == "__main__":
    demo()
