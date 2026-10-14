"""写参数的**出处**（provenance）：逐字引文核对 + 站内实体解析（20261014 新通道）。

## 为什么换表征（这一节是本次改造的全部理由，改本模块之前先读）

这一层要回答的是两个问题：
  ① **"这个值有没有出处"**（主人说过吗）；
  ② **"这个名字落到站内哪一行"**（真有这一个吗、是哪一行）。

旧实现（graph.py 的词表族 + 位置窗口，20261014 第一批前约 2100–2600 行 ≈ 19%）
回答①的方式是：**"主人这句话里有没有我认识的那个动词"**——约 40 张手抄中文词表
（`_ROLE_MARKS` / `_MOVE_MARKS` / `_TARGET_ACTION_MARKS` …）+「名词锚 → 名字 → 动作词」
的位置窗口四个抽取器。它的失效形状是**假拒**，且每遇一个新词形就复现一次：
生产 trace `20261014T175114`「猫猫给用户 niuniu **升级为**管理员」——名字明明白白
在句子里，`_ROLE_MARKS` 里没有「升级为」⇒ 零写 + 一句"把那个名字原样再说一次即可"
（照做仍然 False 的死路）。本仓自己早就写过这条教训（代词是封闭类、动词是开放类，
**不许**扩词表）。

**行业规范的做法**：把**抽取**（难、开放类）交给模型，把**核对**（易、精确）留在
代码里。于是本模块只做三件事，全部是确定性、可测的：
  · `quote_spans` —— 读**主人自己打的引号**（标点级封闭结构，不进任何词表）；
  · `sources` —— 列出"他看得到的原文"（本轮原话 / 上一张卡的台账行 / 跨轮任务台账
    / 执行记忆行）。**刻意不含自由形状的工具帧**：跨轮跨工具的取值走既有的
    `refs.py` `$tool[N].field`（机器解析，比"模型摘抄一段"更严），两条并行来源是
    第二份名单式的隐患；
  · `review` —— 逐参数判 `归一(value) ⊆ 归一(quote) ⊆ 归一(某来源)`，并记下**来源**。
判定"值 ⊆ 引文"是**逐字**比对（键盘噪声归一见 `agent/textnorm.py`），不含任何
"这个词算不算动作词"的判断——那正是旧实现假拒的来源。

"名字落到站内哪一行"**不在本模块**：那是站内自己的索引解析器族
（`graph._write_target_refusal` 一族，用的是 `_find_named_tag` / `_find_named_board_comment`
这类真接口），本来就与词表无关，原样保留。

## 与已拍板口径的两处对齐（别改回去）

1. **润色字段维持豁免**：`send_user_notice.content` 一族是用户拍板"允许整理、不必逐字"
   的字段。它不是在这里特判的——强制域是**派生**的：
   `write_scope_tools()` = （`authz` 里是写 ∧ **不在** `_ALWAYS_CONFIRM_TOOLS`）。
   理由是这一族**本来就没有免问的前提**：always-confirm 族的卡面永远把值全量印出来、
   人眼就是核验方（"卡片会印全值的字段，不需要出处闸；卡片不印的，必须有出处"）。
   今天散在四张表注释里的"刻意豁免"（通知正文 / 角色档位 / 禁言时长 / 待办正文）
   因此收成一条**可推导的口径**。
2. **"主人自己标出来的那几个字"是唯一保留的结构**（`Ruling.bounded`）。主人这句话里
   **有**引号时，"免弹窗直接写"要求值落在**某一段引号之内**——主人既然标了界碑，
   系统就不该拿界碑外面的字当他点的名字。**注意它的处置方向**：判据一（`review`）
   不因这条而拒绝（那会误伤「把标签「大笨狗」和 Python 两个标签都删掉」），
   它只把这一格**退回弹卡**——最坏后果是多问一次，而卡面把值印给他看。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent import authz
from agent.textnorm import norm

__all__ = ["EVIDENCE_ARG", "EVIDENCE_FIELD_DESC", "evidence_prop_schema",
           "QUOTE_SPAN_RE", "quote_spans", "Source", "sources", "Ruling",
           "parse_evidence", "review", "uncovered", "write_scope_tools",
           "always_confirm_tools"]

# ── schema 面：写技能函数上多一格可选参数 ────────────────────────────────
# 与 `tasks.INTENTS_ARG` 完全同一个做法（注入在 `native_plan.build_tool_schema`、
# 摘走在 `native_plan.tool_calls_to_plan`）：`additionalProperties: False` 意味着
# **不加进 props 模型根本发不出来**，而留着不走就等于跟着 `params` 一路漏进
# `PARAMS=` 计划文本与 `execute` 的参数校验（见 tool_calls_to_plan 的注）。
EVIDENCE_ARG = "evidence"

EVIDENCE_FIELD_DESC = (
    "出处引文。**每一个你填进去的值，都要把主人那句话里据以填出它的那一段原样抄进来**："
    '[{"arg":"参数名","quote":"主人的原话片段"}]。系统会拿 quote 逐字回主人这一轮说的话'
    "（以及他上一轮点过头的那张卡）里核对——抄不出原文，就说明那个值不是他的意思，"
    "请空着那个参数别填。主人用「」引起来的那几个字，就照抄引号里的那一段。"
)


def evidence_prop_schema(desc: str = EVIDENCE_FIELD_DESC) -> dict:
    """`evidence` 那一格的 JSON Schema 片段（**逐参数配对**，用户 20261014 拍板）。"""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"arg": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["arg", "quote"],
        },
        "description": desc,
    }


# ── 主人自己打的引号（**唯一保留的结构**）────────────────────────────────
# 四族成对引号。半角单引号 `'` 刻意不收：英文名里的撇号不是界碑。
# 长度上限 80 是为了不让一段没配对的引号吞掉整句（与旧 `_QUOTE_SPAN_RE` 同口径，
# 该正则 20261014 前住在 graph.py、被四个抽取器共用；现在只住在这里）。
QUOTE_SPAN_RE = re.compile(r"「([^」]{1,80})」|『([^』]{1,80})』|“([^”]{1,80})”"
                           r"|\"([^\"]{1,80})\"")


def quote_spans(user_msg) -> list[str]:
    """主人原话里带引号的片段（按出现顺序，去两端空白后非空）。"""
    out = []
    for m in QUOTE_SPAN_RE.finditer(str(user_msg or "")):
        frag = next((g for g in m.groups() if g), "")
        if frag.strip():
            out.append(frag.strip())
    return out


# ── 来源：他看得到的原文 ────────────────────────────────────────────────
# `kind` 的顺序就是优先级（弹卡要印主人的那一份原话，而不是台账渲染行）。
KIND_USER = "user"
KIND_LEDGER = "ledger"
KIND_EXEC = "exec"


@dataclass(frozen=True)
class Source:
    """一处原文出处。`kind` 是机器取值（判据与 trace 读它，别读文案）。"""
    kind: str
    text: str

    @property
    def norm(self) -> str:
        return norm(self.text)


def sources(user_msg, ledger_src: str = "", exec_src: str = "") -> tuple[Source, ...]:
    """本轮可用的出处原文（空的不收，顺序 = 优先级）。

    `ledger_src` = 上一张卡的「待主人点头」行 + 跨轮任务台账（调用方拼好后传进来，
    见 `graph._decide_write_guard`：那两处各有长注说明为什么它们是**系统事实**、
    以及为什么"系统自己规定的重提路径不许判成编造"）。
    `exec_src` = 执行记忆行（`state["ledger"]["executions"]`）——主人上一轮让系统
    做过的事写在里面，跨轮重提时目标可能只在那里出现过。
    """
    out = []
    for kind, text in ((KIND_USER, user_msg), (KIND_LEDGER, ledger_src),
                       (KIND_EXEC, exec_src)):
        if str(text or "").strip():
            out.append(Source(kind, str(text)))
    return tuple(out)


# ── 核验 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Ruling:
    """一格的核验结果。`why` 是**机器取值**（trace 与判据用），不是给主人看的话。"""
    arg: str                      # 要核的那一格的名字（工具参数名 / 技能参数名）
    value: str                    # 准备写下去的那个值
    quote: str = ""               # 模型声明的那段引文
    kind: str = ""                # user / ledger / exec / ""（无出处时为空）
    ok: bool = False
    bounded: bool = True          # 值是否落在主人某段引号之内（见模块头注 2）
    why: str = ""                 # no_quote / value_not_in_quote / quote_not_in_source

    def as_trace(self) -> dict:
        return {"arg": self.arg, "value": self.value[:80] if self.value else "",
                "quote": self.quote[:80] if self.quote else "",
                "kind": self.kind, "ok": self.ok, "bounded": self.bounded,
                "why": self.why}


def parse_evidence(raw) -> tuple[tuple[str, str], ...]:
    """模型给的那一格 → `((arg, quote), ...)`。**容错，不猜**。

    契约是 `[{"arg":…,"quote":…}]`，但这一格是模型**第一次见**的自由文本容器，
    形状漂移的代价不该由主人承担（旧的文本契约时代，约 15% 的轮次格式就漂了）。
    五形状都收：裸字符串 / `{参数名: 引文}` 映射 / `{arg,quote}` 单对象 / 上面的数组 /
    已经归一好的 `((arg, quote), ...)`。
    最后那一种不是"给模型看的"——`NativeDecision.evidence` **落地时就是归一形态**
    （见 `native_plan.tool_calls_to_plan`），影子与第二批的核验都直接吃它；本函数认它，
    是为了"再喂一次自己刚吐出来的东西"不静默变空（测试 `test_provenance` ⑥ 钉的就是这条）。
    `arg` 缺省 = **空串（通配）**，它匹配任何一格——语义是"这段原文就是这个值的出处"，
    与"这段原文覆盖了这几格"是同一件事（判据只问"能不能找到一段主人说过的原话"）。
    """
    def one(item) -> list[tuple[str, str]]:
        if isinstance(item, str):
            return [("", item)]
        if isinstance(item, dict):
            # 带 `arg`/`quote` 键 ⇒ 是**配对形状**（缺 quote 就是没填完，丢掉、不猜成映射：
            # 一个两键的配对形状与"参数名是 arg"的映射在字面上同形，认错会把 `{"arg":"a"}`
            # 读成一个引文为 "a"、参数名叫 arg 的配对）。
            if "quote" in item or "arg" in item:
                q = str(item.get("quote") or "")
                return [(str(item.get("arg") or ""), q)] if q.strip() else []
            return [(str(k), str(v)) for k, v in item.items()]
        # 归一形态：嵌套的两元组/两元素列表 = 一对 (arg, quote)。
        # **只认嵌套的那一层**：顶层裸数组仍是"几段裸引文"（`["甲","乙"]` = 两段通配引文），
        # 否则一条两段的引文数组会被读成一对，语义整个反过来。
        if isinstance(item, (list, tuple)) and len(item) == 2 \
                and all(isinstance(x, str) for x in item):
            return [(item[0], item[1])]
        return []

    out: list[tuple[str, str]] = []
    if raw is None:
        return ()
    if isinstance(raw, (str, dict)):
        out = one(raw)
    elif isinstance(raw, (list, tuple)):
        for it in raw:
            out.extend(one(it))
    return tuple((a.strip(), q.strip()) for a, q in out if str(q).strip())


def _pick(pairs, arg: str, value: str) -> str:
    """给这一格挑一段引文：**先按参数名精确配**，再退到"哪段引文里有这个值"。

    退这一步是刻意的、也是本通道的地基：判据问的是"主人说过这个值吗"，而
    "模型把这段引文标在哪一格上"是**同一句话的另一种说法**。精确配优先，是为了
    在模型标对格子时让 trace 与弹卡读到最贴的那一份原文。
    """
    exact = [q for a, q in pairs if a == arg]
    if exact:
        return exact[0]
    v = norm(value)
    for a, q in pairs:
        if not a and v and v in norm(q):
            return q
    for a, q in pairs:
        if v and v in norm(q):
            return q
    return ""


def review(evidence, wanted, user_msg, ledger_src: str = "",
           exec_src: str = "") -> tuple[Ruling, ...]:
    """判据一：逐格核"值 ⊆ 引文 ⊆ 来源"。

    `wanted` = 可迭代的 `(arg, value)`（`arg` 是工具参数名，`value` 是要写下去的那个
    值，标量或列表）。**只判非空、非 `$ref` 的值**：空值不是"没出处"（那是
    `check_skill_params` 的缺参问题），而 `$tool[N].field` 是机器解析出来的引用
    （`refs.py`），没有字面可核——调用方本来也该在带引用时整条早退。
    """
    pairs = parse_evidence(evidence)
    srcs = sources(user_msg, ledger_src, exec_src)
    spans = [norm(s) for s in quote_spans(user_msg)]
    out: list[Ruling] = []
    for arg, value in wanted:
        vals = value if isinstance(value, (list, tuple)) else [value]
        for v in vals:
            if not isinstance(v, str):
                continue
            v = v.strip()
            if not v or "$tool[" in v:
                continue
            out.append(_rule_one(pairs, arg, v, srcs, spans))
    return tuple(out)


def _rule_one(pairs, arg: str, v: str, srcs, spans) -> Ruling:
    quote = _pick(pairs, arg, v)
    nv = norm(v)
    if not quote:
        return Ruling(arg=arg, value=v, why="no_quote")
    nq = norm(quote)
    bounded = (not spans) or any(nv in s for s in spans)
    if nv not in nq:
        return Ruling(arg=arg, value=v, quote=quote, bounded=bounded,
                      why="value_not_in_quote")
    hit = next((s for s in srcs if nq in s.norm), None)
    if hit is None:
        return Ruling(arg=arg, value=v, quote=quote, bounded=bounded,
                      why="quote_not_in_source")
    return Ruling(arg=arg, value=v, quote=quote, kind=hit.kind, ok=True,
                  bounded=bounded)


def uncovered(rulings) -> tuple[Ruling, ...]:
    """没出处的那几格（**判据读这个函数，别在调用点自己过滤**）。"""
    return tuple(r for r in rulings if not r.ok)


# ── 强制域的派生（**零手抄名单**，见模块头注 1）────────────────────────────
def always_confirm_tools() -> frozenset[str]:
    """`authz._ALWAYS_CONFIRM_TOOLS` 的公开读口（同名同源，不另立第二份）。"""
    return authz.always_confirm_tools()


def write_scope_tools() -> frozenset[str]:
    """**证据闸的射程** = （authz 声明的写工具）−（一律弹窗族）。

    两个来源都是 `agent/authz.py` 里已有的声明，没有第三份名单：
      · 写 = `TOOL_SCOPE[tool] in WRITE_SCOPES`；
      · 一律弹窗 = `_ALWAYS_CONFIRM_TOOLS`（卡面恒把值全量印出来，人眼是核验方）。
    这条口径的**方向**：射程里的工具**没有**免问的前提（它不走卡、或卡的目的是别的），
    所以"值有没有出处"这一问必须由系统自己回答。
    """
    deny = always_confirm_tools()
    return frozenset(t for t in authz.TOOL_SCOPE
                     if authz.is_write(t) and t not in deny)
