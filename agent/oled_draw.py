# -*- coding: utf-8 -*-
"""OLED 画板：绘图 DSL → 线上 op 列表（**跨仓契约的 Python 侧唯一实现**）。

## 为什么有这个模块

主人的那块 ESP32 屏幕此前只会做一件事：印一段文字（`device_oled_display`）。要让它**画**
（笑脸、箭头、进度条），笔画只能由模型**写出来**——千问多模态是"看图"（VL），出图是另一条
链路，而 128×64 单色抖动降采样出来基本是糊的。所以链路是：

    模型写人可读的绘图 DSL（`circle 64,26,18` 一行一条）
      → 本模块解析/校验/归一（**一个实现、四个纯函数 + 一个入口 build**）
      → 线上 op 列表（`[["circle",64,26,18]]`，数组的数组）
      → 固件按名字查表调 u8g2（设备是笨执行器，C 侧**没有第二个 DSL 解析器**）

模型那一侧只写 DSL（好读、好判、可进回执），设备那一侧只认结构（不需要在 C 里复刻一套
解析器）。**两种形态的区别只存在于本模块**，这就是这个模块存在的理由。

## 纪律（每条都有对应判据，见 `tests/test_oled_draw.py`）

- **不猜、不静默丢**：未知 op / 参数个数不符 / 非整数 / 文字出屏 / 空文案一律**抛
  `OledDrawError`**。静默丢一个图形正是本仓"剔空当收尾"那一族的形状。报错文案里带上合法
  op 名单与出错的那一条，好让创作层**纠偏一次**就能改对（`graph.py::_create_draw_ops`）。
- **钳制要如实计数**：坐标钳进画布、图形数超上限，都进 `notes`（随回执印出来），不装作没发生。
- **人话由本模块印**：`describe()` 的产物进回执与过程行——事实由系统印，不让模型复述。
- **跨仓常量在本模块是"第二份实现"**：op 名单、6/12 像素宽度规则、`MAX_OPS`/`MAX_PAYLOAD_BYTES`
  在 `ESP32-S3-OBC/main/main.c` 里各有一份对应物，所以有一条**跨仓守卫**逐项比对
  （固件仓不在 ⇒ 响亮"未评估"，`SAUDADE_REQUIRE_FIRMWARE=1` 时硬判）。
  **改这里必须同时改固件**，否则红在守卫，而不是红成"屏幕怎么没变"。

## DSL 语法

    每行一条（`\\n`、`;`、`；` 都算分行），大小写敏感：

    circle 64,26,18
    pixel 58,20
    line 48,42,80,42
    text 44,52,加油

- op 名与参数之间用空白分隔（含全角空格）；参数之间用 `,` 或 `，`。
- `text` 的第三个参数是**该行剩余全文**（可含逗号与中文标点），**但不能含 `;`/`；`/换行**
  ——那三个字符是整个 DSL 的分行符。
- 空行、代码围栏（``` 那两行）会被忽略：那是**搬运痕迹**不是绘图内容（创作提示词里
  同时也要求"只输出指令、不要围栏"，两道一起上）。
- 每行开头的行内注释（`#` 之后）不认——模型要说明什么，去回执里说。
"""

import json
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 画布与上限（固件侧同名常量见 main/main.c：DRAW_MAX_OPS、RX_BUF_SIZE）
# ---------------------------------------------------------------------------

CANVAS_W = 128
CANVAS_H = 64

#: 单条指令最多几个图形。固件 `DRAW_MAX_OPS` 同值（守卫比对）。
#: 本模块**先**截断并记账 ⇒ 固件那次截断正常情况永远命中不了（纵深，不互替）。
MAX_OPS = 64

#: 线上 payload 字节上限。固件能收 4096（`RX_BUF_SIZE`），这里只给到 1200：
#: ① 3 倍余量，别贴着固件的线走；② 旧固件（≤1.2.0，512 字节**静默**截断）上会
#: **响亮地红**，而不是画一半没人知道。超了抛错、零下发。
MAX_PAYLOAD_BYTES = 1200

_ASCII_W = 6   # u8g2_font_wqy12_t_gb2312 里 ASCII 的步进像素
_CJK_W = 12    # 其余（含 4 字节字符）按整宽算——与固件 utf8_char_width 同一条规则


class OledDrawError(ValueError):
    """绘图指令不合法（语法/参数/画布/体积）。调用方零下发并把文案交给模型看。"""


@dataclass(frozen=True)
class OpSpec:
    """一个图形 op 的规格。**op 名单只有这一张表**——帮助文本与校验都从它派生。"""
    name: str
    roles: tuple          # 每个数字参数的角色（决定钳制范围）：x/y/w/h/r
    doc: str              # 一句话说明（给模型看）
    tail_str: bool = False  # 末位是否是一段文字（不算在 roles 里）

    @property
    def signature(self) -> str:
        """参数签名（如 `x,y,w,h` / `x,y,s`）——派生，不手写。"""
        return ",".join(list(self.roles) + (["s"] if self.tail_str else []))

    @property
    def nargs(self) -> int:
        return len(self.roles) + (1 if self.tail_str else 0)


#: **唯一 op 表**。加一个图形 = 这里加一行 + 固件 `DRAW_OPS` 加一行（守卫会盯着）。
OPS: dict = {
    "pixel":  OpSpec("pixel",  ("x", "y"), "单点"),
    "line":   OpSpec("line",   ("x", "y", "x", "y"), "直线（起点→终点）"),
    "box":    OpSpec("box",    ("x", "y", "w", "h"), "实心矩形"),
    "frame":  OpSpec("frame",  ("x", "y", "w", "h"), "空心矩形（边框）"),
    "rbox":   OpSpec("rbox",   ("x", "y", "w", "h", "r"), "圆角实心矩形（气泡框）"),
    "disc":   OpSpec("disc",   ("x", "y", "r"), "实心圆"),
    "circle": OpSpec("circle", ("x", "y", "r"), "空心圆（圆环）"),
    "tri":    OpSpec("tri",    ("x", "y", "x", "y", "x", "y"), "实心三角"),
    "text":   OpSpec("text",   ("x", "y"), "一行文字（左上角对齐；中文可用）", tail_str=True),
}

OP_NAMES = tuple(OPS)

#: 每个角色的合法区间（越界钳制、不报错——钳制是"画得出来"与"报错重来"之间的取舍：
#: 出屏的坐标是**能救的**，未知 op / 参数个数不对是**救不了的**，只有后者报错）。
_RANGE = {
    "x": (0, CANVAS_W - 1),
    "y": (0, CANVAS_H - 1),
    "w": (1, CANVAS_W),
    "h": (1, CANVAS_H),
    "r": (0, CANVAS_H),
}

#: 给模型看的 op 表（创作提示词与技能力共用**这一份**，不手写第二遍）。
OPS_HELP = "\n".join(
    f"{s.name} {s.signature} — {s.doc}" for s in OPS.values()
)

_INT_RE = re.compile(r"^[+-]?\d+$")
_FW_NUM = str.maketrans("０１２３４５６７８９＋－", "0123456789+-")
_SEP_RE = re.compile(r"[,，]")
_SPLIT_RE = re.compile(r"[\n;；]")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def text_width(s: str) -> int:
    """一段文字的像素宽（ASCII 6、其余 12）——固件 `utf8_char_width` 的同一条规则。"""
    return sum(_ASCII_W if ord(ch) < 0x80 else _CJK_W for ch in s)


# ---------------------------------------------------------------------------
# ① 解析：DSL 文本 → op 列表（结构/类型不对就报错）
# ---------------------------------------------------------------------------

def parse_dsl(text: str) -> list:
    """把 DSL 文本解析成 `[[名字, 参数…], …]`（数字已转 int，文字保持原样）。

    任何一处看不懂都抛 `OledDrawError` 并指出是第几条、错在哪。
    """
    if not isinstance(text, str) or not text.strip():
        raise OledDrawError("没有任何绘图指令")
    ops: list = []
    for line in _SPLIT_RE.split(text):
        seg = line.strip()
        if not seg or _FENCE_RE.match(seg):
            continue                       # 空行 / 代码围栏：搬运痕迹，不是绘图内容
        idx = len(ops) + 1
        parts = seg.split(None, 1)
        name = parts[0]
        spec = OPS.get(name)
        if spec is None:
            raise OledDrawError(
                f"第 {idx} 条「{seg}」：认不出 op「{name}」。可用的有：{'、'.join(OP_NAMES)}")
        # maxsplit = 参数个数-1：`text` 的文案里可以有逗号（那是正文，不是分隔符）
        fields = _SEP_RE.split(parts[1], maxsplit=spec.nargs - 1) if len(parts) > 1 else []
        if len(fields) != spec.nargs:
            raise OledDrawError(
                f"第 {idx} 条「{seg}」：{name} 要 {spec.nargs} 个参数（{spec.signature}），"
                f"这里给了 {len(fields)} 个")
        op = [name]
        for i, role in enumerate(spec.roles):
            op.append(_to_int(fields[i].strip(), name, role, idx, seg))
        if spec.tail_str:
            s = fields[-1].strip()
            if not s:
                raise OledDrawError(f"第 {idx} 条「{seg}」：{name} 的文字是空的")
            op.append(s)
        ops.append(op)
    if not ops:
        raise OledDrawError("没有任何绘图指令")
    return ops


def _to_int(field: str, name: str, role: str, idx: int, seg: str) -> int:
    """一个数字参数 → int。**只认整数**：`64`、`-3`、全角数字都行，`64.0` 不行。"""
    s = field.translate(_FW_NUM)
    if not _INT_RE.match(s):
        raise OledDrawError(
            f"第 {idx} 条「{seg}」：{name} 的 {role} 需要整数，拿到的是「{field}」")
    return int(s)


# ---------------------------------------------------------------------------
# ② 归一：钳进画布、截到上限，**如实记账**
# ---------------------------------------------------------------------------

def normalize(ops: list) -> tuple:
    """`(归一后的 ops, notes)`。坐标/尺寸钳进画布、`rbox` 的圆角跟着 w/h 收、
    图形数截到 `MAX_OPS`——每一处改动都进 `notes`（人话，随回执印出来）。"""
    if not isinstance(ops, (list, tuple)) or not ops:
        raise OledDrawError("没有任何图形可以下发")
    notes: list = []
    work = list(ops)
    if len(work) > MAX_OPS:
        notes.append(f"图形数超过上限 {MAX_OPS}，末尾 {len(work) - MAX_OPS} 个已丢弃")
        work = work[:MAX_OPS]
    out: list = []
    nudge = 0
    for op in work:
        if not isinstance(op, (list, tuple)) or not op:
            raise OledDrawError(f"图形「{op!r}」不是「名字+参数」的形式")
        spec = OPS.get(op[0])
        if spec is None:
            raise OledDrawError(f"认不出 op「{op[0]}」。可用的有：{'、'.join(OP_NAMES)}")
        if len(op) != spec.nargs + 1:
            raise OledDrawError(
                f"op {spec.name} 要 {spec.nargs} 个参数（{spec.signature}），"
                f"这里给了 {len(op) - 1} 个")
        vals = []
        for role, v in zip(spec.roles, op[1:]):
            if isinstance(v, bool) or not isinstance(v, int):
                raise OledDrawError(f"op {spec.name} 的 {role} 需要整数，拿到的是 {v!r}")
            lo, hi = _RANGE[role]
            cv = min(max(v, lo), hi)
            if cv != v:
                nudge += 1
            vals.append(cv)
        if spec.name == "rbox":
            # u8g2 的 DrawRBox 对 r > min(w,h)/2 画出来是坏的（不是"圆一点"）⇒ 按 w/h 收，
            # 收过就记账。这是 Python 侧的收口，固件不需要知道。
            cap = max(min(vals[2], vals[3]) // 2, 0)
            if vals[4] > cap:
                nudge += 1
                vals[4] = cap
        if spec.tail_str:
            vals.append(op[-1])
        out.append([spec.name] + vals)
    if nudge:
        notes.append(f"{nudge} 个坐标/尺寸越界已收进画布")
    _check_text_fits(out)
    return out, notes


def _check_text_fits(ops: list) -> None:
    """文字出屏预检（用**钳后**的 x）。超宽报错而不静默裁：半个字与一个字都没画
    一样坏，但它看起来"成功了"——正是本仓在治的那类静默。"""
    for op in ops:
        if op[0] != "text":
            continue
        x, y, s = op[1], op[2], op[3]
        w = text_width(s)
        if w > CANVAS_W:
            raise OledDrawError(f"文字「{s}」自己就有 {w} 像素宽，超过画布 {CANVAS_W}，请缩短")
        if x + w > CANVAS_W:
            raise OledDrawError(
                f"文字「{s}」宽 {w} 像素，从 x={x} 起会画出屏幕（x 最远只能到 "
                f"{CANVAS_W - w}；也可以缩短文字）")


# ---------------------------------------------------------------------------
# ③ 编码：ops → 线上 payload（**逐字节就是发出去的那一份**）
# ---------------------------------------------------------------------------

def encode(ops: list) -> str:
    """ops → 线上 payload 字符串。

    紧凑分隔符 + `ensure_ascii=False`：中文按 UTF-8 原文写（转义成 `\\uXXXX` 白占一倍
    字节；cJSON 两种都解得开）。工具是**直接把这份字符串当 body 发出去**的（不是让
    httpx 再序列化一遍）——"测的字节"与"发的字节"因此是同一份。
    """
    if not ops:
        raise OledDrawError("没有任何图形可以下发")
    payload = json.dumps({"type": "draw", "ops": ops},
                         ensure_ascii=False, separators=(",", ":"))
    size = len(payload.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise OledDrawError(
            f"绘图指令 {size} 字节，超过 {MAX_PAYLOAD_BYTES} 字节上限，请减少图形数量")
    return payload


# ---------------------------------------------------------------------------
# ④ 摘要：ops → 确定性人话（回执/过程行印的就是它）
# ---------------------------------------------------------------------------

_DESC_LIMIT = 240   # 执行台账列宽（Rust 侧 300）之内留余量


def describe(ops: list) -> str:
    """`点(58,20)、线(0,32→127,32)、字「加油」(44,52)`——逐字确定，供系统打印。"""
    if not ops:
        return "（无图形）"
    parts = [_describe_one(op) for op in ops]
    acc: list = []
    size = 0
    for p in parts:
        add = len(p) + (1 if acc else 0)
        if size + add > _DESC_LIMIT:
            break
        acc.append(p)
        size += add
    if not acc:                     # 单条就超长：至少要印得下一个（并如实标注）
        acc = [parts[0][:_DESC_LIMIT] + "…"]
    s = "、".join(acc)
    if len(acc) < len(parts):
        s += f"…（共 {len(parts)} 个图形，此处列 {len(acc)} 个）"
    return s


def _describe_one(op: list) -> str:
    name, a = op[0], op[1:]
    if name == "pixel":
        return f"点({a[0]},{a[1]})"
    if name == "line":
        return f"线({a[0]},{a[1]}→{a[2]},{a[3]})"
    if name == "box":
        return f"实心矩形({a[0]},{a[1]},{a[2]}×{a[3]})"
    if name == "frame":
        return f"空心矩形({a[0]},{a[1]},{a[2]}×{a[3]})"
    if name == "rbox":
        return f"圆角矩形({a[0]},{a[1]},{a[2]}×{a[3]},r{a[4]})"
    if name == "disc":
        return f"实心圆({a[0]},{a[1]},r{a[2]})"
    if name == "circle":
        return f"空心圆({a[0]},{a[1]},r{a[2]})"
    if name == "tri":
        return f"三角({a[0]},{a[1]}→{a[2]},{a[3]}→{a[4]},{a[5]})"
    if name == "text":
        return f"字「{a[2]}」({a[0]},{a[1]})"
    return name            # 走不到（normalize 已把未知 op 挡掉）；不编。


# ---------------------------------------------------------------------------
# 入口：DSL 文本 → 可以直接下发的一份东西
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Drawing:
    """一次绘制：`payload` 是**要原样发出去的 body**，`summary` 是给系统印的人话。"""
    ops: tuple
    payload: str
    summary: str
    notes: tuple = ()


def build(text: str) -> Drawing:
    """DSL 文本 → `Drawing`（解析 → 归一 → 编码 → 摘要）。出错抛 `OledDrawError`。"""
    ops, notes = normalize(parse_dsl(text))
    return Drawing(ops=tuple(tuple(op) for op in ops), payload=encode(ops),
                   summary=describe(ops), notes=tuple(notes))
