"""Component 0: Benchmark Integrity Guard.

Enforces evaluation provenance safeguards:
- Blocks official benchmark execution when the git working tree is dirty (`git status --porcelain`).
- Computes SHA256 hashes of the test suite and corpus manifest.
- Produces immutable benchmark provenance records.
"""

import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DirtyWorktreeError(Exception):
    """Raised when an official benchmark is executed with uncommitted changes."""
    pass


def check_git_status(repo_path: Path | None = None) -> tuple[bool, str, str]:
    """Check git repository status.

    Returns:
        (is_dirty, commit_sha, status_output)
    """
    cwd = repo_path or Path(__file__).resolve().parents[4]
    try:
        status_proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        status_output = status_proc.stdout.strip()
        is_dirty = bool(status_output)

        rev_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        commit_sha = rev_proc.stdout.strip()
        return is_dirty, commit_sha, status_output
    except Exception as exc:
        logger.error("benchmark_guard: failed to execute git commands: %s", exc)
        return True, "unknown", f"error: {exc}"


def enforce_clean_worktree(repo_path: Path | None = None, allow_dirty: bool = False) -> str:
    """Ensure working tree is completely clean before benchmark run.

    Returns current commit SHA if clean.
    Raises DirtyWorktreeError if uncommitted changes exist and allow_dirty is False.
    """
    is_dirty, commit_sha, status_output = check_git_status(repo_path)
    if is_dirty and not allow_dirty:
        raise DirtyWorktreeError(
            f"Official benchmark aborted: working tree contains uncommitted changes.\n"
            f"Git status output:\n{status_output}\n\n"
            f"Please commit or stash your changes before running an official benchmark."
        )
    return commit_sha


def compute_file_sha256(file_path: Path) -> str:
    """Compute SHA256 checksum of a file."""
    if not file_path.exists():
        return ""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_corpus_hash(workspace_id: str) -> str:
    """Compute a deterministic hash over the documents and manifests of a workspace."""
    from db.database import CloudRepository

    repo = CloudRepository()
    try:
        docs = repo.get_all_documents(workspace_id=workspace_id)
        if not docs:
            # Fallback to chunk count
            chunks = repo.get_all_chunks(workspace_id=workspace_id)
            seed = f"{workspace_id}:{len(chunks)}"
            return hashlib.sha256(seed.encode("utf-8")).hexdigest()

        # Sort documents deterministically by ID
        doc_summaries = []
        for d in sorted(docs, key=lambda x: getattr(x, "id", "")):
            d_id = getattr(d, "id", "")
            fname = getattr(d, "filename", "")
            fmt = getattr(d, "source_format", "")
            c_count = len(getattr(d, "chunks", ()))
            doc_summaries.append(f"{d_id}:{fname}:{fmt}:{c_count}")

        combined = "|".join(doc_summaries)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()
    except Exception as exc:
        logger.warning("compute_corpus_hash: failed to hash corpus for workspace=%s: %s", workspace_id, exc)
        return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()


def create_benchmark_provenance(
    workspace_id: str,
    test_file_path: Path,
    allow_dirty: bool = False,
    repo_path: Path | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record comprehensive, tamper-evident benchmark provenance."""
    is_dirty, commit_sha, status_output = check_git_status(repo_path)
    if is_dirty and not allow_dirty:
        raise DirtyWorktreeError(
            f"Official benchmark aborted: working tree contains uncommitted changes:\n{status_output}"
        )

    suite_sha = compute_file_sha256(test_file_path)
    corpus_sha = compute_corpus_hash(workspace_id)

    provenance = {
        "commit_sha": commit_sha,
        "dirty_worktree": is_dirty,
        "test_suite_path": str(test_file_path),
        "test_suite_sha256": suite_sha,
        "corpus_workspace_id": workspace_id,
        "corpus_sha256": corpus_sha,
        "timestamp_utc": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "extra_metadata": extra_metadata or {},
    }
    return provenance
