import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.evaluation.benchmark_guard import (
    DirtyWorktreeError,
    check_git_status,
    compute_file_sha256,
    create_benchmark_provenance,
    enforce_clean_worktree,
)


def test_compute_file_sha256(tmp_path: Path):
    test_file = tmp_path / "sample.txt"
    test_file.write_text("benchmark test contents", encoding="utf-8")
    sha = compute_file_sha256(test_file)
    assert len(sha) == 64
    assert sha == compute_file_sha256(test_file)

    # Empty for missing file
    assert compute_file_sha256(tmp_path / "non_existent.txt") == ""


def test_enforce_clean_worktree_raises_on_dirty():
    with patch("subprocess.run") as mock_run:
        # First call: git status --porcelain
        mock_status = MagicMock()
        mock_status.stdout = " M backend/src/services/planner.py\n"

        # Second call: git rev-parse HEAD
        mock_rev = MagicMock()
        mock_rev.stdout = "abcdef1234567890\n"

        mock_run.side_effect = [mock_status, mock_rev]

        with pytest.raises(DirtyWorktreeError) as exc_info:
            enforce_clean_worktree()
        assert "working tree contains uncommitted changes" in str(exc_info.value)


def test_enforce_clean_worktree_allows_clean():
    with patch("subprocess.run") as mock_run:
        mock_status = MagicMock()
        mock_status.stdout = ""

        mock_rev = MagicMock()
        mock_rev.stdout = "1234567890abcdef\n"

        mock_run.side_effect = [mock_status, mock_rev]

        sha = enforce_clean_worktree()
        assert sha == "1234567890abcdef"


def test_create_benchmark_provenance(tmp_path: Path):
    test_suite = tmp_path / "test.json"
    test_suite.write_text('{"queries": []}', encoding="utf-8")

    with patch("services.evaluation.benchmark_guard.check_git_status") as mock_status, \
         patch("services.evaluation.benchmark_guard.compute_corpus_hash") as mock_corpus:
        mock_status.return_value = (False, "commit_hash_123", "")
        mock_corpus.return_value = "corpus_hash_abc"

        prov = create_benchmark_provenance(
            workspace_id="test_ws",
            test_file_path=test_suite,
            allow_dirty=False,
        )

        assert prov["commit_sha"] == "commit_hash_123"
        assert prov["dirty_worktree"] is False
        assert prov["corpus_workspace_id"] == "test_ws"
        assert prov["corpus_sha256"] == "corpus_hash_abc"
        assert len(prov["test_suite_sha256"]) == 64
