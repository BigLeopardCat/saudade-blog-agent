"""确认令牌、旧图弹窗与跨语言执行边界的离线回归。

覆盖签名与身份绑定、具体参数、确认清单收窄、待办字段及无效请求零执行。
新确认领域的持久撤销和回放由 test_pending_favorite.py 等领域套件验证。
"""


from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from _judge import collector
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）


sys.path.insert(0, str(ROOT))


import _parent_repo  # noqa: E402  （父仓三态：找得到/要求却找不到=红/找不到=响亮跳过）

import agent.graph as g
from agent import adminops_confirm as _confirm
from agent import adminops_tags as _tags
from agent import authz, confirm
from agent.graph import _confirm_grant_plan, _confirm_popup, execute_node, plan_state
from agent.principal import Principal
from agent.workflow_defs import WORKFLOW_MAP, WORKFLOWS
from agent.workflow_menu import build_planner_context  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402
from tools import base as _base  # 工具的返回值契约（ToolResult：str 子类 + kind）

FAILED: list[str] = []
check = collector(FAILED)


import hashlib  # noqa: E402
import hmac  # noqa: E402

from config.settings import settings  # noqa: E402

PLAN_TAG = {"workflow": "tag_create", "params": {},
            "tools": ['create_tag({"title": "测试标签", "parent_id": null, "color": "#eb2f96"})'],
            "note": "x", "reply": "y"}


CFG = {"configurable": {"principal": Principal(uid=7, role="admin"), "user_id": 7,
                        "conversation_id": 42, "stop_event": None}}


def _popup(msg, plan=PLAN_TAG, cfg=CFG, extra=(), principal=None):
    st = {"messages": [HumanMessage(content=msg), *extra], **plan_state(plan),
          "plan_rounds": 0, "done": False}
    return _confirm_popup(st, list(plan["tools"]),
                          principal or Principal(uid=7, role="admin"), msg, cfg)


CALLS: list = []


class _FakeTool:
    def __init__(self, out):
        self._out = out
        self.name = "set_article_status"

    def invoke(self, args):
        CALLS.append(args)
        return self._out


_CALLS2: list = []


class _FakeNotice:
    name = "send_user_notice"

    def invoke(self, args):
        _CALLS2.append(args)
        return _base.ok("已发出站内通知")


import inspect  # noqa: E402

import tools.base as B  # noqa: E402
from agent.graph import (  # noqa: E402
    EXECUTE_ROUTES,
    GATE_ROUTES,
    PLANNER_ROUTES,
    REFLECTOR_ROUTES,
    build_graph,
    graph_input,
    route_after_execute,
    route_after_gate,
    route_after_planner,
    route_after_reflector,
)


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


from tools import base as _tb  # noqa: E402

_INSPECT_CALL = re.compile(r"(?<![\w])(?:confirm\.)?inspect\s*\(")


def _looks_like_inspect_call(ln: str) -> bool:
    """这一行是不是在**调用**只读解载荷那个 `inspect`（而不是定义它/stdlib 反射）。

    判据只认"`inspect(`"或"`confirm.inspect(`"，并且排除注释行与 `def inspect(`——
    stdlib 的 `inspect.getsource(x)` 因为 `(` 不紧跟 `inspect` 天然不命中。
    抽成函数是为了让"零引用"这条断言**自己也被验一次**（见下面"有牙齿"那条）：
    否则一个恒假的扫描器会安静地报"零引用"，而那正是它想防的假绿。
    """
    s = ln.strip()
    if s.startswith("#") or s.startswith("def inspect("):
        return False
    return bool(_INSPECT_CALL.search(ln))


def _own_popup(workflow, spec, msg, ev):
    # 手拼**对象**（20260928 批 C）：此前手拼契约文本，`_plan_workflow` 改读 `plan_obj`
    # 之后那份文本读不出技能名 ⇒ 令牌签不出来。夹具与生产同路：走 `plan_state`。
    obj = {"workflow": workflow, "params": {}, "tools": [spec], "note": "x", "reply": "y"}
    msgs = [HumanMessage(content=msg)] + ([ev] if ev is not None else [])
    st = {"messages": msgs, **plan_state(obj), "plan_rounds": 0, "done": False}
    return _confirm_popup(st, obj["tools"],
                          Principal(uid=7, role="admin"), msg, CFG)


_HOST_CLAIM_MARKERS = ("ClaimedAt", "rows_affected == 1", "if agent_owned(token)", "return None")

def test_confirm():
    _SAVED_SECRET = settings.jwt_secret
    _STUB_SECRET = "test-secret-for-confirm-tokens-not-a-real-secret"
    settings.jwt_secret = _STUB_SECRET
    SPECS = [{"tool": "create_tag", "args": {"title": "测试标签", "parent_id": None, "color": "#eb2f96"}}]
    print("① 令牌签发/验签")
    tok = confirm.sign(7, 42, "tag_create", SPECS)
    check("往返：verify(sign(...)) 取回同一份 payload",
          (lambda p: p is not None and p["uid"] == 7 and p["conv"] == 42
           and p["workflow"] == "tag_create" and p["specs"] == SPECS)(confirm.verify(tok, 7, 42)))
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
               "workflow": "tag_create",
               "specs": [{"tool": "create_tag", "args": {"title": "别的标签"}}]},
              ensure_ascii=False, separators=(",", ":")).encode()) + "." + tok.split(".")[1],
              7, 42) is None)
    check("格式坏（无点 / 空 / 非字符串）→ None 且不抛",
          confirm.verify("nodot", 7, 42) is None and confirm.verify("", 7, 42) is None
          and confirm.verify(None, 7, 42) is None)
    check("base64 坏 → None 且不抛", confirm.verify("!!!.???", 7, 42) is None)
    _past = {"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) - 1,
             "workflow": "tag_create", "specs": SPECS}
    _body = json.dumps(_past, ensure_ascii=False, separators=(",", ":")).encode()
    _sig = hmac.new(settings.jwt_secret.encode(), confirm._DOMAIN + _body, hashlib.sha256).digest()
    _stale = confirm._b64e(_body) + "." + confirm._b64e(_sig)
    check("过期（exp 在签发后已过）→ None（签名合法也不行）",
          confirm.verify(_stale, 7, 42) is None)
    check("TTL 是 10 分钟量级（够读完想一下，又短到过夜必失效）",
          300 <= confirm.TTL_SECONDS <= 900, str(confirm.TTL_SECONDS))
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
    check("前置探针：此刻密钥在位、签得出令牌（下面正例才有意义）",
          len(confirm.sign(7, 42, "tag_create", SPECS)) > 20)
    SPEC_STATUS = 'set_article_status({"article_id": 12, "status": "private"})'
    PLAN_STATUS = {"workflow": "article_status", "params": {}, "tools": [SPEC_STATUS],
                   "note": "x", "reply": "y"}
    EVID = ToolMessage(content="后台文章共 3 篇：\n- id=12 [私密]《架构文档》标签：",
                       tool_call_id="t1", name="list_admin_notes")
    p = _popup("一级标签，名字叫X，使用粉色颜色")
    check("生产事故原句（有意向、没判成命令）→ 弹窗", p is not None)
    if p:
        q = p["pending_confirm"]["q"]
        check("  问句里把颜色**名 + 值**都给全（点确定前看得见自己同意了什么）",
              "粉色" in q and "#eb2f96" in q, q)
        check("  问句点名标签名", "测试标签" in q, q)
        # 三枚（20261006）：确定 / 其他（我来说）/ 取消。「其他」是卡内改口的入口——
        # 点它不结算、不发请求，前端就地开输入框，提交出去的是**普通新轮**（见
        # chat-render.js 的 .chat-ask-other 与 chat-stream.js 的 handleAskChoice）。
        check("  选项三枚（确定 / 其他（我来说）/ 取消），确定是 primary、取消恒在末位",
              [o["value"] for o in p["pending_confirm"]["opts"]] == ["yes", "other", "no"]
              and p["pending_confirm"]["opts"][0]["kind"] == "primary"
              and p["pending_confirm"]["opts"][-1]["value"] == "no")
        _oth = p["pending_confirm"]["opts"][1]
        check("  「其他（我来说）」是 default 档、字面逐字（它不是同意，别画成主按钮）",
              _oth["kind"] == "default" and _oth["label"] == "其他（我来说）", str(_oth))
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
              p["pending_confirm"]["workflow"] == "tag_create")
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
    PLAN_ANN = {"workflow": "announcement_create", "params": {},
                "tools": ['create_announcement({"title": "今晚不许熬夜！", '
                      '"content": "全体用户今晚不许熬夜"})'],
                "note": "x", "reply": "y"}
    check("公告原句弹得出卡（裸名词「要求/注意」不是疑问锚）：一字不改的两条生产原句 + 两条",
          _popup("小猫咪替我发一个公告要求全体用户今晚不许熬夜，以你的口吻声明",
                 PLAN_ANN) is not None
          and _popup("小猫咪替我发个公告，要求全体用户今晚务必早睡！", PLAN_ANN) is not None
          and _popup("帮我发个公告，提醒大家注意身体", PLAN_ANN) is not None
          and _popup("[当前问题]: 帮我发个公告说明今晚更新，注意提前保存", PLAN_ANN) is not None)
    check("疑问词住在**引号内容**里 ⇒ 不是主人在问，卡照弹",
          _popup("帮我发个公告，正文写「今晚几点睡？」", PLAN_ANN) is not None)
    check("疑问词住在**后半句的补充正文**里（命令骨架在前）⇒ 同样是内容，卡照弹",
          _popup("帮我发个公告，说说这次活动有什么注意事项", PLAN_ANN) is not None
          and _popup("帮我发个公告，告诉大家站内有多少新功能", PLAN_ANN) is not None)
    check("真在问的照旧不弹——句尾收在疑问语气词上时，**哪怕句首有命令骨架**",
          _popup("帮我发个公告好吗", PLAN_ANN) is None
          and _popup("帮我把公告发出去行吗", PLAN_ANN) is None)
    check("  但句尾的「吧 / 了」是吩咐不是问（别把语气词一律当问号）",
          _popup("帮我发个公告吧", PLAN_ANN) is not None)
    check("同一张表没被收窄过头：真提问照样不弹",
          _popup("公告的标题和正文要怎么写", PLAN_ANN) is None
          and _popup("发公告有什么注意事项", PLAN_ANN) is None
          and _popup("发公告的流程是什么", PLAN_ANN) is None)
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
                     principal=Principal(uid=9, role=None)) is None)
    check("目标无据的文章写 → 不弹（弹出来的是「要不要改文章 12」，而 12 是编的）",
          _popup("《架构文档》我想改成私密", PLAN_STATUS) is None)
    check("目标有据（本轮读到过 id=12）→ 弹（确认轮没有帧，凭据是签发时校验的）",
          _popup("《架构文档》我想改成私密", PLAN_STATUS, extra=(EVID,)) is not None)
    EVID2 = ToolMessage(content="后台文章共 3 篇：\n- "
        "id=12 [私密]《架构文档》\n- id=14 [草稿]《随笔》",
                        tool_call_id="t2", name="list_admin_notes")
    check("点名 14 而计划写 12 → **不弹**（否则等于把误靶洗成一条已授权的写）",
          _popup("文章 14 那篇我想改成私密", PLAN_STATUS, extra=(EVID2,)) is None)
    check("点名 12 而计划写 12 → 弹（判据只否决不一致）",
          _popup("文章 12 那篇我想改成私密", PLAN_STATUS, extra=(EVID2,)) is not None)
    check("主人没点名（纯指代）→ 弹（判据不启用，行为与改动前一致）",
          _popup("《架构文档》我想改成私密", PLAN_STATUS, extra=(EVID2,)) is not None)
    check("参数含 $ref → 不弹（令牌签不出来，退回追问）",
          _popup("改一下文章标签", {"workflow": "article_tags", "params": {},
                                  "tools": ['set_article_tags({"article_id": 12, '
                                        '"add": ["$list_tags[0].name"]})'],
                                  "note": "x", "reply": "y"}, extra=(EVID,)) is None)
    check("确认轮自己（confirm_grant 在场）→ 不弹（否则点完确定又弹一个）",
          # spec 走**签名体里那一件**（20261012：免卡判据现在比对工具名与参数，
          # 随手编一组别的参数不再算"同一件"——那正是 `⑤b` 要判的那件事）。
          _confirm_popup({"messages": [HumanMessage(content="确认执行：建标签")],
                          **plan_state(PLAN_TAG), "confirm_grant": {"workflow": "tag_create",
                                                                    "specs": SPECS}},
                         [dict(SPECS[0])],
                         Principal(uid=7, role="admin"), "确认执行：建标签", CFG) is None)
    print("\n④ 点确定之后：照签名执行")
    plan_obj = _confirm_grant_plan({"workflow": "tag_create", "specs": SPECS})
    check("_confirm_grant_plan：技能名取自签名（不猜）", plan_obj["workflow"] == "tag_create")
    check("  TOOLS 行逐条落签名参数（JSON 形态，参数一字不改）",
          plan_obj["tools"] == ['create_tag(' + json
              .dumps(SPECS[0]["args"], ensure_ascii=False) + ')'],
          str(plan_obj["tools"]))
    check("  NOTE 明写「用户已确认、不得增改参数」",
          "不得" in plan_obj["note"] and "确定" in plan_obj["note"])
    check("技能名与工具对不上 → 空清单 + 如实告知（防令牌被换工具）",
          _confirm_grant_plan({"workflow": "chat", "specs": SPECS})["tools"] == []
          and "无效" in _confirm_grant_plan({"workflow": "chat", "specs": SPECS})["note"])
    check("技能名不存在 / 缺规格 → 空清单（不回落成 chat 就放行）",
          _confirm_grant_plan({"workflow": "不存在的技能", "specs": SPECS})["tools"] == []
          and _confirm_grant_plan({"workflow": "", "specs": SPECS})["tools"] == [])
    check("技能内**部分**工具越界也整单拒绝（不做「挑出合法的那几个」）",
          _confirm_grant_plan({"workflow": "tag_create", "specs": SPECS + [
              {"tool": "set_article_status", "args": {"article_id": 1}}]})["tools"] == [])
    check("specs 为空 → 空清单（不猜要做什么）",
          _confirm_grant_plan({"workflow": "tag_create", "specs": []})["tools"] == [])
    _AT_SPECS = [{"tool": "create_tag", "args": {"title": "embeding"}},
                 {"tool": "set_article_tags", "args": {"article_id": 46, "add": ["embeding"]}}]
    check("条件工具照常过闸（article_tags 的先建再挂一条都不许被吞）",
          _confirm_grant_plan({"workflow": "article_tags", "specs": _AT_SPECS})["tools"]
          == ['create_tag({"title": "embeding"})',
              'set_article_tags({"article_id": 46, "add": ["embeding"]})'],
          str(_confirm_grant_plan({"workflow": "article_tags", "specs": _AT_SPECS})["tools"]))
    check("  加了条件工具不等于开了口子：越界的照旧整单拒绝",
          _confirm_grant_plan({"workflow": "article_tags", "specs": _AT_SPECS + [
              {"tool": "delete_tag", "args": {"name": "编程"}}]})["tools"] == [])
    check("  条件工具只对**声明了它**的技能开口（别的技能带上 create_tag 照样拒）",
          _confirm_grant_plan({"workflow": "article_status", "specs": [
              {"tool": "set_article_status", "args": {"article_id": 22, "is_top": 0}},
              {"tool": "create_tag", "args": {"title": "embeding"}}]})["tools"] == [])
    print("\n⑤ 确认轮执行：两道确定性门放行、权限不放行")
    _saved = g._TOOL_MAP.get("set_article_status")
    try:
        g._TOOL_MAP["set_article_status"] = _FakeTool(
            _base.ok("已修改文章 12：私密 → 公开（后台已复核读到新值）"))
        grant_cfg = {"configurable": {"principal": Principal(uid=7, role="admin"),
                                     "user_id": 7, "conversation_id": 42, "stop_event": None}}
        grant_state = {**plan_state(PLAN_STATUS), "plan_rounds": 1, "done": False,
                       "messages": [HumanMessage(content="确认执行：修改文章 12")],
                       "confirm_grant": {"workflow": "article_status", "specs": [
                           {"tool": "set_article_status",
                            "args": {"article_id": 12, "status": "private"}}]}}
        CALLS.clear()
        r = execute_node(grant_state, grant_cfg)
        check("同意闸放行（本轮消息没有命令语，靠的是那一张令牌）",
              CALLS == [{"article_id": 12, "status": "private"}] and r["receipts"], str(CALLS))
        check("目标有据放行（确认轮没有本轮帧，凭据来自签发时）",
            not r["blocked"], str(r["blocked"]))

        # 权限不放行：非 admin 拿着同一份 grant 照样被硬拦
        CALLS.clear()
        r2 = execute_node({**grant_state, **plan_state(PLAN_STATUS)},
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
    print("\n⑤b 令牌**只授权签名体里那一件**（20261012：一次点头曾授权两件写）")
    _ROLE_SPEC = {"tool": "set_account_role", "args": {"name": "demo", "role": "user"}}
    _NOTICE_SPEC = {"tool": "send_user_notice",
                    "args": {"name": "demo", "content": "正文"}}
    _TK = {"workflow": "account_set_role", "specs": [_ROLE_SPEC]}
    check("整批都在签名体里 ⇒ 免卡（兑现轮拼的就是签名体）",
          g._all_granted({"confirm_grant": _TK}, [_ROLE_SPEC]) is True)
    check("签名体里是**另一件** ⇒ 不免（要弹卡）",
          g._all_granted({"confirm_grant": _TK}, [_NOTICE_SPEC]) is False)
    check("同一件工具**换个目标** ⇒ 同样是另一件写（只比工具名不够）",
          g._all_granted({"confirm_grant": _TK}, [{"tool": "set_account_role",
                                                   "args": {"name": "demo_admin",
                                                            "role": "admin"}}]) is False)
    check("没有令牌 ⇒ 不免（照常走弹卡逻辑，与改动前逐字节相同）",
          g._all_granted({}, [_ROLE_SPEC]) is False
          and g._grant_covers({}, _ROLE_SPEC) is False)
    check("空批 + 有令牌 ⇒ 免（一件写都没有，本来就什么都不弹）",
          g._all_granted({"confirm_grant": _TK}, []) is True)
    check("参数没解析出来的 spec 一律不算覆盖（fail-closed）",
          g._grant_covers({"confirm_grant": _TK},
                          'set_account_role({不是 JSON})') is False)
    _saved2 = g._TOOL_MAP.get("send_user_notice")
    g._TOOL_MAP["send_user_notice"] = _FakeNotice()
    try:
        _nspec = 'send_user_notice(' + json.dumps(_NOTICE_SPEC["args"],
                                                  ensure_ascii=False) + ')'
        _st2 = {**plan_state({"workflow": "notice_send", "params": {},
                              "tools": [_nspec], "note": "x", "reply": "y"}),
                "plan_rounds": 1, "done": False,
                "messages": [HumanMessage(content="确认执行：把账号「demo」的身份改成普通用户")],
                "confirm_grant": _TK}
        _CALLS2.clear()
        _r2 = execute_node(_st2, grant_cfg)
        _pop2 = _r2.get("pending_confirm") or {}
        check("⭐ 令牌是**改身份**那张卡，同一轮排的**发通知** ⇒ 零调用、当场弹第二张卡",
              _CALLS2 == []
              and [s.get("tool") for s in (_pop2.get("specs") or [])] == ["send_user_notice"]
              and not _r2.get("receipts"),
              f"calls={_CALLS2} specs={_pop2.get('specs')}")
        # 反控：**被点过的那一件**照旧放行（收窄不能把兑现轮一起掐死）
        _st3 = {**plan_state({"workflow": "account_set_role", "params": {},
                              "tools": ['set_account_role(' + json.dumps(
                                  _ROLE_SPEC["args"], ensure_ascii=False) + ')'],
                              "note": "x", "reply": "y"}),
                "plan_rounds": 1, "done": False,
                "messages": [HumanMessage(content="确认执行：把账号「demo」的身份改成普通用户")],
                "confirm_grant": _TK}
        check("  反控：签名体里那一件（同工具同参数）仍免同意闸",
              g._grant_covers(_st3, _st3["plan_obj"]["tools"][0]) is True)
    finally:
        if _saved2 is None:
            g._TOOL_MAP.pop("send_user_notice", None)
        else:
            g._TOOL_MAP["send_user_notice"] = _saved2
    print("\n⑥ 颜色：站内 8 色板 + 中文色名映射")
    check("色板与前端/后台同源同序（顺序变了颜色就全变）",
          _tags.NEW_TAG_COLORS == ['#1677ff', '#52c41a', '#fa8c16', '#eb2f96',
                               '#722ed1', '#13c2c2', '#f5222d', '#a0d911'],
          str(_tags.NEW_TAG_COLORS))
    check("8 个规范中文名各对应色板里的一个值，无重名",
          len({_tags.color_cn(h) for h in _tags.NEW_TAG_COLORS}) == 8
          and all(_tags.color_cn(h) for h in _tags.NEW_TAG_COLORS))
    check("用户说的「粉色」→ #eb2f96（用户点名的那一种）",
          _tags.match_tag_color("粉色") == "#eb2f96")
    check("色值原样认（含大小写与 # 可省）",
          _tags.match_tag_color("#EB2F96") == "#eb2f96"
          and _tags.match_tag_color("eb2f96") == "#eb2f96")
    check("认不出就不认（**不做子串匹配**：天蓝不会被换成蓝）",
          _tags.match_tag_color("天蓝") is None and _tags.match_tag_color("浅蓝") is None
          and _tags.match_tag_color("香槟金") is None)
    check("没说颜色 → 回落按名哈希（同名同色的既有契约不变）",
          _tags.resolve_tag_color("", "测试标签") == _tags.color_for_name("测试标签"))
    check("说了颜色 → 用说的那个（哈希让位）",
          _tags.resolve_tag_color("粉色", "测试标签") == "#eb2f96")
    check("回程渲染带色名 + 色值（色板外只有色值可给）",
          _tags.describe_color("#eb2f96") == "粉色（#eb2f96）"
          and _tags.describe_color("#123456") == "#123456")
    check("色板清单给提示词用（8 项、含中文名与色值）",
          _tags.TAG_COLOR_SPEC.count("（#") == 8 and "粉色" in _tags.TAG_COLOR_SPEC)
    src = inspect.getsource(B.create_tag.func if hasattr(B.create_tag, "func") else B.create_tag)
    check("create_tag 有 color 参数且透传（Rust 侧存 String 不校验，零改动）",
          "color" in src and "match_tag_color" in src)
    check("认不出的颜色 → unavailable（不静默回落成哈希色）",
          "不在站内色板里" in src and "unavailable" in src)
    settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子
    print("\n⑦ 图接线：确认轮必须能走完（20260921 22:37 生产事故的回归锁）")
    _GRANT = {"workflow": "article_status", "specs": [
        {"tool": "set_article_status", "args": {"article_id": 12, "status": "private"}}]}
    _ROUTE_CASES = [
        ("planner", route_after_planner, PLANNER_ROUTES, [
            {**plan_state(PLAN_STATUS)},
            {**plan_state(instantiate_plan("chat", {}))}]),
        ("execute", route_after_execute, EXECUTE_ROUTES, [
            {"blocked": []}, {"blocked": [], "confirm_grant": _GRANT},
            {"blocked": [], "pending_confirm": {"q": "?", "token": "t"}},
            {"blocked": [{"reason": "error_frame"}], "blocked_seen": ["x"]},
            {"blocked": [{"reason": "error_frame"}], "blocked_repeat": True}]),
        ("reflector", route_after_reflector, REFLECTOR_ROUTES, [
            {"reflect_end": True}, {"reflect_end": False}]),
        # gate 自 20260926 起不再是无条件 `add_edge("gate", END)`——"该查而没查"那一族
        # 回 planner 重规划一次（见 graph.route_after_gate）。同一条事故纪律：路由函数
        # 能返回的每个标签都必须在 GATE_ROUTES 里，否则节点跑完才炸。
        ("gate", route_after_gate, GATE_ROUTES, [
            {"done": True, "gate_replan": False},
            {"done": False, "gate_replan": True},
            {"done": True, "gate_replan": True},   # 防呆：已收尾就不再回 planner
            {"gate_replan": False}]),
    ]
    for _node, _fn, _routes, _states in _ROUTE_CASES:
        _seen = {_fn(s) for s in _states}
        check(f"{_node} 的每个路由去向都有条件边映射（漏了 = 节点跑完才炸）",
              _seen <= set(_routes), f"{_seen} ⊄ {set(_routes)}")
    check("确认轮执行成功 → 去 narrator（不再回 planner 重规划）",
          route_after_execute({"blocked": [], "confirm_grant": _GRANT}) == "model")
    check("execute 的条件边**真有** model 这一支（事故点）", "model" in EXECUTE_ROUTES)
    check("gate 的条件边**真有** planner 这一支（重规划通道）",
          route_after_gate({"done": False, "gate_replan": True}) == "planner"
          and "planner" in GATE_ROUTES)
    check("gate 未标重规划 → 收尾（缺省即终局，不靠真值才结束）",
          route_after_gate({"done": True, "gate_replan": False}) == "end"
          and route_after_gate({}) == "end")
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
        # 模型按台账帧写出来的计划（`talk_id` 抄的是帧里那串数字，verdict 是它自己的判断）。
        _plan_obj = instantiate_plan(
            "board_audit", {"talk_id": "94", "verdict": "reject"}, "admin")
        _r = execute_node({"messages": [HumanMessage(content="小猫咪按你想法来吧")],
                           **plan_state(_plan_obj), "plan_rounds": 0, "done": False,
                           "receipts": []}, CFG)
        check("带台账编号的审核调用 ⇒ 执行前弹确认框（模型决策 ≠ 替主人签字）",
              isinstance(_r, dict) and "pending_confirm" in _r,
              f"{str(_r)[:80]}（令牌长度 {len((_r or {})
                  .get('pending_confirm', {}).get('token', ''))}"
              "—— 为 0 就是签名密钥空缺，见本段开头的密钥桩注释）")
        _q = (_r or {}).get("pending_confirm", {}).get("q", "")
        check("  ⭐ 负锁一：弹窗轮**零执行**（一个工具都没跑，`_CALLS` 空）",
              _r.get("messages") == [], str(_r.get("messages"))[:80])
        check("  弹窗把目标印给主人（#id + 作者 + 原文 + 现状）",
              "#94" in _q and "垃圾网站" in _q and "待审" in _q, _q[:110])
        check("  卡上签的就是这一件（编号 + 模型给的结论，签名后逐字可对账）",
              [_s.get("args", {}).get("verdict") for _s in (_r or {})
               .get("pending_confirm", {}).get("specs", [])] == ["reject"], _q[:70])
        check("  令牌照常签发（点确定后照签名拼计划，不靠模型回忆）",
              len((_r or {}).get("pending_confirm", {}).get("token", "")) > 20)
        # 对照组：同一个计划、同样真跑 execute，**确认轮**必须真执行（弹卡 ≠ 不办）。
        # 令牌里带的必须是**卡上签的那一件**（20261012 起 `_grant_covers` 按工具名+参数比对：
        # 光有令牌不算数——一个空壳 `{"token": "x"}` 会让这一轮重新弹卡）。
        _r2 = execute_node({"messages": [HumanMessage(content="确定")],
                            **plan_state(_plan_obj), "plan_rounds": 0, "done": False,
                            "receipts": [],
                            "confirm_grant": {"token": "x",
                                              "specs": [dict(_s) for _s in (_r or {})
                                                        .get("pending_confirm", {})
                                                        .get("specs", [])]}},
                           {**CFG, "configurable": {**CFG.get("configurable", {}),
                                                    "principal": Principal(uid=7,
                                                                           role="admin")}})
        check("  对照：同一条计划在**确认轮**放行执行（人闸是闸门，不是拒绝）",
              isinstance(_r2, dict) and "pending_confirm" not in _r2
              and any(t in str(_r2.get("messages")) for t in ("audit_board_comment", "复核")),
              str(_r2)[:120])
    finally:
        _tb._board_index = _orig_board
        settings.jwt_secret = _SAVED_SECRET   # ⑥ 段自己会再设一次桩
    print()
    settings.jwt_secret = _STUB_SECRET   # ⑤ 的 finally 还原成 _SAVED_SECRET（CI 里是空串）
    _pa = _popup("一级标签，名字叫X，使用粉色颜色") or {}
    _pad = _pa.get("pending_action") or {}
    check("弹窗那一轮同时产出结构化待办（不是只发一个 __CONFIRM__ 就完了）",
          bool(_pad), str(_pa.get("pending_confirm") is not None))
    check("  字段恰是十一件（增字段＝改跨语言契约：Rust 侧读的就是这十一件）",
          set(_pad) == {"task_id", "skill", "specs", "target", "requested_by", "source_event",
                        "question", "options", "token", "jti", "expires_at"},
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
    _pc = _pa.get("pending_confirm") or {}
    check("  重建卡片五件齐（问句/按钮/令牌/一次性编号/到期时刻）",
          bool(_pad.get("question")) and bool(_pad.get("options"))
          and bool(_pad.get("token")) and bool(_pad.get("jti"))
          and isinstance(_pad.get("expires_at"), int),
          str(sorted(k for k in ("question", "options", "token", "jti", "expires_at")
                     if not _pad.get(k))))
    check("  问句/按钮与当轮弹的那张**逐字同源**（不是各算一遍）",
          _pad.get("question") == _pc.get("q") and _pad.get("options") == _pc.get("opts"))
    check("  令牌与帧里那张是同一张（重放同一张才谈得上'用掉一次'）",
          _pad.get("token") == _pc.get("token") and bool(_pad.get("token")))
    check("  jti 与令牌自带的那个相等（落库侧认领的就是它）",
          _pad.get("jti") == confirm.token_jti(_pad.get("token", ""))
          and bool(re.fullmatch(r"[0-9a-f]{32}", str(_pad.get("jti", "")))),
          str(_pad.get("jti")))
    check("  expires_at 与令牌里的 exp 相等（读侧按它滤掉过期卡片）",
          _pad.get("expires_at") == _pc.get("exp") and _pad["expires_at"] > 0)
    check("不弹窗的轮次**没有**待办（提问/无权限不许凭空记一条等主人点头的事）",
          not (_popup("把文章 12 设为私密会有什么影响？") or {}).get("pending_action")
          and not (_popup("一级标签，名字叫X，使用粉色颜色",
                          principal=Principal(uid=9, role="user")) or {}).get("pending_action"))
    settings.jwt_secret = _SAVED_SECRET
    print()
    print("⑦ 被拒的确认请求必须留痕（20260924：点确定没生效这件事，agent 侧此前零证据）")
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
    _real_token = confirm.sign(1, 246, "favorite_add", [{"tool": "add_favorite", "args": {"article_id": 19}}])
    _meta2 = confirm.invalid_trace_meta(1, 246, len(_real_token))
    _flat = json.dumps(_meta2, ensure_ascii=False) + str(sorted(_meta2))
    check("  真令牌的任何一段（完整串/签名段/载荷段）都不在元数据里",
          _real_token not in _flat
          and _real_token.split(".")[0] not in _flat and _real_token.split(".")[1] not in _flat)
    _src = open("server.py", encoding="utf-8").read()
    check("接线：验签失败分支调用了落 trace（不是只打个 warning 就 return）",
          "_record_invalid_confirm(get_trace_id()" in _src
          and _src.index("_record_invalid_confirm(get_trace_id()")
              > _src.index("if grant is None:"))
    check("接线：正常轮的 trace input 带 conversation_id",
          '"conversation_id": req.conversation_id' in _src)
    from agent.runtime.confirmation import frames as confirmation_frames
    frame = next(confirmation_frames({"pending_confirm": {"exp": 123}}))
    check("确认帧携带到期时间供前端倒计时", json.loads(frame.split(":", 1)[1])["exp"] == 123)
    settings.jwt_secret = _SAVED_SECRET   # 还原（⑥ 与本节各自立桩，改完归还原值）
    print("\n⑧ 令牌的一次性编号 jti：同一张令牌不许兑现两次")
    settings.jwt_secret = _STUB_SECRET
    _t1 = confirm.sign(7, 42, "tag_create", [{"tool": "create_tag_one", "args": {"name": "X"}}])
    _t2 = confirm.sign(7, 42, "tag_create", [{"tool": "create_tag_one", "args": {"name": "X"}}])
    _j1, _j2 = confirm.token_jti(_t1), confirm.token_jti(_t2)
    check("jti 是 32 位十六进制（落库列 varchar(64)，不撞不改写）",
          bool(re.fullmatch(r"[0-9a-f]{32}", _j1)) and bool(re.fullmatch(r"[0-9a-f]{32}", _j2)),
          f"{_j1!r}/{_j2!r}")
    check("  同一秒内两次签发得到**不同** jti（不派生自 uid/会话/时间——派生就会撞）",
          _j1 != _j2)
    check("  jti 取自令牌自身、与验签解出的 payload 一致（认领键的信任来源）",
          confirm.token_jti(_t1) == (confirm.verify(_t1, 7, 42) or {}).get("jti"))
    check("  坏输入给空串、不抛（签发那一轮绝不因为解不出 jti 而断）",
          confirm.token_jti("") == "" and confirm.token_jti("x.y.z") == ""
          and confirm.token_jti(None) == "" and confirm.token_jti("没有点号") == "")
    check("  版本号已 +1（改 payload 结构必须 +1：留着旧号旧令牌会'少个字段也照发'）",
          confirm._VERSION == 2)
    _v1_body = json.dumps({"v": 1, "uid": 7, "conv": 42, "exp": int(time.time()) + 600,
                           "workflow": "tag_create", "specs": []},
                          ensure_ascii=False, separators=(",", ":")).encode()
    _v1 = confirm._b64e(_v1_body) + "." + confirm._b64e(
        hmac.new(_STUB_SECRET.encode(), confirm._DOMAIN + _v1_body, hashlib.sha256).digest())
    check("  签名正确但版本为 1 的令牌验不过（旧令牌一律失效，而不是尽力解析）",
          confirm.verify(_v1, 7, 42) is None)
    settings.jwt_secret = _SAVED_SECRET
    print()
    print("⑨ 只读解载荷 `inspect` 的信任等级（20260925）：它绝不许出现在授权路径上")
    settings.jwt_secret = _STUB_SECRET
    _ins = confirm.sign(7, 42, "tag_create", [{"tool": "create_tag_one", "args": {"name": "X"}}])
    _pay = confirm.inspect(_ins)
    check("解得出技能与参数（评测据此断言'卡片上的问法与参数可溯源'）",
          isinstance(_pay, dict) and _pay.get("workflow") == "tag_create"
          and _pay.get("specs") == [{"tool": "create_tag_one", "args": {"name": "X"}}],
          str(_pay)[:120])
    _forged = _ins.split(".")[0] + "." + confirm._b64e(b"whatever")
    check("**对伪造签名的令牌照样返回载荷**（这就是它不能进授权路径的原因）",
          (confirm.inspect(_forged) or {}).get("workflow") == "tag_create"
          and confirm.verify(_forged, 7, 42) is None)
    check("  坏输入给 None、不抛（排障时手滑贴了半截令牌不该炸）",
          confirm.inspect("") is None and confirm.inspect("x.y.z") is None
          and confirm.inspect(None) is None and confirm.inspect("没有点号") is None)
    _scan_targets = [ROOT / "server.py"] + sorted((ROOT / "agent").rglob("*.py")) \
        + sorted((ROOT / "tools").rglob("*.py"))
    _hits = []
    for _p in _scan_targets:
        for _i, _ln in enumerate(_p.read_text(encoding="utf-8").splitlines(), 1):
            if _looks_like_inspect_call(_ln):
                _hits.append(f"{_p.relative_to(ROOT)}:{_i}")
    check(f"  源码扫描：生产代码（server.py + agent/ + tools/ 共 {len(_scan_targets)}"
          " 个文件）零引用",
          not _hits, "；".join(_hits[:5]))
    check("  扫描器本身有牙齿（含调用的样本行命中、"
        "定义行/stdlib 反射不命中——否则'零引用'是恒真假绿）",
          _looks_like_inspect_call("    x = confirm.inspect(tok)")
          and _looks_like_inspect_call("payload = inspect(token)")
          and not _looks_like_inspect_call("def inspect(token):")
          and not _looks_like_inspect_call("    # 见 confirm.inspect(token) 的说明")
          and not _looks_like_inspect_call("    return inspect.getsource(obj)"))
    _q_notice_all = _confirm.render_confirm_question(
        [{"tool": "read_notifications", "args": {"all": True}}])
    _q_notice_ids = _confirm.render_confirm_question(
        [{"tool": "read_notifications", "args": {"ids": [7]}}])
    _q_mail_all = _confirm.render_confirm_question([{"tool"
        : "read_messages", "args": {"all": True}}])
    _q_mail_ids = _confirm.render_confirm_question([{"tool"
        : "read_messages", "args": {"ids": [3]}}])
    check("两族的问句都是人话（不落到『执行 <工具名>』那条兜底）",
          all("read_" not in q for q in (_q_notice_all, _q_notice_ids, _q_mail_all, _q_mail_ids)),
          " | ".join((_q_notice_all, _q_mail_all)))
    check("全写面没有一条问句说『红点会变小』（那是屏幕上看不到的状态）",
          not any("变小" in q for q in (_q_notice_all, _q_notice_ids, _q_mail_all, _q_mail_ids)))
    check("通知·全部：可以说红点会消失（判据是未读总数 > 0，标完它真的没了）",
          "红点会消失" in _q_notice_all, _q_notice_all)
    check("通知·点名几条：**不许**说这次标完红点就没了——只标一部分时它没有变化",
          "红点会消失" not in _q_notice_ids and "变小" not in _q_notice_ids
          and "要等未读全部读完才会消失" in _q_notice_ids, _q_notice_ids)
    check("站内信一族一个字都不提红点（头顶的点是通知+信两类之和，只标信它不消失）",
          "红点" not in _q_mail_all and "红点" not in _q_mail_ids,
          f"{_q_mail_all} | {_q_mail_ids}")
    check("两族都写明不可撤销（全写面里唯一点完回不到原状的操作）",
          all("不可撤销" in q for q in (_q_notice_all, _q_notice_ids, _q_mail_all, _q_mail_ids)))
    check("问句里没有嵌套的全角括号（『（…（…））吗？』读不出去哪半边收在哪儿）",
          not any("（" in q.split("不可撤销：", 1)[-1].split("）吗")[0]
                  for q in (_q_notice_all, _q_notice_ids, _q_mail_all, _q_mail_ids)),
          _q_notice_ids)
    settings.jwt_secret = _SAVED_SECRET
    print()
    print("⑩ 零写轮的事实与出口（20260926）：narrator 拿得到「本轮没有写操作」，"
       "planner 菜单里有「做不到」这条兜底语义")
    _NARRATOR_CHAT_PLAN = {"workflow": "chat", "params": {}, "tools": [], "note": "",
                           "reply": "直接回答"}
    _NARRATOR_WRITE_PLAN = {"workflow": "account_freeze", "params": {},
                            "tools": ['freeze_account({"name": "x"})'],
                                "note": "", "reply": "直接回答"}
    _st_chat = g.plan_state(dict(_NARRATOR_CHAT_PLAN))
    _st_write = g.plan_state(dict(_NARRATOR_WRITE_PLAN))
    _out_chat = g._narrator_plan(_st_chat)
    check("零写轮：计划段带上那条系统事实（此前只有 data_repeat 那一支有）",
          _out_chat != _st_chat["plan"] and "一个写操作都没提出来" in _out_chat)
    check("  事实里给出「做不到」这条正面出口（不只是禁止句）",
          "直接说" in _out_chat and "做不到" in _out_chat and "替代" in _out_chat)
    check("  旧措辞「要动手还得说清对哪一条做什么」已撤掉（那是同一洞的另一半："
      "等于教它回一句『你说一声我就去办』）",
          "说清**对哪一条**做什么" not in _out_chat)
    check("写轮（本轮真排了写操作）：一个字都不加——说了就是假的，等于把刚办成的事说成没做",
          g._wrote_this_round(_st_write)
          and g._narrator_plan(_st_write) == _st_write["plan"])
    check("弹卡轮（pending_confirm 在场）：不加（那种轮次结构上也轮不到 narrator 说话）",
          g._narrator_plan(dict(_st_chat, pending_confirm={"workflow": "x"}))
          == _st_chat["plan"])
    _PRE = _st_chat["plan"] + "\n" + g._no_popup_fact(_st_chat)
    check("data_repeat 那一支已拼过：不重复追加（同一段话在提示里出现两次）",
          g._narrator_plan(dict(_st_chat, plan=_PRE)) == _PRE)
    _ctx_admin = build_planner_context("admin")
    _ctx_public = build_planner_context(None)
    check("planner 菜单有「技能都不覆盖 ⇒ 如实说做不到 + 给替代」这条兜底语义（管理员菜单）",
          "根本没有" in _ctx_admin and "如实说做不到" in _ctx_admin)
    check("  公开菜单同样有（能力缺位与身份无关）",
          "如实说做不到" in _ctx_public)
    check("  同一段里禁止索要「要不要我办」（不排写操作就不会有卡，那句承诺保证是假）",
          "你说一声我就去办" in _ctx_admin and "保证是假的" in _ctx_admin)
    check("  兜底只在**菜单**、不在 chat 的 description 里"
      "（写进 description 会让'没有能力'变成 chat 的常规用法，"
      "真有技能的请求也可能被判成做不到）",
          "如实说做不到" not in WORKFLOW_MAP["chat"].description)
    check("  并且要求「拿不准就照常选技能」（失败取向往能干活那侧倒）",
          "拿不准就照常选技能" in _ctx_admin)
    print("\n⑪ 用户自己数据的四件：弹窗触发矩阵（20260928 契约精度修正）")
    settings.jwt_secret = _STUB_SECRET
    _OWN_CASES = [
        ("favorite_add", 'add_favorite({"article_id": 12})',
         "我想收藏这一篇", "收藏这篇文章 12"),
        ("favorite_remove", 'remove_favorite({"article_id": 12})',
         "我想把那篇取消收藏", "取消收藏文章 12"),
        ("notice_read", 'read_notifications({"ids": [23]})',
         "通知那边有点乱", "把通知都标记已读"),
        ("message_read", 'read_messages({"ids": [5]})',
         "信箱那边有点乱", "把站内信都标记已读"),
    ]
    _EV_ART = ToolMessage(content="后台文章共 3 篇：\n- id=12 [私密]《架构文档》标签：Rust",
                          tool_call_id="o1", name="list_admin_notes")
    _EV_NOTIF = ToolMessage(content="站内通知（未读 1）：notifId:23《留言已通过审核》",
                            tool_call_id="o2", name="list_notifications")
    for _workflow, _spec, _intent, _cmd in _OWN_CASES:
        _ev = _EV_ART if _workflow.startswith("favorite") else _EV_NOTIF
        check(f"{_workflow}：有意向没判成命令 + 目标有据 → **弹卡**",
              bool(_own_popup(_workflow, _spec, _intent, _ev)))
        check(f"  同一件事写成明确命令 → **不弹卡**（快路径直执行，卡不是必经之路）",
              _own_popup(_workflow, _spec, _cmd, _ev) is None)
        check(f"  提问形态照旧不弹（提问不是下令）",
              _own_popup(_workflow, _spec, "这个操作是什么意思呀", _ev) is None)
    check("收藏两件：目标**无据**（本轮没读到那篇文章）→ 本轮不弹卡（交回 planner 先读）",
          _own_popup("favorite_add", 'add_favorite({"article_id": 12})',
                     "我想收藏这一篇", None) is None)
    check("  对照：已读两件没有这一档——目标不要求有据 ⇒ 判不出来照样弹",
          bool(_own_popup("notice_read", 'read_notifications({"ids": [23]})',
                          "通知那边有点乱", None)))
    print("\n  描述 ↔ 代码事实：契约句必须跟着 `_ALWAYS_CONFIRM_TOOLS` 走")
    _always = authz._ALWAYS_CONFIRM_TOOLS
    _over_promise, _missing_rule, _under_claim = [], [], []
    for _s in WORKFLOWS:
        _tools = [t for t, _ in _s.plan if t]
        if not _tools:
            continue
        if not ({authz.required_scope(t) for t in _tools} & set(authz.CONSENT_SCOPES)):
            continue  # 不是需确认的写技能（读技能、无工具技能）
        if all(t in _always for t in _tools):
            if "判成明确命令就直接办" in _s.description:
                _under_claim.append(_s.name)
        else:
            if "由系统弹确认框问主人" in _s.description:
                _over_promise.append(_s.name)
            if "判成明确命令就直接办" not in _s.description:
                _missing_rule.append(_s.name)
    check("非「一律弹窗」的写技能**不再**承诺「由系统弹确认框问主人」（那句只对一律弹窗族成立）",
          not _over_promise, str(_over_promise))
    check("  且都写清了两档结局（判成明确命令就直接办）", not _missing_rule, str(_missing_rule))
    check("「一律弹窗」族没被顺手改成两档（它们的卡每轮都弹）",
          not _under_claim, str(_under_claim))
    settings.jwt_secret = _SAVED_SECRET   # 收尾还原（与其它节同一约定）
    print("\n⑫ 收窄 `pick:<i>` 与跨语言守卫（原 `test_change_set.py` 里没被批 H 删掉的那几条）")
    settings.jwt_secret = _STUB_SECRET
    _grant_specs = [{"tool": "audit_board_comment",
                     "args": {"talk_id": 94, "verdict": "reject"}},
                    {"tool": "approve_quota_request", "args": {"user_id": 5}}]
    _tok = confirm.sign(7, 42, "review_inbox", _grant_specs)
    _pay = confirm.verify(_tok, 7, 42)
    check("令牌签发→验签往返（批 H **不改**令牌格式、不改版本号——一次点头办 N 件的机器已在位）",
          isinstance(_pay, dict) and _pay.get("v") == confirm._VERSION
          and _pay.get("specs") == _grant_specs, str(_pay)[:80])
    _opts = _confirm.confirm_opts(len(_grant_specs))
    check("卡面 N+2 枚按钮：全部处理 + 逐条「只处理第 i 个」+ 其他（我来说）+ 取消",
          [o["value"] for o in _opts] == ["yes", "pick:0", "pick:1", "other", "no"],
          str([o["value"] for o in _opts]))
    check("  「全部处理」写着任务数（主人点之前看得见自己要同意几个任务）",
          f"{len(_grant_specs)} 个任务" in _opts[0]["label"])
    for _i in (0, 1):
        _narrowed, _err = confirm.narrow(_pay, _opts[_i + 1]["value"])
        check(f"  按钮「{_opts[_i + 1]['label']}」⇒ 恰好裁到签名清单里那一件",
              _err == "" and (_narrowed or {}).get("specs") == [_grant_specs[_i]],
              f"{_err} {str(_narrowed)[:60]}")
    check("问句里点名的按钮与卡面按钮同一套字面（不一致 = 问句指着一个不存在的按钮）",
          all(o["label"].startswith("只处理第") for o in _opts[1:-2])
          and "只处理第" in _confirm.render_confirm_question(_grant_specs)
          and "全部处理" in _confirm.render_confirm_question(_grant_specs))
    check("  「其他（我来说）」恒紧挨在「取消」前面（两种卡面同一位置）",
          _opts[-2]["value"] == "other" and _confirm.confirm_opts(1)[-2]["value"] == "other")
    check("  多件问句里**逐条编号**（编号就是按钮指的那个下标）",
          "1. " in _confirm.render_action_lines(_grant_specs)
          and "2. " in _confirm.render_action_lines(_grant_specs))
    for _bad_pick in ("pick:9", "pick:99", "2", "pick:-1", "pick:", "全部"):
        _n2, _e2 = confirm.narrow(_pay, _bad_pick)
        check(f"  读不懂/越界的选择「{_bad_pick}」⇒ 空 + 原因（fail-closed，绝不放大成全部处理）",
              _n2 is None and bool(_e2), _e2)
    _n3, _e3 = confirm.narrow(_pay, "")
    check("  空选择 = 全部处理（旧客户端不发 confirm_pick 时逐字兼容）",
          _n3 is not None and len(_n3["specs"]) == 2 and _e3 == "")
    # 20261006：单件卡是**三枚**（「其他（我来说）」）；20261019 句尾换「处理」；
    # 20261020 单件那支改成与多件**同形** ⇒ 句尾字面随之变；判据见 test_confirm_question_shape.py
    check("  单件三枚（确定 / 其他（我来说）/ 取消），句尾动词是「处理」",
          [o["label"] for o in _confirm.confirm_opts(1)] == ["确定", "其他（我来说）", "取消"]
          and _confirm.render_confirm_question(_grant_specs[:1]).endswith(
              "点「确定」我就去处理；点「取消」就当没说过。"))
    _n4, _e4 = confirm.narrow(_pay, "other")
    check("  「其他」不是选择记号 ⇒ fail-closed（它永不该走确认请求）",
          _n4 is None and bool(_e4), _e4)
    settings.jwt_secret = _SAVED_SECRET
    print("\n⑫b 「先建再挂」是有依赖的一组 ⇒ 只算一格（20261010，见 adminops.linked_units）")
    settings.jwt_secret = _STUB_SECRET
    _pair = [{"tool": "create_tag", "args": {"title": "测试"}},
             {"tool": "set_article_tags", "args": {"article_id": 11, "add": ["测试"]}}]
    check("依赖组并成一个单元（一格 = 一次点击能单独办的那件事）",
          _confirm.linked_units(_pair) == [[0, 1]], str(_confirm.linked_units(_pair)))
    check("  ⇒ 卡面退回单件措辞、只剩三枚按钮（没有那枚点不动的「只处理第 2 个」）",
          [o["label"] for o in _confirm.confirm_opts(len(_confirm.linked_units(_pair)))]
          == ["确定", "其他（我来说）", "取消"]
          and "点「确定」" in _confirm.render_confirm_question(_pair)
          and "只处理第" not in _confirm.render_confirm_question(_pair))
    check("  ⇒ 那一格仍把「新建」与名字印出来（主人签字前看得见站里会多一个标签）",
          "新建" in _confirm.render_action_lines(_pair)
          and "测试" in _confirm.render_action_lines(_pair))
    check("  ⇒ 两步连在同一格（「，并」），不是两个编号",
          "，并" in _confirm.render_action_lines(_pair)
          and "2. " not in _confirm.render_action_lines(_pair))
    _tok2 = confirm.sign(7, 42, "article_tags", _pair, _confirm.linked_units(_pair))
    _pay2 = confirm.verify(_tok2, 7, 42)
    _ok2, _e2b = confirm.narrow(_pay2, "pick:0")
    check("  ⇒ 令牌里带着这份分组，「只办第 1 件」拿回来的就是**整组**（先建再挂一起走）",
          isinstance(_pay2, dict) and _pay2.get("units") == [[0, 1]]
          and _e2b == "" and (_ok2 or {}).get("specs") == _pair,
          f"{_e2b} {str(_ok2)[:80]}")
    _bad2, _be2 = confirm.narrow(_pay2, "pick:1")
    check("  ⇒ 那一格之外没有第二格可挑（越界 fail-closed，绝不放大成整批）",
          _bad2 is None and bool(_be2), _be2)
    check("  没有依赖的两件仍是两个单元（老卡面一个字节没变）",
          _confirm.linked_units(_grant_specs) == [[0], [1]]
          and _confirm.linked_units([{"tool": "create_tag", "args": {"title": "Git"}}]) == [[0]])
    check("  名字对不上就不并（create Git + add SVN：各走各的）",
          _confirm.linked_units([{"tool": "create_tag", "args": {"title": "Git"}},
                          {"tool": "set_article_tags",
                           "args": {"article_id": 11, "add": ["SVN"]}}]) == [[0], [1]])
    check("  只有挂、没有建（标签本来就在，那一步被 `_drop_satisfied_tag_creates` 摘掉了）"
      " ⇒ 它自己一个单元", _confirm.linked_units([_pair[1]]) == [[0]])
    for _bad_units in ([[0]], [[0], [0]], "x", [[]], [[0], [2]], [[True]]):
        _n5, _e5 = confirm.narrow({**_pay2, "units": _bad_units}, "pick:0")
        check(f"  分组读不懂（{_bad_units!r}）⇒ fail-closed 零执行，绝不猜一个更大的范围",
              _n5 is None and bool(_e5), _e5)
    _g_token, _g_units = g._sign_confirm_picks(
        {"plan_obj": {"workflow": "article_tags"}},
        {"configurable": {"conversation_id": 42}}, Principal(uid=7), _pair)
    _gw, _gwe = confirm.narrow(confirm.verify(_g_token, 7, 42), "pick:0")
    check("  ⇒ graph 那半签发的是同一份分组（卡面与令牌同源，不是各算一次）",
          _g_units == [[0, 1]]
          and (confirm.verify(_g_token, 7, 42) or {}).get("units") == [[0, 1]]
          and len(_confirm.confirm_opts(len(_g_units))) == 3)
    check("  ⇒ 端到端：那张令牌上「只办第 1 件」拿回的就是整组",
          _gwe == "" and (_gw or {}).get("specs") == _pair, _gwe)
    settings.jwt_secret = _SAVED_SECRET
    print("\n⑫c 「这张卡有几格」在评测侧读得到（20261010，`confirm.units_of`）")
    check("缺席分组 ⇒ 逐条一单元（老令牌/单件卡，与批 F 逐字相同）",
          confirm.units_of({"specs": _grant_specs}) == [[0], [1]])
    check("  带了分组 ⇒ 就是那一份（一格两件）",
          confirm.units_of({"specs": _pair, "units": [[0, 1]]}) == [[0, 1]])
    check("  分组读不懂 / 没有 specs ⇒ None（零执行的信号，不是「一件一件办」）",
          confirm.units_of({"specs": _pair, "units": [[0]]}) is None
          and confirm.units_of({"specs": [], "units": []}) is None
          and confirm.units_of({}) is None)
    check("接线：生产的收窄在验签之后（server.chat_stream）",
          _src.index("confirm.narrow(grant, req.confirm_pick)")
          > _src.index("confirm.verify(req.confirm_token, principal.uid"))
    _rg_src = open("eval/run_golden.py", encoding="utf-8").read()
    check("接线：评测侧同样收窄（run_golden.run_one）——缺了它「挑一件」在语料里跑不到",
          _rg_src.index("confirm.narrow(confirm_grant, confirm_pick)")
          > _rg_src.index("confirm.verify(confirm_token, uid"))
    check("接线：用例轮里的记号真的接到请求上（run_case → run_one）",
          'req.confirm_pick = rnd.get("confirm_pick")' in _rg_src
          and "confirm_pick=req.confirm_pick" in _rg_src
          and '"confirm_pick"' in _rg_src)
    check("生产那一句「一个任务都没有处理」只有一处字面，且生成器用的是它",
          _src.count("一个任务都没有处理") == 1
          and "INVALID_PICK_TEXT = (" in _src
          and "json.dumps(INVALID_PICK_TEXT" in _src
          and "server.INVALID_PICK_TEXT" in _rg_src)
    _rs = _parent_repo.read(
        "src/routes/chat.rs",
        why="「只办其中一件」的凭据是 `confirm_pick` 纯透传 —— Rust 那半若不转发它，"
        "前端点了「只办第 1 件」到服务端就退化成「全部办」（一次挑一件变成整批执行）")
    if _rs is not None:
        check("Rust 有 `confirm_pick` 透传字段（不验签、不落库）",
              re.search(r"pub confirm_pick:\s*Option<String>", _rs) is not None)
        check("  且真的进了转发 body（键名与 Python 侧逐字一致）",
              '"confirm_pick":' in _rs or '"confirm_pick" :' in _rs)
        check("「上一轮执行过哪些工具」（结构化事实）也在 body 里",
              "recent_tools" in _rs and '"recent_tools"' in _rs)
        _approval = _parent_repo.read("src/routes/chat/approval.rs")
        check("旧卡单次认领保留 CAS，原生卡转交持久执行账",
              all(marker in _approval for marker in _HOST_CLAIM_MARKERS))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


if __name__ == "__main__":
    test_confirm()
