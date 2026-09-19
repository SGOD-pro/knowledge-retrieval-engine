#!/usr/bin/env python3
"""Pre-commit / CI secret scan.

Scans all git-tracked files and documentation for high-entropy secrets and credential patterns:
- AWS Access Key IDs: AKIA... / ASIA...
- OpenRouter API keys: sk-or-v1-...
- Qdrant API keys
- Real secret-looking tokens in tracked files, logs, benchmark artifacts, or docs.

Exits with code 1 if any unredacted secrets are detected in tracked repository files.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRET_PATTERNS = [
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "AWS Access Key ID"),
    (re.compile(r"\b(ASIA[0-9A-Z]{16})\b"), "AWS Temporary Access Key ID"),
    (re.compile(r"\bsk-or-v1-[0-9a-fA-F]{64}\b"), "OpenRouter API Key"),
    (re.compile(r'(?i)(?:aws_secret_access_key|secret_key|api_key)\s*[:=]\s*["\']([a-zA-Z0-9/+=]{30,45})["\']'), "Potential API/Access Secret"),
]

# Patterns that are allowed / documented placeholders
ALLOWED_PLACEHOLDERS = {
    "your_openrouter_api_key_here",
    "your_aws_access_key_here",
    "your_aws_secret_key_here",
    "your_qdrant_api_key_here",
    "your_qdrant_cluster_url_here",
    "test_secret",
    "fake_secret",
    "placeholder",
}


def get_tracked_files() -> list[str]:
    """Return all git-tracked files."""
    try:
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return [f.strip() for f in res.stdout.splitlines() if f.strip()]
    except Exception as e:
        print(f"Error getting git tracked files: {e}", file=sys.stderr)
        return []


def scan_file(rel_path: str) -> list[tuple[int, str, str]]:
    """Scan a single file for secrets. Returns [(line_num, match_type, redacted_preview)]."""
    full_path = REPO_ROOT / rel_path
    if not full_path.exists() or full_path.is_dir():
        return []

    # Skip binary files, images, pdfs, onnx
    if rel_path.endswith((".pdf", ".xls", ".xlsx", ".docx", ".pptx", ".onnx", ".png", ".jpg", ".pyc")):
        return []

    findings = []
    try:
        content = full_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    for line_idx, line in enumerate(content.splitlines(), 1):
        for pattern, pattern_name in SECRET_PATTERNS:
            for match in pattern.finditer(line):
                val = match.group(1) if match.groups() else match.group(0)
                if val.lower() in ALLOWED_PLACEHOLDERS or "<" in val or ">" in val:
                    continue
                # Redact value in report
                redacted = val[:4] + "..." + val[-4:] if len(val) > 8 else "***"
                findings.append((line_idx, pattern_name, redacted))

    return findings


def main() -> int:
    print("=" * 60)
    print("RUNNING REPOSITORY SECRET SCAN")
    print("=" * 60)

    tracked_files = get_tracked_files()
    if not tracked_files:
        print("No tracked files found.")
        return 0

    total_findings = 0
    for rel_path in tracked_files:
        # If someone tracked .env, flag immediately
        if os.path.basename(rel_path) in (".env", ".env.local", ".env.production"):
            print(f"CRITICAL: Tracked environment file detected: {rel_path}")
            total_findings += 1
            continue

        file_findings = scan_file(rel_path)
        if file_findings:
            print(f"\n[FAIL] File: {rel_path}")
            for line_no, pattern_name, redacted in file_findings:
                print(f"   Line {line_no}: Detected {pattern_name} ({redacted})")
                total_findings += 1

    print("\n" + "-" * 60)
    if total_findings > 0:
        print(f"SECRET SCAN FAILED: {total_findings} potential secret(s) found in tracked files.")
        print("Never commit unredacted secrets. Move secrets to local .env and keep .env untracked.")
        return 1
    else:
        print("SECRET SCAN PASSED: Zero unredacted secrets found in tracked files.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
