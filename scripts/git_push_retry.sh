#!/usr/bin/env bash
# git_push_retry.sh — commit + push with retry-on-conflict
#
# When multiple GitHub Actions workflows run concurrently and all try to push
# to the same branch, git pushes fail with "non-fast-forward" errors. This
# script retries with rebase up to 5 times with a 10s backoff.
#
# Usage: git_push_retry.sh "<commit message>" [file1 file2 ...]
#   - If no files specified, stages all changes (git add -A)
#   - If files specified, stages only those (git add file1 file2 ...)

set -e

COMMIT_MSG="$1"
shift
FILES_TO_ADD="$@"

MAX_ATTEMPTS=5
BACKOFF_SECONDS=10

for attempt in $(seq 1 $MAX_ATTEMPTS); do
  echo "[INFO] Push attempt $attempt/$MAX_ATTEMPTS"

  # Stage files
  if [ -z "$FILES_TO_ADD" ]; then
    git add -A
  else
    git add $FILES_TO_ADD 2>/dev/null || true
  fi

  # Check if there's anything to commit
  if git diff --cached --quiet; then
    echo "[INFO] No changes to commit. Done."
    exit 0
  fi

  # Commit
  git commit -m "$COMMIT_MSG" || {
    echo "[WARN] git commit failed (maybe nothing to commit). Retrying pull..."
    git pull --rebase || true
    continue
  }

  # Push
  if git push; then
    echo "[INFO] Push succeeded on attempt $attempt."
    exit 0
  fi

  echo "[WARN] Push failed on attempt $attempt. Pulling with rebase and retrying in ${BACKOFF_SECONDS}s..."
  git pull --rebase || {
    echo "[WARN] git pull --rebase failed. Will retry."
  }
  sleep $BACKOFF_SECONDS
done

echo "[ERROR] Failed to push after $MAX_ATTEMPTS attempts."
exit 1
