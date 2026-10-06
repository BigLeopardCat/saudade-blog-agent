"""受阻原因码的**类型表**（20261007）——把 checker 手里那个字符串变成可判读的类型。

## 为什么要有这个模块

`_check_spec` 判 BLOCK 时会给出一个原因码（`unavailable` / `args_parse` / …），那是
**执行侧对失败种类**的判定。但这个判定此前只有两个去处：进 trace，以及喂给 reflector。
**planner 拿不到它**——它只能从错误帧的**那句中文**里猜"刚才发生了什么"，而
"服务这一轮给不出数据"与"你参数写错了"在帧文本里长得一模一样（`unavailable` 那条帧
写的就是一句"服务不可用"）。于是 20261007 那次事故的形态是：planner 把 `unavailable`
当成参数错，原地再点一次同一个调用 ⇒ 同键二次受阻 ⇒ reflector ⇒ `wrap_up` ⇒
主人要办的那件**完全能办**的事整条没有入口（现场与读数见 `docs/问题记录.md` §1.55；
"两版提示词纠偏被 A/B 否掉"的负结果见那里的 1b）。

本模块就是"把类型接上线"的那一格：每个原因码配 `(中文, 改参数重试是否有意义)`。
渲染端（`agent/context.py::blocked_rows`）据此给 planner 一张**常驻的类型表**，
而不是再往纠偏槽里加一句话——20261007 两次 A/B 的教训是"手写散文救不回来"，
而"由机器从原因码判出来的类型"是另一件可以试一试的事。

## 字段语义

`cn`：给访客看的中文。与 `server._REASON_CN`（过程行那张表）**必须同话术**——
两张表分居两侧（server 贴过程行，本表贴喂给 planner 的类型），判据在
`tests/test_block_reasons.py`：**键集合相等 + 逐值相等**。漏登记一个码不是静默，
而是那条用例红。

`retry_param=False` 的意义**不是"别试了"**，而是"这一条不是你没填对"：服务这一轮
给不出数据 / 后台规则不许 / 身份不够——这几种情况下重试同一个调用不会有别的结果。
planner 规则 5 那句"按错误修正参数重试一次"对参数型原因是对的，对这几族是空的。

**fail-safe（刻意如此）**：表里没有的码一律按 `retry_param=True`、中文退回原因码本身。
于是"新加了原因码忘了登记"的后果是**行为不变**（多试一次），而不是被误判成不可重试。
同族教训见 `agent/adminops.py` 里"为什么走错误帧族"那段长注：漏接的后果宁可良性地
退化成「执行出错」，也不能是静默地改变语义。
"""

# 原因码 → (中文, 改参数重试是否有意义)。
#
# 分组只是注释，判据不读分组：**本表的键集合**被测试与 `server._REASON_CN` 对着比，
# 加一个码就要在两边都加（那边还有一条既有判据在锁 target_not_found/unavailable 的
# 字面，所以两侧都是"必须同时改"）。
REASONS: dict[str, tuple[str, bool]] = {
    # ── 参数 / 引用 / 形状：改对了就能过，规则 5 的"改参重试一次"成立 ──────────
    "unknown_tool": ("未知工具", True),
    "args_parse": ("参数解析失败", True),
    "empty_result": ("结果为空", True),
    "error_frame": ("执行出错", True),
    # 参数引用失败（`agent/refs.py::ref_error_reason` 取回，20260919）
    "ref_unknown_tool": ("引用的工具尚未执行", True),
    "ref_unparsed": ("引用的返回不是结构化数据", True),
    "ref_index_range": ("引用的序号越界", True),
    "ref_path_missing": ("引用的字段不存在", True),
    "ref_not_scalar": ("引用取到的不是单个值", True),
    # 写操作目标（`agent/adminops.py::target_error_reason` 取回，20260921）
    # 两条都算"可救"：unknown_target 的下一条路是**先去读一次**、target_mismatch 的
    # 下一条路是**把 id 换成主人点名的那个**——都不是"重试同一个调用"，但都不是死路。
    "unknown_target": ("目标未经确认", True),
    "target_mismatch": ("目标与主人点名的不是同一篇", True),
    # 目标不存在（20260923 三轮，kind=not_found）：与 unavailable 刻意分开——
    # 那个的下一步是"稍后再试"，这个的下一步是"换个 id 或如实问主人"。
    "target_not_found": ("目标不存在", True),
    # ── 环境 / 授权 / 政策：不是"你没填对"，重试同一个调用不会有别的结果 ──────
    "unavailable": ("服务不可用", False),
    "cmd_shape": ("返回格式异常", False),
    "policy_refused": ("后台规则拒绝", False),
    "consent_required": ("未获主人确认", False),
    "denied": ("身份权限不足", False),
    "no_manifest": ("工具未声明权限范围", False),
    "unknown_role": ("身份不明", False),
}

# 表里没有的码：按"还有救"处理（fail-safe，见模块头注）。中文退回原因码本身——
# 那是个**看得见的**缺口（过程行会打出英文码），正是这条设计的意图：宁可难看，
# 不可误判。`tests/test_block_reasons.py` 锁住这条行为。
_FALLBACK: tuple[str, bool] = ("", True)


def block_reason_type(code: object) -> tuple[str, bool]:
    """原因码 → `(中文, 改参数重试是否有意义)`。未登记的码走 fail-safe（见模块头注）。"""
    key = str(code or "")
    cn, retry = REASONS.get(key, _FALLBACK)
    return (cn or key or "执行受阻", retry)


# 永不禁用的技能：`chat` 是"这一轮不需要站内数据"的表达，**不是**一件会受阻的能力，
# 它也从不出现在受阻项里。这条是**防御**：禁用它等于把"如实收尾"这条路也堵死——
# 而"换不了路就如实说"正是这一格想留给 planner 的出口。
_NEVER_DENY = frozenset({"chat"})


def denied_skills(blocked: list | None) -> set[str]:
    """上一轮受阻项里，**这一轮不该再出现在技能菜单里**的技能名（20261007，1d）。

    就是 §1.55 的 1b 末尾写下、1c 结尾点名"未做、需先点头"的那条**菜单层摘工具**
    （主人当时口头叫它"1B"）——本仓的编号顺位给了它 `1d`。

    判据只取"**改参数重试无效**"那一族（`block_reason_type(...)[1] is False`）：
    参数写错、引用越界、目标 id 找不到这些**正是重试的用法**，把它们也从菜单里摘掉
    等于顺手堵掉一条合法路径。

    为什么要在菜单层做（而不是再写一句"别重试"）：同一条禁令此前写过两版、都被 A/B
    否掉——**它没有把"原地重试"变成"改选"，只把"原地重试"变成了"当场放弃"**（见
    `docs/问题记录.md` §1.55 的 1b）。摘掉菜单不一样：模型**没有可再点的东西**，
    只能改选或如实作答。这是这一格与那两版的全部区别，也是它值得单独 A/B 的理由。

    这一族里唯一"看着像不该禁"的是 `consent_required`（未获主人确认）——写操作的正确
    下一步**不是**换技能，而是把确认卡抬起来，而卡是从**计划里的写 spec** 生成的
    （`graph._confirm_popup` 收 `plan["tools"]`），摘了技能岂不是连卡一起摘了？
    核过之后不成立：弹卡与执行用的是**同一条用户消息**判的，且判在**执行之前**
    （`execute_node` 先 `_confirm_popup`、命中就一个工具都不执行）。所以能走到
    `consent_required` 那次 BLOCK 的，必然是**这一轮卡没抬起来**（判成提问/假设、
    或目标无据）——而下一轮还是同一条消息，卡照样抬不起来。**禁它不会丢卡**。

    返回**空集**是常态（无阻碍轮、或障碍属可救族）；调用方据此决定要不要动菜单——
    空集时 schema 逐字节不变。
    """
    out: set[str] = set()
    for b in blocked or []:
        if not isinstance(b, dict):
            continue
        name = str(b.get("skill") or "").strip()
        if name and name not in _NEVER_DENY and not block_reason_type(b.get("reason"))[1]:
            out.add(name)
    return out
