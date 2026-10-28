"""Typed tool outcomes, write evidence and bounded list presentation.

ToolResult keeps readable text alongside result kind, structured data and compact
receipt metadata. Empty results differ from unavailable services and missing targets.
Presentation helpers preserve ID namespaces and ownership meaning without I/O."""

# Public exports; helper names stay private to their modules.
__all__ = ["ToolResult", "ok", "empty", "unavailable", "not_found", "invalid_args", "TGT_KINDS",
           "tgt", "fact", "is_noop", "UPSTREAM_DOWN", "error_frame"]

# Readable tool results retain explicit kinds: ok, empty, unavailable and not_found.
# Page commands use structured metadata; ordinary reply text is not executable.
class ToolResult(str):
    kind: str = "ok"
    # Compact machine-readable write evidence, separate from display text.
    meta: dict = {}
    # Original structured tool data. None means no structured payload; {} means empty data.
    # Receipt metadata stays compact; full data is not copied into the host receipt.
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
    """Return an unavailable target, distinct from service failure or an empty collection.

    The source may expose only a bounded visible window, so callers must explain
    that scope rather than infer global nonexistence."""
    return ToolResult(text, "not_found", meta)


def invalid_args(text: str, meta: dict | None = None, *, data: object = None) -> ToolResult:
    """Return parsed but invalid tool arguments as a typed failure.

    This is distinct from malformed call syntax and unavailable external services;
    the caller must correct the arguments before attempting the operation."""
    return ToolResult(text, "invalid_args", meta, data)


# Write evidence: op, changed, target, before/after and optional read-back evidence.
# changed=False requires an observed no-op; never use it as an unverified default.
# State fields contain values, not narration. Preserve cross-language receipt keys.
TGT_KINDS = ("note", "tag", "category", "announcement", "board_comment",
             # Article comments and guestbook messages have distinct ID namespaces and storage.
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
    """Read observed no-op evidence from changed, or the compatible noop key.

    Missing evidence never certifies a no-op: return False rather than fabricate
    an assertion that nothing changed."""
    if not isinstance(meta, dict):
        return False
    ch = meta.get("changed")
    if isinstance(ch, bool):
        return not ch
    return bool(meta.get("noop"))


def error_frame(text: str, reason: str) -> ToolResult:
    """Return an error frame with its machine-readable reason metadata.

    The existing wire kind remains ok; consumers inspect reason or the error frame.
    Return this object directly: wrapping it again would lose its metadata."""
    return ToolResult(text, "ok", {"reason": reason})


# 上游故障哨兵：`_get` 失败时返回它而不是 `[]`（"故障伪装成空"的源头就在那）。
# 工具的出口要用 `_shape(data)` 而不是 `str(data)`——理由见 _shape 的注释。
UPSTREAM_DOWN = unavailable("服务暂时不可用，请稍后再试")


# Closed parameters use Literal so validation and JSON Schema share a source.
# Parameters with explicit alias normalization keep str until that normalization.
# Tools also retain runtime validation for callers that bypass schema validation.


def _shape(data, *, meta: dict | None = None) -> str:
    """Preserve typed outcomes and expose the same structured data as rendered text.

    Normalize ID namespaces once for every consumer. Existing ToolResult objects
    pass through unchanged; plain collections gain structured data and optional
    pagination metadata. Converting the result to str would discard its kind."""
    if isinstance(data, ToolResult):
        return data
    relabeled = _label_id_keys(data)
    return ToolResult(str(relabeled), "ok", meta, relabeled)


# Expose stable, distinct ID namespaces for articles, talks, messages and announcements.
_FRAME_ID_KEYS = {
    "talkKey": "talkId",
    "noteKey": "noteId",
    "tagKey": "tagId",
    "categoryKey": "categoryId",
    "fatherKey": "fatherId",
    "friendKey": "friendId",
}


def _board_text_keys(rows: list) -> list:
    """Expose a guestbook display signature as 留名, without altering its value.

    The source author field is visitor-controlled text and is not an account
    identity. Account attribution requires the authorized administration source."""
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


# Bound list rows and free-text fields before they enter model context.
# Truncation retains original totals and explains which content was omitted.
_LIST_ROWS_MAX = 60
_LIST_FIELD_MAX = 500

# 会被 `_clip_fields` 封顶的自由文本字段。**只列自由文本**：`avatar` 是站内资源路径、
# `createTime` 是机器钟面，它们长不长由系统决定，截断只会让值失去意义。
_LIST_FIELD_KEYS = ("content", "talkContent", "留名")


def _page(total: int, start: int = 0, end: int = 0) -> dict:
    """Return structured pagination facts for the same visible count note.

    Zero start/end means the latest window has no explicit numbered range."""
    return {"total": total, "start": start, "end": end}


def _cap_rows(rows: list, limit: int, noun: str,
              offset: int = 0) -> tuple[list, str, dict | None]:
    """Bound a newest-first list and return rows, an explicit count note and page metadata.

    Offset selects an older window from the available source list. Truncated or
    out-of-range windows retain the total so they cannot resemble an empty source."""
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
        # The caller attaches an explicit count note, so truncation cannot resemble an empty source.
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
