#!/usr/bin/env bash
set -euo pipefail
cd -- "$(git rev-parse --show-toplevel)"
message_file="$(mktemp)"
trap 'rm -f "$message_file"' EXIT
if [[ "${EVENT_NAME:-push}" == pull_request ]]; then
  printf '%s\n' "${PR_TITLE:-}" > "$message_file"
  bash .githooks/commit-msg "$message_file" --title
  exit 0
fi
head="${HEAD_SHA:-HEAD}"
git cat-file -e "$head^{commit}"
commits="$head"
if [[ -n "${BASE_SHA:-}" ]] && git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null; then
  commits="$(git rev-list --reverse "$BASE_SHA..$head")"
fi
while IFS= read -r commit; do
  [[ -n "$commit" ]] || continue
  git log -1 --format=%B "$commit" > "$message_file"
  bash .githooks/commit-msg "$message_file"
done <<< "$commits"
