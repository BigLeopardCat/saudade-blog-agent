#!/usr/bin/env bash
# Advisory only: compare changed widget/model trees with the host pin; never block CI.
set -uo pipefail

PARENT="BigLeopardCat/Saudade-Blog"
LOCK_PATH="frontend/widget.lock.json"
WIDGET_PATHS=(frontend/public/live2d-widgets frontend/public/live2d_model)

note() { echo "::notice::$1"; }
warn() { echo "::warning::$1"; }

EVENT_NAME="${EVENT_NAME:-push}"
BEFORE="${BEFORE:-}"
SHA="${SHA:-HEAD}"

if [[ "$EVENT_NAME" != push && "$EVENT_NAME" != pull_request ]]; then
  note "非 push/PR 事件，无需比对组件 pin。"
  exit 0
fi
if [[ -z "$BEFORE" || "$BEFORE" == 0000000000000000000000000000000000000000 ]] || \
    ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null || \
    ! git cat-file -e "${SHA}^{commit}" 2>/dev/null; then
  note "提交区间不可用，未评估组件 pin 漂移。"
  exit 0
fi

CHANGED=()
for widget_path in "${WIDGET_PATHS[@]}"; do
  if [[ -n "$(git diff --name-only "$BEFORE" "$SHA" -- "$widget_path")" ]]; then
    CHANGED+=("$widget_path")
  fi
done
if [[ ${#CHANGED[@]} == 0 ]]; then
  note "组件与模型资源子树未变，无需比对 pin。"
  exit 0
fi

PIN_JSON="$(curl -fsSL --connect-timeout 5 --max-time 10 \
  "https://raw.githubusercontent.com/${PARENT}/HEAD/${LOCK_PATH}" 2>/dev/null)" || PIN_JSON=""
if [[ -z "$PIN_JSON" ]]; then
  note "读取宿主 ${LOCK_PATH} 失败，未评估 pin 漂移。"
  exit 0
fi

for widget_path in "${CHANGED[@]}"; do
  local_tree="$(git rev-parse "$SHA:$widget_path" 2>/dev/null || true)"
  pin_tree="$(printf '%s' "$PIN_JSON" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin)
except json.JSONDecodeError:
    value = {}
trees = value.get("trees", {}) if isinstance(value, dict) else {}
print(trees.get(sys.argv[1], "") if isinstance(trees, dict) else "")
' "$widget_path" 2>/dev/null)" || pin_tree=""
  if [[ -z "$local_tree" || -z "$pin_tree" ]]; then
    note "${widget_path} 的本地或宿主 tree 不可用，未评估。"
  elif [[ "$local_tree" == "$pin_tree" ]]; then
    note "${widget_path} 已与宿主 pin 同步。"
  else
    warn "${widget_path} 漂移：本地 ${local_tree}，宿主 ${pin_tree}；采用此变更时同步 ${LOCK_PATH} 并运行 npm run fetch:widget -- --check。"
  fi
done
exit 0
