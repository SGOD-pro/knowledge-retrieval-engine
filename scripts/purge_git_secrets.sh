#!/usr/bin/env bash
# ==============================================================================
# MANUAL PROCEDURE: Git History Secret Scrub & Credential Rotation
# ==============================================================================
# IMPORTANT NOTICE:
# Do NOT run this script automatically.
# Rewriting git history modifies commit SHAs and affects all clones/collaborators.
#
# PREREQUISITE:
# 1. Externally rotate all previously committed AWS IAM access keys via AWS Console
#    or AWS CLI:
#      aws iam create-access-key --user-name <USER>
#      aws iam delete-access-key --user-name <USER> --access-key-id <OLD_KEY>
# 2. Externally rotate OpenRouter API keys and Qdrant cluster tokens.
# 3. Ensure all local changes are committed and pushed to a backup branch.
# ==============================================================================

set -euo pipefail

echo "========================================================================"
echo "GIT REPOSITORY HISTORY PURGE (MANUAL SAFETY CHECK)"
echo "========================================================================"
echo "This script will scrub .env from the entire Git commit history using git-filter-repo."
echo "Have you already externally rotated all AWS, OpenRouter, and Qdrant credentials? (y/N)"
read -r CONFIRM

if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo "Aborted. Please rotate your credentials externally first."
    exit 1
fi

if ! command -v git-filter-repo &> /dev/null; then
    echo "git-filter-repo is required. Install via: pip install git-filter-repo"
    exit 1
fi

echo "Purging .env from Git history..."
git-filter-repo --path .env --invert-paths --force

echo "History scrub complete."
echo "To force push the scrubbed history to remote:"
echo "  git push origin --force --all"
echo "  git push origin --force --tags"
