#!/usr/bin/env bash
# ═ 看板娘 pin 漂移提示（20261002）═══════════════════════════════════════════
#
# **只警告，恒退 0。** 这不是门禁：该不该立刻 bump pin 由人决定（一次纯注释改动
# 完全可以攒着跟下一个真改动一起发），这里只负责把"你忘了"这件事说出来。
#
# 20261019 起也在 PR 上跑（`EVENT_NAME=pull_request`，区间由 eval.yml 传 base.sha..merge）：
# 漂移最常见的时刻就是 PR 里，等合并后才说就晚了。
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
# The host repository is public. This advisory check needs no personal token.
set -uo pipefail

PARENT="BigLeopardCat/Saudade-Blog"
LOCK_PATH="frontend/widget.lock.json"
WIDGET_PATH="frontend/public/live2d-widgets"

note() { echo "::notice::$1"; }
warn() { echo "::warning::$1"; }

EVENT_NAME="${EVENT_NAME:-push}"
BEFORE="${BEFORE:-}"
SHA="${SHA:-HEAD}"

# ① 只有 push 与 PR 才有"这次改了哪些文件"这回事。
#    PR 用 `base.sha..merge` 这个区间（eval.yml 把 BEFORE/SHA 两个 env 按事件类型填好，
#    语义与 push 那一趟相同）—— 漂移提示最有用的时刻恰恰是 PR 里，别等合并后才说。
if [ "$EVENT_NAME" != "push" ] && [ "$EVENT_NAME" != "pull_request" ]; then
  note "事件是 $EVENT_NAME（既不是 push 也不是 pull_request），没有可比的提交区间，跳过 pin 漂移比对。"
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
PIN_JSON="$(curl -fsSL --connect-timeout 5 --max-time 10 \
  "https://raw.githubusercontent.com/${PARENT}/HEAD/${LOCK_PATH}" 2>/dev/null)" || PIN_JSON=""
if [ -z "$PIN_JSON" ]; then
  note "读父仓 ${LOCK_PATH} 失败（网络或公开资源暂不可用），这次跳过（不是失败）。"
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
