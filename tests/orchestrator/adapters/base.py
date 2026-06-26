from abc import ABC, abstractmethod


class BaseWorkerAdapter(ABC):
    """
    Abstract interface that describes how to run a legacy tool inside a container.

    The adapter does NOT execute anything directly. It only provides metadata
    that the RuntimeEngine uses to configure and spawn containers.

    Subclass this for each language or specific tool.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable adapter name (e.g., 'python', 'node', 'gitstats')."""

    @abstractmethod
    def detect(self, source_path: str) -> bool:
        """
        Return True if this adapter can handle the codebase at source_path.
        Implementations should check for language-specific marker files
        (e.g., requirements.txt, package.json).
        """

    @abstractmethod
    def get_base_image(self) -> str:
        """Return the Docker base image to use (e.g., 'python:3.10-slim')."""

    @abstractmethod
    def get_install_command(self) -> str:
        """
        Return a shell command string that installs dependencies.
        This command will run inside the container with /cache mounted.
        Should prefer offline/cached installation for speed.
        """

    @abstractmethod
    def get_run_command(self, payload: dict) -> str:
        """
        Return a shell command string that executes the legacy tool.
        The payload dict contains user-provided arguments.
        The legacy source code will be available at /app inside the container.
        Output should be written to /output/result.json.
        """

    def get_entrypoint_script(self, payload: dict) -> str:
        """
        Generate the full entrypoint shell script that the container will execute.
        Combines install + run into a single script with error handling.
        """
        install_cmd = self.get_install_command()
        run_cmd = self.get_run_command(payload)

        return f"""#!/bin/sh
set -e

echo '{{"phase": "install", "status": "starting"}}' > /output/status.json

# Install dependencies (cached)
cd /app
{install_cmd}

echo '{{"phase": "run", "status": "starting"}}' > /output/status.json

# Execute the legacy tool
{run_cmd}

echo '{{"phase": "complete", "status": "success"}}' > /output/status.json
"""
