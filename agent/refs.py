"""工具参数引用（参数绑定）：把"上一步的真实返回值"绑进"下一步的参数"。

背景（20260919 用户裁决，POC 之后）：
  现状是 planner 每轮看 `_frame_texts`（截断文本）自己"读"出上一步的 id 再写进
  下一步参数——取值靠模型从文本里挑。POC 实测：生产 326 条 trace 里 27.9% 的
  请求 planner 带清单决策 ≥2 轮（多轮顺序决策是常态），但**轮内无任何参数绑定**
  机制（execute 按 spec 字面执行，参数是 planner 规划时写死的 JSON）。于是
  "先读数据、再决定下一步"只能靠两件事：跨轮重规划 + 模型从 300 字截断帧里
  读出 id。截断、多候选、字段名漂移都会让这条链断。

本模块把这一步变成**程序化取值**：planner 在参数里写引用而不是字面值——

  {"tool": "get_article_detail", "args": {"article_id": "$search_notes[0].noteKey"}}

execute 在调用工具前解析引用，从**本请求内已成功执行**的工具返回值（结构化，
非截断文本）里取值填参；解析失败 → 不执行该 spec，产带原因码的 __ERROR__ 帧，
走既有 blocker 链路（planner 改参重试 → 同 spec 二次受阻 → reflector）。

设计边界（刻意窄）：
  - 只认**顶层参数值**是引用的形态（`{"article_id": "$x[0].y"}`）；不做嵌套/
    表达式/函数——引用语法一旦能算，就变回了"让模型写代码"。
  - 只认**本请求内已经执行过**的工具（tool_data 按执行顺序累积）；跨请求的
    "上次会话读到 19"另有通道（execution_log / doc_anchors），不走这里。
  - 取值失败给**原因码**（ref_unknown_tool / ref_unparsed / ref_index_range /
    ref_path_missing / ref_not_scalar），planner 和 reflector 都据它修正。
  - 解析不出结构（工具返回既不是 JSON/Python 字面量，也不是 rag_search 那种
    行式候选）→ 明确报 ref_unparsed，不静默降级成"当作字面量调用"（那会拿
    `$x[0].y` 当文章 id 去查）。

索引语义：`$<工具>[<序号>]` 里序号是**该工具返回值列表的下标**（0 = 候选第一条）；
工具返回单个对象（dict，如 get_article_detail）时序号只能是 0。
"""

from __future__ import annotations

import ast
import json
import re

# 引用字面量：$tool[0] 或 $tool[0].path.to.field（字段名允许点分嵌套）
REF_RE = re.compile(r"^\$([a-z_][a-z0-9_]*)\[(\d+)\](?:\.([A-Za-z0-9_.]+))?$")

# 单条 hint 行最多列几个字段（防长结构撑爆提示词）
_HINT_FIELDS = 6


def is_ref(value) -> bool:
    """值是否是引用字面量（只认字符串形态）。"""
    return isinstance(value, str) and REF_RE.match(value.strip()) is not None


def has_refs(specs) -> bool:
    """specs 里是否还有**未解析的引用**（`$tool[N].field`）。

    用途（20260921 确认弹窗）：签进令牌的参数必须是**具体值**——引用依赖的是
    "签发那一轮已执行过的工具帧"，而执行轮是另一轮对话，那些帧早就不在了。
    把带引用的 spec 签进令牌 = 发一张到期必然兑现不了的支票，所以检出即
    **不签发**（退回既有追问链路，让 planner 先把值取到手再说）。
    递归进 list/dict：参数可以是标签名数组（`{"add": ["$list_tags[0].name"]}`），
    只看顶层会漏。
    """
    for s in specs or []:
        if isinstance(s, dict) and _walk_refs(s.get("args")):
            return True
    return False


def _walk_refs(value) -> bool:
    """递归找引用字面量（list/dict 里的也算）。"""
    if is_ref(value):
        return True
    if isinstance(value, dict):
        return any(_walk_refs(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_walk_refs(v) for v in value)
    return False


def parse_ref(value: str) -> tuple[str, int, str] | None:
    """引用字面量 → (工具名, 序号, 字段路径)。不是引用返回 None。"""
    m = REF_RE.match((value or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), m.group(3) or ""


# rag_search 的行式候选（tools/base.py）：`1. type=note id=12 score=0.83 title=… [命中节=…]`
# ——不是 JSON 也不是 Python 字面量，但对"检索定位 → 读全文"这条链是最常用的
# 来源（planner 规则 3 对机制型问题首选 rag_search），所以单独给一个解析器。
_RAG_LINE_RE = re.compile(
    r"^\d+\.\s+type=(\S+)\s+id=(\d+)\s+score=([\d.]+)\s+title=(.*?)(?:\s+命中节=(.*))?$")


def _parse_rag_lines(text: str) -> list | None:
    """行式检索候选 → [{id,type,score,title,section}]（无一行匹配 → None）。"""
    rows = []
    for line in (text or "").splitlines():
        m = _RAG_LINE_RE.match(line.strip())
        if not m:
            continue
        rows.append({"id": int(m.group(2)), "type": m.group(1), "score": m.group(3),
                     "title": m.group(4), "section": m.group(5) or ""})
    return rows or None


def parse_data(text) -> object | None:
    """工具返回文本 → 可取值结构（JSON 优先，其次 Python repr，再行式检索候选）。

    工具的出口是字符串（`_shape` → str(data)，列表/字典走 Python repr），所以
    这里要能吃 Python repr（单引号/None/True）；rag_search 是行式文本，另有
    解析器。解析不出返回 None —— 调用方据此报 ref_unparsed，**不猜**。
    """
    if isinstance(text, (dict, list)):
        return text
    s = (text or "").strip()
    if not s:
        return None
    for loader in (json.loads, ast.literal_eval):
        try:
            obj = loader(s)
        except Exception:
            continue
        if isinstance(obj, (dict, list)):
            return obj
    return _parse_rag_lines(s)


def _walk(data: object, path: str, idx: int) -> tuple[object, str | None]:
    """按 序号+字段路径 取值 → (值, 错误码)。"""
    if path == "":
        return data, None
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None, "ref_path_missing"
            cur = cur[part]
        elif isinstance(cur, list):
            # 列表内的点分路径按"第 0 个元素"取（列表是候选集，逐项取字段
            # 属于筛选语义，本模块不做）
            if not cur:
                return None, "ref_index_range"
            nxt = cur[0]
            if not isinstance(nxt, dict) or part not in nxt:
                return None, "ref_path_missing"
            cur = nxt[part]
        else:
            return None, "ref_path_missing"
    if isinstance(cur, (dict, list)):
        return None, "ref_not_scalar"
    return cur, None


def resolve_one(value: str, tool_data: list) -> tuple[object, str | None]:
    """单个引用字面量 → (解析值, 错误码)。非引用原样返回。"""
    parsed = parse_ref(value)
    if parsed is None:
        return value, None
    tool, idx, path = parsed
    # 取该工具**最近一次**执行的返回值——含失败/解析不出的那次（同名工具多轮执行
    # 以最新为准）。失败时按 ref_unparsed 如实报，**不悄悄退回更早的旧数据**：
    # 旧返回属于上一轮的问题，拿它填这一轮的参数是把"最新的失败"藏起来。
    entry = None
    for e in reversed(tool_data or []):
        if e.get("tool") == tool:
            entry = e
            break
    if entry is None:
        return None, "ref_unknown_tool"
    data = entry.get("data")
    if data is None:
        return None, "ref_unparsed"
    if isinstance(data, list):
        if idx >= len(data):
            return None, "ref_index_range"
        data = data[idx]
    elif isinstance(data, dict):
        if idx != 0:
            return None, "ref_index_range"
    else:
        return None, "ref_unparsed"
    got, err = _walk(data, path, idx)
    if err:
        return None, err
    return got, None


def resolve_args(args: dict, tool_data: list) -> tuple[dict, str | None]:
    """参数 dict 里的引用全部解析 → (新参数, 错误码)。

    只处理顶层值；任一引用解析失败即整体失败（该 spec 不执行——半个参数清单
    去调用工具是更坏的结果）。无引用时原样返回（零开销、行为不变）。
    """
    if not isinstance(args, dict):
        return args, None
    if not any(is_ref(v) for v in args.values()):
        return args, None
    out = dict(args)
    for k, v in args.items():
        got, err = resolve_one(v, tool_data)
        if err:
            return None, f"{err}:{v}"
        out[k] = got
    return out, None


def ref_hints(tool_data: list, max_tools: int = 3, fields: int = _HINT_FIELDS) -> str:
    """可引用字段提示（注入 planner 提示词）：让它知道能引什么、别臆造路径。

    只列**最近 max_tools 个**成功执行过、且结构可解析的工具，字段取首个元素的
    键名。没有可引用的返回 → 明确说"无从引用"（避免模型凭空写引用）。
    """
    rows: list[str] = []
    seen: set[str] = set()
    for e in reversed(tool_data or []):
        name = e.get("tool") or ""
        if name in seen:
            continue
        seen.add(name)
        data = e.get("data")
        if data is None:
            continue
        sample = data[0] if isinstance(data, list) and data else data
        if not isinstance(sample, dict):
            continue
        keys = [k for k in sample.keys() if not str(k).startswith("_")][:fields]
        if not keys:
            continue
        n = len(data) if isinstance(data, list) else 1
        rows.append(f"· ${name}[0]（共 {n} 条）可用字段: " + " / ".join(keys))
        if len(rows) >= max_tools:
            break
    if not rows:
        return "（本轮还没有可引用的工具返回）"
    return "\n".join(rows)


def ref_error_reason(text: str) -> str | None:
    """从 __ERROR__ 帧文本里取回引用失败原因码（execute 产帧 → checker 判 reason）。

    帧格式：`__ERROR__: 参数引用无法解析[<原因码>:<引用原文>]`；非引用失败返回 None。
    """
    m = re.search(r"参数引用无法解析\[([a-z_]+):", text or "")
    return m.group(1) if m else None
