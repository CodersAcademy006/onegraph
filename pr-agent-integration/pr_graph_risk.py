"""/graph_risk: a risk review grounded in Graphify's real dependency graph,
not an LLM guess.

Deliberately bypasses PR-Agent's AI-handler/TokenHandler/Jinja prompt
machinery that every other tool in this directory uses — this tool's
reasoning comes from onegraph_core.analyze_risk() (Gemma 4 via Gemini API,
fed real graph edges), not the repo's configured LiteLLM model. Mirrors the
command2class pattern from pr_reviewer.py (pr_url -> git_provider -> run()
-> publish_comment), nothing else from that file is reused.
"""
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.log import get_logger

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "onegraph-core"))
import core as onegraph_core  # noqa: E402


class PRGraphRisk:
    def __init__(self, pr_url: str, ai_handler=None, args: list = None):
        self.pr_url = pr_url
        self.git_provider = get_git_provider_with_context(pr_url)

    async def run(self) -> None:
        local_path = None
        try:
            diff_files = self.git_provider.get_diff_files() or []
            changed_symbols = [f.filename for f in diff_files]
            if not changed_symbols:
                self.git_provider.publish_comment("**/graph_risk**: no changed files found on this PR.")
                return

            branch = self.git_provider.get_pr_branch()
            clone_url = f"https://github.com/{self.git_provider.repo}.git"
            local_path = Path(tempfile.gettempdir()) / f"onegraph-pr-{uuid.uuid4().hex[:8]}"
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", str(branch), clone_url, str(local_path)],
                check=True, capture_output=True, text=True,
            )

            onegraph_core.build_graph(local_path)
            node_count, edge_count = onegraph_core.graph_stats(local_path)
            risk_text = onegraph_core.analyze_risk(local_path, changed_symbols)
            mermaid = onegraph_core.graph_mermaid(local_path, changed_symbols)
            graph_section = f"\n\n<details><summary>Changed-file subgraph (preview)</summary>\n\n```mermaid\n{mermaid}\n```\n\n</details>" if mermaid else ""

            link_section = ""
            try:
                html_path = onegraph_core.export_graph_html(local_path)
                pr_number = self.pr_url.rstrip("/").split("/")[-1]
                slug = f"{self.git_provider.repo.replace('/', '-')}-pr{pr_number}"
                site_dir = Path(__file__).resolve().parents[4] / "graphs-site"
                graph_url = onegraph_core.deploy_graph_html(html_path, slug, site_dir)
                link_section = f"\n\n### [Open the full interactive graph →]({graph_url})\nGraphify's own viewer — force layout, community clusters, click any node.\n"
            except Exception:
                get_logger().exception("/graph_risk: graph.html deploy failed, posting without the live link")

            self.git_provider.publish_comment(
                f"**/graph_risk** (Graphify + Gemma 4, grounded in real call-graph edges)\n\n"
                f"Full repo graph: **{node_count:,} nodes / {edge_count:,} edges** "
                f"(tree-sitter, $0 LLM cost to build)\n\n{risk_text}{link_section}{graph_section}"
            )
        except Exception:
            get_logger().exception("/graph_risk failed")
            self.git_provider.publish_comment(
                "**/graph_risk** failed to run — see server logs."
            )
        finally:
            if local_path is not None:
                shutil.rmtree(local_path, ignore_errors=True)
