# -*- coding: utf-8 -*-
"""OLED 画板（`device_oled_draw`）：DSL → op 列表 → 下发，以及它与固件的那份契约。

**为什么这套判据必须存在**：这条路把"模型写的自由文本"变成"设备上的点阵"，中间隔了
两仓两份实现（`agent/oled_draw.py` 与固件 `main/main.c` 的 `DRAW_OPS`）。物理世界那半
**判据够不着**（屏幕没有回读通道，画得对不对只有人眼判），所以能判的那半要判死：

  ① 解析：各分隔形态等价；看不懂的一律报错（不猜、不静默丢）
  ② 归一：越界收进画布 + **如实计数**；文字出屏报错（6/12 像素规则）
  ③ 编码：逐字节确定的 payload；不含 req_id；不超字节上限
  ④ 摘要：逐字确定的人话；空 ops 不产空串
  ⑤ **跨仓守卫**：op 名单/参数个数/6-12 规则/上限/固件版本，逐项与固件源码比对
  ⑥ 工具层：零下发的每条路都不留回执（`__ERROR__` 帧）、去重与并发、失败撤占位
  ⑦ 接线：注册表 / scope / 技能模板 / 措辞臂 / 参数必填性
  ⑧ 快道：命中画图说法，**不**命中漫画/动画/画风（负例锁）
  ⑨ 能力边界与"免问"语义写在模型/系统读得到的地方
  ⑩ **回执必须有据**：创作两次都失败 ⇒ 该 spec 既不下发、也不留回执（打桩端到端）

跑法（cd saudade-blog-agent）：`.venv/bin/python tests/test_oled_draw.py`
固件仓不在本机常见位置时：`SAUDADE_FIRMWARE_REPO=/path/to/ESP32-S3-OBC`；
夜间门禁设 `SAUDADE_REQUIRE_FIRMWARE=1` ⇒ ⑤ 跑不到就红（CI 接线本批有意延后，见
`tests/_firmware_repo.py` 头注）。
"""
import inspect
import json
import re
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _firmware_repo  # noqa: E402
from agent import authz, decisions, refs  # noqa: E402
from agent import oled_draw as OD  # noqa: E402
from agent import skills as S  # noqa: E402
from agent.action_text import receipt_action, tool_action_text  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402

FAILS: list[str] = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name} {detail}")
    else:
        print(f"  ✓ {name}")


def eq(got, exp, name):
    check(name, got == exp, f"got={got!r} exp={exp!r}")


# ────────────────────────────────── ① 解析

def test_parse():
    print("\n── ① 解析：分隔形态等价、看不懂就报错 ──")
    want = [["circle", 64, 26, 18], ["pixel", 58, 20]]
    eq(OD.parse_dsl("circle 64,26,18\npixel 58,20"), want, "换行分隔")
    eq(OD.parse_dsl("circle 64,26,18; pixel 58,20"), want, "分号分隔")
    eq(OD.parse_dsl("circle 64,26,18；pixel 58,20"), want, "全角分号分隔")
    eq(OD.parse_dsl("  circle 64,26,18  \n\n  pixel 58,20  \n"), want, "多余空白与空行")
    eq(OD.parse_dsl("circle 64，26，18\npixel 58,20"), want, "全角逗号")
    eq(OD.parse_dsl("circle\t64,26,18\npixel 58,20"), want, "op 名与参数之间是制表符")
    eq(OD.parse_dsl("```\ncircle 64,26,18\npixel 58,20\n```"), want, "代码围栏（搬运痕迹）")
    eq(OD.parse_dsl("text 44,52,加油，你最棒"), [["text", 44, 52, "加油，你最棒"]],
       "text 的文案可含全角逗号（不再是分隔符）")
    eq(OD.parse_dsl("circle ６４,26,18"), [["circle", 64, 26, 18]], "全角数字")
    eq(OD.parse_dsl("pixel -3,10"), [["pixel", -3, 10]], "负数解出来（越界交给归一）")

    for bad, why in (("circle 64,26", "参数个数少给"),
                     ("circle 64,26,18,1", "参数个数多给"),
                     ("circel 64,26,18", "op 名字拼错"),
                     ("circle 64,26,x", "数字位给了字母"),
                     ("circle 64.5,26,18", "非整数"),
                     ("text 44,52,", "文字为空"),
                     ("", "空输入"),
                     ("   \n  ", "全空白")):
        try:
            OD.parse_dsl(bad)
            check(f"报错：{why}", False, f"{bad!r} 没报错")
        except OD.OledDrawError:
            check(f"报错：{why}", True)
        except Exception as e:                       # 别的异常族说明抛得不规范
            check(f"报错：{why}", False, f"{type(e).__name__}: {e}")

    # 报错文案要能教模型改对（合法名单在里面）——纠偏一次就够，不该让它猜
    try:
        OD.parse_dsl("circel 64,26,18")
    except OD.OledDrawError as e:
        check("报错文案带合法 op 名单（创作层纠偏一次就能改对）",
              "circle" in str(e) and "pixel" in str(e), str(e)[:70])
    try:
        OD.parse_dsl("circle 64,26")
    except OD.OledDrawError as e:
        check("报错文案带上出错的那一条与签名",
              "x,y,r" in str(e) and "64,26" in str(e), str(e)[:70])
    # normalize 单独被调用时也要自证（`action_text` 的措辞臂走 normalize→describe 那条路）
    try:
        OD.normalize([["nope", 1]])
        check("normalize 单独调用也拦未知 op", False, "没报错")
    except OD.OledDrawError:
        check("normalize 单独调用也拦未知 op", True)


# ────────────────────────────────── ② 归一

def test_normalize():
    print("\n── ② 归一：钳制如实计数、文字出屏报错 ──")
    ops, notes = OD.normalize([["pixel", 999, -5]])
    eq(ops, [["pixel", 127, 0]], "越界坐标钳进画布")
    check("钳制**如实计数**（不装作没发生）", notes and "越界" in notes[0], str(notes))

    ops, notes = OD.normalize([["pixel", 10, 10]])
    eq(notes, [], "没越界就不虚报")

    ops, notes = OD.normalize([["rbox", 10, 10, 20, 20, 99]])
    eq(ops, [["rbox", 10, 10, 20, 20, 10]],
       "圆角半径按 min(w,h)/2 收（u8g2 对更大的 r 画出来是坏的）")
    check("收半径也记账", notes and "越界" in notes[0], str(notes))

    many = [["pixel", i % 128, i % 64] for i in range(OD.MAX_OPS + 3)]
    ops, notes = OD.normalize(many)
    eq(len(ops), OD.MAX_OPS, f"超出 {OD.MAX_OPS} 个的部分被截掉")
    check("截掉的个数如实记账",
          any("丢弃" in n and "3" in n for n in notes), str(notes))

    # 6/12 规则：ASCII 6px、其余 12px（与固件 utf8_char_width 同一条）
    eq(OD.text_width("abc"), 18, "ASCII 宽 6px/字")
    eq(OD.text_width("加油"), 24, "汉字宽 12px/字")
    eq(OD.text_width("ab油"), 24, "混排按各自宽度累加")
    for dsl, why in (("text 120,52,加油", "从 x=120 起会画出屏幕"),
                     ("text 0,52,一二三四五六七八九十十一", "整段自己就超 128 像素")):
        try:
            OD.build(dsl)
            check(f"文字出屏报错：{why}", False, dsl)
        except OD.OledDrawError as e:
            check(f"文字出屏报错：{why}", "宽" in str(e), str(e)[:60])
    # 边界：正好放得下不许报错（判据不虚严——虚严会让创作层白纠偏一轮）
    eq(OD.build("text 104,52,加油").ops, (("text", 104, 52, "加油"),), "恰好贴边不报错")


# ────────────────────────────────── ③ 编码

def test_encode():
    print("\n── ③ 编码：逐字节确定的 payload ──")
    d = OD.build("circle 64,26,18\npixel 58,20\nline 48,42,80,42\ntext 44,52,加油")
    eq(d.payload,
       '{"type":"draw","ops":[["circle",64,26,18],["pixel",58,20],'
       '["line",48,42,80,42],["text",44,52,"加油"]]}',
       "固定 DSL → 逐字节相同的 payload（中文原文，不转义）")
    obj = json.loads(d.payload)
    eq(obj["type"], "draw", "type == draw（固件按它选分支）")
    check("Python 不填 req_id（由 device-service 按 X-Request-Id 注入）",
          "req_id" not in d.payload, d.payload[:60])
    check(f"payload ≤ {OD.MAX_PAYLOAD_BYTES} 字节",
          len(d.payload.encode("utf-8")) <= OD.MAX_PAYLOAD_BYTES,
          len(d.payload.encode("utf-8")))
    eq(len(obj["ops"]), 4, "ops 是数组的数组（固件用 cJSON 直接遍历，不在 C 里解析 DSL）")
    eq(OD.build("circle 64,26,18\npixel 58,20\nline 48,42,80,42\ntext 44,52,加油").payload,
       d.payload, "同样的输入两次 build 出同一份字节（可作去重签名）")
    # 上限是**真的会拦**：塞满到超限必须报错，而不是发出去被固件截掉一半
    big = "\n".join(["text 0,10," + "一二三四五六七八九十"] * OD.MAX_OPS)
    try:
        OD.build(big)
        check("超字节上限报错（旧固件 512 字节静默截断的那道红）", False, "没报错")
    except OD.OledDrawError as e:
        check("超字节上限报错（旧固件 512 字节静默截断的那道红）", "字节" in str(e), str(e)[:60])


# ────────────────────────────────── ④ 摘要

def test_describe():
    print("\n── ④ 摘要：逐字确定的人话 ──")
    d = OD.build("line 0,32,127,32\ndisc 64,26,18\ntext 44,52,加油")
    eq(d.summary, "线(0,32→127,32)、实心圆(64,26,r18)、字「加油」(44,52)", "逐字相同")
    eq(OD.describe([]), "（无图形）", "空 ops 不产空串（回执行不能出现空动作）")
    # 9 个 op 都要有话说（新加一条 op 忘了写这句 → 这里红）
    ones = {"pixel": "pixel 1,2", "line": "line 1,2,3,4", "box": "box 1,2,3,4",
            "frame": "frame 1,2,3,4", "rbox": "rbox 1,2,3,4,1", "disc": "disc 1,2,3",
            "circle": "circle 1,2,3", "tri": "tri 1,2,3,4,5,6", "text": "text 1,2,字"}
    eq(sorted(ones), sorted(OD.OP_NAMES), "判据自己不漏 op（9 个都在表里）")
    for name, one in ones.items():
        s = OD.build(one).summary
        check(f"{name} 有中文说法", bool(s) and not s.startswith(name), s)
    long_d = OD.build("\n".join(["pixel 1,2"] * 40)).summary
    check("长摘要带如实标注（列不下就数个数，不静默截断）",
          "共 40 个图形" in long_d and len(long_d) <= OD._DESC_LIMIT + 40, long_d[-34:])


# ────────────────────────────────── ⑤ 跨仓守卫

_C_OP_RE = re.compile(r'\{\s*"(\w+)",\s*(\d+),\s*(true|false)\s*,')


def test_firmware_contract():
    print("\n── ⑤ 跨仓守卫：与固件 main.c 逐项比对 ──")
    src = _firmware_repo.read("main/main.c",
                              "画板的 op 名单/参数个数/6-12 像素宽度规则/上限，固件与 agent "
                              "两侧各有一份实现，谁改了自己那半而不知道另一侧，症状是"
                              "「屏幕怎么没变」或「画了一半」")
    if src is None:
        return                       # 三态守卫已经打了响亮的 ⏭
    block = src.split("DRAW_OPS[] = {", 1)
    check("固件里有 DRAW_OPS 派发表", len(block) == 2, "找不到 DRAW_OPS[] = {")
    if len(block) != 2:
        return
    table = block[1].split("};", 1)[0]
    fw = {n: (int(argc), tail == "true") for n, argc, tail in _C_OP_RE.findall(table)}
    check("固件 op 表解析出来了（正则失配会让下面几条假绿）", len(fw) >= 9, str(fw))
    missing = [n for n in OD.OP_NAMES if n not in fw]
    check("Python 的 op 名单 ⊆ 固件（多一个 = 屏幕不会变）", not missing, str(missing))
    for name, spec in OD.OPS.items():
        eq(fw.get(name), (spec.nargs, spec.tail_str), f"{name} 的参数个数/末位文字与固件一致")

    m = re.search(r"utf8_char_width\(const char \*c\)\s*\{(.*?)\}", src, re.S)
    check("固件里有 utf8_char_width", m is not None)
    if m:
        nums = re.search(r"<\s*0x80\s*\)\s*\?\s*(\d+)\s*:\s*(\d+)", m.group(1))
        check("6/12 像素规则与固件一致（第二份实现，靠这条钉住）",
              nums is not None
              and (int(nums.group(1)), int(nums.group(2))) == (OD._ASCII_W, OD._CJK_W),
              nums.group(0) if nums else "没解析出三元表达式")
    eq(int(re.search(r"#define\s+DRAW_MAX_OPS\s+(\d+)", src).group(1)), OD.MAX_OPS,
       "MAX_OPS 与固件 DRAW_MAX_OPS 同值")
    rx = int(re.search(r"#define\s+RX_BUF_SIZE\s+(\d+)", src).group(1))
    check(f"payload 上限（{OD.MAX_PAYLOAD_BYTES}）< 固件缓冲（{rx}）——留余量，不贴线走",
          OD.MAX_PAYLOAD_BYTES < rx, f"{OD.MAX_PAYLOAD_BYTES} vs {rx}")
    ver = re.search(r'#define\s+APP_VERSION\s+"(\d+)\.(\d+)\.(\d+)"', src)
    check("固件版本 ≥ 1.3.0（draw 是 1.3.0 起的指令；OTA 判据是 strcmp != 0，不 bump 设备不拉）",
          ver is not None and tuple(int(x) for x in ver.groups()) >= (1, 3, 0),
          ver.group(0) if ver else "没解析出 APP_VERSION")
    check("固件真有 draw 分支与执行函数（只认 display 的固件不回执，症状像设备离线）",
          '"draw"' in src and "oled_draw_ops" in src)


# ────────────────────────────────── ⑥ 工具层（httpx 打桩）

class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _Httpx:
    """记录 PUT 的 (url, headers, content)；GET 分「设备列表」与「指令回执状态」两种。"""

    def __init__(self, put=None, acked=True, devices=({"id": "dev-1", "online": True},)):
        self.puts: list = []
        self._put = put or _Resp({"req_id": "r1"})
        self._acked = acked
        self._devices = list(devices)

    def get(self, url, **kw):
        if url.endswith("/cmd/r1"):
            return _Resp({"acked": self._acked})
        return _Resp(self._devices)

    def put(self, url, headers=None, content=None, **kw):
        self.puts.append((url, dict(headers or {}), content))
        return self._put


def test_tool_layer():
    print("\n── ⑥ 工具层：零下发的路不留回执、去重与并发 ──")
    import tools.base as base

    orig = (base.httpx, base._valid_device_id, base._sign_user_jwt, base.time.sleep)
    stub = _Httpx()
    base.httpx, base._valid_device_id, base._sign_user_jwt = stub, (lambda x: True), (lambda u: "t")
    base.time.sleep = lambda s: None          # 5 秒回执轮询不该让套件慢；finally 里还原
    cfg = lambda uid: {"configurable": {"user_id": uid}}       # noqa: E731
    DSL = "circle 64,26,18\ntext 44,52,加油"
    try:
        # ① 身份缺失：零下发 + 错误帧（**不是**回纯字符串——纯字符串会被判 PASS 记成事实）
        stub.puts.clear()
        out = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(0)))
        check("uid≤0 零下发", stub.puts == [], str(stub.puts))
        check("uid≤0 返回错误帧（无回执 ⇒ 台账不留一笔没发生过的屏幕变化）",
              out.startswith("__ERROR__"), out[:60])

        # ② 非法 ops：零下发 + 错误帧（文案里带得出纠偏信息）
        stub.puts.clear()
        out = str(base.device_oled_draw.invoke({"ops": "circle 64,26"}, config=cfg(1)))
        check("非法 ops 零下发", stub.puts == [], str(stub.puts))
        check("非法 ops 返回错误帧且说明原因",
              out.startswith("__ERROR__") and "参数" in out, out[:80])

        # ③ 正常路径：发出去的就是 encode() 那一份字节
        base._last_draw.clear()
        stub.puts.clear()
        out = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(1)))
        eq(len(stub.puts), 1, "下发一次")
        url, headers, content = stub.puts[0]
        eq(url, f"{base.DEVICE_SERVICE_URL}/api/devices/dev-1/cmd", "PUT 到设备 cmd 端点")
        eq(content, OD.build(DSL).payload.encode("utf-8"),
           "线上发出的字节 == 判据里测的那份（不是让 httpx 再序列化一遍）")
        eq(headers.get("Content-Type"), "application/json",
           "带 Content-Type（axum 的 Json 提取器要它）")
        check("回执确认后如实说已确认，并印出系统算出来的画了什么",
              "设备已确认执行" in out and "空心圆(64,26,r18)" in out, out[:80])

        # ④ 未回执：如实说未确认（软失败——指令确已下发，照常有回执）
        base._last_draw.clear()
        base.httpx = _Httpx(acked=False)
        out = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(1)))
        check("设备未回执时如实说未确认（不假装已显示）",
              "未在 5 秒内回执确认" in out and not out.startswith("__ERROR__"), out[:70])

        # ⑤ 404 / 409：错误帧（没下发 ⇒ 无回执）
        for status, kw in ((404, "不存在"), (409, "不在线")):
            base._last_draw.clear()
            base.httpx = _Httpx(put=_Resp({}, status=status))
            out = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(1)))
            check(f"HTTP {status} 返回错误帧（{kw}）",
                  out.startswith("__ERROR__") and kw in out, out[:60])

        # ⑥ 没绑设备：空结果是**事实**（照常有据，与"下发失败"不是同一条路）
        base._last_draw.clear()
        nod = _Httpx(devices=())
        base.httpx = nod
        out = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(1)))
        check("没绑设备 ⇒ 零下发 + 如实说空（不是错误帧）",
              nod.puts == [] and "还没有绑定" in out and not out.startswith("__ERROR__"),
              out[:60])

        # ⑦ 去重：同 payload 30s 内不重发；不同 payload 各自发；不同用户互不影响
        base._last_draw.clear()
        stub = _Httpx()
        base.httpx = stub
        base.device_oled_draw.invoke({"ops": DSL}, config=cfg(2))
        b = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(2)))
        eq(len(stub.puts), 1, "同内容第二次不重发")
        check("去重时如实说明（且不去回错误帧——那份内容确实已下发过）",
              "刚刚已下发过" in b and not b.startswith("__ERROR__"), b[:50])
        base.device_oled_draw.invoke({"ops": "disc 20,20,8"}, config=cfg(2))
        eq(len(stub.puts), 2, "不同内容各自发")
        base.device_oled_draw.invoke({"ops": DSL}, config=cfg(3))
        eq(len(stub.puts), 3, "另一个用户不被前一个用户的占位挡住")

        # ⑧ 失败撤占位：否则一次失败会挡掉 30s 内的正常重试
        base._last_draw.clear()
        base.httpx = _Httpx(put=_Resp({}, status=409))
        base.device_oled_draw.invoke({"ops": DSL}, config=cfg(4))
        check("失败不留占位（30s 内的重试不被误挡）", base._last_draw.get(4) is None)

        # ⑨ 并发：8 个同内容只有 1 个真下发（占位与检查在同一把锁里）
        base._last_draw.clear()
        stub = _Httpx()
        base.httpx = stub
        res: list = []
        lock = threading.Lock()

        def call():
            r = str(base.device_oled_draw.invoke({"ops": DSL}, config=cfg(5)))
            with lock:
                res.append(r)

        ts = [threading.Thread(target=call) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        eq(len(stub.puts), 1, "并发 8 次只真下发 1 次")
        eq(sum(1 for r in res if "刚刚已下发过" in r), 7, "其余 7 次被去重")
    finally:
        base.httpx, base._valid_device_id, base._sign_user_jwt, base.time.sleep = orig
        base._last_draw.clear()


# ────────────────────────────────── ⑦ 接线

def test_wiring():
    print("\n── ⑦ 接线：注册表 / scope / 技能模板 / 措辞臂 ──")
    from agent.graph import _TOOL_MAP
    check("工具在注册表里（_TOOL_REGISTRY 是手工名单，漏一行则参数规格退化成 any）",
          "device_oled_draw" in _TOOL_MAP)
    eq(authz.required_scope("device_oled_draw"), "write.device", "scope = write.device（与屏显同档）")
    sk = S.SKILL_MAP.get("device_draw")
    check("技能在册", sk is not None)
    eq(sk.plan, [("device_oled_draw", {"ops": "$ops"})], "技能模板：ops 走占位符")
    spec = S.skill_param_specs(sk)["ops"]
    check("ops 不是必填（必填则画板从 planner 通道整体不可达）", not spec.required, str(spec))
    eq(spec.type, "str", "ops 的类型从工具 schema 派生")
    check("技能对普通访客可见（与屏显同档）",
          "device_draw" in [s.name for s in S.visible_skills("user")])
    obj = instantiate_plan("device_draw", {})
    eq(obj["skill"], "device_draw", "计划里的技能名")
    eq(len(obj["tools"]), 1, "技能模板恰好一条工具调用")
    check("缺参展开成空参调用（ops 由 execute 的创作层补）", "device_oled_draw(" in obj["tools"][0],
          obj["tools"][0])
    # 措辞臂：没有它 `receipt_action` 返回空串 ⇒ 台账行只剩 Rust 老表兜底，
    # `test_action_text.py` 的"未武装集合恰好是那两件死工具"直接红
    full = receipt_action("device_oled_draw", {"ops": "circle 64,26,18"}, {})
    check("台账行有真臂（带系统印的画了什么）",
          full.startswith("屏幕绘图「") and "空心圆(64,26,r18)" in full, full)
    prev = tool_action_text("device_oled_draw", {"ops": "circle 64,26,18"}, {}, preview=True)
    check("过程行是同一句话的短版", prev.startswith("屏幕绘图「") and len(prev) <= 24 + 8, prev)
    eq(receipt_action("device_oled_draw", {"ops": "看不懂的指令"}, {}), "屏幕绘图",
       "解析不了就只说「屏幕绘图」，绝不编画了什么")


# ────────────────────────────────── ⑧ 快道

def test_fast_path():
    print("\n── ⑧ 快道：命中画图说法，不误伤漫画/动画 ──")
    hits = ("在屏幕上画个笑脸", "屏幕上画个爱心吧", "给我画个箭头到屏幕上",
            "OLED 上绘制一个太阳", "屏幕上涂鸦几笔")
    for msg in hits:
        obj = decisions._draw_fast_path(msg)
        check(f"命中：{msg}", obj is not None and obj["skill"] == "device_draw",
              str(obj)[:60] if obj else "None")
    misses = ("这个漫画在屏幕上看很爽", "动画在屏幕上播放", "屏幕上的画面真好看",
              "你画画真好看", "国画在屏幕里显示", "屏幕上那个描述很准",
              "屏幕上画的这是什么？", "不用在屏幕上画了", "为什么屏幕上画了个圈")
    for msg in misses:
        check(f"不命中：{msg}", decisions._draw_fast_path(msg) is None, "命中了（快道误伤）")
    check("绘图说法不被显示快道截胡（两套动词表互斥）",
          decisions._display_fast_path("在屏幕上画个笑脸") is None)
    check("显示说法不被绘图快道截胡",
          decisions._draw_fast_path("屏幕上显示一段文字") is None)
    check("显示快道自己还在（对照：证明上一条不是因为显示快道整体坏了）",
          decisions._display_fast_path("屏幕上显示一段文字") is not None)
    # 意图扫描（多意图丢失那条链）：两个意图各自记账，谁都不许漏
    intents = decisions._scan_action_intents("在屏幕上画个笑脸")
    check("意图扫描认得出「屏幕绘图」",
          any(i["key"] == "draw" and i["tool"] == "device_oled_draw" for i in intents),
          str(intents)[:120])
    intents2 = decisions._scan_action_intents("屏幕上显示一段文字")
    check("意图扫描不把显示当绘图",
          any(i["key"] == "display" for i in intents2)
          and not any(i["key"] == "draw" for i in intents2), str(intents2)[:120])


# ────────────────────────────────── ⑨ 能力边界 + 免问语义

def test_capability_and_consent():
    print("\n── ⑨ 能力边界与「免问」语义写在读得到的地方 ──")
    sk = S.SKILL_MAP["device_draw"]
    check("能力清单写了画布尺寸", "128×64" in sk.capability, sk.capability)
    check("能力清单写了图形种类（圆/线/矩形/三角/点/文字都在）",
          all(w in sk.capability for w in ("圆", "线", "矩形", "三角", "点", "文字")),
          sk.capability)
    check("技能描述说的是「画图」而不是「写文字」（与 device_display 分得开）",
          "画图" in sk.description and S.SKILL_MAP["device_display"].plan != sk.plan,
          sk.description)
    check("回复契约禁止编造屏幕细节（没有回读通道）",
          "不得" in sk.reply_contract and "回读" in sk.reply_contract, sk.reply_contract)
    # op 表进**模型读得到的地方**（创作提示词与工具描述共用 OPS_HELP 这一份实现）
    lines = OD.OPS_HELP.strip().splitlines()
    eq(len(lines), len(OD.OPS), f"OPS_HELP 一行一个 op（共 {len(OD.OPS)}）")
    for name in OD.OP_NAMES:
        check(f"OPS_HELP 里有 {name}", any(ln.startswith(name + " ") for ln in lines))
    from tools.base import device_oled_draw as tool
    doc = tool.description
    for name in OD.OP_NAMES:
        check(f"工具描述里有 {name}", name in doc)
    check("工具描述写明没有回读通道（不许描述屏幕细节）", "回读" in doc)
    gsrc = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("创作提示词用的是 OPS_HELP 这一份（不手写第二张 op 表）",
          "{ops_help}" in gsrc and "OPS_HELP" in gsrc)
    # 免问语义：与屏显**结构性**一致（write.device 不进同意闸、不在强制弹窗名单）
    check("write.device 不在同意闸里（设备写结构性免问，与屏显一致）",
          "write.device" not in authz.CONSENT_SCOPES, str(sorted(authz.CONSENT_SCOPES)))
    check("不在强制弹窗名单里", "device_oled_draw" not in authz._ALWAYS_CONFIRM_TOOLS)


# ────────────────────────────────── ⑩ 回执必须有据

def test_creation_failure_leaves_no_receipt():
    print("\n── ⑩ 创作两次都失败：不下发、不留回执 ──")
    import agent.graph as g
    from agent.graph import execute_node, plan_state
    from langchain_core.messages import HumanMessage

    calls = {"n": 0}

    class _Spy:
        name = "device_oled_draw"

        def invoke(self, args):
            calls["n"] += 1
            return "OLED 绘图指令已下发，设备已确认执行：点什么"

    orig_tool = g._TOOL_MAP.get("device_oled_draw")
    orig_create = g._create_draw_ops
    g._TOOL_MAP["device_oled_draw"] = _Spy()
    g._create_draw_ops = lambda user_msg, page_ctx: None       # 两次都失败
    try:
        obj = instantiate_plan("device_draw", {})
        state = {**plan_state(obj), "plan_rounds": 1, "done": False,
                 "messages": [HumanMessage(content="在屏幕上画个笑脸")]}
        out = execute_node(state, {"configurable": {"stop_event": threading.Event()}})
        eq(calls["n"], 0, "创作失败 ⇒ 工具零调用（屏幕没动）")
        blocked = [b for b in out.get("blocked") or [] if b.get("tool") == "device_oled_draw"]
        eq(len(blocked), 1, "进了受阻项")
        eq(blocked[0]["reason"] if blocked else None, "error_frame",
           "受阻原因码是**通用的** error_frame（不新开原因码）")
        eq([r for r in out.get("receipts") or [] if r.get("tool") == "device_oled_draw"], [],
           "**没有回执**（台账不留一笔没发生过的屏幕变化）")
        check("帧文本是通用 __ERROR__ 族（不新开帧族）",
              bool(blocked) and str(blocked[0]["result"]).startswith("__ERROR__"),
              str(blocked[0]["result"])[:60] if blocked else "")
    finally:
        g._TOOL_MAP["device_oled_draw"] = orig_tool
        g._create_draw_ops = orig_create

    # 纯函数那半：帧文案落 `error_frame`，不被别的取码器偷走（三个取码器一个都不许命中）
    frame = "__ERROR__: 绘图指令创作失败（两次都解析不出合法图形，未下发）"
    for fn, name in ((refs.ref_error_reason, "ref_err"),
                     (authz.scope_error_reason, "scope"),
                     (authz.consent_error_reason, "consent")):
        check(f"帧不被 {name} 取码器命中", fn(frame) is None, str(fn(frame)))
    eq(g._check_spec("device_oled_draw", {"ops": "circle 1,2,3"}, True, frame, "device_draw"),
       ("BLOCK", "error_frame"), "_check_spec 判 BLOCK + error_frame")
    # 分界要在 checker 上闭合（这是"回执必须有据"的另一半）：**每一条"没下发"都是
    # BLOCK**（无台账行），而"你还没绑定设备"（空结果是事实）**判 PASS**（照常有据）
    # ——两类都由同一个 `_check_spec` 判，所以这条线不可能只靠工具那侧自觉。
    for txt, why in (("__ERROR__: 无法获取当前用户身份，绘图指令未下发", "身份缺失"),
                     ("__ERROR__: 绘图指令非法（未下发）：第 1 条…", "指令非法"),
                     ("__ERROR__: 设备不存在或不属于当前用户，绘图指令未下发", "404"),
                     ("__ERROR__: 设备当前不在线，无法绘图（设备可能断电或 MQTT 连接断开）",
                      "409")):
        eq(g._check_spec("device_oled_draw", {"ops": "x"}, True, txt, "device_draw"),
           ("BLOCK", "error_frame"), f"{why} ⇒ BLOCK（无回执）")
    eq(g._check_spec("device_oled_draw", {"ops": "x"}, True, "当前用户还没有绑定任何 IoT 设备",
                     "device_draw", "empty"),
       ("PASS", "ok"), "空结果是事实 ⇒ PASS（与「没下发」分得开）")
    # 源码锁：这一支**绝不给它 `meta["cmd"]`**（那是命令工具的契约，撞上去是另一个
    # 原因码 `cmd_shape`——红在这里说明有人接错了支路）
    cs = inspect.getsource(g._create_draw_ops)
    check('创作层不设 meta["cmd"] =', 'meta["cmd"] =' not in cs)
    gsrc = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("失败帧原文在 graph.py 里（判据锚的是真源码，不是记忆里的字符串）",
          'out = "__ERROR__: 绘图指令创作失败（两次都解析不出合法图形，未下发）"' in gsrc)
    branch = gsrc.split("elif draw_create_failed:", 1)[1].split("\n        else:", 1)[0]
    check('失败分支里没有 meta["cmd"] = 的赋值', 'meta["cmd"] =' not in branch, branch[:200])


def main():
    for fn in (test_parse, test_normalize, test_encode, test_describe, test_firmware_contract,
               test_tool_layer, test_wiring, test_fast_path, test_capability_and_consent,
               test_creation_failure_leaves_no_receipt):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
