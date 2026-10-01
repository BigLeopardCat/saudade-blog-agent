#!/usr/bin/env bash
# ═ 看板娘 pin 漂移提示（20261002）═══════════════════════════════════════════
#
# **只警告，恒退 0。** 这不是门禁：该不该立刻 bump pin 由人决定（一次纯注释改动
# 完全可以攒着跟下一个真改动一起发），这里只负责把"你忘了"这件事说出来。
#
# 为什么值得一条守卫：本仓 `frontend/public/live2d-widgets/` 是**源码**，而线上跑的
# 是父仓 `frontend/widget.lock.json` 里钉住的那个 sha 稀疏检出来的**副本**。改了这里
# 却不回父仓 bump pin ⇒ 改动永远不上线，而**没有任何东西会变红**：本地绿、CI 绿、
# 线上照旧。父仓 `widget.lock.json` 的头注、README、CONTRIBUTING 三处都写了这条纪律，
# 但写在文档里的纪律**挡不住忘记**——所以补一条机械的。
#
# 判据用**树 sha**而不是提交号：pin 指着一个更早的提交、只要那棵树一字不差，就是同步的
# （比如后面又推了个只动 Python 的提交）。树 sha 一次抓三件事：pin 挪了、稀疏锥配错了、
# 这棵树被改写过。
#
# 读父仓那份 lock 走 GitHub API（父仓私有 ⇒ 用已有的只读 PAT）。**刻意不改** eval.yml
# 里那个稀疏锥：那个锥（`src/routes`）是为跨语言守卫服务的，为了读一个 1KB 的 json
# 把父仓整个 `frontend/`（9MB / 334 文件）拉下来不值当。
set -uo pipefail

PARENT="BigLeopardCat/Saudade-Blog"
LOCK_PATH="frontend/widget.lock.json"
WIDGET_PATH="frontend/public/live2d-widgets"

note() { echo "::notice::$1"; }
warn() { echo "::warning::$1"; }

EVENT_NAME="${EVENT_NAME:-push}"
BEFORE="${BEFORE:-}"
SHA="${SHA:-HEAD}"

# ① 只有 push 才有"这次改了哪些文件"这回事
if [ "$EVENT_NAME" != "push" ]; then
  note "事件是 $EVENT_NAME（不是 push），没有可比的提交区间，跳过 pin 漂移比对。"
  exit 0
fi

# 新分支/强推时 before 是全 0 或不存在的提交 —— 比不出区间就别硬比
if [ -z "$BEFORE" ] || [ "$BEFORE" = "0000000000000000000000000000000000000000" ]; then
  note "before 为空（新分支或强推），比不出提交区间，跳过 pin 漂移比对。"
  exit 0
fi
if ! git cat-file -e "${BEFORE}^{commit}" 2>/dev/null; then
  note "before（$BEFORE）不在本地历史里（浅克隆？），跳过 pin 漂移比对。"
  exit 0
fi

# ② 这次 push 动没动看板娘前端？没动就没什么可提醒的
if ! git diff --name-only "$BEFORE" "$SHA" -- "$WIDGET_PATH" | grep -q .; then
  note "本次 push 没动 $WIDGET_PATH，无需比对 pin。"
  exit 0
fi

LOCAL_TREE="$(git rev-parse "$SHA:$WIDGET_PATH" 2>/dev/null || true)"
if [ -z "$LOCAL_TREE" ]; then
  note "本提交里没有 $WIDGET_PATH 这棵树（被删了？），跳过 pin 漂移比对。"
  exit 0
fi

# ③ 读父仓 pin。读不到就**说一声**再跳过 —— 静默跳过正是这条守卫要防的那种失效
if [ -z "${PARENT_REPO_TOKEN:-}" ]; then
  note "没有 PARENT_REPO_TOKEN，读不到父仓 pin，这次跳过（不是失败）。配置见 docs/adr/adr-0004-cross-language-guard-in-ci.md"
  exit 0
fi
PIN_JSON="$(curl -fsSL \
  -H "Authorization: Bearer ${PARENT_REPO_TOKEN}" \
  -H "Accept: application/vnd.github.raw" \
  "https://api.github.com/repos/${PARENT}/contents/${LOCK_PATH}" 2>/dev/null)" || PIN_JSON=""
if [ -z "$PIN_JSON" ]; then
  note "读父仓 ${LOCK_PATH} 失败（凭据权限或网络），这次跳过（不是失败）。"
  exit 0
fi
PIN_TREE="$(printf '%s' "$PIN_JSON" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("")
else:
    print((d.get("trees") or {}).get("frontend/public/live2d-widgets", ""))
' 2>/dev/null)" || PIN_TREE=""

if [ -z "$PIN_TREE" ]; then
  note "读到父仓 ${LOCK_PATH} 但解析不出 ${WIDGET_PATH} 的 tree（文件格式变过？），跳过比对。"
  exit 0
fi

# ④ 比对
if [ "$PIN_TREE" = "$LOCAL_TREE" ]; then
  note "父仓 pin 已指向本提交的看板娘树（$LOCAL_TREE），无需 bump。"
  exit 0
fi

warn "看板娘前端这次改到了（tree $LOCAL_TREE），但父仓 ${LOCK_PATH} 仍指着 $PIN_TREE。回父仓把 sha 与两棵 tree 一起 bump（npm run fetch:widget 会校验），否则这次改动**永远不会上线**——本地绿、CI 绿、线上照旧。攒着下次一起发也可以，只要别忘了。"
exit 0
