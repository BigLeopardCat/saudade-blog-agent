#!/usr/bin/env bash
# 夜间回归的**开关**（20261017）。
#
# ## 为什么要有这个脚本：出厂是**关**的
#
# 这个仓库是公开的，而夜间回归**不是**它默认该有的样子：`scripts/nightly_regression.sh`
# 一次全量要真调 LLM 跑一遍 golden（≈10M tok / ¥2.4 —— 这个数字是维护者自己记在 crontab
# 注释里的实测），跑的是**别人那台机器上的**博客语料与夹具。谁克隆这个仓库都不该在不知情
# 的情况下开始每天烧自己的额度，也不该在语料对不上的红面前怀疑自己的模型。
#
# 所以：**仓库里没有任何东西替你装 cron**（`deploy/install.sh` 只装心跳探针那一行）。
# 想让它天天跑的人——通常是想改 agent 行为、需要一条稳定的回归基线的人——用这个脚本开。
#
# ## 代价先看，再决定
#
#   · 每次全量 golden + LLM 判官 ≈10M tok / ¥2.4（维护者 20261012 实测；模型与用例数
#     会变，这是量级不是报价）。每天一次。
#   · 它还会真写生产库的**一次性靶子**（`probe_revoke_*` 账号）——真链路探针那一节。
#   · 语料对不上时它**不是**红的，是"未评估 + 退出码 3"（判据见 eval/corpus_terms.py）；
#     换句话说它不会骗你说模型退化了，但它也不会替你准备语料。
#   · 想先只跑便宜的那半（L0 套件 / L1 检索 / 零 LLM 的巡检）——那些不必开这个开关，
#     直接 `bash scripts/nightly_regression.sh` 手跑一次就够，看的是同一份日志格式。
#
# ## 用法
#
#   bash scripts/enable_nightly.sh           # 干跑：只说要加哪一行（默认，什么都不改）
#   bash scripts/enable_nightly.sh --apply   # 真写进 crontab
#   bash scripts/enable_nightly.sh --off     # 摘掉（只摘指向本仓这一支的那一行）
#
# 默认干跑、`--apply` 才动手 —— 与本仓 `eval/artifact_retention.py`、父仓
# `scripts/deploy/prune_r2.py` 同一约定（"默认只列不删"），别改成默认就写。
#
# ## 它写的到底是哪一行
#
#   0 4 * * * <本仓>/scripts/nightly_regression.sh >/dev/null 2>&1
#
# 04:00 是维护者的选择（避开 04:20 的 apt-daily；父仓那班沙箱排在 04:40，见
# `scripts/nightly_sandboxes.sh`）。**路径从本脚本自己的位置推**，不写死任何人的家目录
# —— 写死路径的开关，在别人机器上开出来的是一个每晚静默失败的 cron。
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NIGHTLY="$REPO_ROOT/scripts/nightly_regression.sh"
CRON_LINE="0 4 * * * $NIGHTLY >/dev/null 2>&1"

MODE="dry"
case "${1:-}" in
    "")        MODE="dry" ;;
    --apply)   MODE="apply" ;;
    --off)     MODE="off" ;;
    -h|--help) sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         echo "不认识的参数：$1（用 --apply / --off / 不带参数干跑）" >&2; exit 2 ;;
esac

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

# 判"已经开着"看的是**没被注释掉**的那一行（注释掉的同路径行不算——那是有人刻意留的病史，
# 见维护者机器上那三条 20261012/20261017 的记录）。
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
        echo "代价：一次全量 ≈10M tok / ¥2.4（维护者实测的量级），每天一次；还会真写生产库的"
        echo "一次性靶子账号。要开：bash scripts/enable_nightly.sh --apply"
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
