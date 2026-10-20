"""写目标名的**来源态**（20260924）：主人说出口的那几个字才是名字。

秒级、纯函数、无网络无 LLM；由 eval.yml 在 push 时跑。

要治的病（`admin_tag_delete_popup` 现场）：主人说「标签「大笨狗」我不想要了，删掉吧」，
planner 填 `name="删掉吧"`——它**恰是这句话的子串**，于是"值在原话里有据"那道判据放行，
校正不发生，弹卡问成「删除标签「删掉吧」」，而卡片文案是错值进库**唯一的防线**。
同族的还有泛称（`name="标签"`）与"站内正好有这个字"（`name="河灯留言"`）。

判据换成了结构性的：目标名的字面必须能由**三个具名抽取器**从主人这句话里取出来
（引号段 / 免引号目标名 / 目标槽位，见 `_msg_grounded_name`）——**"恰是整句的子串"不再是
出处**。这样"标签就叫「删掉吧」"自然正确（主人加了引号 ⇒ 引号段就是那个字面），
不需要任何"像不像动作短语"的词表分类。

覆盖七块：
  ① 三个抽取器各自取得到什么（正例）；
  ② 真 planner 采样值的校正（20260924 三条现场 + move 族的父子错位）；
  ③ 门本身（`_target_grounding_refusal`）：该拒的拒、话术不越界、五条早退；
  ④ 12 条弹卡用例的**正解不受误伤**（意图正确的 spec 原样通过——这条锁的是反向风险：
     判据收紧了，别把主人说过的话也拒掉）；
  ⑤ 判据里不再有词表（AST 级：`_owner_target_span` 规则④ 与门函数都不引用
     `_GENERIC_NAME_WORDS` / `_TARGET_ACTION_MARKS`——防止有人把补丁式的词表加回来）。
  ⑥ **值**通道的指代（20260927）：命名标记后面跟的是代词时不算名字，来源态判据也不
     认它——代词是原话的子串，那条逐字子串的地基对它本来是失效的（现场重放见下）。
  ⑦ 待办族"引用式唯一命中"的**来源**（20260929 批 G）：候选只来自主人**没加引号**的
     原话（引号里那一段归引号通道），且**没有第二个来源**（签名里没有 planner 的值）；
     行为面（唯一命中才校正、0/多即零写）在 `test_todo_schedule.py` ⑰b 用真门跑。

**令牌层的不变量**（弹卡印出来的那个名字可溯源）在**产物层**锁：需要两轮跑法把
`__CONFIRM__:` 帧里的令牌载荷带回来（`run_case` 的 `confirm_payloads`），随确认轮
评测支持一并落地；本文件是它的离线半边。

用法：.venv/bin/python tests/test_target_grounding.py
"""


from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


import _ctx_src  # noqa: E402  （`_` 开头 ⇒ 不被 run_all 当套件收）

import agent.graph as g  # noqa: E402
from agent.graph import (  # noqa: E402
    _DEICTIC_WORDS,
    _bare_target_name,
    _grounded_value,
    _msg_grounded_name,
    _msg_name_slot,
    _msg_named_value,
    _msg_quote_spans,
    _name_arg_fix,
    _name_like,
    _name_target_fix,
    _target_grounding_refusal,
    _value_clean,
)

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _plan(tool: str, args: dict, workflow: str = "chat") -> dict:
    """一条 spec 的计划对象（门只读 tools 里的参数，够用）。"""
    return {"workflow": workflow, "params": dict(args),
            "tools": [f"{tool}({json.dumps(args, ensure_ascii=False)})"],
            "note": "x", "reply": "y"}


def _args_of(plan_obj: dict) -> dict:
    spec = (plan_obj.get("tools") or [""])[0]
    return json.loads(spec[spec.index("(") + 1:spec.rindex(")")])


def _names_in(func) -> set[str]:
    """函数**代码**里出现的名字（不含 docstring 里的散文提及）。"""
    tree = ast.parse(inspect.getsource(func))
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


from agent.adminops_target import target_mentioned  # noqa: E402
from agent.graph import (  # noqa: E402  # noqa: E402
    _FULLWIDTH_ASCII_MAP,
    _board_quote_fix,
    _fold_typing,
    _ledger_pending_text,
)


def _tn(text) -> str:
    """归一全形（比较前的那一份）：去空白 + 全角 ASCII→半角 + casefold。"""
    return _fold_typing(g._squash_spaces(text))


from agent.tasks import TASK_OPEN_STATES, render_open_tasks  # noqa: E402


def test_target_grounding():
    print("① 三个具名抽取器（引号段 / 免引号目标名 / 目标槽位）")
    _M = "帮我把标签 Asyncio 挪到「编程」下面"
    check("引号段：加引号的那一段原样取出（可多段）",
          _msg_quote_spans(_M) == ["编程"]
          and _msg_quote_spans("我想在「编程」下面加一个二级标签，名字叫「向量数据库」")
          == ["编程", "向量数据库"])
    check("免引号目标名：名词锚与动作锚之间那一段", _bare_target_name(_M) == "Asyncio")
    check("目标槽位：同一段的**原始**字面（脏了也给，供判出处用）",
          _msg_name_slot("帮我把标签「编程」下面那个 Asyncio 删掉") == "「编程」下面那个 Asyncio"
          and _msg_name_slot(_M) == "Asyncio")
    check("出处判定：三种位置任一命中即算取出",
          _msg_grounded_name("编程", _M) and _msg_grounded_name("Asyncio", _M))
    check("「恰是整句的子串」**不算**出处（本轮要拿掉的那条假通道）",
          not _msg_grounded_name("删掉吧", "标签「大笨狗」我不想要了，删掉吧")
          and not _msg_grounded_name("挪到", _M)
          and not _msg_grounded_name("看着有点乱", "帮我把那条写着「泠月喵真棒！」的留言驳回吧，看着有点乱"))
    check("名词前的同指语序也算出处（`大笨狗那个标签`，否则如实追问会自相矛盾）",
          _msg_grounded_name("大笨狗", "大笨狗那个标签我不想要了，删掉吧")
          and not _msg_grounded_name("删掉吧", "大笨狗那个标签我不想要了，删掉吧"))
    _ROLE_MSG_1 = "猫猫给用户niuniu升级为管理员"                              # 20261014T175114 逐字
    _ROLE_MSG_2 = "猫咪告诉用户demo，由于近期活跃度不佳，现将他降级为杂鱼"      # 20261012T081408 前半句逐字（原话还有「，然后你给他身份降级」）
    _ROLE_LEX = g._lexicon("set_account_role")
    check("N 名词紧跟：名字紧贴名词时取得到名字（两次生产现场逐字）",
          _msg_grounded_name("niuniu", _ROLE_MSG_1, [], _ROLE_LEX)
          and _msg_grounded_name("demo", _ROLE_MSG_2, [], _ROLE_LEX))
    check("  中文名**刻意**不认（要认它就得回答「哪一个字算名字」，那是又一张中文词表）",
          not _msg_grounded_name("小猫", "把用户小猫升级为管理员", [], _ROLE_LEX))
    check("  一句话里两个没加引号的名字 ⇒ 说不清，不给值（同 `_bare_target_name`）",
          g._msg_post_noun_name("把用户 A 升级为管理员，另外把用户 B 降级为杂鱼", _ROLE_LEX) == "")
    check("  N 只放宽**取值**，不放宽「这一门要不要介入」（`_name_like` 不读它）",
          "_msg_post_noun_name" not in _names_in(_name_like))
    _ACC = g._lexicon("freeze_account")
    check("纯指代（剥完只剩指代词）⇒ 本门不介入（`_name_like` 假）",
          not _name_like("把那个标签删掉吧")
          and not _name_like("把那个用户降级为杂鱼", _ACC)
          and not _name_like("把 那个 用户 降级", _ACC)
          and not _name_like("把那些标签都删了吧"))
    check("P 语序（名字在名词前）⇒ 本门**介入**（与第 97 条的 `_msg_grounded_name` 同口径）",
          _name_like("大笨狗那个标签删掉吧")
          and _name_like("泠月喵，把jingbao这个用户降级为杂鱼", _ACC)
          # 夹空白的写法也要认（空白不是句读；拉丁账号名两侧常带空格）
          and _name_like("把 jingbao 这个用户降级为杂鱼", _ACC)
          and _name_like(_M))
    check("  出处判定同步：`jingbao` 有据、planner 那个 `niuniu` 无据（现场那对取值）",
          _msg_grounded_name("jingbao", "泠月喵，把jingbao这个用户降级为杂鱼", lex=_ACC)
          and _msg_grounded_name("jingbao", "把 jingbao 这个用户降级为杂鱼", lex=_ACC)
          and not _msg_grounded_name("niuniu", "泠月喵，把jingbao这个用户降级为杂鱼", lex=_ACC))
    print("\n② 真 planner 采样值的校正（20260924 现场）")
    _SENT = "标签「大笨狗」我不想要了，删掉吧"
    for _bad in ("删掉吧", "河灯留言", "标签"):
        po = {"workflow": "tag_delete", "params": {"name": _bad, "level": "one"},
              "tools": [f'delete_tag({json.dumps({"name": _bad, "level": "one"}, ensure_ascii=False)})'],
              "note": "x", "reply": "y"}
        _name_target_fix(po, _SENT)
        check(f"  目标名校正：{_bad!r} → 主人引号里那一段（弹卡印的是它）",
              _args_of(po).get("name") == "大笨狗", str(po["tools"]))
    po = {"workflow": "tag_delete", "params": {"name": "大笨狗", "level": "one"},
          "tools": ['delete_tag({"name": "大笨狗", "level": "one"})'], "note": "x", "reply": "y"}
    _name_target_fix(po, _SENT)
    check("  正解不被改写（防线不是重写器）", _args_of(po).get("name") == "大笨狗")
    po = {"workflow": "tag_update",
          "params": {"name": "编程", "parent_tag": "父标签名", "level": "one"},
          "tools": ['update_tag({"name": "编程", "parent_tag": "父标签名", "level": "one"})'],
          "note": "x", "reply": "y"}
    _name_target_fix(po, _M)
    check("  父子错位两处同修：目标取回 Asyncio、父标签取回「编程」",
          _args_of(po).get("name") == "Asyncio" and _args_of(po).get("parent_tag") == "编程",
          str(po["tools"]))
    print("\n③ 门（_target_grounding_refusal）")
    _CTX = "我想在「编程」下面加一个二级标签，名字叫「向量数据库」"
    po = _plan("create_tag", {"title": "向量数据库", "parent_tag": "父标签名"})
    r = _target_grounding_refusal(po, _CTX)
    check("父标签取不出处 → 拒绝（不回弹卡）", r is not None and r[0] == "create_tag")
    if r:
        why = r[1]
        check("  话术只谈主人这句话：点名主人说过的那几段",
              "向量数据库" in why and "编程" in why, why)
        check("  话术不越界（本层没读过台账，不许说站内/字典/查不到）",
              not any(w in why for w in ("站内", "字典", "查不到", "没有叫")), why)
        check("  话术不替主人下结论（不说\"主人没说过这个名字\"——他可能是换了个语序）",
              "不是主人说出口" not in why and "没说过" not in why, why)
        check("  话术声明零改动", "没有改动" in why)
    check("父标签就是主人点过名的那个 → 不拒", _target_grounding_refusal(
        _plan("create_tag", {"title": "向量数据库", "parent_tag": "编程"}), _CTX) is None)
    _REFUSED = _target_grounding_refusal(
        _plan("set_account_role", {"name": "niuniu", "role": "admin"}), _ROLE_MSG_1)
    check("名字紧跟名词 ⇒ 不拒（20261014T175114 原话逐字）", _REFUSED is None, str(_REFUSED)[:120])
    check("名字紧跟名词 ⇒ 不拒（20261012T081408 原话逐字）",
          _target_grounding_refusal(
              _plan("set_account_role", {"name": "demo", "role": "user"}), _ROLE_MSG_2) is None)
    _GEN = _target_grounding_refusal(
        _plan("set_account_role", {"name": "小猫", "role": "admin"}), "猫猫给用户小猫升级为管理员")
    check("中文名仍拒（fail-closed：N 不认这一格的中文名，宁可问清）", _GEN is not None,
          str(_GEN)[:80])
    if _GEN:
        check("  追问里给出**能走通的那条路**：加引号（与名字长什么样无关）",
              "引号" in _GEN[1], _GEN[1])
        check("  不再说「原样再说一次」（那句话把主人送进死循环）",
              "原样" not in _GEN[1] and "没有加引号点名的名字" not in _GEN[1], _GEN[1])
    check("引号里的目标（公告/留言族）→ 不拒",
          _target_grounding_refusal(_plan("delete_announcement", {"title": "公告"}),
                                    "把标题是「公告」的那条公告删掉吧") is None
          and _target_grounding_refusal(_plan("delete_board_comment", {"quote": "泠月喵好笨啊"}),
                                        "把那条写着「泠月喵好笨啊」的留言删掉吧") is None)
    check("早退① 文章族不在名字表（走 target_* 三条判据）",
          _target_grounding_refusal(
              _plan("set_article_status", {"article_id": 999999, "status": "private"}),
              "文章 999999 那篇我想设成私密，先别急着动") is None)
    check("早退② 多 spec 混排不判",
          _target_grounding_refusal(
              {"workflow": "chat", "params": {}, "note": "x", "reply": "y",
               "tools": ['delete_tag({"name": "删掉吧"})', 'delete_tag({"name": "删掉吧"})']},
              _SENT) is None)
    check("早退③ 不在名字表的写工具（新建公告的标题走值通道，不在这一层判）",
          _target_grounding_refusal(_plan("create_announcement", {"title": "标题"}), _CTX) is None)
    check("早退④ 参数解不出来 → 不判（不是这一层的事）",
          _target_grounding_refusal({"workflow": "tag_delete", "params": {}, "note": "x",
                                     "reply": "y", "tools": ["delete_tag(不是 JSON)"]},
                                    _SENT) is None)
    check("早退⑤ 带 `$tool[N]` 引用的 spec → 不判（取值不来自这句话）",
          _target_grounding_refusal(
              {"workflow": "tag_delete", "params": {}, "note": "x", "reply": "y",
               "tools": ['delete_tag({"name": "$list_tags[0].name"})']}, _SENT) is None)
    print("\n④ 12 条弹卡用例：意图正确的 spec 一律原样通过（不误伤主人说过的话）")
    _POPUPS = {
        "admin_write_natural_confirm_popup":
            ("create_tag", {"title": "秋日随笔", "parent_tag": "", "color": "#eb2f96"}),
        # 20261004 改靶：golden 那条的前提（文章 1＋摄影）生产里双假（`get_article_detail(1)`
        # not_found、「摄影」noteCount 0）⇒ 换成 note 23 +「Python」。表里的值必须与
        # `basic.jsonl` 的原话同步——它是**从原话读出来的正解**，不同步这条锁就成了空断言。
        "admin_write_intent_tag_remove_popup":
            ("set_article_tags", {"article_id": 23, "remove": ["Python"], "add": []}),
        "admin_write_intent_named_target_only":
            ("set_article_status", {"article_id": 999999, "status": "private"}),
        "admin_tag_move_popup":
            ("update_tag", {"name": "Asyncio", "parent_tag": "编程", "level": "one"}),
        "admin_tag_delete_popup":
            ("delete_tag", {"name": "大笨狗", "level": "one"}),
        "admin_category_create_popup":
            ("create_category", {"title": "随手记"}),
        "admin_announcement_create_popup":
            ("create_announcement", {"title": "今晚维护",
                "content": "今晚 23 点开始维护，预计一小时"}),
        "admin_announcement_delete_popup":
            ("delete_announcement", {"title": "公告"}),
        # `admin_board_audit_popup` **撤出本表**（20260930 批 H）：复核那件的目标改成台账
        # 编号（`_ledger_target_refusal`），名字通道的表里已经没有它 ⇒ 留在这里也只是
        # 每跑一次都恒过的一条空断言（函数见工具名不在 `_WRITE_NAME_FIELDS` 里就直接放行）。
        # 它现在有两处真判据：离线那半边在 `test_tag_admin.py` ⑪（台账里在/不在/已复核
        # 三态），端到端那半边是 golden 的 `admin_board_audit_reviewed_refusal`。
        "admin_board_delete_popup":
            ("delete_board_comment", {"quote": "泠月喵好笨啊"}),
        "admin_tag_create_invented_name_popup":
            ("create_tag", {"title": "向量数据库", "parent_tag": "编程"}),
        "admin_tag_create_generic_name_popup":
            ("create_tag", {"title": "夜航船", "parent_tag": "", "color": "#eb2f96"}),
    }
    _cases = {c["id"]: c for c in (
        json.loads(ln) for ln in
        (ROOT / "eval/golden/basic.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip())}
    check("11 条弹卡用例都在（用例文件改名/被删时这里要跟着改）",
          all(cid in _cases for cid in _POPUPS),
          "；".join(sorted(set(_POPUPS) - set(_cases))))
    for cid, (tool, args) in _POPUPS.items():
        case = _cases.get(cid)
        if not case:
            continue
        msg = case["user_input"]
        got = _target_grounding_refusal(_plan(tool, args), msg)
        check(f"  {cid}：正解不被拒（卡片照弹）", got is None, str(got)[:120])
        # 目标名字段（表内那条）逐字段可溯源——"卡片上的名字是主人说出口的字"
        tkey, pkey = g._WRITE_NAME_FIELDS.get(tool) or (None, None)
        for key in (tkey, pkey):
            if key and str(args.get(key) or "").strip():
                check(f"    {cid}.{key}={args[key]!r} 可由主人这句话取出",
                      _msg_grounded_name(str(args[key]), msg))
    print("\n⑤ 判据里不再有词表（AST 级：防补丁式词表加回来）")
    check("_owner_target_span 规则④ 不引用泛称/动作词表",
          not (_names_in(g._owner_target_span)
               & {"_GENERIC_NAME_WORDS", "_TARGET_ACTION_MARKS"}),
          str(sorted(_names_in(g._owner_target_span))))
    check("门函数不引用任何词表",
          not (_names_in(_target_grounding_refusal)
               & {"_GENERIC_NAME_WORDS", "_TARGET_ACTION_MARKS", "_GENERIC_VALUE_WORDS"}))
    check("判据读的是主人原话本身（五个抽取器，全部由名词/引号/句读这些结构锚定）",
          {"_msg_quote_spans", "_msg_name_slot", "_bare_target_name", "_msg_pre_noun_runs",
           "_msg_post_noun_name"} <= _names_in(_msg_grounded_name),
          str(sorted(_names_in(_msg_grounded_name))))
    check("P 以**剥过**的形态参与 `_name_like`（裸的 `_msg_pre_noun_runs` 仍不参与）",
          "_pre_noun_names" in _names_in(_name_like)
          and "_msg_pre_noun_runs" not in _names_in(_name_like))
    check("  剥的规矩住在 `_pre_noun_names` 里（判据与取值同源，改一端会被抓住）",
          "_PRE_NOUN_LEAD_RE" in _names_in(g._pre_noun_names)
          and "_PRE_NOUN_TAIL_RE" in _names_in(g._pre_noun_names)
          and "_DEICTIC_WORDS" in _names_in(g._pre_noun_names))
    print("\n⑥ 值通道的指代（20260927：代词是原话的子串 ⇒ 来源态判据被上游校正器自满足）")
    _DEFECT_MSG = "给我建个新标签，然后把这篇文章的标签换成它"
    check("抽取端：命名标记后面跟的是指代 ⇒ 不是名字（原话实证）",
          _msg_named_value(_DEFECT_MSG) == "" and _value_clean("它") == ""
          and _value_clean("这个") == "" and _value_clean("那些") == "")
    check("引号 = 主人明说的字面量 ⇒ 起名叫「它」仍然合法（判据收窄不误伤）",
          _msg_named_value("把那个标签改名叫「它」") == "它"
          and _value_clean("「它」") == "它")
    check("真名字两端都不受影响",
          _msg_named_value("在「编程」下面建一个二级标签，名字叫「向量数据库」") == "向量数据库"
          and _value_clean("向量数据库") == "向量数据库")
    print("\n⑥b 值通道跨过引号界碑的捕获（20261009：并列两个名字时抽出一串引号碎片）")
    _TWO_NAMES_MSG = "我想在「编程」下面加两个二级标签，名字叫「Tokio」和「Axum」"
    check("抽取端：剥掉两端引号后段里还剩引号 ⇒ 跨了界碑，不是名字（原话实证）",
          _msg_named_value(_TWO_NAMES_MSG) == ""
          and _value_clean("Tokio」和「Axum") == ""
          and _value_clean("Rust」和别的") == "")
    check("不连坐：半角单引号不是界碑（英文名里的撇号照旧）",
          _value_clean("O'Brien") == "O'Brien")
    _two = _plan("create_tag", {"title": "Tokio", "parent_tag": "编程"}, workflow="tag_create")
    _two_params = dict(_two["params"])
    check("现场重放：planner 自己填的值原样留着（不拒、不改，更没有引号碎片）",
          _name_arg_fix(_two, _TWO_NAMES_MSG, role="admin") is None
          and _two["params"] == _two_params
          and "」" not in " ".join(_two["tools"]), str(_two["tools"])[:160])
    check("判据端：指代一律不算有据（就在原话里也不算）",
          not _grounded_value("它", _DEFECT_MSG) and not _grounded_value("那些", "把那些标签删掉")
          and _grounded_value("向量数据库", "名字叫「向量数据库」"))
    po = _plan("create_tag", {"title": "AI Agent"}, workflow="tag_create")
    _r = _name_arg_fix(po, _DEFECT_MSG, role="admin")
    check("现场重放：返回值是零写 + 追问（不是校正）",
          _r is not None and _r[0] == "create_tag", str(_r)[:120])
    check("现场重放：计划里的值一个字没动（没有「它」）",
          _args_of(po).get("title") == "AI Agent", str(po["tools"]))
    check("现场重放：追问文案点名的是模型那个值（不是代词）",
          _r is not None and "AI Agent" in _r[1], (_r or ("", ""))[1][:160])
    po = _plan("create_tag", {"title": "向量数据库"}, workflow="tag_create")
    check("反向：值本来就有据 ⇒ 校正器不介入（防线不是重写器）",
          _name_arg_fix(po, "在「编程」下面建一个二级标签，名字叫「向量数据库」",
                        role="admin") is None
          and _args_of(po).get("title") == "向量数据库")
    check("两端同源（AST 级：抽取器与判据都得认这一族，改一端会被抓住）",
          "_DEICTIC_WORDS" in _names_in(_value_clean)
          and "_DEICTIC_WORDS" in _names_in(_grounded_value),
          f"{sorted(_names_in(_value_clean))} / {sorted(_names_in(_grounded_value))}")
    check("这一族是**封闭类**（穷举得完：短词表，不是词形族的开放扩张）",
          len(_DEICTIC_WORDS) <= 40 and all(len(w) <= 3 for w in _DEICTIC_WORDS))
    print("\n⑦ 待办的引用式唯一命中（20260929 批 G）：候选只来自主人**没加引号**的原话")
    _ROWS = [{"text": "更新简历（国庆后）"}, {"text": "买猫粮"}, {"text": "菜"}]
    check("没加引号的指称 ⇒ 命中那一行（主人说「简历那条」，台账行里含「简历」）",
          [r["text"] for r in g._todo_reference_rows(_ROWS, "把简历那条挪到 10 月 8 号")]
          == ["更新简历（国庆后）"])
    check("★ 同一批字放进引号 ⇒ **不**命中（引号里那一段归引号通道，20260927 的纪律）",
          g._todo_reference_rows(_ROWS, "把「简历」那条挪到 10 月 8 号") == [])
    check("  抹掉空白后仍判得动（主人/模型转写常带空格）",
          [r["text"] for r in g._todo_reference_rows(_ROWS, "把 简历 那条挪到 10 月 8 号")]
          == ["更新简历（国庆后）"])
    check("一行都指向不到 ⇒ 空（调用方按「查无此条」如实拒绝，不替主人挑）",
          g._todo_reference_rows(_ROWS, "把那条挪到 10 月 8 号") == [])
    check("多行被引用 ⇒ **全部**返回（歧义交给调用方拒绝，这里不做「挑一条」这件事）",
          [r["text"] for r in g._todo_reference_rows(
              [{"text": "更新简历"}, {"text": "简历附件"}], "把简历那条挪一下")]
          == ["更新简历", "简历附件"])
    check("只有**一个字**的正文永远不入选（凑不出 2-gram ⇒ 一个字没有指认力）",
          g._todo_reference_rows(_ROWS, "把菜那条挪一下") == [])
    check("★ 没有第二个来源：判据只看主人原话（签名里没有 planner 填的那个值）",
          list(inspect.signature(g._todo_reference_rows).parameters) == ["rows", "user_msg"],
          str(list(inspect.signature(g._todo_reference_rows).parameters)))
    check("  去掉引号段 ≠ 去掉标点（只摘引号里那一段，别的字一个不动）",
          g._msg_without_quotes("把「简历」那条挪到 10 月 8 号").replace(" ", "")
          == "把那条挪到10月8号")
    print("\n⑧ 第二本账：系统台账那一行「待主人点头（还没做）」（20261006 事故）")
    _GSRC = _ctx_src.write_guard_deprefixed()
    check("接线：**四道**闸都吃 `ledger_src`，`planner_node` 一次算好往下传（漏传＝静默回旧行为）",
          "ledger_src" in inspect.signature(_name_arg_fix).parameters
          and "ledger_src" in inspect.signature(_board_quote_fix).parameters
          and "ledger_src" in inspect.signature(g._todo_text_fix).parameters
          # 第四道（20261006 补）：它此前躲过这一撞靠 `_name_like` 早退——是运气，不是设计。
          and "ledger_src" in inspect.signature(g._target_grounding_refusal).parameters
          # 第三本账（20261009）：跨轮任务台账里还挂着的那一行也进同一份原文——与
          # `_ledger_pending_text` 是**一次算好、往下传**的同一条纪律，不是各闸各算。
          and "ledger_src = \"\\n\".join(" in _GSRC
          and "_ledger_pending_text(state.get(\"ledger\"))" in _GSRC
          and "_task_ledger_text(config)" in _GSRC)
    check("  目标名那道闸的**调用点**也真的传了（只改签名不接线＝一行都没生效）",
          "grounded_refuse = _target_grounding_refusal(plan_obj, user_msg," in _GSRC
          and "ledger_src=ledger_src)" in _GSRC)
    check("台账取用只认那一个键（`ledger` 缺席/不是 dict ⇒ 空串，不是异常、更不是 None 串）",
          _ledger_pending_text(None) == "" and _ledger_pending_text({}) == ""
          and _ledger_pending_text({"pending": None}) == ""
          and _ledger_pending_text({"pending": "X"}) == "X"
          and _ledger_pending_text({"executions": "Y"}) == "")
    _TAG = "探针色_20261006"
    _LED = ('新建标签「%s」；动作 create_tag；参数 '
        '[{"args":{"title":"%s"},"tool":"create_tag"}]；状态 awaiting（等主人点头，尚未执行）'
            % (_TAG, _TAG))
    check("值那一族：`嗯` + 台账里记着这个新名字 ⇒ 放行（上一轮那张卡上就是它）",
          _name_arg_fix(_plan("create_tag", {"title": _TAG}), "嗯", role="admin",
                        ledger_src=_LED) is None)
    check("  反向对照：台账缺席 ⇒ 仍然拒（这一支不能因为加了第二本账就松掉）",
          _name_arg_fix(_plan("create_tag", {"title": _TAG}), "嗯", role="admin") is not None)
    check("  反向对照二：台账在、但里面**没有**这个值（模型新编的）⇒ 仍然拒",
          _name_arg_fix(_plan("create_tag", {"title": "AI Agent"}), "嗯", role="admin",
                        ledger_src=_LED) is not None)
    check("  **故意**的宽边界：台账那一行是**渲染过的行**，骨架里的字（如 `awaiting`）"
      "也落在「逐字出现」里 ⇒ 本判据放行——它是行内的字符串判据，不是字段级对照。"
      "代价可接受的理由写在这里：那个值会进弹卡由主人过目（写面没有静默路）",
          _grounded_value("awaiting", "嗯", _LED) is True
          and _grounded_value("created_at_不存在", "嗯", _LED) is False)
    check("  脏判据先判且对第二本账同样生效：指代词即使在台账里出现也不算有据",
          _grounded_value("它", "把它换成那个", "改名叫 它") is False
          and _grounded_value("标签", "标签", "标签") is False)
    check("  `user_msg` 那一支照旧先成立（有第二本账也不改变原判据）；"
      "⚠️ 这个函数的 `sq_msg` 契约是**已归一**的（`_squash_spaces`）——"
      "传原话进去会静默判否，调用点一律先归一",
          _grounded_value("AI Agent", g._squash_spaces("给我建个新标签 AI Agent"), "") is True
          and _grounded_value("AI Agent", "给我建个新标签 AI Agent", "") is False)
    _Q = "今天天气真好"
    _PB = _plan("delete_board_comment", {"quote": _Q}, workflow="board_delete")
    _LED_B = ('删掉河灯集里的那条留言「%s」；动作 delete_board_comment；参数 '
          '[{"args":{"quote":"%s"},"tool":"delete_board_comment"}]；状态 awaiting'
              % (_Q, _Q))
    check("留言那一族：`嗯` + 台账里记着那个片段 ⇒ 放行",
          _board_quote_fix(dict(_PB), "嗯", 0, "admin", ledger_src=_LED_B) is None)
    check("  反向对照：台账缺席 ⇒ 仍然拒（这一族是 %s 里的工具，一定会有 pending 行）"
          % "`_ALWAYS_CONFIRM_TOOLS`",
          _board_quote_fix(dict(_PB), "嗯", 0, "admin") is not None)
    check("  反向对照二：台账行里没有这个片段 ⇒ 仍然拒",
          _board_quote_fix(dict(_PB), "嗯", 0, "admin",
                           ledger_src="删掉河灯集里的那条留言「别的什么」") is not None)
    check("  主人自己加引号给的片段照旧优先（引号通道一个字不变）",
          _board_quote_fix(_plan("delete_board_comment", {"quote": "天气"},
                                 workflow="board_delete"),
                           "删掉「今天天气真好」那条", 0, "admin") is None
          and _args_of(_plan("delete_board_comment", {"quote": "天气"},
                             workflow="board_delete")).get("quote") == "天气")
    _LED_T = ('删除标签「%s」；动作 delete_tag；参数 '
          '[{"args":{"name":"%s"},"tool":"delete_tag"}]；状态 awaiting（等主人点头，尚未执行）'
              % (_TAG, _TAG))
    _RE_MSG = "嗯，「别的」那个先留着，就按你说的办"   # 带引号段 ⇒ `_name_like` 为真
    check("  目标名那一族：这句话里带了个别的名字 ⇒ 早退不成立（所以它此前是**运气**）",
          g._name_like(_RE_MSG) is True)
    check("目标名那一族：台账里记着这个目标名 ⇒ 放行（此前会判成「主人没说过这个名字」）",
          g._target_grounding_refusal(_plan("delete_tag", {"name": _TAG}), _RE_MSG,
                                      ledger_src=_LED_T) is None)
    check("  反向对照：台账缺席 ⇒ 仍然拒（这一支不能因为加了第二本账就松掉）",
          g._target_grounding_refusal(_plan("delete_tag", {"name": _TAG}), _RE_MSG) is not None)
    check("  反向对照二：台账在、但里面没有这个目标名（模型新编的）⇒ 仍然拒",
          g._target_grounding_refusal(_plan("delete_tag", {"name": "AI Agent"}), _RE_MSG,
                                      ledger_src=_LED_T) is not None)
    check("  主人这句话里取不出处**且**台账里也没有 ⇒ 拒（两本账都不认才拒）",
          g._target_grounding_refusal(_plan("delete_tag", {"name": _TAG}),
                                      "删掉「别的」那个",
                                      ledger_src="删掉河灯集里的那条留言「别的」") is not None)
    check("  早退那一条**照旧保留**（纯「嗯」= 一处名字都没标出来 ⇒ 本门不介入）："
      "它是刻意的设计，不是靠它躲过碰撞——上面那条带名字的句子才是原来会撞的形态",
          g._name_like("嗯") is False
          and g._target_grounding_refusal(_plan("delete_tag", {"name": _TAG}), "嗯") is None)
    check("  主人这句话里**位置槽位**取得出处时照旧放行（加第二本账不改变原判据）",
          g._target_grounding_refusal(_plan("delete_tag", {"name": _TAG}),
                                      "把「%s」这个标签删掉" % _TAG) is None)
    check("  第二本账走的是 `_grounded_value` 的同一句 ⇒ 脏判据（泛称/指代）对它同样生效",
          g._grounded_value("标签", "", _LED_T) is False
          and g._grounded_value(_TAG, "", _LED_T) is True)
    _LED_ART = ('修改文章 16 的标签：加上 Git、代码版本管理；动作 set_article_tags；参数 '
            '[{"args":{"add":["Git","代码版本管理"],"article_id":16},'
            '"tool":"set_article_tags"}]；状态 awaiting（等主人点头，尚未执行）')
    check("弹卡的目标闸：台账那一行里印着 id=16 ⇒ 那一篇算有据（第三本账，与值那一族同源）",
          target_mentioned(16, [""] + [_ledger_pending_text({"pending": _LED_ART})]) is True)
    check("  反向对照：台账在、但那一行里没有这个 id ⇒ 仍然无据（有了台账≠整行放行）",
          target_mentioned(99, [""] + [_ledger_pending_text({"pending": _LED_ART})]) is False)
    check("  反向对照二：**两端不挨数字**那条边界对台账同样成立（id=166 不许被 16 冒充）",
          target_mentioned(16, [_ledger_pending_text(
              {"pending": _LED_ART.replace("文章 16", "文章 166")
                                  .replace('"article_id":16', '"article_id":166')})]) is False)
    _LED_WIRE = '+ [_ledger_pending_text(state.get("ledger"))])'
    _W = _GSRC.find(_LED_WIRE)
    _TM = _GSRC.rfind("_target.target_mentioned(", 0, _W) if _W > 0 else -1
    check("  接线：那一行真的并进了 `target_mentioned` 的证据列（只加函数不接线＝没生效）",
          _W > 0 and 0 < _TM < _W and _W - _TM < 2000,
          f"ledger_line={_W} target_mentioned={_TM}")
    print("\n⑨ 键盘噪声归一（20261008）：大小写 / 全角差一格**不算**「主人没说过这个名字」")
    _MSG_LOWER = "把git，代码版本管理挂上去"
    _TAGS_PLAN = _plan("set_article_tags",
                       {"article_id": 16, "add": ["Git", "代码版本管理"]},
                       workflow="article_tags")
    check("★ 主人小写打、字典大写收 ⇒ 有据（现场重放：`Git` 在「把git，…」里）",
          _grounded_value("Git", g._squash_spaces(_MSG_LOWER)) is True
          and _grounded_value("git", g._squash_spaces("把Git，代码版本管理挂上去")) is True)
    check("  全角 ASCII 同理（中文输入法全角态打出的 `Ｇｉｔ` 是同一类转写噪声）",
          _grounded_value("Git", g._squash_spaces("把Ｇｉｔ，代码版本管理挂上去")) is True
          and _grounded_value("10006", g._squash_spaces("新建标签 id=１０００６")) is True)
    check("  现场重放：值那一族**不再拒绝**（校正器不介入，计划原样往下走）",
          _name_arg_fix(_TAGS_PLAN, _MSG_LOWER, role="admin") is None)
    check("  现场重放：**值一个字没动**——正字是字典那一份，改写成主人打的 `git` 是写不进去的",
          _args_of(_TAGS_PLAN)["add"] == ["Git", "代码版本管理"],
          str(_args_of(_TAGS_PLAN)))
    check("  归一**只动比较**：与「同一个词的不同打法」无关的字形一个都不碰（不用 NFKC）",
          _tn("①㎡㈱Ｇｉｔ　１") == "①㎡㈱git1"
          and "normalize" not in _names_in(_fold_typing)
          # 映射表只许覆盖 ASCII 全角区，且键必须是**码位整数**（拿 chr 当键会静默不换字）
          and sorted(_FULLWIDTH_ASCII_MAP) == list(range(0xFF01, 0xFF5F)))
    check("  反向对照一：**换个来路不算有据**——跨轮指代（「重来」里一个字都没有）照旧拒",
          _grounded_value("Git", g._squash_spaces("重来")) is False
          and _name_arg_fix(_plan("set_article_tags",
                                  {"article_id": 16, "add": ["Git", "代码版本管理"]},
                                  workflow="article_tags"), "重来", role="admin") is not None)
    check("  反向对照二：脏判据的入口没被打开（泛称/指代仍是「没出处」，全角归一改不了它）",
          _grounded_value("标签", g._squash_spaces("把标签挂上去")) is False
          and _grounded_value("它", g._squash_spaces("把它挂上去")) is False)
    check("  反向对照三：真没说过还是拒（归一不等于放行）",
          _grounded_value("Axum", g._squash_spaces(_MSG_LOWER)) is False)
    print("\n⑩ 第三本账：跨轮任务台账（20261009 挂标签一步走完的另一半）")
    _TASK_ROW = {"task_id": "at_probe_0001", "state": "running",
                 "goal": "给文章 46 挂上 embeding 标签", "cursor": 0, "total_steps": 2,
                 "steps": [{"tool": "create_tag", "label": "新建标签 embeding"},
                           {"tool": "set_article_tags", "label": "把 embeding 挂到文章 46"}]}
    _CFG_TASK = {"configurable": {"open_tasks": [_TASK_ROW]}}
    _TASK_TXT = g._task_ledger_text(_CFG_TASK)
    check("台账还挂着 ⇒ 那段文本里有 goal 与剩下的步骤",
          "embeding" in _TASK_TXT and "at_probe_0001" in _TASK_TXT, _TASK_TXT[:70])
    check("  **指令块一个字都不进**（给模型看的祈使句不许当出处——否则自由文本类的值"
      "与其中几个字撞上就成了「有来源」）",
          _TASK_TXT == render_open_tasks([_TASK_ROW], with_guide=False)
          and "不要撤下" not in _TASK_TXT and "原样问出来" not in _TASK_TXT)
    check("  终态行不渲染（`succeeded` 不在 `TASK_OPEN_STATES` 里）",
          "succeeded" not in TASK_OPEN_STATES
          and g._task_ledger_text({"configurable": {"open_tasks": [
              {**_TASK_ROW, "state": "succeeded"}]}}) == "")
    check("  读不到 / 形状脏 ⇒ 空串（fail-closed：回到只认前两本账的旧行为，逐字节一致）",
          g._task_ledger_text(None) == "" and g._task_ledger_text({}) == ""
          and g._task_ledger_text({"configurable": {"open_tasks": "不是列表"}}) == ""
          and g._task_ledger_text({"configurable": {"open_tasks": [{"nope": 1}]}}) == "")
    check("★ 现场重放：主人回「要」+ 台账里记着这个名字 ⇒ **不再拒**（此前必拒、零写）",
          _name_arg_fix(_plan("set_article_tags", {"article_id": 46, "add": ["embeding"]},
                              workflow="article_tags"),
                        "要", role="admin", ledger_src=_TASK_TXT) is None)
    check("  反向对照：台账缺席 ⇒ 照旧拒（第三本账是补位，不是放行兜底）",
          _name_arg_fix(_plan("set_article_tags", {"article_id": 46, "add": ["embeding"]},
                              workflow="article_tags"), "要", role="admin") is not None)
    check("  反向对照二：台账在、但那一行里**没有**这个值（模型新编的）⇒ 仍然拒",
          _name_arg_fix(_plan("set_article_tags", {"article_id": 46, "add": ["RAG"]},
                              workflow="article_tags"),
                        "要", role="admin", ledger_src=_TASK_TXT) is not None)
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_target_grounding()
