#!/usr/bin/env bash
#
# Deploy the current `main` tree to the Hugging Face Space as a clean,
# single-commit snapshot.
#
# Why a snapshot instead of `git push space main`?
#   GitHub history contains binary media (generated-images/, generated-videos/)
#   that HF's pre-receive hook rejects. We push a fresh orphan commit that
#   contains only the current tracked files (no binary blobs anywhere in its
#   history), so HF accepts it. Local files and GitHub history are untouched.
#
# Usage:
#   ./scripts/deploy-hf.sh                # deploy current main
#
# Prerequisites (one-time):
#   - `hf auth login`  (stores a write token in your git credential helper)
#   - the `space` remote, added automatically below if missing.

set -euo pipefail

SPACE_URL="https://huggingface.co/spaces/Opencontent/video-gen"
SOURCE_BRANCH="main"
DEPLOY_BRANCH="hf-deploy"

# Run from the repo root regardless of where the script is invoked.
cd "$(git rev-parse --show-toplevel)"

# Refuse to run with uncommitted changes — the snapshot is built from the
# committed tree, so dirty state would be silently excluded or included.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: working tree has uncommitted changes. Commit or stash first." >&2
  exit 1
fi

# Ensure the HF remote exists.
if ! git remote get-url space >/dev/null 2>&1; then
  echo "Adding 'space' remote -> ${SPACE_URL}"
  git remote add space "${SPACE_URL}"
fi

ORIGINAL_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

# Always return to the original branch, even on failure.
cleanup() {
  git checkout "${ORIGINAL_BRANCH}" >/dev/null 2>&1 || true
  git branch -D "${DEPLOY_BRANCH}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Building clean snapshot of '${SOURCE_BRANCH}'..."
git checkout "${SOURCE_BRANCH}"
git branch -D "${DEPLOY_BRANCH}" >/dev/null 2>&1 || true
git checkout --orphan "${DEPLOY_BRANCH}"
git add -A
git commit -m "Deploy snapshot for Hugging Face Space" --quiet

echo "Pushing snapshot -> ${SPACE_URL} (branch: main)"
git push space "${DEPLOY_BRANCH}:main" --force

echo "Done. Watch the build at ${SPACE_URL}"
