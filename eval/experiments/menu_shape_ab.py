#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**菜单形状**定点 A/B：同一个模型、同一套解码参数、同一批句子，只换菜单。

**这是离线实验资产，不是 CI 套件**（`tests/run_all.py` 只收 `tests/*.py`）。它要网络与
API key，一轮烧几十万 token —— 按需手跑，别接进夜间。

## 它问的那一个问题

生产 planner 的动作空间是 **技能**（`agent/workflow_defs/` 的 50 条手写模板，模型点
`navigate` 这种名字）；这个 POC 要量的是**把菜单换成真工具**（模型点 `navigate_to`）
会不会把决策质量打下来。两臂：

  · `skill`：菜单 = `planner_llm.build_tool_schema(role)`（现状）；
  · `tool` ：菜单 = `agent/tool_menu.build_tool_menu(role)`（`tools/base.py` 的 70 个工具
    按 `authz` 的 scope 表过滤）。

## 为什么两臂的提示词由**同一个渲染器**生成

20261011 那次教训（`react_line_ab.py` 头注）：跨臂喂同一份提示词，等于拿一份另一臂
结构上满足不了的契约考它，量出来的差值里掺着"提示词不匹配"。但反过来各写各的提示词，
又会让差值掺进"谁的字写得好"。所以这里取第三条路：**菜单块、契约块、页面块、收尾块
各只有一个渲染函数**，两臂只是把菜单数据换成各自那份、把契约里一个名词换掉
（"技能" ↔ "工具"）。渲染器相同 ⇒ 剩下的差异只有菜单内容本身。

## 它**答不了**什么（结论的边界，别越界引用）

  · **端到端通过率**：这里没有 gate、没有同意卡、没有回执、没有 narrator、没有多轮
    记忆——`create_agent` 的最小循环而已。真正的行为级判据在 golden。
  · **写族**：射程只有只读 + 导航 + 检索；工具全用桩（`_stub_text`），**零真实写**。
  · **工具合并**："把 70 个工具并成几个可组合的"**本身就是另一种手写分组**，量它等于
    量"手写分组 v2 vs 手写模板 v1"。这里量的必须是**没合并过的**角色过滤菜单。

## 判据：读**计数**，不读通过率（单遍结果一律当噪声）

  · `called_any` / `zero_ok`：这一句有没有产生调用；"不该动手"的句子有没有忍住；
  · `hit` / `matched`：点到的工具 ∩ 该句**正确**该落的工具族（**两臂都折算到工具名再比**
    ——拿技能名与工具名直接比是本仓点过名的测量错误）；
  · `wrong_direction`：踩了该句的反向工具（做反了）；
  · `arg_ok`：参数对不对（导航的目标路径 / 特效的名字与开关），两臂的参数名不同
    （`target` vs `path`）⇒ 先归一成"同一个动作"再比；
  · `unfolded`：**点了但折算不出动作**（与"零调用"分两栏记——混成一栏就会把"模型点了
    一个不存在的名字"读成"模型什么都没做"，同 `docs/zero-call-residual.md` 那族纪律）；
  · `rounds`：模型轮数（绕圈）。

## 反假绿

`_selfcheck()` 在**烧 token 之前**跑，任一格不对就退出码 1：

  · **折算链正控**：`_WORKFLOW_TOOLS` 对 navigate/effect/darkmode 必须折得出工具名。
    这一条要是坏了，`skill` 臂会**恒判 0 命中**，而报告上长得像"工具菜单赢了"——
    这是本文件最危险的一种失效；
  · **判据能红**：喂一个不存在的工具名、喂一个方向反了的技能调用，`matched` 必须为假；
  · **判据能绿**：喂正确结果必须为真（否则"恒红"也能假装成"判据很严"）；
  · **菜单非空且不相交**：两个菜单的名字集合不能有交集（有交集则折算歧义）。

跑法（**必须在仓根跑，且 `PYTHONPATH` 指到本 worktree** —— venv 的 editable `.pth`
把主仓钉在 `sys.path` 上，不指就静默用到主仓那份代码）：

    PYTHONPATH=$PWD .venv/bin/python eval/experiments/menu_shape_ab.py \
        --role user --rounds 3 --out /tmp/menu_shape_ab.json

    # 只跑自检（零 token、零网络）：
    PYTHONPATH=$PWD .venv/bin/python eval/experiments/menu_shape_ab.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # 仓根（eval/experiments/ -> 仓根）
sys.path.insert(0, str(ROOT))

from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.tools import StructuredTool  # noqa: E402

import agent.react_arm as RA  # noqa: E402
from agent import tool_index as TI  # noqa: E402
from agent.planner_llm import build_tool_schema  # noqa: E402
from agent.tool_menu import build_tool_menu  # noqa: E402
from agent.workflow_defs import WORKFLOWS  # noqa: E402
from agent.workflow_defs.nav_map import NAV_MAP  # noqa: E402
from models.llm import get_llm  # noqa: E402
from tools import get_all_tools  # noqa: E402

# ── 解码参数：两臂**必须**一致（同 `react_line_ab.DECODE` 的理由，见那份头注）──────────
DECODE = {"temperature": 0.2, "max_tokens": 1200, "timeout": 60, "enable_thinking": False}
REC_LIMIT = 12          # 图级保险丝：撞到这里说明模型在绕圈，本身也是读数
BUDGET = 6

PSEUDO = {"task_hold", "task_drop", "task_intents"}   # 跨轮任务的申报口，不是动作
# 索引档的取回口（ADR-0015 阶段 0）：**读自己菜单**的纯只读口，不是动作。
DESCRIBE = TI.DESCRIBE_SKILL
# 各臂里"什么都不做"的编码：技能臂有 `chat` 这个纯应答技能（模板是空的），工具臂没有
# 对应函数 ⇒ 零调用。判"有没有动手"时必须把这几者归一到同一格（见 `judge`）。
# `describe_skill` 同 `chat`：它不改变站内任何东西，把它算成"动手"会让索引臂在
# `none` 句上恒判"没忍住"——那读出来像"索引菜单更容易乱动手"，其实是口径。
NO_ACTION = PSEUDO | {"chat", DESCRIBE}

# ── 期望族（工具级）──────────────────────────────────────────────────────────
NAV = {"navigate_to"}
EFFECT = {"toggle_effect"}
DARK = {"toggle_dark_mode"}
RAG = {"rag_search", "search_notes", "get_article_detail"}
LIST = {"list_notes", "get_top_notes", "search_notes", "list_categories", "list_tags",
        "list_guestbook", "list_talks", "get_article_detail"}
OWN = {"list_notifications", "get_unread_summary", "list_my_messages",
       "list_dashboard_todos", "list_my_favorites"}
WRITE_REV = {"read_notifications", "read_messages", "add_favorite", "remove_favorite"}

# 逐句：`(id, 句, 期望族, 反向族, kind, 期望导航路径)`。
# `kind` 三档（20261020 试跑后从两档拆成三档，理由见下）：
#   · `act`    —— 期望产生**这一族**动作；`matched` = 命中且没踩反向集；
#   · `none`   —— 期望**不产生任何动作**（闲聊 / 反问）；
#   · `refuse` —— 期望**不跳那个页面**（页面已下线 / 路径不存在）。**不能并进 `act`**：
#                 `nav_friends_down` 的正确行为是"不跳"，而它在 `act` 档里会被记成
#                 "没命中"——于是"忍住了"与"没做到"被算成同一格。试跑实测这两档的错
#                 恰好互相抵消（skill 19/20 里混着一条真错），数字看着对、其实是两笔账。
# 第三格空集 = "这句话没有方向可言"，不是"没查过"（同 `react_line_ab.SENTS` 的注）。
# 导航路径 `None` = 这句话**不该**跳 ⇒ `arg_ok` 判的是"忍住了"。
SENTS: list[tuple[str, str, set, set, str, str | None]] = [
    ("nav_guestbook", "带我去留言板", NAV, set(), "act", "/guestbook"),
    ("nav_device_console", "打开物联网平台", NAV, set(), "act", "/device-console/"),
    ("nav_request_phrase", "带我去设备控制台", NAV, set(), "act", "/device-console/"),
    ("nav_home", "回首页", NAV, set(), "act", "/"),
    ("nav_friends_down", "去友链板块看看", set(), NAV, "refuse", None),
    ("nav_nonexistent", "帮我去 /iot 这个页面", set(), NAV, "refuse", None),
    ("nav_question_not_nav", "你读到留言为什么没有按留言执行任务", set(), NAV, "none", None),
    ("challenge_claim_phantom_nav", "你确定？真的有这个列表页？", set(), NAV, "none", None),
    ("eff_on_sakura", "帮我开樱花特效", EFFECT, set(), "act", None),
    ("eff_off_sakura", "把樱花特效关掉吧", EFFECT, set(), "act", None),
    ("dark_on", "开启夜间模式", DARK, set(), "act", None),
    ("multi_intent_two_effects", "帮我把樱花特效打开，顺便切一下夜间模式",
     EFFECT | DARK, set(), "act", None),
    ("rag_git_branch", "Git 的分支为什么很轻量？", RAG, set(), "act", None),
    ("rag_ota_partition", "ESP32-S3 OTA 更新需要哪些分区？", RAG, set(), "act", None),
    ("rag_arch_memory", "agent 的对话记忆存在哪里？", RAG, set(), "act", None),
    ("rag_noise_cake", "博客里有做巧克力蛋糕的文章吗？", RAG | LIST, set(), "act", None),
    ("search_multiword_keyword", "站内是不是有篇讲 ESP32-S3 OBC 固件接入的文档？",
     RAG | LIST, set(), "act", None),
    ("cq_query_agent_articles", "博客里有没有介绍 agent 的文章", RAG | LIST, set(), "act", None),
    ("rag_talk_rag", "留言板里有人聊过 RAG 的本质吗？", RAG | LIST, set(), "act", None),
    ("dep_search_read_ota", "站内搜一下 OTA 相关的记录，把最对口那篇里传输环节踩的坑说给我",
     RAG | LIST, set(), "act", None),
    ("own_unread_not_logged_in", "我有哪些未读通知？", OWN, WRITE_REV, "act", None),
    ("exec_memory_none_honest", "我咋没看到？你屏幕上到底显示了什么？", set(), set(), "none", None),
    ("sticker_praise_shy", "泠月喵你真的好可爱呀！回答又清楚又有耐心，我最喜欢你了！",
     set(), set(), "none", None),
    ("after_offer_then_look_up", "那你找找吧", RAG | LIST, set(), "act", None),
    ("repeat_ask_no_verbatim", "小猫咪你会做蛋糕吗", set(), RAG | LIST, "none", None),
]

# ── 菜单：两臂各自的 (名字, 说明) 清单 + 绑给模型的 schema ────────────────────
def _menu_lines(entries: list[dict]) -> list[tuple[str, str]]:
    """OpenAI `tools` 数组 → `(名字, 说明)` 清单。**两臂共用这一个渲染器**。

    名字与说明**只从各自那份 schema 上读**（技能那份由 `build_tool_schema` 现场展开
    `__无参只读工具清单__` 这类角色标记、并把 `complete_when` 拼在后面；工具那份由
    `build_tool_menu` 读 `graph._TOOL_MENU_LINES`）——这里**不抄第二份**，否则文本块
    与绑给模型的 schema 会各说各话，而模型只看得到其中一份。
    """
    return [(e["function"]["name"], (e["function"]["description"] or "").replace("\n", " / "))
            for e in entries]


_MENU_HEAD_SKILL = ("## 你能选的技能\n"
                    "每个技能 = 一条**固定的多步工具配方**，系统会替你展开成工具调用。\n"
                    "你只需要选技能、填参数，**不要**自己去拼工具序列。\n")
_MENU_HEAD_TOOL = ("## 你能调用的工具\n"
                   "**直接用工具调用表达你的决定**：本轮要做什么就调用那个工具，\n"
                   "参数按工具自己的定义填。没有「技能」这一层，也没有工具序列给你抄。\n")

# 索引臂多出来的一句：菜单上每个本领只有一行索引，完整说明要**主动取**。这句只给
# `index` 臂——`names` 臂是对照（名字都没有说明、也没有取回口），它不该看见这句话，
# 否则量到的就不是"取回值多少"而是"提示词值多少"。
_MENU_HEAD_INDEX_EXTRA = (
    "\n⚠️ 上面每个本领**只有一行索引**。不确定某个本领到底管什么、或者要在两三个近义\n"
    "本领里挑一个时，先调 `describe_skill(name)` 把它的完整说明取回来再决定"
    "（一次可以查一到三个）。\n")

_CONTRACT_SKILL = """## 怎么表达决定
1. 你的决定通过**工具调用**表达：调用的函数名 = 上面那张表里的**技能名**，参数按它的
   schema 填。
2. 一件事只调一次。同一轮里要把主人交代的几件事都点上。
3. 正文**不是**决策通道：正文只用来补充说明，系统只看你的工具调用。
4. 闲聊、问候、情感、反问、或者你不需要任何技能就能答：调用 `chat`（没有参数）。
5. 信息够了就调用 `chat` 收尾，别反复调同一个技能。"""

_CONTRACT_INDEX = _CONTRACT_SKILL + """
6. 说明只有一行。**不确定某个本领管什么、或者要在几个近义本领里挑一个时**，先调
   `describe_skill(name)` 取回它的完整说明，再决定调哪个技能。"""

_CONTRACT_TOOL = """## 怎么表达决定
1. 你的决定通过**工具调用**表达：直接调用上面那张表里的**那个工具**，参数按它的
   schema 填。没有「技能」这一层。
2. 一件事只调一次。同一轮里要把主人交代的几件事都点上。
3. 正文**不是**决策通道：正文只用来补充说明，系统只看你的工具调用。
4. 闲聊、问候、情感、反问、或者你不需要任何工具就能答：**不要调用任何函数**
   （本轮没有 `chat` 这类函数）——零调用就是收尾轮，系统会另派叙述者。
5. 信息够了就停止调用，别反复调同一个工具。"""


def _prompt(arm: str, role: str | None, sent: str, lines: list[tuple[str, str]]) -> str:
    """各臂的提示词由**同一个渲染器**生成（见头注「为什么」）。

    块序：人设 → 受众 → 菜单 → 契约 → 当前页面 → 主人这一句。臂之间只有两处不同：
    菜单数据、契约块（`_CONTRACT_SKILL` / `_CONTRACT_INDEX` / `_CONTRACT_TOOL`）。
    """
    skillish = arm in ("skill", "index", "names")
    menu = "\n".join(f"· {n} — {d}" if d else f"· {n}" for n, d in lines)
    if skillish:
        head = _MENU_HEAD_SKILL + (_MENU_HEAD_INDEX_EXTRA if arm == "index" else "")
        contract = _CONTRACT_INDEX if arm == "index" else _CONTRACT_SKILL
    else:
        head, contract = _MENU_HEAD_TOOL, _CONTRACT_TOOL
    return "\n\n".join(x for x in (
        RA.BLOG_ASSISTANT_PROMPT or "",
        RA.audience_block(role),
        head + menu,
        contract,
        "## 当前页面\n路径 /，标题「首页」，无特效，白天模式。",
        f"## 主人这一句\n{sent}",
    ) if x)


# ── 桩：**零真实写**（两臂都只挂桩，函数体只返回假结果）──────────────────────
def _stub_text(name: str) -> str:
    """桩载荷。**不许太贫**：只回一句"已执行"会让模型反复重调（20261004 实测把
    撞上限率从 15% 虚增到 33%）。"""
    if name.startswith(("list_", "search_", "get_", "rag_")):
        return (f"「{name}」返回（离线桩，示例数据）：\n"
                "- 示例条目一：这是一条用于占位的说明文字\n"
                "- 示例条目二：第二条占位说明\n"
                "（以上为离线桩数据，不是真实站内内容）")
    return f"「{name}」操作成功（离线桩，未产生真实写操作）。"


def _menu_stubs(entries: list[dict], payload_of) -> list:
    """OpenAI `tools` 数组 → 同名同 schema 的桩工具（`func` 只回假载荷）。

    `payload_of(name)` 决定这个桩回什么：技能臂回**它展开后那件工具**的载荷，
    这样两臂看到的"数据"是同一份（否则比的是载荷贫富，不是菜单形状）。
    """
    out = []
    for item in entries:
        fn = (item.get("function") or item) if isinstance(item, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        out.append(StructuredTool(
            name=name, description=fn.get("description") or "",
            args_schema=fn.get("parameters") or {"type": "object", "properties": {}},
            func=(lambda *a, _n=name, **k: _stub_text(payload_of(_n)))))
    return out


def _skill_payload(name: str) -> str:
    """技能桩回的载荷按**它展开后第一件工具**取（`chat`/`content_query` 没有展开 ⇒ 用它自己）。"""
    tools = _WORKFLOW_TOOLS.get(name) or set()
    return sorted(tools)[0] if tools else name


# ── 三条技能菜单的臂：skill（现状）/ index（索引档）/ names（对照）─────────────
def _names_only(entries: list[dict]) -> list[dict]:
    """`names` 对照臂：**只留名字**——说明清空、取回口摘掉。

    它是"索引档到底值不值"的**分母**：`index` 与它打平 ⇒ 索引行没在起作用，
    `describe_skill` 是多余的（ADR-0015《复验》第一条：出现这种情况就**停**）。
    **不能省掉这条臂**——没有它，"index 比 skill 差多少"这个数里分不开
    "索引切掉了信息"与"描述本来就没人读"。
    """
    out = []
    for e in entries:
        if e["function"]["name"] == DESCRIBE:
            continue
        e = json.loads(json.dumps(e))
        e["function"]["description"] = ""
        out.append(e)
    return out


def _describe_stub(role: str | None) -> StructuredTool:
    """`describe_skill` 的桩：回**真说明**（`tool_index.describe_skill_text`）。

    回真文本而不是假载荷是这一臂的全部意义——模型取回什么，就是它决策时看到什么。
    够不着的名字回一句明说"没这个本领"，**不静默回空**（静默回空会把"名字写错了"
    读成"这个本领没说明"）。
    """
    def _call(name: str = "", **_kw) -> str:
        txt = TI.describe_skill_text(name, role)
        return txt if txt else f"（没有叫「{name}」的本领，或者你的身份看不到它。）"

    return StructuredTool(name=DESCRIBE, description="查看某个本领的完整说明。",
                          args_schema={"type": "object",
                                       "properties": {"name": {"type": "string"}},
                                       "required": ["name"]},
                          func=_call)


def _arm_entries(arm: str, role: str | None) -> list[dict]:
    """一条臂的 OpenAI `tools` 数组（**唯一**来源，`main` 与 `_selfcheck` 都走这里）。"""
    if arm == "skill":
        return build_tool_schema(role)
    if arm == "index":
        return build_tool_schema(role, index=True)
    if arm == "names":
        return _names_only(build_tool_schema(role, index=True))
    if arm == "tool":
        return build_tool_menu(role)
    raise SystemExit(f"未知的臂：{arm}（可选 skill / index / names / tool）")


def _arm_stubs(arm: str, role: str | None) -> list:
    """一条臂的桩集合（零真实写）。索引臂多挂一个 `describe_skill`。"""
    entries = _arm_entries(arm, role)
    payload = (lambda n: n) if arm == "tool" else _skill_payload
    stubs = _menu_stubs(entries, payload)
    if arm == "index":
        stubs.append(_describe_stub(role))
    return stubs


# ── 折算：技能名 → 工具名（两臂可比的前提）──────────────────────────────────
TOOL_NAMES = {t.name for t in get_all_tools()}


def _tools_of_workflow(s: object) -> set[str]:
    """技能被展开后**真正落到哪些工具**——从结构化模板取，不扫字符串（同
    `react_line_ab._tools_of_workflow`：扫子串会让互为子串的两个名字一起命中）。"""
    names = {str(t[0]) for t in (getattr(s, "plan", None) or []) if t}
    names |= {str(n) for n in (getattr(s, "extra_plan_tools", None) or ())}
    return names & TOOL_NAMES


_WORKFLOW_TOOLS: dict[str, set[str]] = {s.name: _tools_of_workflow(s) for s in WORKFLOWS}


def _tools_in_args(args: object) -> set[str]:
    """调用参数里**恰好等于**某个工具名的字面值（按值精确比对，不扫子串）。

    这一格不能删：数据工具点名族（`content_query` 的 `tools`/`calls`）确实把工具名当
    参数值传（"留言板里有人聊过 X 吗"要成对点名 `list_guestbook` 与 `list_talks`）。
    """
    out: set[str] = set()

    def walk(v: object) -> None:
        if isinstance(v, str):
            out.update(p for p in re.split(r"[,\s]+", v) if p in TOOL_NAMES)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)

    walk(args)
    return out


def _canon(name: str, args: dict) -> dict:
    """把一次调用归一成"同一个动作"（两臂的参数名不同：技能 `target` / 工具 `path`）。

    只归一只读射程内的三类：导航、特效、夜间模式。归不出动作的返回 `{}`——
    那是"这一格没东西可比"，不是"参数错了"（两件事混起来会读出假的方向）。
    """
    a = {k: v for k, v in (args or {}).items() if k not in ("intents", "reply_kind")}
    if name in ("navigate", "navigate_to"):
        return {"nav": str(a.get("target") or a.get("path") or "").strip()}
    if name in ("effect", "toggle_effect"):
        return {"effect": str(a.get("effect") or ""), "action": str(a.get("action") or "on")}
    if name in ("darkmode", "toggle_dark_mode"):
        return {"darkmode": str(a.get("mode") or "")}
    return {}


def _nav_resolves(arg: str, want: str | None) -> bool | None:
    """导航参数对不对：别名走 `NAV_MAP`，字面路径直接比。`want is None` ⇒ 不可判（None）。

    `NAV_MAP` 是别名映射的**唯一事实源**（"物联网平台→/device-console/"是系统数据，
    不是模型猜测）——这里读它，不抄第二份。
    """
    if want is None:
        return None
    a = (arg or "").strip()
    if not a:
        return False
    if a in NAV_MAP:
        return NAV_MAP[a] == want
    return a.rstrip("/") == want.rstrip("/")


# ── 跑一句 ────────────────────────────────────────────────────────────────────
def run_one(arm: str, role: str | None, sent: str, stubs: list,
            lines: list[tuple[str, str]]) -> dict:
    """跑一句，读**模型自己产出的 tool_calls**（两臂同一条读取通道）。"""
    out: dict = {"calls": [], "tools": set(), "raw_args": [], "rounds": 0, "capped": False,
                 "error": "", "text": ""}
    llm = get_llm(**DECODE)
    agent = create_agent(model=llm, tools=stubs,
                         system_prompt=_prompt(arm, role, sent, lines),
                         name=f"menu_shape_{arm}")
    last: dict = {}
    n_ai = 0
    try:
        for st in agent.stream({"messages": [HumanMessage(content=sent)]},
                               {"recursion_limit": REC_LIMIT}, stream_mode="values"):
            last = st
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        out["capped"] = "Recursion" in type(e).__name__
    for m in (last.get("messages") or []):
        if not isinstance(m, AIMessage):
            continue
        n_ai += 1
        for c in (m.tool_calls or []):
            name = str(c.get("name") or "")
            args = dict(c.get("args") or {})
            out["calls"].append({"name": name, "args": args, "canon": _canon(name, args)})
            out["raw_args"].append(f"{name}{json.dumps(args, ensure_ascii=False)}")
            out["tools"] |= _tools_in_args(args)
            out["tools"] |= _WORKFLOW_TOOLS.get(name, set())
            if name in TOOL_NAMES:
                out["tools"].add(name)
    out["rounds"] = n_ai
    out["tools"] = {t for t in out["tools"] if t not in PSEUDO}
    msgs = last.get("messages") or []
    out["text"] = str(getattr(msgs[-1], "content", "") or "")[:200] if msgs else ""
    out["n_tool_msgs"] = sum(1 for m in msgs if isinstance(m, ToolMessage))
    return out


def _asked_skills(r: dict) -> list[str]:
    """这一轮**取回了哪几个**本领的完整说明（去重、去空）。

    取回率是 ADR-0015 阶段 0 的**主读数**（模型有没有真去取回）。只对索引臂可能非零，
    其余臂恒空（它们没有那个口）⇒ 这一栏**只许在臂之间横着读**，不许当"质量"读。
    """
    got = {str((c.get("args") or {}).get("name") or "").strip()
           for c in r["calls"] if str(c.get("name") or "") == DESCRIBE}
    return sorted(x for x in got if x)


def judge(r: dict, exp: set, must_not: set, kind: str, want_path: str | None) -> dict:
    """逐句读数（纯函数，自检里被反复喂合成输入——判据本身也要能红能绿）。"""
    tools = set(r["tools"])
    wrong = tools & set(must_not or ())
    hit = tools & set(exp or ())
    # **两臂的"什么都不做"编码不同**（20261020 试跑实测）：技能臂调 `chat` 就是"我只答
    # 不办"（`chat` 的模板是空的，`_WORKFLOW_TOOLS` 折不出任何工具），工具臂则是**零调用**。
    # 不归一，`none` 句上技能臂会恒判"没忍住"，而那是口径不是行为（同 `_canon` 那条）。
    acted = any(str(c.get("name") or "") not in NO_ACTION for c in r["calls"])
    # `canon` 由 `run_one` 预计算；自检里喂进来的合成调用没带它 ⇒ 就地归一出同一把键。
    canon = [c.get("canon") or _canon(str(c.get("name") or ""), c.get("args") or {})
             for c in r["calls"]]
    navs = [c["nav"] for c in canon if c.get("nav")]
    nav_ok = None
    if kind == "act" and want_path is not None and exp == NAV:
        nav_ok = bool(navs) and any(_nav_resolves(x, want_path) for x in navs)
    elif kind == "refuse":
        # 页面已下线 / 路径不存在：**没跳**就对（`nav_friends_down` 的 gold 就是这么写的
        # ——它禁的是 `NAVIGATE:` 前缀，不是"什么都不许做"）。这里判"没跳这个页面"，
        # 不判"一个工具都没调"：查一下别的只读工具不算错。
        nav_ok = not navs
    zero_ok = None
    if kind == "none":
        zero_ok = not acted
    elif kind == "refuse":
        zero_ok = not (tools & set(must_not or ()))
    asked = _asked_skills(r)
    return {"called_any": acted,
            "hit": sorted(hit),
            "matched": (bool(hit) and not wrong) if kind == "act" else None,
            "wrong_direction": sorted(wrong),
            "zero_ok": zero_ok,
            "unfolded": acted and not tools,
            "nav_arg_ok": nav_ok,
            "retrieved": bool(asked),
            "asked": asked,
            "rounds": int(r.get("rounds") or 0),
            "capped": bool(r.get("capped"))}


# ── 自检：烧 token 之前跑，任一格不对就退出码 1 ───────────────────────────────
def _selfcheck_menus(role: str | None, ck) -> None:
    """④ / ④b：各臂菜单的形状，以及取回口的判据。

    **为什么单起一个函数**：`_selfcheck` 已经压在复杂度上限上，而这一族判据还在长
    （每加一条臂就多几格）。拆出来的判据与原来逐字相同——`ck` 由调用方传进来，
    这样"失败记进同一个 `bad` 列表"这件事不变。
    """
    print("\n④ 各臂的菜单非空、且动作名集合不相交（有交集则折算歧义）")
    sk = {n for n, _d in _menu_lines(_arm_entries("skill", role))}
    ix = {n for n, _d in _menu_lines(_arm_entries("index", role))}
    nm = {n for n, _d in _menu_lines(_arm_entries("names", role))}
    tl = {n for n, _d in _menu_lines(_arm_entries("tool", role))}
    for tag, s in (("技能", sk), ("索引", ix), ("名字对照", nm), ("工具", tl)):
        ck(f"{role!r} {tag}菜单非空（{len(s)} 个）", len(s) > 0)
    ck("skill / index / tool 三臂的说明都非空（空说明 = 模型只看得见名字）",
       all(d for _n, d in _menu_lines(_arm_entries("skill", role)))
       and all(d for _n, d in _menu_lines(_arm_entries("index", role)))
       and all(d for _n, d in _menu_lines(_arm_entries("tool", role))))
    # `names` 臂的**全部意义**就是说明为空、且没有取回口 —— 这两条要是失效，
    # 它就不再是对照臂，而 `index` 与它的差会被读成 0（看着像"索引没用"）。
    ck("names 臂的说明**全空**（它是「只看得见名字」那条对照）",
       not any(d for _n, d in _menu_lines(_arm_entries("names", role))))
    ck(f"names 臂没有 {DESCRIBE}（没有取回口才叫对照）", DESCRIBE not in nm)
    ck(f"index 臂有且只有一个 {DESCRIBE}、且排最后",
       [e["function"]["name"] for e in _arm_entries("index", role)].count(DESCRIBE) == 1
       and _arm_entries("index", role)[-1]["function"]["name"] == DESCRIBE)
    ck("skill / index / names 三臂的**技能名集合相同**（只换说明，不换菜单本身；"
       "索引臂多出来的那一格只能是取回口）",
       sk == (ix - {DESCRIBE}) == nm,
       str(sorted(sk ^ (ix - {DESCRIBE})) + sorted(sk ^ nm)))
    # **动作名**不相交（三个伪函数两臂都有——它们是 harness 资产，见头注；
    # 折算歧义只可能出在动作名上，所以这里比的是 `sk - PSEUDO` 与 `tl - PSEUDO`）。
    ck("技能族与工具臂的**动作名**集合不相交（有交集则 `_WORKFLOW_TOOLS` 与真名分不开）",
       not ((sk - PSEUDO) & (tl - PSEUDO)), str(sorted((sk - PSEUDO) & (tl - PSEUDO))))
    ck("三个伪函数**技能族与工具臂都在**（照搬的跨切面槽位，见头注 ③）",
       PSEUDO <= sk and PSEUDO <= tl, str(sorted(PSEUDO - sk | (PSEUDO - tl))))
    ck("工具菜单里的名字**都是真工具或三个伪函数**", tl <= (TOOL_NAMES | PSEUDO),
       str(sorted(tl - TOOL_NAMES - PSEUDO)))
    ck("技能族菜单里的名字**都是真技能或申报过的那几个**",
       sk <= ({w.name for w in WORKFLOWS} | PSEUDO | {DESCRIBE}),
       str(sorted(sk - {w.name for w in WORKFLOWS} - PSEUDO - {DESCRIBE})))
    # 索引臂的说明必须**真的是索引**（不是把整段描述原样搬过来）——否则这一臂与
    # `skill` 臂逐字节相同，跑出来的"没差别"会被读成"索引不影响决策"。
    idx_lines = _menu_lines(_arm_entries("index", role))
    full_lines = dict(_menu_lines(_arm_entries("skill", role)))
    shorter = [n for n, d in idx_lines if n in full_lines and len(d) < len(full_lines[n])]
    ck("索引臂的说明**确实比现状短**（否则这一臂等于现状，跑出来的差恒 0）",
       len(shorter) >= len(full_lines) - 5, f"变短 {len(shorter)}/{len(full_lines)}")

    print("\n④b 取回口的判据（它坏了 ⇒ 取回率恒 0，读出来像「模型不需要索引」）")
    ck("`describe_skill` **不算动手**（同 `chat`：它不改变站内任何东西）",
       judge({"tools": set(), "calls": [{"name": DESCRIBE, "args": {"name": "navigate"}}]},
             set(), NAV, "none", None)["called_any"] is False)
    ck("`describe_skill` 调用被记成**取回**",
       judge({"tools": set(), "calls": [{"name": DESCRIBE, "args": {"name": "navigate"}}]},
             set(), NAV, "none", None)["retrieved"] is True)
    ck("没取回 ⇒ `retrieved` 为假", judge({"tools": set(), "calls": []}, set(), NAV,
                                          "none", None)["retrieved"] is False)
    ck("取回桩回的是**真说明**（不是空串/不是假载荷）",
       len(TI.describe_skill_text("navigate", role) or "") > 10)
    ck("取回桩对**够不着的名字**明说没这个本领（不静默回空）",
       "没有叫" in _describe_stub(role).func(name="no_such_skill_xyz"))


def _selfcheck(role: str) -> list[str]:
    bad: list[str] = []

    def ck(desc: str, cond: bool, detail: str = "") -> None:
        print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
        if not cond:
            bad.append(desc)

    print("\n① 折算链正控（它坏了 ⇒ 技能臂恒判 0 命中，而报告上像「工具赢了」）")
    for name, want in (("navigate", {"navigate_to"}), ("effect", {"toggle_effect"}),
                       ("darkmode", {"toggle_dark_mode"}),
                       ("read_article", {"get_article_detail"}),
                       ("ops_report", {"get_server_status", "get_service_health"})):
        ck(f"{name} 折得出 {sorted(want)}", _WORKFLOW_TOOLS.get(name) == want,
           str(sorted(_WORKFLOW_TOOLS.get(name) or [])))
    ck("折算表覆盖全部技能（不是空转）",
       len(_WORKFLOW_TOOLS) == len(WORKFLOWS) and len(WORKFLOWS) > 40,
       f"{len(_WORKFLOW_TOOLS)}/{len(WORKFLOWS)}")
    ck("折出来的名字**都在真工具注册表里**（否则是造了个不存在的名字）",
       all(t in TOOL_NAMES for s in _WORKFLOW_TOOLS.values() for t in s))

    print("\n② 判据能红、也能绿（只会绿或只会红的判据都不是判据）")
    exp, must = {"navigate_to"}, set()
    good = {"tools": {"navigate_to"}, "calls": [{"name": "navigate_to", "args": {}}]}
    ck("喂正确结果 ⇒ 命中", judge(good, exp, must, "act", None)["matched"] is True)
    ghost = {"tools": {"tool_that_does_not_exist"}, "calls": [{"name": "x", "args": {}}]}
    ck("喂一个不存在的工具名 ⇒ **不命中**（正控）",
       judge(ghost, exp, must, "act", None)["matched"] is False)
    rev = {"tools": {"navigate_to", "toggle_dark_mode"},
           "calls": [{"name": "darkmode", "args": {}}]}
    ck("喂正确 + 反向 ⇒ **不命中**（反向集真的在开火）",
       judge(rev, exp, {"toggle_dark_mode"}, "act", None)["matched"] is False)
    none_kind = judge({"tools": set(), "calls": []}, set(), NAV, "none", None)
    ck("`none` 句忍住不动手 ⇒ zero_ok", none_kind["zero_ok"] is True)
    ck("`none` 句动了手 ⇒ **不 zero_ok**",
       judge({"tools": NAV, "calls": [{"name": "navigate", "args": {}}]},
             set(), NAV, "none", None)["zero_ok"] is False)
    unf = judge({"tools": set(), "calls": [{"name": "navigate", "args": {}}]}, exp, must,
                "act", None)
    ck("**「点了但折算不出动作」与「零调用」分两栏**",
       unf["called_any"] is True and unf["unfolded"] is True)
    # 两臂的"什么都不做"编码不同（技能臂调 `chat`，工具臂零调用）⇒ 必须归一到同一格，
    # 否则 `none` 句上技能臂恒判"没忍住"，而那读出来像"技能菜单更容易乱动手"。
    chat = judge({"tools": set(), "calls": [{"name": "chat", "args": {}}]}, set(), NAV,
                 "none", None)
    ck("技能臂调 `chat` ⇒ **不算动手**（zero_ok 为真、unfolded 为假）",
       chat["zero_ok"] is True and chat["called_any"] is False
       and chat["unfolded"] is False)
    ck("工具臂零调用 ⇒ 同样不算动手",
       judge({"tools": set(), "calls": []}, set(), NAV, "none", None)["zero_ok"] is True)
    # `refuse` 档：判的是"没跳那个页面"，**不是**"一个工具都没调"（查别的只读工具不算错）。
    ref = judge({"tools": set(), "calls": [{"name": "search_notes", "args": {}}]},
                set(), NAV, "refuse", None)
    ck("`refuse` 句：查了个别的只读工具 ⇒ **仍算忍住**（判据是「没跳」，不是「没动」）",
       ref["zero_ok"] is True and ref["nav_arg_ok"] is True)
    ref2 = judge({"tools": NAV, "calls": [{"name": "navigate", "args": {"target": "友链"}}]},
                 set(), NAV, "refuse", None)
    ck("`refuse` 句：真跳了 ⇒ **红**", ref2["zero_ok"] is False and ref2["nav_arg_ok"] is False)

    print("\n③ 导航参数判据（别名走 NAV_MAP，字面路径直接比）")
    ck("别名「留言板」⇒ /guestbook", _nav_resolves("留言板", "/guestbook") is True)
    ck("别名「物联网平台」⇒ /device-console/",
       _nav_resolves("物联网平台", "/device-console/") is True)
    ck("字面路径 /guestbook ⇒ /guestbook", _nav_resolves("/guestbook", "/guestbook") is True)
    ck("**答错页面 ⇒ 红**（「友链」已下线 ⇒ None ⇒ 不该跳）",
       _nav_resolves("友链", "/guestbook") is False)
    ck("空参数 ⇒ 红", _nav_resolves("", "/guestbook") is False)
    ck("`want=None` ⇒ 不可判（None，不是 False——「没查过」与「错了」是两件事）",
       _nav_resolves("友链", None) is None)

    _selfcheck_menus(role, ck)

    print("\n⑤ 句子表自身的完备性")
    ck("每句的期望族与反向族都在真工具名里",
       all((e | m) <= TOOL_NAMES for _i, _s, e, m, _k, _p in SENTS),
       str(sorted({t for _i, _s, e, m, _k, _p in SENTS for t in (e | m)} - TOOL_NAMES)))
    ck("`act` 句都有期望族、`none`/`refuse` 句都没有",
       all((bool(e) if k == "act" else not e) for _i, _s, e, _m, k, _p in SENTS))
    ck("`refuse` 句都有反向族（没有反向集它就退化成「什么都不许做」，与 `none` 同义）",
       all(bool(m) for _i, _s, _e, m, k, _p in SENTS if k == "refuse"))
    ck("id 唯一", len({i for i, *_ in SENTS}) == len(SENTS))
    return bad


# ── 汇总 ──────────────────────────────────────────────────────────────────────
def _frac(recs: list, key: str) -> str:
    """`x/y` 计数（**不写通过率**：单遍结果一律当噪声，同仓既有纪律）。"""
    vals = [x[key] for x in recs if x[key] is not None]
    return f"{sum(1 for v in vals if v)}/{len(vals)}"


def _summarize(arm: str, rnd: int, recs: list, secs: float) -> dict:
    asked_n = sum(len(x.get("asked") or []) for x in recs)
    row = {"called_any": _frac(recs, "called_any"),
           "matched": _frac(recs, "matched"),
           "zero_ok": _frac(recs, "zero_ok"),
           "nav_arg_ok": _frac(recs, "nav_arg_ok"),
           "retrieved": _frac(recs, "retrieved"),
           "asked_total": asked_n,
           "wrong_direction": sum(1 for x in recs if x["wrong_direction"]),
           "unfolded": sum(1 for x in recs if x["unfolded"]),
           "capped": sum(1 for x in recs if x["capped"]),
           "errors": sum(1 for x in recs if x["error"]),
           "llm_rounds": sum(int(x.get("rounds") or 0) for x in recs),
           "secs": round(secs, 1), "rows": recs}
    print(f"== {arm} r{rnd}: 有调用 {row['called_any']}  命中 {row['matched']}"
          f"  忍住 {row['zero_ok']}  导航参数对 {row['nav_arg_ok']}"
          f"  反向 {row['wrong_direction']}  点了折不出 {row['unfolded']}"
          f"  取回 {row['retrieved']}（共 {asked_n} 次）"
          f"  撞上限 {row['capped']}  出错 {row['errors']}"
          f"  模型轮数 {row['llm_rounds']}  用时 {row['secs']}s", flush=True)
    return row


def _pick_sents(limit: int, only: str) -> list:
    """这一轮跑哪几句（`--limit` 取前缀、`--only` 点名）。

    `--only` 存在的理由：**真正吃紧的句子在表尾**（近义检索那 9 条），而 `--limit` 只能
    取前缀——按前缀跑一遍等于只量了最好量的那一半。名字写错**响亮**（`SystemExit`），
    不静默少跑几句：静默少跑会让"这轮只跑了 3 句"长得像"这轮全绿"。
    """
    if not only:
        return SENTS[:limit] if limit else SENTS
    want = {x for x in only.split(",") if x}
    unknown = sorted(want - {i for i, *_ in SENTS})
    if unknown:
        raise SystemExit(f"--only 里有表上没有的 id：{unknown}")
    return [s for s in SENTS if s[0] in want]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="user", help="身份（决定两臂的菜单大小）")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--arms", default="skill,tool")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 句（试跑用）")
    ap.add_argument("--only", default="", help="只跑这几句（逗号分隔的 id；`--limit` 只能取前缀，"
                                               "而真正吃紧的句子在表尾）")
    ap.add_argument("--dry-run", action="store_true", help="只跑自检，零 token")
    ap.add_argument("--out", default="/tmp/menu_shape_ab.json")
    a = ap.parse_args()

    print(f"== 自检（role={a.role}）==")
    bad = _selfcheck(a.role)
    if bad:
        print(f"\n❌ 自检 {len(bad)} 项未过 —— 先修判据，别烧 token：")
        for x in bad:
            print("   -", x)
        return 1
    # 选句子放在 `--dry-run` **之前**：`--only` 写错名字要当场响亮，而不是等到烧 token
    # 那一步才炸（也免得 `--dry-run` 里"自检全过"却选出了一批不存在的 id）。
    sents = _pick_sents(a.limit, a.only)
    print(f"\n✅ 自检全过（本次 {len(sents)} 句）")
    if a.dry_run:
        return 0

    arms = [s for s in a.arms.split(",") if s]
    entries = {arm: _arm_entries(arm, a.role) for arm in arms}
    stubs = {arm: _arm_stubs(arm, a.role) for arm in arms}
    menu_lines = {arm: _menu_lines(entries[arm]) for arm in arms}
    print(f"\n菜单规模（role={a.role}）："
          + " / ".join(f"{arm} {len(entries[arm])} 项" for arm in arms))

    R: dict = {"role": a.role, "decode": DECODE, "rec_limit": REC_LIMIT,
               "arms": arms,
               "menu_size": {arm: len(entries[arm]) for arm in arms},
               # 菜单侧字符数：索引档省下来的量**在这一层就是死的数**（不用等模型）。
               "menu_chars": {arm: len(json.dumps(entries[arm], ensure_ascii=False))
                              for arm in arms},
               "rounds": []}
    for r in range(1, a.rounds + 1):
        order = arms if r % 2 else list(reversed(arms))   # 两臂交替，抵消端点漂移
        row = {"round": r, "arms": {}}
        for arm in order:
            t0, recs = time.time(), []
            for i, (sid, sent, exp, must, kind, want) in enumerate(sents, 1):
                rr = run_one(arm, a.role, sent, stubs[arm], menu_lines[arm])
                rr.update(judge(rr, exp, must, kind, want))
                rr.update(id=sid, sent=sent, kind=kind, exp=sorted(exp))
                recs.append(rr)
                print(f"[{arm} r{r} {i}] {sid} 调用={[c['name'] for c in rr['calls']]}"
                      f" 命中={rr['hit']}"
                      f"{' 反方向=' + str(rr['wrong_direction']) if rr['wrong_direction'] else ''}"
                      f"{' 折不出' if rr['unfolded'] else ''}"
                      f"{' CAPPED' if rr['capped'] else ''}"
                      f"{' ERR:' + rr['error'][:40] if rr['error'] else ''}", flush=True)
            row["arms"][arm] = _summarize(arm, r, recs, time.time() - t0)
        R["rounds"].append(row)
        Path(a.out).write_text(json.dumps(R, ensure_ascii=False, indent=1, default=str))

    print("\n=== 逐轮配对（round, arm, 命中, 反向, 折不出, 撞上限, 模型轮数, 用时s）===")
    for arm in arms:
        print(" ", arm, [(x["round"], x["arms"][arm]["matched"],
                          x["arms"][arm]["wrong_direction"], x["arms"][arm]["unfolded"],
                          x["arms"][arm]["capped"], x["arms"][arm]["llm_rounds"],
                          x["arms"][arm]["secs"]) for x in R["rounds"]])
    print("\n⚠️ 这是**决策层探针**：没有 gate / 同意卡 / 回执 / narrator / 多轮。"
          "它给的数只用来**看方向**，不用来判谁赢——行为级判据在 golden。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
