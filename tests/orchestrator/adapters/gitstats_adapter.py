import os

from tests.orchestrator.adapters.python_adapter import PythonAdapter


class GitstatsAdapter(PythonAdapter):
    """
    Proof-of-concept adapter: wraps gitstats3 as the first on-demand worker.

    Extends PythonAdapter (same base image, same pip install) but overrides
    the run command to call gitstats3's own CLI directly.
    """

    @property
    def name(self) -> str:
        return "gitstats"

    def detect(self, source_path: str) -> bool:
        """
        Detect gitstats3 specifically: must have pyproject.toml with
        gitstats3 in the project name, or the gitstats.py entry script.
        """
        # Check for gitstats.py (classic entry point)
        if os.path.exists(os.path.join(source_path, "gitstats.py")):
            return True

        # Check pyproject.toml for project name
        toml_path = os.path.join(source_path, "pyproject.toml")
        if os.path.exists(toml_path):
            try:
                with open(toml_path, "r") as f:
                    content = f.read()
                    if "gitstats3" in content:
                        return True
            except Exception:
                pass

        return False

    def get_install_command(self) -> str:
        # Gitstats3 needs editable install for its package structure
        return (
            "pip install --cache-dir=/cache/pip --quiet -r requirements.txt && "
            "pip install --cache-dir=/cache/pip --quiet -e ."
        )

    def get_run_command(self, payload: dict) -> str:
        """
        Run gitstats3 analysis.

        Expected payload keys:
            repo_path: str — path to the target repo (inside the container)
            output_path: str — where to write results (default: /output)
            mode: str — "report" | "metrics" | "index" (default: "report")
        """
        repo_path = payload.get("repo_path", "/app")
        output_path = payload.get("output_path", "/output")
        mode = payload.get("mode", "report")

        if mode == "report":
            return (
                f"python gitstats.py {repo_path} {output_path} && "
                f"echo '{{\"status\": \"success\", \"mode\": \"report\", "
                f"\"output\": \"{output_path}\"}}' > /output/result.json"
            )
        elif mode == "metrics":
            what = payload.get("what", "all")
            return (
                f"python -c \""
                f"import asyncio, json; "
                f"from src.mcp_server.core import do_metrics; "
                f"r = asyncio.run(do_metrics('{repo_path}', ['{what}'])); "
                f"print(json.dumps(r, indent=2))\" > /output/result.json 2>/output/stderr.log"
            )
        elif mode == "index":
            return (
                f"python -c \""
                f"import asyncio, json; "
                f"from src.mcp_server.core import do_update_index; "
                f"r = asyncio.run(do_update_index('{repo_path}')); "
                f"print(json.dumps(r, indent=2))\" > /output/result.json 2>/output/stderr.log"
            )
        else:
            return f"echo '{{\"error\": \"Unknown mode: {mode}\"}}' > /output/result.json"
