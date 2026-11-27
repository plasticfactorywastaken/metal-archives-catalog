"""Wait for this run's Pages artifact to appear before requesting deployment.

Only GitHub.com's current repository/run metadata endpoint is requested. Artifact
download URLs, pagination links and other URLs returned in metadata are ignored.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time


ARTIFACT_NAME = "github-pages"


def validate_context(repository: str, run_id: str):
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected the current GitHub owner/repository name")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise ValueError("Invalid GitHub repository name")
    if not isinstance(run_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise ValueError("Expected the current numeric GitHub Actions run ID")


def fetch_artifacts(repository: str, run_id: str, timeout_seconds: float):
    validate_context(repository, run_id)
    endpoint = f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
    environment = dict(os.environ)
    environment["GH_HOST"] = "github.com"
    try:
        result = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint],
            capture_output=True, text=True, timeout=timeout_seconds, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("GitHub artifact metadata request unavailable") from None
    if result.returncode:
        # Never echo gh stderr, credentials, or raw API response bodies.
        raise RuntimeError("GitHub artifact metadata request failed")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError):
        raise RuntimeError("GitHub artifact metadata response was not JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), list):
        raise RuntimeError("GitHub artifact metadata has an unexpected shape")
    return payload["artifacts"]


def wait_for_pages_artifact(repository: str, run_id: str, *, timeout_seconds=120,
                            fetcher=fetch_artifacts, clock=time.monotonic,
                            sleeper=time.sleep, emit=print):
    validate_context(repository, run_id)
    if not 10 <= timeout_seconds <= 120:
        raise ValueError("Artifact visibility timeout must be between 10 and 120 seconds")
    started = clock()
    deadline = started + timeout_seconds
    while clock() < deadline:
        remaining = deadline - clock()
        try:
            artifacts = fetcher(repository, run_id, min(15, remaining))
            candidates = [artifact for artifact in artifacts if isinstance(artifact, dict)
                          and artifact.get("name") == ARTIFACT_NAME
                          and artifact.get("expired") is False
                          and isinstance(artifact.get("id"), int) and artifact["id"] > 0
                          and isinstance(artifact.get("size_in_bytes"), int) and artifact["size_in_bytes"] > 0
                          and str((artifact.get("workflow_run") or {}).get("id", run_id)) == run_id]
            available = True
        except RuntimeError:
            candidates, available = [], False
        if len(candidates) > 1:
            raise RuntimeError("Multiple active github-pages artifacts exist for this run; refusing ambiguous deployment")
        elapsed = clock() - started
        # This script starts only after upload has succeeded, so this guarantees
        # at least ten seconds of backend propagation after upload completion.
        if candidates and 10 <= elapsed <= timeout_seconds:
            artifact = candidates[0]
            emit(json.dumps({"pages_artifact":"visible", "artifact_id":artifact["id"],
                             "size_in_bytes":artifact["size_in_bytes"], "waited_seconds":round(elapsed, 1)}))
            return artifact["id"]
        emit(json.dumps({"pages_artifact":"waiting", "metadata_available":available,
                         "matching_artifacts":len(candidates), "elapsed_seconds":round(elapsed, 1)}))
        remaining = deadline - clock()
        if remaining > 0:
            sleeper(min(10, remaining))
    raise RuntimeError("github-pages artifact did not become ready within 120 seconds; saved catalog state is unchanged")


def main():
    if os.environ.get("GITHUB_SERVER_URL", "https://github.com") != "https://github.com":
        raise ValueError("This publication workflow supports GitHub.com only")
    wait_for_pages_artifact(os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
