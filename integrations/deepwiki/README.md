# deepwiki-integration

The same pattern as `pr-agent-integration/`, applied to a second existing
open-source tool: [AsyncFuncAI/deepwiki-open](https://github.com/AsyncFuncAI/deepwiki-open)
(MIT), a repo-to-wiki generator that normally answers questions with
embedding-RAG retrieval.

`research.py.diff` is the real commit against deepwiki-open's
`api/services/research.py`, applied and tested in a local clone of that
project. It gates `research_chat()`'s context source behind an environment
variable:

- `ONEGRAPH_BACKEND` unset: deepwiki-open's original embedding-RAG path runs
  untouched. Zero behavior change for anyone not opting in.
- `ONEGRAPH_BACKEND=graphify`: swaps `rag.acall(query)` for
  `onegraph_core.query_graph(repo_path, query)`, a real BFS traversal of the
  same dependency graph the showcase viewers use, building it on first use if
  it does not already exist for that repo.

`research.py` here is the full file after the patch, for reference.
`test_onegraph_gate.py` is a standalone smoke test (no FastAPI app boot
required) that checks both branches: gate off leaves the import unset, gate
on imports `core` and returns real `NODE` citations from
`query_graph()` against a real cloned repo.

## Why this lives here, not in a fork

deepwiki-open's own repository has no personal fork with push access in this
project; the local clone's `origin` remote is the upstream project itself.
Rather than leave a working, tested integration stranded in a disposable
local clone, it is vendored here the same way the PR-Agent integration is:
as a diff plus the patched file, reviewable and reproducible without needing
write access to deepwiki-open.

## Running the smoke test

```sh
cd public-repo
python3 deepwiki-integration/test_onegraph_gate.py
```

Requires `core/` as a sibling directory (already true in this repo)
and a built graph at `testrepos/flask/graphify-out/graph.json` for the
gate-on assertion. `test_core.py`-style, no pytest, no fixtures.
