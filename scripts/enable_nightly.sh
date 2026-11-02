#!/usr/bin/env bash
# Opt-in scheduled regression. Dry-run by default; explicit apply changes this user's crontab.
# Provide dedicated GOLDEN_ADMIN_UID/GOLDEN_USER_UID; the job calls models and fixture probes.
# Usage: enable_nightly.sh [--apply|--off|--help]
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NIGHTLY="$REPO_ROOT/scripts/nightly_regression.sh"
CRON_LINE="0 4 * * * GOLDEN_ADMIN_UID=${GOLDEN_ADMIN_UID:-} GOLDEN_USER_UID=${GOLDEN_USER_UID:-} \"$NIGHTLY\" >/dev/null 2>&1"

MODE="dry"
case "${1:-}" in
    "")        MODE="dry" ;;
    --apply)   MODE="apply" ;;
    --off)     MODE="off" ;;
    -h|--help) echo "Usage: enable_nightly.sh [--apply|--off|--help]"; exit 0 ;;
    *)         echo "不认识的参数：$1（用 --apply / --off / 不带参数干跑）" >&2; exit 2 ;;
esac

if [ "$MODE" = "apply" ]; then
    for actor_key in GOLDEN_ADMIN_UID GOLDEN_USER_UID; do
        actor_id="${!actor_key:-}"
        if ! [[ "$actor_id" =~ ^[1-9][0-9]*$ ]]; then
            echo "请显式配置 $actor_key 为专用测试账号编号；尚未写入 crontab。" >&2
            exit 3
        fi
    done
fi

if ! [ -f "$NIGHTLY" ]; then
    echo "找不到 $NIGHTLY —— 这个脚本只对本仓有效，别把它拷到别处跑。" >&2
    exit 2
fi

if ! command -v crontab >/dev/null 2>&1; then
    echo "这台机器上没有 crontab 命令 —— 换成别的调度器（systemd timer / CI schedule）时，" >&2
    echo "要跑的就是这一行：$CRON_LINE" >&2
    exit 2
fi

cur="$(crontab -l 2>/dev/null || true)"

# Commented jobs do not count as enabled.
live="$(printf '%s\n' "$cur" | grep -F "$NIGHTLY" | grep -v '^[[:space:]]*#' || true)"

if [ "$MODE" != "off" ]; then
    echo "本仓：$REPO_ROOT"
    echo "夜间脚本：$NIGHTLY"
    echo "要装的 cron 行："
    echo "  $CRON_LINE"
    echo
fi

case "$MODE" in
    dry)
        if [ -n "$live" ]; then
            echo "crontab 里已经有它了（下面这一行），--apply 是空操作："
            printf '%s\n' "$live" | sed 's/^/  /'
        else
            echo "crontab 里**没有**它 —— 也就是说这台机器上夜间回归是关着的（出厂默认）。"
        fi
        echo
        echo "本任务会调用真实模型，实际消耗以当轮usage和配置价格计算；还会使用"
        echo "一次性靶子账号。先配置专用测试账号，再以 --apply 显式启用。"
        ;;
    apply)
        if [ -n "$live" ]; then
            echo "已经在 crontab 里了，不动（幂等）。"
            exit 0
        fi
        if [ -n "$(printf '%s\n' "$cur" | grep -F "$NIGHTLY" || true)" ]; then
            echo "注意：crontab 里有一行同路径但**被注释掉**的 —— 保持原样（那通常是刻意留的记录）。"
            echo "下面会另加一行活的。"
        fi
        { printf '%s\n' "$cur"; printf '%s\n' "$CRON_LINE"; } | grep -v '^[[:space:]]*$' | crontab -
        # 读回来核对，不凭"命令没报错"就宣布成功（写 crontab 失败的样子是静默的）。
        if crontab -l 2>/dev/null | grep -qF "$CRON_LINE"; then
            echo "✅ 已写进 crontab，每天 04:00 跑。结果追加到 ~/agent_regression.log；"
            echo "   失败会在 ~/agent_regression.failed 留标记（心跳探针每分钟顺带看一眼）。"
            echo "   摘掉：bash scripts/enable_nightly.sh --off"
        else
            echo "❌ 写了但读回来没有 —— 别当它开着。crontab -l 看一眼。" >&2
            exit 1
        fi
        ;;
    off)
        if [ -z "$(printf '%s\n' "$cur" | grep -F "$NIGHTLY" || true)" ]; then
            echo "crontab 里没有指向本仓夜间脚本的行，没什么可摘的。"
            exit 0
        fi
        printf '%s\n' "$cur" | grep -vF "$NIGHTLY" | crontab -
        echo "已摘掉指向本仓夜间脚本的行（同路径的注释行一并摘掉，本脚本不区分）。"
        echo "现在 crontab 里剩下的："
        crontab -l 2>/dev/null | sed 's/^/  /'
        ;;
esac
