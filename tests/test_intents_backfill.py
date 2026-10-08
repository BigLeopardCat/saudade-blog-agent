# -*- coding: utf-8 -*-
"""意图清单的**反方向**补交（20261009）：点了技能却没交清单 ⇒ 催一次。

**这一件治什么**：批 ②（ADR-0002 追记）让模型把"主人这句话里有几件事"一次列全，
但清单有两个出口（伪函数 `task_intents`、技能调用上的 `intents` 字段），而 native 档
`parallel_tool_calls=False` ⇒ 一轮只发得出一条调用、两个出口**互斥**。于是有两个方向
的失败，此前只堵了一个：

  · ③ 只交清单、一个动作都没点（伪函数那一路）—— 已有的 `_INTENTS_ONLY_NUDGE`；
  · ④ **点了技能、清单却没留下要记的**（内联字段那一路）—— 本件。真链路实证：主人说
    「想建个新分类叫「临江仙」，文章 23 的标签也想换成「Rust」」，planner 点了
    `category_create`、`intents` 一个字没填 ⇒ "没上卡的那件"从登记里彻底消失。
    **这一格堵完还漏了一种更长见的形状**（同一批复核当场抓到）：清单**非空**、但里面
    只写着"这一轮正要办的那件"，被排除规则减完一件不剩（trace `20261009_032513`，
    `listed=1 / acted=['category_create'] / frames=0`）——另一件照样消失。判据因此从
    "清单是不是空的"改成"**减完还剩几件**"（`_intents_left_count`），两种形状一个信号
    （处方相同：把清单补全，含它正要办的那件——多填无害）。
    两件的处方**同一个手法**：把清单那条伪函数从 schema 里摘掉，剩下的出口正好长在
    模型已经选中的那个技能的参数里（不用改主意、不用多点一次工具）。

**判据落在这四处**（本套件）：
  · ① `_multi_item_shape` —— "这句话像不像含两件以上"的形态判据（纯函数，正反例）；
  · ② `_task_goal_sources` —— 出处对账读哪几段主人话（纯函数；`[0]` 必须是本轮那句）；
  · ③ 纠偏语文本 —— 不点任何技能名、必须说清"清单那条通道这一轮没有了"（不说清，
    模型会照旧报一个不在 schema 里的函数名 ⇒ `tool_calls_to_plan` 读不出决策）；
  · ④ 接线 —— 那一格在 `planner_node` 的一次性纠偏通道里，守卫齐备，且**与 ③
    互换条件不重叠**（③ 收 `skill == "chat"`，④ 收 `skill not in ("", "chat")`）。

对账那一半（`tasks.reconcile_goal` / `sources` 贯穿）在 `tests/test_tasks.py`——那是
tasks 层的契约，与"不许 import agent.graph"同一条纪律。

用法：.venv/bin/python tests/test_intents_backfill.py
（跑法两种档位都行：这一件不碰 schema 的档位，`AGENT_TASK_STATE` 取 0/1 结论相同。）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
from agent.skills import visible_skills  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_GRAPH_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
# ④ 那一格的起点（源码锁用；只在这里算一次）
_I_BRANCH = _GRAPH_SRC.index("_acted_no_intents = bool(")

# 现场那两句（逐字抄自 golden `mix2_two_writes_one_breath_card_only` 与
# `admin_write_intent_named_target_only` 的头注/用例，别改写——形态判据要判的正是它们）。
MIX2 = "想建个新分类叫「临江仙」，文章 23 的标签也想换成「Rust」"


# ── ① 形态判据 ──────────────────────────────────────────────────────────
def test_multi_item_shape():
    print("\n[形态] `_multi_item_shape`：多子句 + 非首句的连接词（偏召回，代价只是一次调用）")
    cases = [
        (MIX2, True),
        ("帮我看看后台待办，顺便把文章 5 删了", True),
        ("猫猫给 double9 发个通知，以及你的口吻，然后你去给他降级", True),
        ("帮我把文章 5 置顶，另外把标签也换一下", True),
        # 反例：一句话一件事。**兜底那一类是打招呼**——问候句是子句但不是一个"件"，
        # 只有"非首句里出现连接词"才认（所以这里刻意用 `evaluate` 而不是全文搜连接词）。
        ("你好，帮我把文章 23 的标签换成 Rust", False),
        ("帮我把文章 23 的标签换成 Rust", False),
        ("把樱花也打开", False),                            # 连接词在**首句**：只是语气，不是第二件
        ("帮我把樱花打开", False),
        ("啊真好看，帮我收藏一下", False),
        ("嗯，好的，那就这样", False),
        # 真现场（`tests/test_todo_schedule.py` 的 `_INCIDENT_MSG`，逐字）：`和` 长在待办
        # 正文的**名词短语**里（"修改面和后续评估升级"），不是子句连接词 ⇒ 不许判成两件
        # （判了就是白催一轮，而那一支的轮次是计数进判据的）。
        ("闺女，给我加一条今天的待办，1.agent开发：探讨引入JEV等决策模式的修改面"
         "和后续评估升级。2.后台面板移动端适配是灾难级别的，亟待优化。", False),
        # 三条**已知边界**（形态判据读不出来，如实钉住而不是假装能判）：
        ("不错收藏啦，开启夜间模式和雪花", False),          # ① 和 并列的两件事在同一子句里
        ("带我过去后开启一个特效", False),                  # ② 整句没有子句分隔符
        ("把樱花和雨一起打开", False),                      # ③ 和 在唯一那个子句里
        ("", False),
        (None, False),
    ]
    bad = [(t, g._multi_item_shape(t)) for t, exp in cases if g._multi_item_shape(t) != exp]
    check(f"正反例 {len(cases)} 条全部判对（含三条已知边界与一条真现场误伤）",
          not bad, str(bad))


# ── ② 出处对账读哪几段主人话 ─────────────────────────────────────────────
def test_task_goal_sources_picks_masters_words():
    print("\n[出处] `_task_goal_sources`：本轮那句在最前，`[System: …]` 注入不算主人说的话")
    state = {"messages": [
        HumanMessage("[System: current_time=2026-10-09 10:00; url=/article/100"),
        AIMessage("上上轮的回复"),
        HumanMessage("把文章 100 的标签换成 Rust"),
        AIMessage("上一轮的回复"),
        HumanMessage(MIX2),
    ]}
    src = g._task_goal_sources(state)
    check("顺序：`[0]` 是本轮那句（对账逐来源找，只有它必须最新）",
          src[0] == MIX2, str(src))
    check("往前带的是**主人的话**（不含 [System: …] 注入、不含 AI 回复）",
          src == [MIX2, "把文章 100 的标签换成 Rust"], str(src))
    check("回看轮数是常量、且 >1（跨轮复述要能取到更早那轮的主人话）",
          g._TASK_GOAL_SRC_TURNS >= 2 and len(src) <= g._TASK_GOAL_SRC_TURNS)
    check("重复/空白不算新的一轮（同一句话不会占两格）",
          g._task_goal_sources({"messages": [HumanMessage(MIX2), HumanMessage(MIX2)]})
          == [MIX2])
    check("没有 messages / 空值一律空列表（不炸，也不凭空造一句主人话）",
          g._task_goal_sources({}) == [] and g._task_goal_sources({"messages": [None]}) == [])


# ── ③ 纠偏语文本 ────────────────────────────────────────────────────────
def test_backfill_nudge_text():
    print("\n[纠偏] `_INTENTS_BACKFILL_NUDGE`：不替模型选技能、且必须交代清单通道没了")
    check("不念任何技能名（念一遍等于替它做选择；与本仓另外几条纠偏语同一条纪律）",
          all(s.name not in g._INTENTS_BACKFILL_NUDGE
              for s in visible_skills("admin") if s.name != "chat"),
          str([s.name for s in visible_skills("admin")
               if s.name in g._INTENTS_BACKFILL_NUDGE]))
    check("点名要填的是**内联字段**（`intents` 参数），不是那条伪函数",
          "`intents`" in g._INTENTS_BACKFILL_NUDGE)
    check("说清「含你这一轮正要办的那件」（排除规则保证多填无害——不说它不敢填）",
          "正要办" in g._INTENTS_BACKFILL_NUDGE)
    check("**两种形状都要说**：清单交了这一格也会开火（清单里只有它正要办的那件），"
          "只说「你没交清单」在那一轮是**假话**——模型被安一个它没犯的错，"
          "接着多半会去证明自己交过了",
          "没交清单" in g._INTENTS_BACKFILL_NUDGE
          and "只有你这一轮正要办的那一件" in g._INTENTS_BACKFILL_NUDGE)
    check("说清伪函数这一轮**已从工具清单里摘掉**（不说 ⇒ 它再报一次不在 schema 里的"
          "函数名，`tool_calls_to_plan` 读不出决策 = 确定性收尾，比不交清单更坏）",
          "已经没有独立的那条通道" in g._INTENTS_BACKFILL_NUDGE)
    check("是**单行以外的散文**也无妨，但不许含换行以外的控制字符（它会进 LLM 消息）",
          all(ch == "\n" or ch.isprintable() for ch in g._INTENTS_BACKFILL_NUDGE))


# ── ④ 接线 ──────────────────────────────────────────────────────────────
def test_branch_wiring():
    print("\n[接线] ④ 那一格：守卫齐备 + 摘伪函数 + 重绑 llm + 记账 + continue")
    i = _GRAPH_SRC.index("_acted_no_intents = bool(")
    blk = _GRAPH_SRC[i:i + 2200]
    check("守卫：点了**真技能**才进这一格（skill 既不是空串也不是 chat ⇒ 与 ③ 互斥）",
          'not in ("", "chat")' in blk)
    check("守卫：`rounds == 0`（枚举回答的是「这句话里有几件事」，只该问一次）",
          "rounds == 0" in blk)
    check("守卫：`not decided.declare`（显式登记过的不催）",
          "not decided.declare" in blk)
    check("**判的是「减完还剩几件」，不是「清单空不空」**（空清单只是两种形状之一，"
          "另一种更常见：清单里只写了正要办的那件。写成 `not decided.intents` 那一版"
          "在 trace `20261009_032513` 上当场漏掉）",
          "_intents_left_count(state, config, decided) == 0" in blk
          and "not decided.intents" not in blk)
    check("形态判据参与守卫（只有像多件的那句才值得多花一次调用）",
          "_multi_item_shape(user_msg)" in blk)
    check("与 ③ 的条件互斥：③ 收 `skill == \"chat\"`、④ 收 `skill not in (\"\", \"chat\")`",
          'decided.skill == "chat"' in _GRAPH_SRC
          and 'not in ("", "chat")' in blk)
    _outer = _GRAPH_SRC.index("(decided.undecided or _asks_data")
    check("④ 也进了外层那一个 `if` 的条件里（不然分支永远到不了——本仓出现过这种"
          "“排在前面的出口让后面的代码永远到不了”的事故）",
          "_acted_no_intents" in _GRAPH_SRC[_outer:_outer + 120],
          repr(_GRAPH_SRC[_outer:_outer + 120]))
    check("摘掉清单伪函数（同一个手法：不是再劝一次，是把那个选项从 schema 里拿走）",
          "deny_pseudo.add(TASK_INTENTS)" in blk)
    check("摘完**重新绑定** llm（只 add 不重绑 = 摘了个没人用的集合）",
          "llm = bind_native(" in blk and "deny_pseudo=deny_pseudo" in blk)
    check("记账用**格**的名字 `intents_backfill`（全量 trace 复扫按它数这一格）",
          'record("planner", "intents_backfill"' in blk)
    check("一次性：`if _acted_no_intents and not correction:` 且随后 `continue`",
          "if _acted_no_intents and not correction:" in blk and "continue" in blk)
    check("正控：③ 那一格**还在**（新加一格不是把旧格换掉）",
          "if _intents_only and not correction:" in _GRAPH_SRC)
    j = _GRAPH_SRC.index("def _auto_task_frames(")
    check("正控：自动登记那一路仍把 `sources` 交下去（没有它，出处对账整条不生效）",
          "sources=_task_goal_sources(state)" in _GRAPH_SRC[j:j + 2000])


# ── ⑤ 「减完还剩几件」与真登记走同一条路 ───────────────────────────────────
class _Decided:
    """`_planner_decide` 的决策对象里，这两个函数用到的那几格。"""

    def __init__(self, skill="", intents=(), declare=None):
        self.skill = skill
        self.intents = list(intents)
        self.declare = declare


def test_left_count_shares_the_registration_path():
    print("\n[共用] `_acted_skills` / `_intents_left_count`：与真登记同一份现场事实")
    check("派下去的那个技能算「办了」（卡一次只装一个 ⇒ 它就是上卡的那件）",
          g._acted_skills({"receipts": []}, _Decided(skill="category_create"))
          == {"category_create"})
    check("`chat` 与零调用（空串 / 没这个属性）不算「办了」",
          g._acted_skills({}, _Decided(skill="chat")) == set()
          and g._acted_skills({}, _Decided()) == set())
    check("回执里的技能也算（多轮里前面办过的那些不再重复登记），且脏回执不会炸",
          g._acted_skills({"receipts": [{"skill": "category_create"}, {"tool": "x"}, None]},
                          _Decided(skill="article_tags"))
          == {"article_tags", "category_create"})

    from agent.principal import Principal
    cfg = {"configurable": {"conversation_id": 20261011,
                            "principal": Principal(uid=721, role="admin")}}
    msg = "想建个新分类叫「临江仙」，文章 23 的标签也想换成「Rust」"
    state = {"messages": [HumanMessage(msg)], "receipts": []}
    check("空清单 ⇒ 0（形状一：点了技能却没交）",
          g._intents_left_count(state, cfg, _Decided(skill="category_create")) == 0)
    check("**清单里只有「正要办的那件」⇒ 也是 0**（形状二，`20261009_032513` 的现场；"
          "写成 `not decided.intents` 的那一版判不出来）",
          g._intents_left_count(
              state, cfg,
              _Decided(skill="category_create",
                       intents=[{"goal": "新建分类「临江仙」", "skill": "category_create"}]))
          == 0)
    check("清单里还有**没上卡**的那件 ⇒ 1（不该催——这一轮该记的已经记着了）",
          g._intents_left_count(
              state, cfg,
              _Decided(skill="category_create",
                       intents=[{"goal": "新建分类「临江仙」", "skill": "category_create"},
                                {"goal": "把文章 23 的标签换成「Rust」",
                                 "skill": "article_tags"}]))
          == 1)
    check("**同一件事换个措辞写两遍 ⇒ 数成两件**（归一化器只管技能闭集与空白，不做 goal "
          "去重；去重只长在「已用 task_hold 显式登记过」那一支，见 `same_goal` 的唯一消费方）。"
          "如实钉住现状——这是**已知边界**，本格不碰它",
          g._intents_left_count(
              state, cfg,
              _Decided(skill="category_create",
                       intents=[{"goal": "把文章 23 的标签换成「Rust」",
                                 "skill": "article_tags"},
                                {"goal": "把文章 23 的标签也换成「Rust」",
                                 "skill": "article_tags"}]))
          == 2)
    j = _GRAPH_SRC.index("def _intents_left_count(")
    check("**与登记走同一个函数**（`intent_frames`）——照着登记那条路另写一份排除规则，"
          "迟早一边松一边紧；数出来的 0 必须等于登记出来的 0",
          "intent_frames(" in _GRAPH_SRC[j:j + 1200])
    k = _GRAPH_SRC.index("def _auto_task_frames(")
    check("**登记那一路也用同一个 `_acted_skills`**（否则两处会各算一份「已办」）",
          "_acted_skills(state, decided)" in _GRAPH_SRC[k:k + 1500])
    check("记账带 `listed`（这一格两种形状的分水岭：0 = 没交，>0 = 交了但只剩它正要办的那件）",
          "listed=len(getattr(decided, \"intents\", ()) or ())" in _GRAPH_SRC[_I_BRANCH:])
    check("纠偏语在这一格**只发一次**（`not correction` 的守卫仍在）",
          "if _acted_no_intents and not correction:" in _GRAPH_SRC)


def main() -> int:
    for fn in (test_multi_item_shape,
               test_task_goal_sources_picks_masters_words,
               test_backfill_nudge_text,
               test_branch_wiring,
               test_left_count_shares_the_registration_path):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
