"""工具结果的形状：`ToolResult`、信封构造器、列表帧塑形件。

20261017 从 `tools/base.py` 整段搬出。

## 这里为什么是一个独立模块

`tools/base.py` 起步 **5845 行**（全仓第二大体量文件，仅次于 `agent/graph.py`）。这一族
（341 行）是它唯一**实测出边为零**的一段——不引用 `tools/base.py` 里任何其他顶层名
⇒ 搬得动，且搬完不成环。搬走后 `tools/base.py` 按**原名**再导出这里的一切
（`from tools.base import ok, tgt, ...` 一处不改）。

## 这一族管什么

工具的返回值不是裸数据，是**两类之一**（20260916 加固，动机见下方长注）：

  · **正常内容**——人话文本，进帧；
  · **结构化信封**——`ToolResult`，带 `ok` / `empty` / `unavailable` / `not_found` 四种
    **失败取向**，与 `fact` / `tgt` 两个**事实信封**（写工具的"有没有净改动 / 动的是谁"）。

再加一撮**列表帧塑形件**：`_cap_rows`（行数封顶）、`_clip_fields`（自由文本单值封顶）、
`_board_text_keys`（公开行 `author` → `留名`）、`_label_id_keys`（上游字段名 → 命名空间名）。

## 刻意**不**在这里的东西

`API_BASE` / `SITE_BASE` / `_site_base` / `_client` / `_get` 留在 `tools/base.py`。
理由不是"它们更像底座"，是**可测性**：五个套件直接给 `tools.base._client` 打桩
（假 httpx 客户端），把 `_client` 搬走会让那些桩**静默失效**、测试真的去发网络请求
——本仓最贵的失效就是「看着在跑、其实没跑」。
"""

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外；
# `tests/test_module_exports.py` 按这条规则机械判着，漂了就红）。
__all__ = ["ToolResult", "ok", "empty", "unavailable", "not_found", "invalid_args", "TGT_KINDS",
           "tgt", "fact", "is_noop", "UPSTREAM_DOWN", "error_frame"]

# ── 工具返回值的结构化"两类"（20260916 加固）──
# 背景：工具失败时返回的是**人话字符串**，与正常内容同型；`_get` 更是把所有上游故障
# 吞成 `[]`。"当前用户还没有绑定任何 IoT 设备"（空结果）与"查询设备列表失败:
# Connection refused"（服务不可用）在 checker 眼里都是"非空文本" ⇒ 双双 PASS 并被记成
# **系统确认事实**（receipt）——于是"服务挂了"被当成"查到了、就是空的"进了跨轮执行
# 记忆，narrator 也可能照着念。
#
# 最小解：返回值**仍然是字符串**（人话，给 narrator 与用户看，下游所有 `str()`/
# 切片/拼接照旧），只是多带一个机器可读的 kind：
#   ok          正常数据
#   empty       服务正常、结果就是空（"你还没绑定设备"是**事实**，照常 PASS 进回执）
#   unavailable 服务不可用 / 鉴权失败 / 超时（**不是事实**：checker 判 BLOCK，
#               planner 据 reason=unavailable 决定重试还是如实告知）
# 只覆盖"能碰外部服务或可能查空"的工具；命令类工具（导航/特效/暗色）另有一条契约
# ——连线命令走 `meta["cmd"]`（见 navigate_to 的长注），`_check_spec` 的 cmd_shape
# 按它校验，**返回文本里不再有命令标签**。屏显（device_oled_display）不走这条。
class ToolResult(str):
    kind: str = "ok"
    # 机器可读的副产物（20260921）：给**写操作**用——文本是给人/narrator 看的人话，
    # `meta` 是给回执（execution_log.detail）看的**结构**：谁执行的、改了什么字段、
    # 从什么变成什么。为什么不让 narrator/Rust 去解析人话：那是在拿正则啃中文，
    # 改一次措辞就静默失配（回执是落库的审计，不能靠"读起来差不多"）。
    # 与 kind 同一条生命线，经 `.invoke()` 透传（test_reports/test_admin_write 有锁）。
    meta: dict = {}
    # **结构化载荷**（20261019 批 G1）：文本是它的**渲染**，它是文本的**本体**。
    #
    # 为什么要有它：`_shape` 的出口此前是 `str(data)`，于是每一个想读结构的消费方
    # （参数引用 `$x[0].y`、实体摘要、跨轮指代锚点）都得把那个 repr **再解析回来**
    # ——`agent/arg_refs.py::parse_data` 的 `literal_eval` 兜底、`entity_digest._parse`、
    # `deterministic_plan._doc_title` 的正则，全是同一件事的不同写法。结构本来就在手上，
    # 丢掉的只有"它在哪一格"。
    #
    # 与 `meta` 的分工别混：`meta` 是**给回执/跨语言契约**看的**小**结构（op/changed/
    # target/…，`_RCPT_META_KEYS` 是白名单）；`data` 是**工具返回值本身**（列表/字典，
    # 可能就是几十行），只在本进程里传，**不落回执、不跨语言**。
    #
    # 缺省 `None` 而不是 `{}`：`None` 是"这个返回**没有**结构化载荷"（固定告知语、
    # 纯文本），消费方据此回落文本解析；`{}` 会伪装成"有载荷，只是空的"。
    data: object = None

    def __new__(cls, text: str, kind: str = "ok",
                meta: dict | None = None, data: object = None) -> "ToolResult":
        obj = str.__new__(cls, text)
        obj.kind = kind
        obj.meta = dict(meta) if meta else {}
        obj.data = data
        return obj


def ok(text: str, meta: dict | None = None, *, data: object = None) -> ToolResult:
    return ToolResult(text, "ok", meta, data)


def empty(text: str, meta: dict | None = None, *, data: object = None) -> ToolResult:
    return ToolResult(text, "empty", meta, data)


def unavailable(text: str, meta: dict | None = None, *, data: object = None) -> ToolResult:
    return ToolResult(text, "unavailable", meta, data)


def not_found(text: str, meta: dict | None = None) -> ToolResult:
    """目标不存在 / 不属于你（20260923 三轮）——**第三个失败族**，与 unavailable 分开。

    为什么必须分开（trace `20260923T130033_9` 实证）：planner 拿"条数"当 id 填进
    `ids:[3]`，服务端按 3 匹配 0 行，写后复核（未读数没降）如实报"未确认生效"——
    但那是 `unavailable`，过程行按 `_REASON_CN` 显示成**「服务不可用」**。用户看到
    的是"系统挂了"，而真问题是"你要标的第 3 条根本不存在"。两者的**应对**也相反：
    服务不可用 = 稍后再试；目标不存在 = 换个 id 或如实问主人（planner 拿到这个
    原因码才会去读列表/追问，而不是重试同一条）。

    与 `empty` 也分开：empty 是"查到了、就是空的"（是事实，checker 判 PASS 进回执），
    这里是"你要动的东西不在我能确认的范围内"（不是"没有这条"的结论——可能是列表
    只回最近 100 条，措辞里必须如实交代这个边界）。
    """
    return ToolResult(text, "not_found", meta)


def invalid_args(text: str, meta: dict | None = None, *, data: object = None) -> ToolResult:
    """工具**自己**判出参数不合法（20261019 批 ⑤-1）——第四个失败族。

    与 `args_parse`（`_check_spec` 在 spec 解析那一步给的码）分开：那个说的是"计划里
    那一行根本解析不出参数"，这个说的是"参数解析出来了，但值不合法"（屏文超 64 字、
    设备 id 格式不对）。两者对 planner 的**应对相同**（改参数），但过程行与 `blocked`
    的类型要说得准——`args_parse` 的文案是「参数解析失败」，用在一个已经解析成功的
    参数上是**谎**。

    为什么**必须**有它、不能拿 `unavailable` 顶（20261019 实测）：`device_oled_display`
    的"屏文超 64 字"此前是一条**裸字符串**，`execute_node` 那一行 `getattr(out, "kind",
    "ok")` 把"没声明"读成"声明了 ok" ⇒ checker 判 PASS ⇒ 回执「屏幕显示「…」」落
    `execution_log` ⇒ 下一轮注回提示词 ⇒ planner/narrator 读成**系统确认的事实**。
    换 `unavailable` 能拦住回执，但过程行会显示「服务不可用」——而设备好好的。
    """
    return ToolResult(text, "invalid_args", meta, data)


# ── 写工具的**事实信封**（20260930 · F1）────────────────────────────────────
# 写工具的 `meta` 从此统一由 `fact()` 构造，形状固定：
#
#   op         动作码（`tag_create` / `board_audit` / `account_freeze`…），既有键不变
#   changed    这一下**对站内数据有没有净改动**（noop = 现状即目标 ⇒ False）
#   target     动的是谁：`{"kind": …, "id": …, "name": …}`（kind 取闭集，见 `TGT_KINDS`）
#   before/after  目标**状态**的短语（"待审"→"已通过"、"剩 499/500"→"恢复到上限"）
#   evidence   写后读回复核时读到的那一句（有则填）
#   noop       **自动**：`changed=False` 时补上（`fact()` 里一处派生，别手写）
#   其余既有键（change / tag_id / article_id / …）原样带着
#
# 为什么要有它（结构动机，别读成"多加几个字段"）：
#   · `changed` 是**唯一**能让上层判"这一下到底动没动站内数据"的字段。此前这个事实
#     只能靠"工具有没有写 `noop: True`"反推，而 30 件写工具里只有 12 件写得出那张
#     证书：剩下 18 件（三类目、公告增删、额度三件、待办新增、账号通知…）**从不声明**。
#     它们一旦走了"现状即目标"那条短路，上层只能按"改了"处理 ⇒ gate 的洞⑩（真改了
#     却说没改）会把一句**如实**的"什么都没动"判成打回。反向同理：一份"改了"的声称
#     也失去了可核对的前提（洞③）。
#   · `target` 把"动的是谁"从参数里解放出来：`action_text` 渲染、台账对账、将来 F2 的
#     事实块都读它，而不是各自去 args 里按字段名猜（现在有六种猜法）。
#
# 三条纪律（每一条都由一次真实故障换来）：
#   ① **既有键一个都不许改**：`op`/`change`/`before`/`after`/`tag_id`… 是 Python 写 /
#      Rust 读的跨语言契约（`graph._RCPT_META_KEYS` ⇄ `src/routes/chat.rs::render_exec_row`）。
#      `fact()` 只**加**键（`changed`/`target`/`evidence`），加出来的键**不进**
#      `_RCPT_META_KEYS` ⇒ 不跨语言、不需要 Rust 同步。**把它们加进白名单才是破坏契约
#      的那一步**（Rust 侧会开始读一个它没排版的键）。
#   ② `changed=False` 必须**如实**：它是"我确实核对过现状、与目标一致"的断言，不是
#     省事的默认值。走真写路径的工具一律 `changed=True`（写请求发出并被读回复核过），
#     没发请求/发了但读回没变（那种走 unavailable）才 False。
#   ③ `before`/`after` 只放**状态词**，不放句子、不复述工具返回正文里的话——它们是给
#     结构与台账读的。拿不到精确值就如实写"存在/不存在""已删除"，**不许编**。
TGT_KINDS = ("note", "tag", "category", "announcement", "board_comment",
             # 文章评论（20261019）：与 `board_comment`（河灯留言）**并列而不是合并**
             # ——两者是不同的表、不同的后台页，读端要按 kind 分派时，"这是评论还是
             # 留言"正是它唯一要说的事（合并成一个 kind 就再也分不开了）。
             "comment", "todo",
             "user", "quota_request", "notice", "message", "device", "page")


def tgt(kind: str, ident=None, name: str = "") -> dict:
    """事实信封里的"动的是谁"（`fact(target=…)` 的实参）。`kind` 取 `TGT_KINDS`。

    闭集而非自由字符串：读端（台账渲染、F2 的事实块）要按 kind 分派，写端各造一个词
    就是又一次"人工同步"。id 拿不到（新建类工具在写之前还没有 id）就只带 name——
    **空的 id 不许填 0**（0 在号段里是合法值，会被读成"某个真的物件"）。"""
    out: dict = {"kind": kind}
    if ident is not None:
        out["id"] = ident
    if name:
        out["name"] = name
    return out


def fact(op: str, *, changed: bool, target: dict | None = None,
         before: str = "", after: str = "", evidence: str = "", **extra) -> dict:
    """写工具的**事实信封**（契约与动机见上方长注）。返回给 `ok(..., meta=fact(…))`。

    `extra` 是既有的那些跨语言键，**原样带上**（`fact()` 只加不改）。"""
    env: dict = {"op": op, "changed": bool(changed)}
    if target:
        env["target"] = target
    if before:
        env["before"] = before
    if after:
        env["after"] = after
    if evidence:
        env["evidence"] = evidence
    if not changed:
        env["noop"] = True
    env.update(extra)
    return env


def is_noop(meta) -> bool:
    """事实信封 → "这一下对站内数据有没有**净改动**"（写工具的**唯一读端**）。

    读 `changed`（F1 之后每件写工具都带，由 `fact()` 派生）；只为兼容才认老键
    `noop`（它现在是 `changed=False` 的同义词，见 `fact()`）。

    **两样都没有 ⇒ 按"改了"处理**——这是刻意的安全方向：两个读端（gate 洞⑩ 的
    "这一轮什么都没改"假阴性、planner 的零改动重复裁剪）都只在**敢确定零改动**时
    才动作，认不出的工具放过即可，绝不把一句诚实的叙述判成谎。所以这里是
    `return False` 而不是抛错或返回 None。
    """
    if not isinstance(meta, dict):
        return False
    ch = meta.get("changed")
    if isinstance(ch, bool):
        return not ch
    return bool(meta.get("noop"))


def error_frame(text: str, reason: str) -> ToolResult:
    """`__ERROR__` 帧 + 它的**原因码**（20261019 批 G4）。

    帧文本本来就是**渲染**：原因码此前只长在它中间那对方括号里（`权限不足[scope_denied]`），
    于是唯一的读端 `graph._check_spec` 只能拿七个 `*_error_reason` 正则/子串把它抠回来
    ——**改一句措辞就静默退化成笼统的 `error_frame`**，而 planner 的应对（改参数？去问主人？
    如实告知？）正是按这个码分叉的。生产方手上本来就有那个常量，这里只是给它一格。

    ⚠️ **kind 仍是 `ok`，不新增 kind**：`_check_spec` 是一条显式 if 链，新 kind 漏接分支
    会 fallthrough 到"文本非空 ⇒ PASS" ⇒ 产回执 ⇒ 进 execution_log ⇒ 下一轮把一次
    **被拒绝的**写当成已完成的事实（见 `agent/adminops_target.py` 里 policy_frame 的长注：
    那是本族最不能出的错，而且完全静默）。`__ERROR__` 文本逐字节不变 ⇒ 线格式、Rust 的
    前缀分类、gate 分支 5a（错误帧 + 完成式声称）全都不动。

    ⚠️ **已经是 `ToolResult` 的别再套一层**：`ToolResult(x)` 会新建一个对象、把 `meta`
    丢掉（它不像 `_shape` 那样有身份透传）。调用方拿到本函数的返回值应当**直接返回**。
    """
    return ToolResult(text, "ok", {"reason": reason})


# 上游故障哨兵：`_get` 失败时返回它而不是 `[]`（"故障伪装成空"的源头就在那）。
# 工具的出口要用 `_shape(data)` 而不是 `str(data)`——理由见 _shape 的注释。
UPSTREAM_DOWN = unavailable("服务暂时不可用，请稍后再试")


# ── 闭集参数 = `Literal[...]`（20260926 批 5，参数 schema 化缺的那半边）──
# 有些参数是**闭集**：只认几个固定取值，别的一律无效。此前这个闭集只活在两处
# **散文**里——`Annotated` 的说明文字 + 工具体内的判据，而且那判据四种形态各异：
#   `toggle_effect` 的 effect/action  → `if x not in (...)` 显式拒（见本文件 :670）
#   `get_article_detail` 的 doc_type  → 查表 `KeyError`（表在工具体内）
#   `toggle_dark_mode` 的 mode        → 无判据（非 "on" 一律当 off）
#   `get_moderation_status` 的 status → 无判据（落回混合视图，**静默**）
# 于是机器侧没有任何地方读得到它：`workflows.check_workflow_params` / `check_call_args`
# 只能判"类型对不对"，判不了"值在不在闭集里"（`agent/workflow_params.py` 那段"枚举没有来源"
# 的注记说的就是这件事）。后两种形态正是"臆造一个看着合理的值"最坏的去处：
# 一个静默给出另一份数据、一个白烧一轮。
# 现在把闭集**写在类型上**（`Literal[...]`）：pydantic 会把它渲染进工具的 JSON
# Schema（`{"enum": [...]}`），参数校验层从 `tool_arg_schemas()` 直接派生 ⇒
# **单一来源，没有第二份手写名单**（手写名单是漏项来源，同 20260913 / 20260925
# 两次教训）。校验被拒时走"参数不齐"那条友好的同轮纠偏（零工具 + 一句列出合法
# 取值的事实），**不是** pydantic 的 `__ERROR__` 帧。
#
# 两条边界（都实测过，别顺手"统一"）：
#   · **带别名归一层的参数刻意保持 `str`**：`audit_board_comment.verdict` /
#     `set_article_status.status` / `update_tag.to_level` / `delete_tag.level` 都
#     接受中文或口语写法（`通过`/`公开`/`一级`/`1`，见 `adminops._*_ALIASES`），
#     而 **pydantic 在工具体之前跑**——写成 `Literal["pass","reject"]` 会让
#     `通过` 在归一化那一步之前就被拒，把一条刻意做出来的容错通道关死。
#     那几族的闭集来源仍是 `adminops` 的别名表（本来就是单一来源）。
#   · 工具体内原来的判据**保留**（纵深）：本文件以外的直接调用方（测试、内部重构、
#     将来新增的通道）不经过参数校验层，那一层仍然兜底——`toggle_effect` 的
#     `if x not in (...)` 不删；`doc_type` 的查表 `KeyError` 也不改成静默兜底
#     （静默兜底会把它变成和 `status` 同一种坏法）。


def _shape(data, *, meta: dict | None = None) -> str:
    """`_get` 结果的统一出口。**别写 `str(data)`**：`str()` 作用在 str 子类上会退化成
    普通 str（CPython 行为），kind 标记就丢了——20260916 加 kind 时出过这个错，
    单测里有一条专门盯"经 .invoke() 透传后标记仍在"。

    出口另做一件事：**把行里的上游字段名换成命名空间名**（见 `_FRAME_ID_KEYS`）。
    出口只有这一个，所以帧、参数引用取值源（`graph.py` 的 `parse_data(str(out))`）
    与实体摘要（`entities.receipt_digest`）三处**同时**看到同一个名字。

    **20261019（批 G1）：结构不再在这里被丢掉。** 出口从"一个普通 str"变成
    `ToolResult(渲染文本, data=改过名的那个结构)`——文本逐字节不变（所有 `str()`/
    切片/拼接的调用点一个字都不用改），但下游要读结构时直接拿 `.data`，不必再把
    repr 解析回来。取法是 `agent/arg_refs.py::structured_of(out)`。

    `data` 存的是**改过名的**那份（与文本同源）——存原始行会让"帧里叫 talkId、
    取值源里叫 talkKey"这对老问题从另一个门进来。

    `meta`（批 G2）只给**列表类**工具用：分页页码（`_cap_rows` 的 `_page`）随它
    出去，跨轮实体摘要就不必再从中文字注记里正则抠「共 N 条」。它不是
    `_RCPT_META_KEYS` 的成员 ⇒ **不落回执、不跨语言**（白名单在 `graph.py`）。
    """
    if isinstance(data, ToolResult):
        return data
    relabeled = _label_id_keys(data)
    return ToolResult(str(relabeled), "ok", meta, relabeled)


# 线上 API 的行字段名（`talkKey`/`noteKey`/…）与我们给模型看的**命名空间名**
# （`talkId`/`noteId`/…）不是一个词。同一个数字在帧里叫 Key、在回执与实体摘要里叫 Id
# ——trace 实证：模型据此判断站内有"两套号段"，并拿 `talkKey` 的号去问另一族的工具
# （20260924T030031 通知帧那条也是同族：id 不带来源只能猜）。
#
# 判据是**同一个数字只以一种身份露面**（`tests/test_id_namespaces.py` 的三组锁）：
# 命名空间名是刻意造出来的那一个（`talkId:`/`userId:`/`notifId:`/`mailId:` 已在
# 回执行/明细行/实体摘要里用了一整批），所以统一方向是 Key → Id，反过来会推翻那一批。
#
# 边界（别顺手扩大）：
#   · **只改键名，不改值**，也不碰 JSON 结构——值仍是上游给的那个数字；
#   · 只管"给模型看的渲染文本"。上游原始行（`_get` 的返回值）保持原样：`_board_index`
#     / `_note_index` / `adminops` 这些内部索引读的仍是 Key 名，它们的产物再出口时才换。
#   · `announcement` 的 id 上游就叫 `id`（没有 Key 形），不在表里——不编不存在的别名。
_FRAME_ID_KEYS = {
    "talkKey": "talkId",
    "noteKey": "noteId",
    "tagKey": "tagId",
    "categoryKey": "categoryId",
    "fatherKey": "fatherId",
    "friendKey": "friendId",
}


def _board_text_keys(rows: list) -> list:
    """公开留言/说说行：把上游的 `author` 改名成 `留名`（**只改键名，不动值**）。

    理由与 `_FRAME_ID_KEYS` 同一条纪律——**帧里同一个字面只以一种身份露面**：
    上游这个字段装的是留言人**自己在留名框里填的字**（自由文本，可留空=匿名），
    字段名却叫 `author`，读的人（与模型）会直接把它当"发表账号"。trace
    `20261001T005722` 实证：那条留言的公开行是 `'author': 'Sora Saudade'`，
    模型据此讲成"ID 100 的账号是 sora"——而**留名是人能随便填的字**，账号只有
    后台名册（放灯必须登录）才有。改名后帧自己会说清它是什么；要认人走
    `list_admin_board`。
    """
    out = []
    for r in rows:
        if isinstance(r, dict) and "author" in r:
            r = {("留名" if k == "author" else k): v for k, v in r.items()}
        out.append(r)
    return out


def _rows_with_note(rows: list, note: str) -> list:
    """列表帧尾补一条**系统注记**（给模型读的边界事实）。

    为什么放在 list **里面**而不是拼在文本外面：帧文本还是 `$ref` 的取值源
    （`graph.py` 的 `parse_data(str(out))`）——外面拼一行说明会让 `literal_eval`
    整个失败、引用退化成 `ref_unparsed`。而多出来的**非 dict 元素**天然被结构化
    消费者跳过（`entities._rows` 按 `isinstance(x, dict)` 过滤、`decisions` 的候选
    扫描同样带 isinstance 判断），下标寻址（`$list_guestbook[0].content`）也不动。
    """
    return list(rows) + [f"〔系统注记〕{note}"]


# 列表类工具的封顶（20261005，输入防线）。
#
# 为什么要有：三个列表接口**上游都没有 LIMIT**（`src/routes/talks.rs::list_by_src`
# 是 `.all()`，公告同理），行数随站点内容无界增长；而 `content` / 留言人自填的「留名」
# 是**自由文本**，单值长度同样无界。两者都会原样进帧、进提示词、进 narrator 上下文。
#
# 两个数都取"今天够不着"的量级（20261005 实测：board 25 行 / talk 11 行 / 公告 9 行，
# 最长自由文本 54 字）⇒ **今天的行为逐字节不变**，防线只在上游真的长起来时生效。
# 这和 `get_unread_summary` 的「只带回 N 条（未读通知共 M 条）」是同一条纪律：
# **宁可少带，也要说清少带了什么**。
_LIST_ROWS_MAX = 60
_LIST_FIELD_MAX = 500

# 会被 `_clip_fields` 封顶的自由文本字段。**只列自由文本**：`avatar` 是站内资源路径、
# `createTime` 是机器钟面，它们长不长由系统决定，截断只会让值失去意义。
_LIST_FIELD_KEYS = ("content", "talkContent", "留名")


def _page(total: int, start: int = 0, end: int = 0) -> dict:
    """注记里那两个数的**结构化那一份**（20261019 批 G2）。

    为什么要有：`total` / `start` / `end` 此前**只**长在中文注记里，跨轮实体摘要
    （`entities._note_total` / `_note_range`）于是只能拿 `共 (\\d+) 条` /
    `第 (\\d+)-(\\d+) 条` 把它们抠回来——措辞一改，摘要就会把 137 条说成 60 条，
    **系统自己说假话**，而且全程不报错（`_cap_rows` 自己的注里早把这条耦合写明了）。

    注记**文本一字不动**（planner/narrator 读的还是那句话），多出来的只是这一格。
    `start`/`end` 都为 0 = "这一页就是最新那批，没有号段"（与 `_note_range` 的
    "没有则 None" 同义）。
    """
    return {"total": total, "start": start, "end": end}


def _cap_rows(rows: list, limit: int, noun: str,
              offset: int = 0) -> tuple[list, str, dict | None]:
    """行数封顶：超上限只保留**最新的** limit 行，返回 `(行, 截断说明, 页码)`。

    上游序即"最新在前"（`list_by_src` 按 `CreatedAt DESC, Id DESC`）⇒ `rows[:limit]`
    就是最近的那批，不需要重排。

    没触发时说明是空串、页码是 `None`——调用方拼进注记的那一步因此今天一字不变，
    而 `None` 是"这一轮没有页码可说"（不是"页码是 0 条"）。
    **说明里必须写总数**：跨轮执行记忆的实体摘要（`entities._entry_digest`）读的就是
    这一句，不写总数就等于让系统在跨轮记忆里把"M 条"说成"limit 条"。

    `offset > 0` = **取回更早的**（20261005）：上游三个列表接口都没有 limit/offset，
    完整列表在本地，所以分页在 Python 侧做（`rows[start:start + limit]`）。没有这一步，
    封顶就只说了"还有更早的"而没说"怎么取"——planner 手上没有任何取回手段。
    号段写进注记（`第 A-B 条`）而不是让调用方自己算，是因为**同一条注记同时喂给
    `entities._note_range`**：跨轮摘要据它说"第 61-120 条"，不会把"取回来的这一页"
    错说成"最近的一页"。

    批 G2 起这两件事**不再只能从注记里读**：第三个返回值 `_page(...)` 与注记同源，
    调用方把它塞进 `_shape(..., meta={"page": …})` ⇒ 摘要读结构、注记只给人看。
    注记文本仍然一字不改（`共 N 条` / `第 A-B 条` 两个子串留着），因为它同时是
    **给模型看的边界事实**，且 `receipt_digest` 没有 meta 时仍走文本回落那一支。
    """
    total = sum(1 for r in rows if isinstance(r, dict))
    if offset <= 0:
        # 今天的行为**逐字节不变**（除了把"更早的未列入"换成可取回的写法）。
        if total <= limit:
            return list(rows), "", None
        return list(rows[:limit]), (
            f"本次只带回最近{limit}条，{noun}共 {total} 条"
            f"（更早的还有；要取更早的，带上 offset={limit} 再调一次）。"), _page(total)
    start = max(0, offset)
    window = list(rows[start:start + limit])
    if not window:
        # ⚠ 这里返回**空列表**是安全的，因为调用方紧接着会 `_rows_with_note` 挂上注记
        # ⇒ 出口帧非空、`kind` 仍是 `ok`。若哪个调用方把注记去掉，裸 `[]` 会被 checker
        # 判成 `empty_result` 并拒绝进跨轮执行记忆——那是另一个事实，别让它悄悄发生。
        return [], f"offset={start} 已超出范围：{noun}共 {total} 条，没有更早的了。", _page(total)
    end = start + len(window)
    tail = ("（已到末尾，没有更早的了）。" if end >= total
            else f"（更早的还有；要取更早的，带上 offset={end} 再调一次）。")
    return (window, f"本次带回第 {start + 1}-{end} 条，{noun}共 {total} 条{tail}",
            _page(total, start + 1, end))


def _clip_fields(rows: list, cap: int) -> list:
    """自由文本字段的单值封顶（超长截断加 `…`）。**只改值、不动结构**。

    结构必须原样：帧还是 `$ref` 的取值源（`$list_guestbook[0].content`），增删键会让
    下标寻址整体错位（同 `_rows_with_note` 头注里那条纪律）。
    """
    out = []
    for r in rows:
        if isinstance(r, dict):
            r = {
                k: (
                    v[:cap] + "…"
                    if k in _LIST_FIELD_KEYS and isinstance(v, str) and len(v) > cap
                    else v
                )
                for k, v in r.items()
            }
        out.append(r)
    return out


def _label_id_keys(data):
    """递归把行里的上游字段名换成命名空间名（**只改键名，不动值**）。

    递归是为了覆盖 `{"data": {"records": [...]}}` 这类包了一层的响应——按行处理的
    实现会漏掉它们，而漏掉的那一处正好又是"同一个数字两个名字"。
    """
    if isinstance(data, list):
        return [_label_id_keys(x) for x in data]
    if isinstance(data, dict):
        return {_FRAME_ID_KEYS.get(k, k): _label_id_keys(v) for k, v in data.items()}
    return data
