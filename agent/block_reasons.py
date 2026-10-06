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
