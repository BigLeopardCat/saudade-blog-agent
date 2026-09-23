# -*- coding: utf-8 -*-
"""确认令牌（agent/confirm.py）+ 确认弹窗链路（graph._confirm_popup / _confirm_grant_plan）。

秒级、纯函数、无网络无 LLM；由 eval.yml 在 push 时跑。

覆盖四块：
  ① 令牌本身：签发→验签往返；签名篡改 / 过期 / 换 uid / 换会话 / 空密钥 / 格式坏
     → **一律 None**（fail-closed），且不抛异常。
  ② 令牌的边界：`$ref` 残留不签发；密钥空缺不签也不验（不降级）。
  ③ 弹窗从哪来（_confirm_popup）：该弹的弹、不该弹的不弹（提问/假设、已判成命令、
     无权限、目标无据、参数没解析出来）。
  ④ 点确定之后：_confirm_grant_plan 照签名拼计划（技能对不上→空清单）；
     execute 放行同意闸与目标有据两门，但**权限不放行**。
  ⑤ 授权式短应答（P2）：台账唯一待审 ⇒ 目标由系统定，但**仍要主人点一下**。
  ⑥ 跨轮待办的结构化形态（P4）：弹窗那一轮同时产出 pending_action，
     且字段/同源关系/MISS 语义与 Rust 侧的读取契约一致。
  ⑦ 被拒的确认请求留痕（20260924）：元数据里**有**会话 id 与令牌长度、
     **没有**令牌本身；并断言两个调用点在 server.py 里真的接上了。

用法：.venv/bin/python tests/test_confirm.py
"""

from __future__ import annotations

import json
import re
import sys
import time

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pathlib import Path

# ── 仓根（20260924：测试统一搬进 tests/）───────────────────────────────────────
# 此前本文件就躺在仓根，`sys.path[0]` 天然是仓根；搬进 tests/ 之后要靠这两行才 import
# 得到 agent/ tools/ rag/。
ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from tools import base as _base  # 工具的返回值契约（ToolResult：str 子类 + kind）

import agent.graph as g
from agent import adminops as A
from agent import authz
from agent import confirm
from agent.graph import _confirm_grant_plan, _confirm_popup, execute_node, parse_plan, plan_encode
from agent.skills import instantiate_plan  # noqa: E402
from agent.principal import Principal

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


# ── 密钥桩：settings.jwt_secret 是全局单例，测试里直接改它 ────────────────
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
# 桩值（不是 _SAVED_SECRET）：CI 里没有 .env，settings.jwt_secret 默认是空串，
# 而"密钥空缺不签也不验"是本套件的一条断言 —— 中间把密钥清空后必须**恢复成桩值**，
# 否则后面所有"该弹窗"的正例全部静默变成"签不出令牌 → 不弹"（20260921 CI 实测：
# 三条正例红、全部反例照样绿，正是这种"反例恒真"的假绿形态）。
_STUB_SECRET = "test-secret-for-confirm-tokens"
settings.jwt_secret = _STUB_SECRET

SPECS = [{"tool": "create_tag", "args": {"title": "测试标签", "parent_id": None, "color": "#eb2f96"}}]

print("① 令牌签发/验签")
tok = confirm.sign(7, 42, "tag_create", SPECS)
check("往返：verify(sign(...)) 取回同一份 payload",
      (lambda p: p is not None and p["uid"] == 7 and p["conv"] == 42
       and p["skill"] == "tag_create" and p["specs"] == SPECS)(confirm.verify(tok, 7, 42)))
check("令牌形状：两段 base64url（正文 + 签名），长度可控",
      tok.count(".") == 1 and len(tok) < 2000, str(len(tok)))
check("换人 → None（令牌绑 uid）", confirm.verify(tok, 8, 42) is None)
check("换会话 → None（令牌绑会话）", confirm.verify(tok, 7, 43) is None)
check("会话从有到无 → None（不能让绑了会话的令牌在无会话请求里生效）",
      confirm.verify(tok, 7, None) is None)
check("篡改签名 → None",
      confirm.verify(tok[:-2] + ("aa" if not tok.endswith("aa") else "bb"), 7, 42) is None)
check("篡改正文（换一个标签名）→ None（签名覆盖正文）",
      confirm.verify(confirm._b64e(json.dumps(
          {"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) + 600,
           "skill": "tag_create",
           "specs": [{"tool": "create_tag", "args": {"title": "别的标签"}}]},
          ensure_ascii=False, separators=(",", ":")).encode()) + "." + tok.split(".")[1],
          7, 42) is None)
check("格式坏（无点 / 空 / 非字符串）→ None 且不抛",
      confirm.verify("nodot", 7, 42) is None and confirm.verify("", 7, 42) is None
      and confirm.verify(None, 7, 42) is None)
check("base64 坏 → None 且不抛", confirm.verify("!!!.???", 7, 42) is None)

# 过期：把 exp 挪到过去再用真密钥签一次（模拟"昨天的令牌"）
_past = {"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) - 1,
         "skill": "tag_create", "specs": SPECS}
_body = json.dumps(_past, ensure_ascii=False, separators=(",", ":")).encode()
import hashlib  # noqa: E402
import hmac  # noqa: E402

_sig = hmac.new(settings.jwt_secret.encode(), confirm._DOMAIN + _body, hashlib.sha256).digest()
_stale = confirm._b64e(_body) + "." + confirm._b64e(_sig)
check("过期（exp 在签发后已过）→ None（签名合法也不行）",
      confirm.verify(_stale, 7, 42) is None)
check("TTL 是 10 分钟量级（够读完想一下，又短到过夜必失效）",
      300 <= confirm.TTL_SECONDS <= 900, str(confirm.TTL_SECONDS))
# token_expiry：给前端画倒计时用的展示值（20260924）。它必须**读令牌自己**，而不是
# 重算 now+TTL——重算会让展示值比签名值晚一秒，卡片在令牌失效后还多活一会儿。
check("token_expiry：解出来的就是签名里那一个（与 verify 的 payload 逐字相等）",
      confirm.token_expiry(tok) == (confirm.verify(tok, 7, 42) or {}).get("exp"))
check("token_expiry：坏输入一律 0、不抛（前端按'无倒计时'处理，不影响验签）",
      confirm.token_expiry("") == 0 and confirm.token_expiry("abc") == 0
      and confirm.token_expiry("!!!.???") == 0 and confirm.token_expiry(None) == 0)

print("\n② 令牌边界")
check("specs 带 $ref → 不签发（引用依赖签发轮的帧，执行轮早已不在）",
      confirm.sign(7, 42, "tag_create",
                   [{"tool": "set_article_tags",
                     "args": {"article_id": 12, "add": ["$list_tags[0].name"]}}]) == "")
check("$ref 在嵌套 list 里也算（只看顶层会漏）",
      confirm.sign(7, 42, "article_tags",
                   [{"tool": "set_article_tags", "args": {"add": ["$x[0].y"]}}]) == "")
check("技能名为空 → 不签发（执行轮无从拼计划）", confirm.sign(7, 42, "", SPECS) == "")
check("不可序列化的参数 → 不签发（不抛）",
      confirm.sign(7, 42, "tag_create",
                   [{"tool": "create_tag", "args": {"title": object()}}]) == "")
settings.jwt_secret = ""
check("密钥空缺 → **既不签也不验**（绝不降级成无签名令牌）",
      confirm.sign(7, 42, "tag_create", SPECS) == "" and confirm.verify(tok, 7, 42) is None)
settings.jwt_secret = _STUB_SECRET   # 恢复桩值（见上方 _STUB_SECRET 处的说明）

print("\n③ 弹窗从哪来（_confirm_popup）")
# 探针：下面整节的"反例"都是 `is None`——密钥若为空，签不出令牌会让**正例也**变 None，
# 于是看起来"全都符合预期"。先证一次"此刻签得出来"，再往下判。
check("前置探针：此刻密钥在位、签得出令牌（下面正例才有意义）",
      len(confirm.sign(7, 42, "tag_create", SPECS)) > 20)
SPEC_STATUS = 'set_article_status({"article_id": 12, "status": "private"})'
PLAN_STATUS = ('SKILL=article_status\nPARAMS={}\nTOOLS: ' + SPEC_STATUS
               + '\nNOTE: x\nREPLY: y')
PLAN_TAG = ('SKILL=tag_create\nPARAMS={}\nTOOLS: create_tag({"title": "测试标签", '
            '"parent_id": null, "color": "#eb2f96"})\nNOTE: x\nREPLY: y')
CFG = {"configurable": {"principal": Principal(uid=7, role="admin"), "user_id": 7,
                        "conversation_id": 42, "stop_event": None}}
EVID = ToolMessage(content="后台文章共 3 篇：\n- id=12 [私密]《架构文档》标签：",
                   tool_call_id="t1", name="list_admin_notes")


def _popup(msg, plan=PLAN_TAG, cfg=CFG, extra=(), principal=None):
    st = {"messages": [HumanMessage(content=msg), *extra], "plan": plan,
          "plan_rounds": 0, "done": False}
    return _confirm_popup(st, parse_plan(st["plan"])["tools"],
                          principal or Principal(uid=7, role="admin"), msg, cfg)

p = _popup("一级标签，名字叫X，使用粉色颜色")
check("生产事故原句（有意向、没判成命令）→ 弹窗", p is not None)
if p:
    q = p["pending_confirm"]["q"]
    check("  问句里把颜色**名 + 值**都给全（点确定前看得见自己同意了什么）",
          "粉色" in q and "#eb2f96" in q, q)
    check("  问句点名标签名", "测试标签" in q, q)
    check("  选项恰是两个（确定/取消），确定是 primary",
          [o["value"] for o in p["pending_confirm"]["opts"]] == ["yes", "no"]
          and p["pending_confirm"]["opts"][0]["kind"] == "primary")
    check("  回复正文（confirm_text）是确定性中文、且**不说已完成**",
          p["confirm_text"] and "已完成" not in p["confirm_text"]
          and "确认" in p["confirm_text"])
    check("  令牌已签发（不是空串）", len(p["pending_confirm"]["token"]) > 20)
    # 失效时刻随帧下发（20260924）：前端拿它起倒计时，到点自动把卡片结算成
    # "已过期，未执行"。判据不是"exp 约等于 now+TTL"（那只能证明算了个大概），
    # 而是**与验签真正比较的那个数逐字相等**——展示值一旦与签名值脱钩，卡片就会
    # 在令牌失效之后还多活一会儿，用户点下去必被拒（20260924 的"看着能点、
    # 点了白点"）。所以直接拿 verify 解出来的 payload 对齐。
    _pay = confirm.verify(p["pending_confirm"]["token"], 7, 42) or {}
    _exp = p["pending_confirm"].get("exp")
    check("  exp 已下发，且**等于签名里那一个**（展示不重算，重算会跨秒漂）",
          isinstance(_exp, int) and _exp == _pay.get("exp") and _exp > 0)
    _now = int(time.time())
    check("  exp ≈ now + TTL_SECONDS（10 分钟量级，不是随便一个未来数）",
          confirm.TTL_SECONDS - 5 <= _exp - _now <= confirm.TTL_SECONDS + 5)
    check("  签发的 spec 是**具体值**（没有 $ref 残留）",
          not confirm.has_refs(p["pending_confirm"]["specs"]))
    check("  带着技能名（执行轮照它拼计划，不靠模型回忆）",
          p["pending_confirm"]["skill"] == "tag_create")

check("提问 → **不弹**（把提问读成意图是最要命的误判）",
      _popup("文章 12 设为私密的步骤是什么") is None
      and _popup("把文章 12 设为私密会有什么影响？") is None
      and _popup("如果我把文章 12 设为私密的话") is None)
check("已判成明确命令 → 不弹（同轮命令即确认，直接执行）",
      _popup("确认创建标签 测试标签") is None
      and _popup("新建一个标签叫 测试标签，用粉色") is None)
check("生产消息壳（`[当前问题]: `，server.py:413）不影响弹窗判据——"
      "带壳的提问/假设照样不弹、带壳的命令照样不弹（与裸句同判）",
      _popup("[当前问题]: 把文章 12 设为私密会有什么影响？") is None
      and _popup("[当前问题]: 如果我把文章 12 设为私密") is None
      and _popup("[当前问题]: 文章 12 设为私密的步骤是什么") is None
      and _popup("[当前问题]: 确认创建标签 测试标签") is None
      # 这两条**必须配 PLAN_STATUS**（20260922 假红复盘）：原先用的是默认的
      # PLAN_TAG（create_tag，title=测试标签），而这句话跟那个计划不搭——旧断言
      # 之所以绿，靠的正是"新建类写操作没有**值**的地基"这个洞（`_ident_grounded`
      # 对 create_tag 只查父标签，于是任何 title 都算有据）。洞补上后这条必然红，
      # 而红的是**判据自己**：壳与裸句的对照实验要变量唯一。
      and _popup("[当前问题]: 把文章 12 设为私密", PLAN_STATUS) is None
      and _popup("把文章 12 设为私密", PLAN_STATUS) is None)
check("带壳的意图原句照样弹窗（壳不许把弹窗也一起弄哑）",
      _popup("[当前问题]: 一级标签，名字叫X，使用粉色颜色") is not None)
check("免弹窗的第二个前提扩到**值**：计划要写进去的字面也得在主人这句话里"
      "（20260922 ②防线；新建类写操作过去只查父标签，title 写什么都不算没据）",
      # 计划写 title=测试标签，主人这句里没有 —— 带壳不带壳一个待遇（不弹＝直接写）
      _popup("把文章 12 设为私密") is not None
      and _popup("[当前问题]: 把文章 12 设为私密") is not None
      # 值原样说出口 → 照旧直接执行，不多一次点击
      and _popup("新建一个标签叫 测试标签，用粉色") is None)
check("非 admin → 不弹（弹了就是承诺一件做不到的事）",
      _popup("一级标签，名字叫X，使用粉色颜色",
             principal=Principal(uid=9, role="user")) is None
      and _popup("一级标签，名字叫X，使用粉色颜色",
                 principal=Principal(uid=9, role="secretary")) is None
      and _popup("一级标签，名字叫X，使用粉色颜色",
                 principal=Principal(uid=9, role=None)) is None)
check("目标无据的文章写 → 不弹（弹出来的是「要不要改文章 12」，而 12 是编的）",
      _popup("《架构文档》我想改成私密", PLAN_STATUS) is None)
check("目标有据（本轮读到过 id=12）→ 弹（确认轮没有帧，凭据是签发时校验的）",
      _popup("《架构文档》我想改成私密", PLAN_STATUS, extra=(EVID,)) is not None)
# 误靶写（20260921 第三轮活体探针）：帧里读到过 12，但主人**点名的是 14**——
# 弹出来的确认框会把 12 明明白白写出来，可那不是我点的那一篇。不弹 → 由 execute
# 主循环产 target_mismatch 帧，planner 按帧改回来。
EVID2 = ToolMessage(content="后台文章共 3 篇：\n- id=12 [私密]《架构文档》\n- id=14 [草稿]《随笔》",
                    tool_call_id="t2", name="list_admin_notes")
check("点名 14 而计划写 12 → **不弹**（否则等于把误靶洗成一条已授权的写）",
      _popup("文章 14 那篇我想改成私密", PLAN_STATUS, extra=(EVID2,)) is None)
check("点名 12 而计划写 12 → 弹（判据只否决不一致）",
      _popup("文章 12 那篇我想改成私密", PLAN_STATUS, extra=(EVID2,)) is not None)
check("主人没点名（纯指代）→ 弹（判据不启用，行为与改动前一致）",
      _popup("《架构文档》我想改成私密", PLAN_STATUS, extra=(EVID2,)) is not None)
check("参数含 $ref → 不弹（令牌签不出来，退回追问）",
      _popup("改一下文章标签", 'SKILL=article_tags\nPARAMS={}\nTOOLS: '
             'set_article_tags({"article_id": 12, "add": ["$list_tags[0].name"]})'
             '\nNOTE: x\nREPLY: y', extra=(EVID,)) is None)
check("确认轮自己（confirm_grant 在场）→ 不弹（否则点完确定又弹一个）",
      _confirm_popup({"messages": [HumanMessage(content="确认执行：建标签")],
                      "plan": PLAN_TAG, "confirm_grant": {"skill": "tag_create",
                                                          "specs": SPECS}},
                     [{"tool": "create_tag", "args": {"title": "X"}}],
                     Principal(uid=7, role="admin"), "确认执行：建标签", CFG) is None)

print("\n④ 点确定之后：照签名执行")
plan_obj = _confirm_grant_plan({"skill": "tag_create", "specs": SPECS})
check("_confirm_grant_plan：技能名取自签名（不猜）", plan_obj["skill"] == "tag_create")
check("  TOOLS 行逐条落签名参数（JSON 形态，参数一字不改）",
      plan_obj["tools"] == ['create_tag(' + json.dumps(SPECS[0]["args"], ensure_ascii=False) + ')'],
      str(plan_obj["tools"]))
check("  NOTE 明写「用户已确认、不得增改参数」",
      "不得" in plan_obj["note"] and "确定" in plan_obj["note"])
check("技能名与工具对不上 → 空清单 + 如实告知（防令牌被换工具）",
      _confirm_grant_plan({"skill": "chat", "specs": SPECS})["tools"] == []
      and "无效" in _confirm_grant_plan({"skill": "chat", "specs": SPECS})["note"])
check("技能名不存在 / 缺规格 → 空清单（不回落成 chat 就放行）",
      _confirm_grant_plan({"skill": "不存在的技能", "specs": SPECS})["tools"] == []
      and _confirm_grant_plan({"skill": "", "specs": SPECS})["tools"] == [])
check("技能内**部分**工具越界也整单拒绝（不做「挑出合法的那几个」）",
      _confirm_grant_plan({"skill": "tag_create", "specs": SPECS + [
          {"tool": "set_article_status", "args": {"article_id": 1}}]})["tools"] == [])
check("specs 为空 → 空清单（不猜要做什么）",
      _confirm_grant_plan({"skill": "tag_create", "specs": []})["tools"] == [])

print("\n⑤ 确认轮执行：两道确定性门放行、权限不放行")
CALLS: list = []


class _FakeTool:
    def __init__(self, out):
        self._out = out
        self.name = "set_article_status"

    def invoke(self, args):
        CALLS.append(args)
        return self._out


_saved = g._TOOL_MAP.get("set_article_status")
try:
    g._TOOL_MAP["set_article_status"] = _FakeTool(
        _base.ok("已修改文章 12：私密 → 公开（后台已复核读到新值）"))
    grant_cfg = {"configurable": {"principal": Principal(uid=7, role="admin"),
                                 "user_id": 7, "conversation_id": 42, "stop_event": None}}
    grant_state = {"plan": PLAN_STATUS, "plan_rounds": 1, "done": False,
                   "messages": [HumanMessage(content="确认执行：修改文章 12")],
                   "confirm_grant": {"skill": "article_status", "specs": [
                       {"tool": "set_article_status",
                        "args": {"article_id": 12, "status": "private"}}]}}
    CALLS.clear()
    r = execute_node(grant_state, grant_cfg)
    check("同意闸放行（本轮消息没有命令语，靠的是那一张令牌）",
          CALLS == [{"article_id": 12, "status": "private"}] and r["receipts"], str(CALLS))
    check("目标有据放行（确认轮没有本轮帧，凭据来自签发时）", not r["blocked"], str(r["blocked"]))

    # 权限不放行：非 admin 拿着同一份 grant 照样被硬拦
    CALLS.clear()
    r2 = execute_node({**grant_state, "plan": PLAN_STATUS},
                      {"configurable": {"principal": Principal(uid=9, role="user"),
                                        "user_id": 9, "conversation_id": 42,
                                        "stop_event": None}})
    frm = str(r2["messages"][-1].content)
    check("非 admin 即便持有有效令牌 → 依然零调用、被 scope 硬拦",
          CALLS == [] and r2["blocked"] and frm.startswith("__ERROR__")
          and "denied" in r2["blocked"][0]["reason"], frm[:70])
finally:
    if _saved is None:
        g._TOOL_MAP.pop("set_article_status", None)
    else:
        g._TOOL_MAP["set_article_status"] = _saved

print("\n⑥ 颜色：站内 8 色板 + 中文色名映射")
check("色板与前端/后台同源同序（顺序变了颜色就全变）",
      A.NEW_TAG_COLORS == ['#1677ff', '#52c41a', '#fa8c16', '#eb2f96',
                           '#722ed1', '#13c2c2', '#f5222d', '#a0d911'],
      str(A.NEW_TAG_COLORS))
check("8 个规范中文名各对应色板里的一个值，无重名",
      len({A.color_cn(h) for h in A.NEW_TAG_COLORS}) == 8
      and all(A.color_cn(h) for h in A.NEW_TAG_COLORS))
check("用户说的「粉色」→ #eb2f96（用户点名的那一种）",
      A.match_tag_color("粉色") == "#eb2f96")
check("色值原样认（含大小写与 # 可省）",
      A.match_tag_color("#EB2F96") == "#eb2f96" and A.match_tag_color("eb2f96") == "#eb2f96")
check("认不出就不认（**不做子串匹配**：天蓝不会被换成蓝）",
      A.match_tag_color("天蓝") is None and A.match_tag_color("浅蓝") is None
      and A.match_tag_color("香槟金") is None)
check("没说颜色 → 回落按名哈希（同名同色的既有契约不变）",
      A.resolve_tag_color("", "测试标签") == A.color_for_name("测试标签"))
check("说了颜色 → 用说的那个（哈希让位）",
      A.resolve_tag_color("粉色", "测试标签") == "#eb2f96")
check("回程渲染带色名 + 色值（色板外只有色值可给）",
      A.describe_color("#eb2f96") == "粉色（#eb2f96）"
      and A.describe_color("#123456") == "#123456")
check("色板清单给提示词用（8 项、含中文名与色值）",
      A.TAG_COLOR_SPEC.count("（#") == 8 and "粉色" in A.TAG_COLOR_SPEC)

# 工具层：点了名的颜色认不出 → 不创建（绝不静默换成哈希色）
import inspect  # noqa: E402

import tools.base as B  # noqa: E402

src = inspect.getsource(B.create_tag.func if hasattr(B.create_tag, "func") else B.create_tag)
check("create_tag 有 color 参数且透传（Rust 侧存 String 不校验，零改动）",
      "color" in src and "match_tag_color" in src)
check("认不出的颜色 → unavailable（不静默回落成哈希色）",
      "不在站内色板里" in src and "unavailable" in src)

settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子

print("\n⑦ 图接线：确认轮必须能走完（20260921 22:37 生产事故的回归锁）")
# 事故：`route_after_execute` 对确认轮返回 "model"（写成功 → 直去 narrator），
# 而 execute 的 `add_conditional_edges` 映射表里**没有 model** —— langgraph 在
# **节点执行完之后**才抛 KeyError('model')，于是"点确定"的每一次都是：
# 站内数据真的改了 + 回执落库了 + 前端收到一行报错 `'model'`。
# 两层锁：① 路由标签必须都在映射表里（结构性，谁漏谁红）；② 用假工具 + 假 LLM
# 把整条确认轮在图里跑一遍（端到端，零网络零真写）。
from agent.graph import (EXECUTE_ROUTES, PLANNER_ROUTES,  # noqa: E402
                         REFLECTOR_ROUTES, build_graph, graph_input,
                         route_after_execute, route_after_planner,
                         route_after_reflector)

_GRANT = {"skill": "article_status", "specs": [
    {"tool": "set_article_status", "args": {"article_id": 12, "status": "private"}}]}
_ROUTE_CASES = [
    ("planner", route_after_planner, PLANNER_ROUTES, [
        {"plan": PLAN_STATUS},
        {"plan": plan_encode(instantiate_plan("chat", {}))}]),
    ("execute", route_after_execute, EXECUTE_ROUTES, [
        {"blocked": []}, {"blocked": [], "confirm_grant": _GRANT},
        {"blocked": [], "pending_confirm": {"q": "?", "token": "t"}},
        {"blocked": [{"reason": "error_frame"}], "blocked_seen": ["x"]},
        {"blocked": [{"reason": "error_frame"}], "blocked_repeat": True}]),
    ("reflector", route_after_reflector, REFLECTOR_ROUTES, [
        {"reflect_end": True}, {"reflect_end": False}]),
]
for _node, _fn, _routes, _states in _ROUTE_CASES:
    _seen = {_fn(s) for s in _states}
    check(f"{_node} 的每个路由去向都有条件边映射（漏了 = 节点跑完才炸）",
          _seen <= set(_routes), f"{_seen} ⊄ {set(_routes)}")
check("确认轮执行成功 → 去 narrator（不再回 planner 重规划）",
      route_after_execute({"blocked": [], "confirm_grant": _GRANT}) == "model")
check("execute 的条件边**真有** model 这一支（事故点）", "model" in EXECUTE_ROUTES)


class _FakeNarrator:
    """假 narrator：零网络。只被调用一次（确认轮跳过 planner）。

    必须回 **AIMessage**：model_node 是 `return {"messages": [resp]}`，langgraph
    会拿这个对象当消息用（回自定义对象会在写消息时抛 MESSAGE_COERCION_FAILURE）。
    """

    def __init__(self):
        self.calls = 0

    def invoke(self, *a, **kw):
        self.calls += 1
        return AIMessage(content="文章 12 已经设为私密啦喵～")


_orig_tool = g._TOOL_MAP.get("set_article_status")
_orig_llm = g.get_llm
_narr = _FakeNarrator()
try:
    g._TOOL_MAP["set_article_status"] = _FakeTool(B.ok("已修改文章 12：公开 → 私密（后台复核读到新值）"))
    g.get_llm = lambda **kw: _narr
    _out = build_graph().invoke(
        graph_input([HumanMessage(content="确认执行：修改文章 12")], confirm_grant=_GRANT),
        {"configurable": {"principal": Principal(uid=7, role="admin"), "user_id": 7,
                          "conversation_id": 42, "stop_event": None}})
    check("确认轮在真图里跑得完（写已生效之后不再炸）",
          _out.get("done") is True and _narr.calls == 1, f"done={_out.get('done')} calls={_narr.calls}")
    check("  最终回复来自 narrator（不是报错、不是弹窗文案）",
          "私密" in str(_out["messages"][-1].content), str(_out["messages"][-1].content)[:60])
    check("  回执在场（写操作的真回执，跨轮记忆靠它）",
          [r.get("tool") for r in _out.get("receipts") or []] == ["set_article_status"])
except Exception as e:  # noqa: BLE001 —— 旧版这里就是 KeyError('model')
    check("确认轮在真图里跑得完（写已生效之后不再炸）", False, f"{type(e).__name__}: {e}")
finally:
    if _orig_tool is None:
        g._TOOL_MAP.pop("set_article_status", None)
    else:
        g._TOOL_MAP["set_article_status"] = _orig_tool
    g.get_llm = _orig_llm

print()
# ── ⑤ 授权式短应答的审查路径（20260923 P2）：目标由系统台账定，但**仍要主人点一下** ──
# 主人说"小猫咪按你想法来吧"（授权式）时目标由系统定（`g._auth_review_path` 读台账里
# approved=0 的那一条）——但**授权不等于替主人签字**：写操作同意闸照旧弹窗，弹窗里
# 印着 #id/作者/原文/现状/动作，主人点"确定"才是身份。这条链路正是用户拍板的形态
# （"弹窗把目标印给主人"），也是 20260923 13:19 那条事故的正解。
from tools import base as _tb  # noqa: E402


_PEND = {94: {"talkKey": 94, "author": "visitor", "approved": 0,
              "content": "垃圾网站，什么破烂，主动申请驳回都失败"}}
_orig_board = _tb._board_index
try:
    # ⚠️ 本段排在 `:319` 那次"还原成进来时的样子"**之后**：CI 无 .env ⇒ 还原回去的是
    # **空串** ⇒ `confirm.sign` 返回 "" ⇒ `_confirm_popup` fail-closed 不弹窗（本地有
    # .env 时还原回真密钥，于是本地全绿）。这正是 `_STUB_SECRET` 注释里那条坑的第二次
    # 踩中（20260923 CI run 35858357150：本段 5 项全红、症状是 execute 产
    # `consent_required` 错误帧而不是 `pending_confirm`）⇒ 这里必须**自己再设一次桩**。
    settings.jwt_secret = _STUB_SECRET
    _tb._board_index = lambda config: dict(_PEND)
    _facts, _plan_obj, _forced = g._auth_review_path(
        "小猫咪按你想法来吧", "有一条留言在等人复核，那我把这条**驳回隐藏**：",
        Principal(uid=7, role="admin"), CFG)
    # G1 的三态：这句提议里结论**读得出**（驳回）⇒ 走快道直接拼计划，
    # `forced`（目标定死、只把结论留给 planner）必须为 None——两个形态同时开火
    # 会互相盖（forced 分支早退，快道拼好的计划就废了）。
    check("  结论读得出时走快道：不进目标定死模式（forced 必须为 None）",
          _forced is None and _plan_obj is not None, str(_forced)[:80])
    _r = execute_node({"messages": [HumanMessage(content="小猫咪按你想法来吧")],
                       "plan": plan_encode(_plan_obj), "plan_rounds": 0, "done": False,
                       "receipts": []}, CFG)
    check("授权式 + 台账唯一待审 ⇒ 执行前弹确认框（授权不等于替主人签字）",
          isinstance(_r, dict) and "pending_confirm" in _r,
          f"{str(_r)[:80]}（令牌长度 {len((_r or {}).get('pending_confirm', {}).get('token', ''))}"
          "—— 为 0 就是签名密钥空缺，见本段开头的密钥桩注释）")
    _q = (_r or {}).get("pending_confirm", {}).get("q", "")
    check("  弹窗把目标印给主人（#id + 作者 + 原文 + 现状）",
          "#94" in _q and "垃圾网站" in _q and "待审" in _q, _q[:110])
    check("  结论取自上一轮那句提议（驳回）——主人签字前看得见自己同意了什么",
          [_s.get("args", {}).get("verdict") for _s in (_r or {})
           .get("pending_confirm", {}).get("specs", [])] == ["reject"], _q[:70])
    check("  弹窗轮零执行（一个工具都没跑）", _r.get("messages") == [])
    check("  令牌照常签发（点确定后照签名拼计划，不靠模型回忆）",
          len((_r or {}).get("pending_confirm", {}).get("token", "")) > 20)
finally:
    _tb._board_index = _orig_board
    settings.jwt_secret = _SAVED_SECRET   # ⑥ 段自己会再设一次桩

print()
# ── ⑥ 跨轮待办的结构化形态（20260923 P4）：弹窗那一轮的提议落成系统记录 ──
# 目的不是"多一个字段"：主人下一轮说"那就办吧"时 planner 要能照这一行**原样重发**
# （技能/工具/参数照抄），而不是回历史自然语言里另挑一个目标——历史是解释层，这一行
# 是系统事实。三端契约：这里产的 dict → server.py 发 `__PENDING__:` 帧 → Rust 落库、
# 下一轮 prepare_chat 读回来注入页面上下文（字段名见 src/routes/chat.rs
# `save_pending_action`，**改一侧必须同步另一侧**）。
settings.jwt_secret = _STUB_SECRET   # ⑤ 的 finally 还原成 _SAVED_SECRET（CI 里是空串）
_pa = _popup("一级标签，名字叫X，使用粉色颜色") or {}
_pad = _pa.get("pending_action") or {}
check("弹窗那一轮同时产出结构化待办（不是只发一个 __CONFIRM__ 就完了）",
      bool(_pad), str(_pa.get("pending_confirm") is not None))
check("  字段恰是六件（增字段＝改跨语言契约：Rust 侧读的就是这六个）",
      set(_pad) == {"task_id", "skill", "specs", "target", "requested_by", "source_event"},
      str(sorted(_pad)))
check("  task_id 形状 pa_YYYYMMDD_9位（同一条待办重发时用它对齐）",
      bool(re.fullmatch(r"pa_\d{8}_\d{9}", str(_pad.get("task_id", "")))),
      str(_pad.get("task_id")))
check("  目标与弹窗问句同源（同一个 render_action_lines——两处措辞永不走散）",
      bool(_pad.get("target"))
      and _pad["target"] in (_pa.get("pending_confirm") or {}).get("q", ""),
      str(_pad.get("target"))[:80])
check("  specs 与令牌里签名的是同一份（照它重发＝重发主人签字时看到的那个具体请求）",
      _pad.get("specs") == (_pa.get("pending_confirm") or {}).get("specs")
      and bool(_pad.get("specs"))
      and confirm.verify((_pa.get("pending_confirm") or {}).get("token", ""), 7,
                         42)["specs"] == _pad["specs"])
check("  技能随计划（下一轮据此拼计划，不靠模型回忆）", _pad.get("skill") == "tag_create")
check("  提出者/来源是系统事实（不是模型叙述，也够不上访客痕迹）",
      _pad.get("requested_by") == "user" and _pad.get("source_event") == "confirm_popup")
check("不弹窗的轮次**没有**待办（提问/无权限不许凭空记一条等主人点头的事）",
      not (_popup("把文章 12 设为私密会有什么影响？") or {}).get("pending_action")
      and not (_popup("一级标签，名字叫X，使用粉色颜色",
                      principal=Principal(uid=9, role="user")) or {}).get("pending_action"))
settings.jwt_secret = _SAVED_SECRET

print()
print("⑦ 被拒的确认请求必须留痕（20260924：点确定没生效这件事，agent 侧此前零证据）")
# 事故：主人点「确定」→ 令牌过期 → 服务端回"这次确认已经失效了…没有执行任何改动"，
# 但这一轮**根本不落 trace**（旧的 invalid 分支在 start_trace 之前就 return 了）。
# 复盘时 agent 侧查不到"有人点过、被拒了"，只能靠前端日志——而前端这条链路正路径
# 也没留痕。这一节同时锁两件事：查得到（字段齐）与**查不到（令牌绝不在里面）**。
# 密钥桩：⑥ 收尾把密钥还原成了 _SAVED_SECRET，而 **CI 里没有 .env、它是空串**——
# 不重新立桩，下面 confirm.sign 会静默返回空串，"令牌不在元数据里"就成了恒真的
# 假绿（CI 实测：本地带 .env 绿、CI 红，正是这一条）。故先立桩再用。
settings.jwt_secret = _STUB_SECRET
check("前置探针：此刻签得出令牌（下面那条'令牌不在里面'才有意义）",
      len(confirm.sign(1, 246, "favorite_add",
                       [{"tool": "add_favorite", "args": {"article_id": 19}}])) > 20)
_meta = confirm.invalid_trace_meta(1, 246, 220)
check("被拒轮有 trace 元数据，且标明'这一跳是点确定'", _meta.get("has_confirm") is True)
check("  标明验签没过（零执行）⇒ 复盘时不会与'真执行了'混淆",
      _meta.get("confirm_rejected") is True)
check("  带会话 id（20260924 之前 trace 只有随机 thread_id，认不出属于哪个会话）",
      _meta.get("conversation_id") == 246)
check("  只记令牌**长度**（够复现'客户端有没有发全'，长度本身不是凭据）",
      _meta.get("confirm_token_len") == 220)
# 令牌本身一个字符都不许进 trace：用真令牌做输入，断言它的任何一段都不在元数据里
_real_token = confirm.sign(1, 246, "favorite_add", [{"tool": "add_favorite", "args": {"article_id": 19}}])
_meta2 = confirm.invalid_trace_meta(1, 246, len(_real_token))
_flat = json.dumps(_meta2, ensure_ascii=False) + str(sorted(_meta2))
check("  真令牌的任何一段（完整串/签名段/载荷段）都不在元数据里",
      _real_token not in _flat
      and _real_token.split(".")[0] not in _flat and _real_token.split(".")[1] not in _flat)

# 接线断言（"能力有测试 ≠ 接线有测试"）：两个调用点必须在 chat_stream 里真的接上
_src = open("server.py", encoding="utf-8").read()
check("接线：验签失败分支调用了落 trace（不是只打个 warning 就 return）",
      "_record_invalid_confirm(get_trace_id()" in _src
      and _src.index("_record_invalid_confirm(get_trace_id()") > _src.index("if grant is None:"))
check("接线：正常轮的 trace input 带 conversation_id",
      '"conversation_id": req.conversation_id' in _src)
# 弹窗帧带 exp（20260924）：图形侧算了、服务端没转发，等于没算（前端拿不到就起不了
# 倒计时，卡片照样永远停在"已确认"）。帧体与 state 增量两处都要在位。
check("接线：__CONFIRM__ 帧体带 exp（前端靠它起倒计时/到期结算）",
      '"exp": popup.get("exp") or 0' in _src)
settings.jwt_secret = _SAVED_SECRET   # 还原（⑥ 与本节各自立桩，改完归还原值）

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
