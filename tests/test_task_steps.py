# -*- coding: utf-8 -*-
"""步骤模型带实参（`agent/tasks.py::step_record` / `certify_steps` / `bound_steps`）。

**为什么单独一套**（20261012 批 A）：这一步改的是**跨轮任务的形状本身**——台账里的
`steps` 从"一串工具名"变成"能执行的一串步骤（工具 + 实参）"。它同时被三处读：
下一轮的 planner 提示词（`render_open_tasks`）、结算（`advance_by_receipts` 按工具名
对回执）、以及**父仓 Rust 的落库/读回**（跨语言契约）。形状上的任何一处不自洽，
症状都是"下一轮忽然不认得这件事了"，而现场只剩一行看上去很正常的 trace。

钉住的契约（这里红 = 某条判据被改掉了，改动要同步改这里）：

  · `step_record` 的形状是**加法的**：老两格 `{label, tool}` 进 ⇒ 老两格出（逐字），
    `workflow`/`args` **有才写**。存量行、Rust 的透传、渲染器都靠这一条；
  · `args` **是不是 dict** 就是"这一步有没有绑定实参"：空 dict（`list_tags({})`，
    一次完整无参调用）**照样写**，和"还不知道怎么调"（没有这一格）是两件事；
    值**一个都不改**（实参是要执行的东西，顺手截断＝把一次真写入改坏）；
  · `certify_steps`：落库前自证"序列化后不超列宽、且仍是合法 JSON"。降级阶梯的
    顺序是**先丢显示、后丢执行**（label → args → 最小形状），**绝不丢步骤**
    （`total_steps` 是分母，少一步就是把进度说成假话），每次降级都留痕；
  · `bound_steps` **all-or-nothing**：参数不齐 / 落不出实参 / 有够不到的工具 ⇒ 返回
    `[]`（调用方退回占位步骤＝今天的行为），绝不"能绑几条绑几条"；
  · 实参用的是**执行侧同一个展开器**（`workflows.instantiate_plan`）——两处各推一份
    迟早对不上；
  · `frame_payload` 的键与 `total_steps` 仍与列一一对应（降级之后也是）；
  · **跨语言**：`tasks.TASK_STEPS_COL_MAX` 与父仓 `src/routes/chat.rs` 里那一格同值
    （父仓读不到时按 `tests/_parent_repo.py` 的三分支处理：缺省跳过、设了
    `SAUDADE_REQUIRE_PARENT=1` 就是红）。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）
sys.path.insert(0, str(ROOT))

from agent import tasks as T  # noqa: E402
from agent.tool_specs import split_tool_spec  # noqa: E402
from agent.workflow_params import args_are_bound  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


ROLE = "superadmin"
# 逐字抄自真展开（`instantiate_plan("tag_create", {"name": "临江仙"}, ROLE)`）：
# 参数名还走了既有的一条通道——`name` 被 `_PARAM_ALIASES` 搬到了 `title`。这里不改写它，
# 因为"步骤里的实参必须与**真执行那一次**逐字相同"正是本套件要钉的东西。
BOUND_TAG = [{"workflow": "tag_create", "tool": "create_tag", "args": {"title": "临江仙"}}]


# ── ① 步骤形状：加法的 ────────────────────────────────────────────────────
def test_step_record_is_additive():
    print("\n[形状] `step_record` 对老两格是**逐字**的，`workflow`/`args` 有才写")
    check("老两格进 ⇒ 老两格出（存量行、Rust 透传、渲染器全靠这一条）",
          T.step_record({"label": "开启特效", "tool": "toggle_effect"})
          == {"label": "开启特效", "tool": "toggle_effect"})
    check("缺 label ⇒ 落到工具名上（与 `normalize_declaration` 的老兜底逐字相同）",
          T.step_record({"tool": "toggle_effect"})
          == {"label": "toggle_effect", "tool": "toggle_effect"})
    check("非 dict 的一步（模型写了字符串）⇒ label 用它，工具名留空",
          T.step_record("开启特效") == {"label": "开启特效", "tool": ""})
    check("什么都没有 ⇒ None（不产出空格子）",
          T.step_record({}) is None and T.step_record(None) is None)
    check("`workflow` 有才多一格",
          T.step_record({"label": "建标签", "tool": "create_tag", "workflow": "tag_create"})
          == {"label": "建标签", "tool": "create_tag", "workflow": "tag_create"})
    check("绑了实参 ⇒ 多 `args` 一格",
          T.step_record(BOUND_TAG[0]) == {"label": "create_tag", "tool": "create_tag",
                                          "workflow": "tag_create",
                                          "args": {"title": "临江仙"}})
    # ⭐ 空 dict 与"没有这一格"必须分得开：前者是**已经能调**（`list_tags({})`），
    # 后者是**还不知道怎么调**（占位步）。合流会让"下一步已绑实参"这个判据恒假。
    check("⭐ 空 `args`（无参工具的完整调用）**照样写**，不与「还没绑定」合流",
          "args" in T.step_record({"label": "列表", "tool": "list_tags", "args": {}}))
    check("`args` 不是 dict ⇒ 整格不写（宁可当占位步，也不塞个坏值进列）",
          "args" not in T.step_record({"label": "x", "tool": "create_tag", "args": "临江仙"}))
    check("实参的值**一个都不改**（不截断、不归一：那是在改一次真写入的内容）",
          T.step_record({"label": "x", "tool": "create_tag",
                         "args": {"title": "很长的标题" * 60}})["args"]
          == {"title": "很长的标题" * 60})
    check("列宽截断仍在（label/tool 各自那格）",
          len(T.step_record({"label": "标" * 500, "tool": "t" * 500})["label"]) == T.LABEL_MAX
          and len(T.step_record({"label": "x", "tool": "t" * 500})["tool"]) == T.TOOL_MAX)


# ── ② 落库前自证：列宽 ────────────────────────────────────────────────────
def _size(x) -> int:
    return len(json.dumps(x, ensure_ascii=False))


def test_certify_steps_stays_legal_and_within_width():
    print("\n[列宽] `certify_steps` 保证「序列化后 ≤ 上限」且「仍是合法 JSON」")
    small = [{"label": "开启特效", "tool": "toggle_effect"}]
    check("不超上限 ⇒ 原样返回、零记录", T.certify_steps(small, 4000) == (small, []))

    # ① 丢实参，且**从最大的那条丢起**：一条一条丢到刚好装下 ⇒ 保住尽可能多的可执行步骤。
    three = [{"label": "建标签", "tool": "create_tag", "workflow": "tag_create",
              "args": {"title": "甲" * 3000}},
             {"label": "建分类", "tool": "create_category", "workflow": "category_create",
              "args": {"title": "乙" * 100}},
             {"label": "列出来", "tool": "list_tags", "workflow": "content_query", "args": {}}]
    m1 = _size(three) - 1        # 只差一字符 ⇒ 丢掉**最大的那条**实参就够
    check("构造的现场真的超了上限（否则下面那几条是空跑）", _size(three) > m1)
    got, notes = T.certify_steps(three, m1)
    check("降级后**仍是合法 JSON**（Rust 从中间截断会切出非法串 ⇒ 那一行永远结算不掉）",
          json.loads(json.dumps(got, ensure_ascii=False)) == got)
    check("降级后不超上限", _size(got) <= m1)
    check("**绝不丢步骤**（`total_steps` 是分母，少一步就是把进度说成假话）",
          len(got) == len(three))
    check("⭐ 只丢**一条**实参（贪心：丢最大的那条就够了，不把八条全丢成占位步）",
          notes == ["args_dropped:1"] and "args" not in got[0]
          and "args" in got[1] and "args" in got[2], str(notes))
    check("  丢的正是**最大**的那条（丢小的顶不上、还要多丢一条）",
          got[0]["tool"] == "create_tag" and got[2]["args"] == {})

    # ② 丢光实参还不够 ⇒ 丢 label（纯显示；`_step_line` 本来就会落回工具名）
    labels = [{"label": "标" * 80, "tool": "create_tag", "workflow": "tag_create"}
              for _ in range(3)]
    m2 = _size(labels) - 1
    got2, notes2 = T.certify_steps(labels, m2)
    check("丢光实参后仍超 ⇒ 第二级丢 label", notes2 == ["label_dropped"], str(notes2))
    check("  丢的是 label，工具名/技能名都在（计划仍然可读、可结算）",
          all("label" not in s and s["tool"] and s["workflow"] for s in got2))
    check("  这一级也不超上限、也不丢步骤",
          _size(got2) <= m2 and len(got2) == len(labels))

    # ③ 连 label 都丢不起 ⇒ 最小形状 `{label: 工具名, tool}`。
    #    技能名故意取**比工具名长**的（`content_query` / `list_tags`）：最小形状把
    #    `workflow` 也丢掉，才挤得出这一级；名字一样长就分不出②和③了。
    wide = [{"label": "标" * 80, "tool": "list_tags", "workflow": "content_query"}
            for _ in range(3)]
    no_label_w = [{k: v for k, v in s.items() if k != "label"} for s in wide]
    minimal_w = [{"label": s["tool"], "tool": s["tool"]} for s in wide]
    m3 = _size(minimal_w) + 1
    check("  构造：②装不下而③装得下（否则这一条测的不是第三级）",
          _size(no_label_w) > m3 >= _size(minimal_w))
    got3, notes3 = T.certify_steps(wide, m3)
    check("第三级只剩最小形状（连技能名一起丢），如实留痕",
          notes3 == ["label_and_workflow_dropped"]
          and all(set(s) == {"label", "tool"} for s in got3), str(notes3))

    # ④ 最小形状都装不下（只可能是"上限被人改小"）⇒ 响亮留痕，**不替 Rust 丢步骤**
    m4 = _size([{"label": s["tool"], "tool": s["tool"]} for s in labels]) - 1
    got4, notes4 = T.certify_steps(wide, _size(minimal_w) - 1)
    check("最小形状仍装不下 ⇒ `unfittable`（WARNING + 留痕），步骤一条都不少",
          notes4[-1] == "unfittable" and len(got4) == len(wide), str(notes4))

    exact = [{"label": "x", "tool": "y"}]
    n = _size(exact)
    check("边界：序列化长度**恰好等于**上限 ⇒ 不降级（`<=` 不是 `<`）",
          T.certify_steps(exact, n) == (exact, []))
    check("边界：比上限少一字符 ⇒ 立刻降一级",
          T.certify_steps(exact, n - 1)[1] != [])
    # 降级是**拷一份**再动：`frame_payload` 拿到的是同一个 `decl`，把原对象改掉会让
    # "登记的内容"与"模型说的内容"在内存里就分家（而 trace 里看不出是谁改的）。
    keep = [{"label": "x", "tool": "create_tag", "args": {"t": "甲" * 90}}]
    T.certify_steps(keep, 10)
    check("降级**不改调用方手里那份**", "args" in keep[0] and keep[0]["args"] == {"t": "甲" * 90})


# ── ③ 实参从哪来：与执行侧同一个展开器 ────────────────────────────────────
def test_bound_steps_comes_from_the_same_expander():
    print("\n[绑定] `bound_steps` 的实参与**真执行那次**同源（`instantiate_plan`）")
    check("技能名 + 参数 ⇒ 已绑实参的步骤（含参数名归一那条既有通道）",
          T.bound_steps("tag_create", {"name": "临江仙"}, ROLE) == BOUND_TAG)
    raws = [split_tool_spec(x)
            for x in instantiate_plan("tag_create", {"name": "临江仙"}, ROLE)["tools"]]
    check("与 `instantiate_plan` 的产出**逐字对账**（两处各推一份迟早对不上）",
          T.bound_steps("tag_create", {"name": "临江仙"}, ROLE)
          == [{"workflow": "tag_create", "tool": t, "args": json.loads(raw)} for t, raw in raws])
    check("多步技能 ⇒ 每条都带自己的实参（顺序即执行顺序）",
          [s["tool"] for s in T.bound_steps("ops_report", {}, ROLE)]
          == ["get_server_status", "get_service_health"]
          and all(s["args"] == {} for s in T.bound_steps("ops_report", {}, ROLE)))
    check("⭐ 参数不齐 ⇒ **整件**返回 []（不许「能绑几条绑几条」），调用方退回占位步骤",
          T.bound_steps("tag_create", {}, ROLE) == [])
    check("有够不到的工具 ⇒ 同样整件 []（`allowed` 是角色的闭集）",
          T.bound_steps("tag_create", {"name": "临江仙"}, ROLE, allowed=[]) == [])
    # `$tool[N].field` 是**未解析的参数引用**：它的值要等前面那次调用回来才知道，
    # 本轮现场之外根本取不到 ⇒ 那一步不算"已绑定"（跨轮拿着它去执行 = 拿空值去写）。
    check("⭐ 实参里有未解析的 `$tool[N]` 引用 ⇒ 整件 []（不能脱离现场执行）",
          T.bound_steps("tag_create", {"name": "$tool[0].title"}, ROLE) == [])
    check("  （同一条判据在展开器那一层：`args_are_bound` 单判）",
          args_are_bound({"a": "$tool[0].field"}) is False
          and args_are_bound({"a": None}) is False
          and args_are_bound({}) is True and args_are_bound({"a": 0}) is True)
    check("非 dict 的 params（模型写了字符串）⇒ 按「什么都没给」处理，不炸",
          T.bound_steps("tag_create", "临江仙", ROLE) == [])


# ── ④ 两条登记通道都带上实参 ──────────────────────────────────────────────
SRC = ("想建个新分类叫「临江仙」，文章 23 的标签也想换成「Rust」",)


def test_intents_carry_params_end_to_end():
    print("\n[端到端] 意图清单的 `params` ⇒ 步骤带实参（无 params 时＝今天的行为）")
    base = [{"goal": "建个新分类叫「临江仙」", "workflow": "category_create"}]
    old = T.intents_to_declarations(base, role=ROLE, sources=SRC)
    check("没有 `params` ⇒ 步骤仍是「只有工具名」的形状，**不带 `args`**（向后兼容）",
          bool(old) and all("args" not in s for s in old[0]["steps"]),
          str(old[0]["steps"] if old else old))
    check("  但 `workflow` 那一格写上了（批 A 只写不读；批 B 的占位步要靠它找回本领）",
          old and all(s.get("workflow") == "category_create" for s in old[0]["steps"]))
    check("`normalize_intents` 只在非空时留 `params`（老形状逐字不变）",
          T.normalize_intents({"intents": [{"goal": "g", "workflow": "category_create"}]}, ROLE)
          == [{"goal": "g", "workflow": "category_create"}]
          and T.normalize_intents(
              {"intents": [{"goal": "g", "workflow": "category_create", "params": {}}]}, ROLE)
          == [{"goal": "g", "workflow": "category_create"}]
          and T.normalize_intents(
              {"intents": [{"goal": "g", "workflow": "category_create",
                            "params": {"title": "临江仙", "path_name": "linjiangxian"}}]},
              ROLE)[0]["params"] == {"title": "临江仙", "path_name": "linjiangxian"})
    withp = [{"goal": "建个新分类叫「临江仙」", "workflow": "category_create",
              "params": {"title": "临江仙", "path_name": "linjiangxian"}}]
    new = T.intents_to_declarations(withp, role=ROLE, sources=SRC)
    bound = T.bound_steps("category_create",
                          {"title": "临江仙", "path_name": "linjiangxian"}, ROLE)
    check("有 `params` ⇒ 步骤里的实参与 `bound_steps` 逐字相同（同源）",
          bool(new) and new[0]["steps"] == [{"label": s["tool"], "tool": s["tool"],
                                             "workflow": s["workflow"], "args": s["args"]}
                                            for s in bound],
          str(new[0]["steps"] if new else new))
    check("⭐ 登记范围没变：有/无 params 两条路的**工具集合相同**（多的只是实参）",
          old and new and [s["tool"] for s in old[0]["steps"]]
          == [s["tool"] for s in new[0]["steps"]])
    check("参数不齐（缺必填）⇒ 退回占位步骤，**不登记一个空计划**",
          (lambda d: d and len(d[0]["steps"]) > 0
           and all("args" not in s for s in d[0]["steps"]))(
              T.intents_to_declarations(
                  [{"goal": "建个新分类叫「临江仙」", "workflow": "category_create",
                    "params": {"path_name": "linjiangxian"}}], role=ROLE, sources=SRC)))


def test_frame_payload_certifies_before_landing():
    print("\n[发帧] `frame_payload` 是两条登记通道**唯一**的帧出口 ⇒ 闸设在这一处")
    decl = T.normalize_declaration({"goal": "带我过去后开启一个特效", "steps": BOUND_TAG})
    audit: dict = {}
    pl = T.frame_payload(decl, 42, audit)
    check("八格仍与 `agent_task` 的列一一对应（多一格就多一份跨语言契约）",
          list(pl) == ["task_id", "goal", "steps", "total_steps", "cursor", "state",
                       "pending_question", "idempotency_key"], str(list(pl)))
    check("`total_steps` ＝ 落库那份 `steps` 的长度（降级之后也一致）",
          pl["total_steps"] == len(pl["steps"]) == 1)
    check("步骤里的实参原样进了帧（登记那一刻的值就是执行那一刻的值）",
          pl["steps"][0]["args"] == {"title": "临江仙"} and audit.get("steps_degraded", 0) == 0)

    decl2 = T.normalize_declaration(
        {"goal": "建三个标签", "steps": [
            {"label": "建标签", "tool": "create_tag", "workflow": "tag_create",
             "args": {"title": "很长" * 4000}}] * 3})
    audit2: dict = {}
    pl2 = T.frame_payload(decl2, 42, audit2)
    check("降级**不静默**：读数记上、`steps` 仍合法且不超列宽",
          audit2.get("steps_degraded") == 1
          and len(json.dumps(pl2["steps"], ensure_ascii=False)) <= T.TASK_STEPS_COL_MAX)
    check("  `total_steps` 跟着**落库那份**走（不算被砍掉的步骤，也不虚报）",
          pl2["total_steps"] == len(pl2["steps"]) == 3)


# ── ⑤ 跨语言：列宽常量必须同值 ────────────────────────────────────────────
def test_steps_col_max_mirrors_parent_repo():
    print("\n[跨语言] `tasks.TASK_STEPS_COL_MAX` 与父仓 `chat.rs` 那一格同值")
    import _parent_repo  # noqa: E402

    src = _parent_repo.read(
        "src/routes/chat.rs",
        why="`steps` 列宽是跨语言契约：父仓从中间截断（`chars().take(N)`），agent 侧"
            "按同一个数自证「序列化后不超上限」。只改一边＝一边在截、一边以为没截，"
            "截出来的串不是合法 JSON ⇒ 那一行永远结算不掉")
    if src:
        m = re.search(r"const\s+TASK_STEPS_COL_MAX:\s*usize\s*=\s*(\d+)", src)
        check("父仓里真有这个常量（名字一改，这里的镜像就成了没人守的孤本）",
              m is not None, "src/routes/chat.rs")
        if m:
            check("两侧同值", int(m.group(1)) == T.TASK_STEPS_COL_MAX,
                  f"父仓 {m.group(1)} / agent {T.TASK_STEPS_COL_MAX}")
        # 上限必须**小于** `text` 的字节上限 ÷ 4：`.chars().take()` 数的是字符，
        # 而 MySQL `text` 限的是字节（65535），utf8mb4 下 1 字符 ≤ 4 字节。
        check(f"取值仍在 `text` 列的安全区内（{T.TASK_STEPS_COL_MAX}×4 < 65535）",
              T.TASK_STEPS_COL_MAX * 4 < 65535)


def test_module_does_not_import_graph():
    print("\n[结构] tasks 不许 import agent.graph（会成环）")
    src = (ROOT / "agent" / "tasks.py").read_text(encoding="utf-8")
    hits = [ln for ln in src.splitlines()
            if "agent.graph" in ln and ln.lstrip().startswith(("import ", "from "))]
    check("没有 agent.graph 的 import", not hits, str(hits))


if __name__ == "__main__":
    for fn in (test_step_record_is_additive,
               test_certify_steps_stays_legal_and_within_width,
               test_bound_steps_comes_from_the_same_expander,
               test_intents_carry_params_end_to_end,
               test_frame_payload_certifies_before_landing,
               test_steps_col_max_mirrors_parent_repo,
               test_module_does_not_import_graph):
        fn()
    print("\n" + ("全部通过 ✅" if not FAILS else f"失败 {len(FAILS)} 项 ❌: {FAILS}"))
    sys.exit(1 if FAILS else 0)
