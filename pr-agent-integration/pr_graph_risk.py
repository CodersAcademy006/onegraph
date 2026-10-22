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

            # Blast radius computed once per file, reused for the per-file table
            # below AND the inline markers — same numbers in both places, one
            # `graphify affected` call per file, not two.
            per_file = self._blast_radius_per_file(local_path, diff_files)
            self._publish_inline_blast_radius(diff_files, per_file)

            risk_text = onegraph_core.analyze_risk(local_path, changed_symbols)
            level = onegraph_core.risk_level([c for c, _ in per_file.values()])
            file_table = self._file_table(per_file)
            mermaid = onegraph_core.graph_mermaid(local_path, changed_symbols)
            graph_section = (
                f"\n\n<details><summary>Changed-file subgraph (preview)</summary>\n\n"
                f"```mermaid\n{mermaid}\n```\n\n</details>"
            ) if mermaid else ""

            link_section = ""
            try:
                html_path = onegraph_core.export_graph_html(local_path)
                pr_number = self.pr_url.rstrip("/").split("/")[-1]
                slug = f"{self.git_provider.repo.replace('/', '-')}-pr{pr_number}"
                site_dir = Path(__file__).resolve().parents[4] / "graphs-site"
                graph_url = onegraph_core.deploy_graph_html(html_path, slug, site_dir)
                link_section = (
                    f"\n\n### [Open the full interactive graph →]({graph_url})\n"
                    f"Graphify's own viewer — force layout, community clusters, click any node.\n"
                )
            except Exception:
                get_logger().exception("/graph_risk: graph.html deploy failed, posting without the live link")

            self.git_provider.publish_comment(
                f"**/graph_risk** (Graphify + Gemma 4, grounded in real call-graph edges)\n\n"
                f"**Risk: {level}** — full repo graph: **{node_count:,} nodes / {edge_count:,} edges** "
                f"(tree-sitter, $0 LLM cost to build)\n\n"
                f"### Changed files\n{file_table}\n\n"
                f"### Analysis\n{risk_text}{link_section}{graph_section}"
            )
        except Exception:
            get_logger().exception("/graph_risk failed")
            self.git_provider.publish_comment(
                "**/graph_risk** failed to run — see server logs."
            )
        finally:
            if local_path is not None:
                shutil.rmtree(local_path, ignore_errors=True)

    def _blast_radius_per_file(self, local_path: Path, diff_files) -> dict:
        """One `graphify affected` call per changed file -> {filename: (count, examples)}."""
        result = {}
        for f in diff_files:
            affected_text = onegraph_core.affected(local_path, f.filename)
            result[f.filename] = onegraph_core.blast_radius_summary(affected_text)
        return result

    def _file_table(self, per_file: dict) -> str:
        rows = ["| File | Downstream dependents | Affects |", "|---|---|---|"]
        for filename, (count, examples) in per_file.items():
            affects = ", ".join(f"`{e}`" for e in examples) or "—"
            if count > len(examples):
                affects += f" (+{count - len(examples)} more)"
            rows.append(f"| `{filename}` | {count} | {affects} |")
        return "\n".join(rows)

    def _publish_inline_blast_radius(self, diff_files, per_file: dict) -> None:
        """Greptile/CodeRabbit-style inline markers, one per changed file,
        anchored to its first changed line. Reuses the already-computed blast
        radius — no extra graphify or LLM call. Best-effort per file so one
        bad anchor never blocks the others or the summary comment."""
        if not hasattr(self.git_provider, "publish_inline_comment"):
            return
        for f in diff_files:
            count, examples = per_file.get(f.filename, (0, []))
            if count == 0:
                continue
            try:
                anchor = next(
                    (line for line in f.patch.splitlines() if line.startswith("+") and not line.startswith("+++")),
                    None,
                )
                if anchor is None:
                    continue
                more = f" (+{count - len(examples)} more)" if count > len(examples) else ""
                body = (
                    f"**Graphify blast radius**: {count} downstream dependent(s) of this file.\n\n"
                    f"Affects: {', '.join(f'`{e}`' for e in examples)}{more}"
                )
                self.git_provider.publish_inline_comment(body, f.filename, anchor)
            except Exception:
                get_logger().exception(f"/graph_risk: inline comment failed for {f.filename}")
