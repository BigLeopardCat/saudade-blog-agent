# -*- coding: utf-8 -*-
"""管理助手写操作单测（纯函数 + 假 httpx + 假工具，零网络、零 LLM、秒级）。

被测四块：
  · `agent/adminops.py` —— 写操作的纯函数层（状态映射、标签编解码、名字↔id、渲染）；
  · `tools/base.py`     —— `_admin_post` 的失败取向与三个写工具的**写前查、写后复核**；
  · `agent/graph.py`    —— checker 三态、execute 的三道门（权限/确认/目标）、
                            写去重判据 `_already_done_writes`；
  · 接线锁               —— 前端标签配色同源、写工具不进 `_CONTENT_TOOLS`。

这一轮的写工具与只读工具最大的区别是**失败的样子**：只读失败顶多少答一句，写失败若
被写成 `ok("创建失败…")`，`_check_spec` 对非空文本一律判 PASS ⇒ 失败变成**系统确认
事实**落进 receipts → execution_log → 下轮 narrator 照着「已创建」讲。所以本文件的
主断言是"没做成 = unavailable"（§8/§9/§10 每族都有一组），另外三组回归锁是：

  1. **三道门各自独立**：身份（能不能做）/ 确认（这次要不要做）/ 目标（做哪一篇）——
     任一不过都必须在**调用之前**产带原因码的错误帧，且**零调用**；
  2. **写去重是 args-aware**：「再帮我把那篇也置顶」与「把这篇置顶」是两个 spec，
     只比工具名的旧判据会把第二件静默收尾，而 narrator 握着第一条真回执必然说成
     "都改好了"；
  3. **回执不留 uid、不留标题**：只落执行**角色**与结构化前后值；标题只出现在给人
     看的文本里（回执会经 execution_log 注入下一轮，带《标题》会被读成"我读过这篇"
     的指代证据）。

用法：.venv/bin/python tests/test_admin_write.py
"""
import base64
import contextlib
import json
import sys
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
from agent import adminops as A  # noqa: E402
from agent import authz  # noqa: E402
from agent.graph import (EXECUTED_ONCE_SKILLS, SNAPSHOT_SKILLS,  # noqa: E402
                         _CONTENT_TOOLS, _already_done_writes, _check_spec,
                         execute_node, plan_state)
from agent.principal import ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402
import tools.base as base  # noqa: E402

# ── 密钥桩：settings.jwt_secret 是全局单例，测试里直接改它 ────────────────
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
# 桩值（不是 _SAVED_SECRET）：CI 里没有 .env，settings.jwt_secret 默认是空串，而
# `confirm.sign` 密钥空缺时返回空串、`_confirm_popup` 据此**不弹窗** ⇒ §⑰ 那三条
# "该弹窗"的正例在本机会绿、在 CI 会静默变成"没弹"（20260922 CI 实测：本套件 5 项
# 红全落在这一族，反例照样绿——正是"反例恒真"的假绿形态）。桩完才是可复现的。
_STUB_SECRET = "test-secret-for-confirm-tokens"
settings.jwt_secret = _STUB_SECRET

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


@contextlib.contextmanager
def patch(**kw):
    """临时替换 tools/base 模块级函数（工具在调用时按模块全局名解析，故替换生效）。"""
    saved = {k: getattr(base, k) for k in kw}
    for k, v in kw.items():
        setattr(base, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(base, k, v)


class _Seq:
    """按序返回的桩：同一个函数被调用多次而每次答案不同（写前读 / 写后复核）。"""

    def __init__(self, *vals):
        self.vals = list(vals)
        self.n = 0

    def __call__(self, *a, **k):
        v = self.vals[min(self.n, len(self.vals) - 1)]
        self.n += 1
        return v


class _Post:
    """记录 POST 调用（路径 + 载荷）并返回预设值。"""

    def __init__(self, ret):
        self.ret = ret
        self.calls: list = []

    def __call__(self, path, payload, config):
        self.calls.append((path, payload))
        return self.ret


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


# 标签字典样本（形态抄自 src/routes/tags.rs：一级 {tagKey,title,level}，
# 二级另有 fatherTag(父名)/fatherKey(父 id)）
ONE = [{"tagKey": 1, "title": "Python", "level": 1},
       {"tagKey": 2, "title": "架构", "level": 1}]
TWO = [{"tagKey": 10000, "title": "爬虫", "level": 2,
        "fatherTag": "Python", "fatherKey": 1},
       {"tagKey": 10001, "title": "分布式", "level": 2,
        "fatherTag": "架构", "fatherKey": 2}]
IDX = A.build_tag_index(ONE, TWO)


def note(aid=12, title="架构", status="private", top=0, tags=""):
    return {"noteKey": aid, "noteTitle": title, "status": status,
            "isTop": top, "noteTags": tags}


# ══════════════════════════════════════════════════════════════════
print("\n① 纯函数：状态映射（认不出来就拒绝，绝不猜）")

check("口语/别名 → 存储值", A.normalize_status("公开") == "public"
      and A.normalize_status("PUBLISHED") == "public"
      and A.normalize_status("隐藏") == "private"
      and A.normalize_status("草稿") == "draft")
check("认不出来的状态 → None（调用方拒绝执行）",
      A.normalize_status("半公开") is None and A.normalize_status("") is None
      and A.normalize_status("publishedd") is None)
check("None（没点名这个字段）→ None，与「认不出来」同形（调用方按 has_* 区分）",
      A.normalize_status(None) is None)
check("置顶映射：1/0 与中文别名",
      A.normalize_top(1) == 1 and A.normalize_top("0") == 0
      and A.normalize_top("是") == 1 and A.normalize_top("取消置顶") == 0)
check("置顶越界值 → None（不把 2 当成置顶）",
      A.normalize_top(2) is None and A.normalize_top("也许") is None)
check("bool 不按 int 处理（True 是「是」，不是越界的 1）",
      A.normalize_top(True) == 1 and A.normalize_top(False) == 0)
check("未知值渲染成带引号的原值，不假装是个已知状态",
      A.status_cn("半公开") == "未知状态「半公开」" and A.top_cn(7) == "未知置顶值「7」")


print("\n② 纯函数：note.tags 编解码（脏值容错、写出口唯一）")

check("读：逗号串 → id 列表", A.parse_tag_ids("1,2") == [1, 2])
check("读：容忍脏值（空段/重复/空白）", A.parse_tag_ids("1,1,,") == [1]
      and A.parse_tag_ids(" 3 , 1 ") == [3, 1])
check("读：`12abc` 不当成 12（前端那条 parseInt 的老行为刻意不学）",
      A.parse_tag_ids("12abc") == [])
check("读：list/None/数字混装", A.parse_tag_ids(["1", 2, "x"]) == [1, 2]
      and A.parse_tag_ids(None) == [] and A.parse_tag_ids("") == [])
check("读：负数/零/布尔被丢（id 空间是正整数）",
      A.parse_tag_ids("0,-3") == [] and A.parse_tag_ids([True, 5]) == [5])
check("写：去重保序", A.join_tag_ids([2, 1, 2]) == "2,1")
check("写：空列表 → 空串（**语义是清空标签**，唯一允许产出空串的出口）",
      A.join_tag_ids([]) == "")
check("编解码往返一致", A.parse_tag_ids(A.join_tag_ids([10000, 12])) == [10000, 12])


print("\n③ 标签配色与前端同源（改一侧必须同步另一侧）")


def frontend_color(name: str) -> str:
    """前端 `colorForName` 的独立实现：逐 **UTF-16 码元**滚动哈希。

    刻意不调 `A.color_for_name`——拿实现验实现等于没验。这里重算一遍，两边同时
    错才算"对拍通过"（所以下面还有一条"与 ord() 版不同色"的反向锁）。
    """
    raw = name.encode("utf-16-le")
    h = 0
    for i in range(0, len(raw), 2):
        h = (h * 31 + (raw[i] | (raw[i + 1] << 8))) % 100000
    return A.NEW_TAG_COLORS[h % len(A.NEW_TAG_COLORS)]


def ord_color(name: str) -> str:
    """**错的**那种实现**（按 Python 码点）：emoji 会与前端算出不同颜色。"""
    h = 0
    for ch in name:
        h = (h * 31 + ord(ch)) % 100000
    return A.NEW_TAG_COLORS[h % len(A.NEW_TAG_COLORS)]


check("配色表与前端 NEW_TAG_COLORS 同序（顺序变了颜色就全变）",
      A.NEW_TAG_COLORS == ['#1677ff', '#52c41a', '#fa8c16', '#eb2f96',
                           '#722ed1', '#13c2c2', '#f5222d', '#a0d911'])
check("BMP 名字：与独立重算一致",
      all(A.color_for_name(n) == frontend_color(n)
          for n in ("Python", "编程", "架构", "测试", "分布式", "C++")),
      "; ".join(f"{n}={A.color_for_name(n)}" for n in ("Python", "编程", "架构")))
check("同名永远同色（哈希不是随机）",
      A.color_for_name("架构") == A.color_for_name("架构"))
check("非 BMP（emoji）按 UTF-16 代理对算 —— 与按码点算**不同色**，这条锁的是"
      "「别把实现简化成 ord()」（前端 charCodeAt 是码元）",
      A.color_for_name("🐍") == frontend_color("🐍")
      and A.color_for_name("🐍") != ord_color("🐍"),
      f"{A.color_for_name('🐍')} vs {ord_color('🐍')}")


print("\n④ 纯函数：名字 ↔ id（歧义不替用户选，悬空不静默吞）")

hit, _ = A.find_tag(IDX, "Python")
check("一级标签精确命中", hit is not None and hit.id == 1 and hit.level == 1)
check("二级标签按父作用域找", A.find_tag(IDX, "爬虫", 1)[0].id == 10000)
check("父作用域下无此名 → 没命中", A.find_tag(IDX, "爬虫", 2)[0] is None)
check("名字不完全相等就不算命中（不做包含/模糊匹配）",
      A.find_tag(IDX, "Pyth")[0] is None and A.find_tag(IDX, "python")[0] is None)
check("去空白后相等算命中", A.find_tag(IDX, " Python ")[0].id == 1)
amb = {1: A.TagInfo(1, "笔记", 1), 2: A.TagInfo(2, "笔记", 1)}
h2, c2 = A.find_tag(amb, "笔记")
check("同名多命中 → 返回候选、不替用户挑一个",
      h2 is None and [c.id for c in c2] == [1, 2])
check("二级展示名带父标签（与前端 flattenTagOptions 一致）",
      IDX[10000].label == "Python / 爬虫" and IDX[1].label == "Python")
check("按 fatherKey 建树（一级改名不该让子标签集体失联）",
      A.build_tag_index([{"tagKey": 1, "title": "已改名", "level": 1}], TWO)[10000]
      .father_id == 1)
check("悬空 id 如实标注（不是静默消失）",
      A.render_tag_list("1,999", IDX) == "Python、（已失效 id=999）",
      A.render_tag_list("1,999", IDX))
check("**读不到字典** ≠ 标签不存在：只给 id，不编「已失效」",
      A.render_tag_list("1,2", None) == "id=1、id=2")
check("超上限折叠是后缀不是并列项（不是真有个叫「等 4 个」的标签）",
      A.render_tag_list("1,2,10000,10001", IDX, limit=2) == "Python、架构 等 4 个",
      A.render_tag_list("1,2,10000,10001", IDX, limit=2))
check("无标签渲染成（无标签）", A.render_tag_list("", IDX) == "（无标签）")

print("\n④b 纯函数：文章详情补标签名（问「这篇有什么标签」不必再烧一轮）")
# 现场（20260921 22:34）：访客问「那篇都有什么标签呀」→ planner 读 get_article_detail
# → 帧里只有 `noteTags: '5'`（内部 id）→ narrator 只能念 id，用户只好说「查查吧」，
# 再调一次 list_tags 才拿到名字。详情帧补一个 `tags`（中文名）就是这个洞的补丁。
import tools.base as _B  # noqa: E402

_saved_index = _B._public_tag_index
try:
    _B._public_tag_index = lambda: IDX
    _row = _B._note_row_with_tag_names({"noteKey": 23, "noteTitle": "x", "noteTags": "1,10000"})
    check("noteTags（id 串）→ tags 给中文名（含层级路径）",
          _row["tags"] == "Python、Python / 爬虫", _row.get("tags"))
    check("  id 原样保留（$tool[N].noteTags 这类参数引用不受影响）",
          _row["noteTags"] == "1,10000")
    check("  不就地改调用方的行（返回新 dict）",
          _B._note_row_with_tag_names({"noteTags": "1"}).get("tags") is not None)
    _B._public_tag_index = lambda: None
    check("  字典读不到 ≠ 标签不存在：只给 id，不编名字也不说（无标签）",
          _B._note_row_with_tag_names({"noteTags": "1"})["tags"] == "id=1")
    _B._public_tag_index = lambda: (_ for _ in ()).throw(AssertionError("本就没标签，不该拉标签字典"))
    check("  本来就没有标签 → （无标签），且**不去拉字典**（白花两个请求）",
          _B._note_row_with_tag_names({"noteTags": ""})["tags"] == "（无标签）")
    check("  非 note 行（说说/留言/设备）原样返回，别套同一把刀",
          _B._note_row_with_tag_names({"talkKey": 1, "talkContent": "x"})
          == {"talkKey": 1, "talkContent": "x"})
finally:
    _B._public_tag_index = _saved_index


print("\n⑤ 纯函数：渲染（清单喂 id、变更只讲真动了的字段）")

check("只改置顶 → 前后串里不出现状态",
      A.render_change([("未置顶", "置顶")]) == ("未置顶", "置顶"))
check("状态+置顶 → 两条都在", A.render_change([("私密", "公开"), ("未置顶", "置顶")])
      == ("私密 / 未置顶", "公开 / 置顶"))
check("回执字段截断带省略号（detail 列宽有限）",
      A.clip("x" * 80) == "x" * 60 + "…" and A.clip("ok") == "ok")
_list = A.render_admin_notes([note(12, "架构文档", "draft", 1)], IDX)
# 20260928 起这里是 `noteId=12`（命名空间名）而不是裸 `id=12`——这一屏正是 planner
# 的**取值来源**，裸 id 会让同一个数字在别的帧里以另一重身份出现。注意 `noteId=12`
# 里那个是**大写 I**，`"id=12"` 不是它的子串 ⇒ 只做包含断言的话，回退成旧形是**静默**
# 的（测试还绿）。所以下面既钉新形，也用负向断言钉死旧形。
check("后台清单带 id 与状态标记（planner 只有这一轮真读到 id 才有据可写）",
      _list.startswith("后台文章共 1 篇") and "noteId=12 [草稿/置顶]《架构文档》" in _list, _list)
check("  且不再是裸 ` id=`（同一个数字只以一种身份露面）",
      " id=12" not in _list and "#12" not in _list, _list)
check("超长标题截断，清单不撑爆提示词",
      "…" in A.render_admin_notes([note(1, "长" * 50)], IDX))


print("\n⑥ 纯函数：target_mentioned（数字边界是这一族的全部价值）")

check("独立出现的 id 命中", A.target_mentioned(12, ["把文章 12 设为私密", "id=12"]))
check("**不许被子串冒充**：id=123 / 1912 都不是 12",
      not A.target_mentioned(12, ["id=123"]) and not A.target_mentioned(12, ["1912"]))
check("已知的宽松处：小数「12.5」里的 12 **算**命中（判据只要求两侧不挨数字）——"
      "刻意不堵：写操作还有回执回显 + 管理员复核两道，堵它反而会在标点形态上误伤",
      A.target_mentioned(12, ["12.5"]))
check("多个材料里任一命中即可（本轮读过的帧 + 页面上下文 + 用户消息）",
      A.target_mentioned(12, ["", "", "id=12 《架构》"]))
check("非法 id 一律 False（不猜、不默认）",
      not any(A.target_mentioned(v, ["12"]) for v in (None, 0, -1, "abc", True)))
check("字符串形式的 id 与 int 等价", A.target_mentioned("12", ["文章 12"]))
check("unknown_target 帧带原因码，checker 可回取；非本族帧回 None",
      A.target_error_reason(A.unknown_target_frame("set_article_status")) == "unknown_target"
      and A.target_error_reason("__ERROR__: 无法获取当前用户身份") is None)


# ══════════════════════════════════════════════════════════════════
print("\n⑦ _admin_post：失败取向（假 httpx 客户端，只桩边界）")


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    """桩 httpx 客户端：记录 (method, url, headers, payload)。只桩边界——写通道
    20260921 从 post 泛化成 request（PUT/DELETE 都走它），桩也得跟着记 method，
    否则"改名走 PUT、删除走 DELETE"这类形态断言无从写起。"""

    def __init__(self, resp=None, exc=None):
        self.calls = []
        self.resp, self.exc = resp, exc

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, headers or {}, json))
        if self.exc:
            raise self.exc
        return self.resp

    def post(self, url, headers=None, json=None, timeout=None):
        return self.request("POST", url, headers=headers, json=json, timeout=timeout)


real_client = base._client
try:
    c = _Client(_Resp(200, {"code": 200, "data": "10002"}))
    base._client = c
    out = base._admin_post("/api/protected/tagone", {"title": "X"}, cfg(7))
    check("成功 → 返回 data 字段", out == "10002", str(out))
    method, url, hdrs, payload = c.calls[0]
    check("走的是 _admin_request 的 POST 形态", method == "POST", method)
    check("打的是本机回环后台地址", url == base.ADMIN_BASE + "/api/protected/tagone", url)
    tok = hdrs.get("Authorization", "")
    check("带 Bearer 局部 JWT（三段）", tok.startswith("Bearer ") and tok.count(".") == 2)
    seg = tok.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    check("JWT sub = 发起人 uid、有效期 60 秒、不带 aud（多一个 aud 会验签失败）",
          claims.get("sub") == 7 and 50 <= claims["exp"] - time.time() <= 60
          and "aud" not in claims, str(claims))
    check("请求体原样发出（工具侧只发它点名的字段）", payload == {"title": "X"})

    for status, why in [(401, "未登录/令牌无效"), (403, "非 admin")]:
        base._client = _Client(_Resp(status, {"code": status}))
        r = base._admin_post("/api/protected/tagone", {}, cfg(7, ROLE_USER))
        check(f"{status}（{why}）→ unavailable 且措辞是「无权」不是「故障」",
              r.kind == "unavailable" and "无权" in r, f"{r.kind}: {r}")

    for resp, why in [(_Resp(500, None), "HTTP 500"),
                      (_Resp(200, None), "非 JSON"),
                      (_Resp(200, {"code": 500, "message": "标签名重复"}), "业务码 500")]:
        base._client = _Client(resp)
        r = base._admin_post("/api/protected/tagone", {}, cfg(7))
        check(f"{why} → unavailable（HTTP 200 也不算成功）",
              r.kind == "unavailable", f"{r.kind}: {r}")
    check("业务码错误时把后台的 message 带进措辞（好让人知道为什么）",
          "标签名重复" in r, str(r))

    base._client = _Client(exc=RuntimeError("connection refused"))
    r = base._admin_post("/api/protected/tagone", {}, cfg(7))
    check("连接异常 → unavailable，且措辞明写「未确认生效，不要声称已改好」",
          r.kind == "unavailable" and "不要声称已改好" in r, f"{r.kind}: {r}")

    c = _Client(_Resp(200, {"code": 200, "data": "1"}))
    base._client = c
    r = base._admin_post("/api/protected/tagone", {}, cfg(0))
    check("uid ≤ 0 → unavailable 且**一个请求都不发**（身份不明时最不该做的就是猜）",
          r.kind == "unavailable" and c.calls == [], f"{r.kind}: {r} / {c.calls}")
finally:
    base._client = real_client


# ══════════════════════════════════════════════════════════════════
print("\n⑧ create_tag：先查后建、建后复核、绝不自作主张")

post = _Post("10002")
with patch(_tag_index=lambda c: IDX, _admin_post=post):
    r = base.create_tag.invoke({"title": "  "}, config=cfg())
    check("空名字 → unavailable，零网络", r.kind == "unavailable" and post.calls == [],
          str(r))
    r = base.create_tag.invoke({"title": "长" * 41}, config=cfg())
    check("名字超 40 字 → unavailable", r.kind == "unavailable" and post.calls == [],
          str(r))
    # 父标签走**名字**通道（20260921 第三轮）：名字对不上就零写拒绝，不猜、不新建。
    r = base.create_tag.invoke({"title": "X", "parent_tag": "没有这个标签"}, config=cfg())
    check("父标签名对不上 → unavailable，零 POST（名字通道下这是唯一的爸爸来源）",
          r.kind == "unavailable" and "没有叫" in r and post.calls == [], str(r))
    check("  拒因带上「本次未创建」（narator 照抄这句才知道该如实说没建成）",
          "本次未创建" in r, str(r))

    r = base.create_tag.invoke({"title": "Python"}, config=cfg())
    check("同名一级标签已存在 → 复用（幂等，**不写库**）",
          r.kind == "ok" and "已经存在" in r and "没有新建" in r, str(r))
    check("  复用路径不发 POST", post.calls == [], str(post.calls))
    check("  回执 meta 记 op=tag_reuse 与真实 id/层级（不是新分配一个）",
          r.meta.get("op") == "tag_reuse" and r.meta.get("tag_id") == 1
          and r.meta.get("level") == 1, str(r.meta))
    r = base.create_tag.invoke({"title": "Pytho"}, config=cfg())
    check("名字差一个字 ≠ 已存在（不做模糊匹配）", "已经存在" not in str(r))

post = _Post("10002")
seq = _Seq(IDX, A.build_tag_index(ONE + [{"tagKey": 10002, "title": "Pytho", "level": 1}], TWO))
with patch(_tag_index=seq, _admin_post=post):
    r = base.create_tag.invoke({"title": "Pytho"}, config=cfg())
    check("新建一级：按名字哈希取色、走 tagone 接口",
          post.calls == [("/api/protected/tagone",
                          {"title": "Pytho", "color": A.color_for_name("Pytho")})],
          str(post.calls))
    check("建后**读回复核**通过 → ok，meta 是库分配的 id（不相信返回值）",
          r.kind == "ok" and r.meta.get("op") == "tag_create"
          and r.meta.get("tag_id") == 10002 and r.meta.get("level") == 1,
          f"{r.kind}: {r} / {r.meta}")
    check("文本说清「只加了字典项、没挂到文章上」（否则 narrator 会讲成已打标签）",
          "没有挂到任何文章上" in r)

post = _Post("10002")
seq = _Seq(IDX, A.build_tag_index(ONE + [{"tagKey": 10002, "title": "Pytho", "level": 2,
                                          "fatherKey": 1}], TWO))
with patch(_tag_index=seq, _admin_post=post):
    r = base.create_tag.invoke({"title": "Pytho", "parent_tag": "Python"}, config=cfg())
    check("新建二级：planner 只给父**名字**，fatherTag 由工具解析成父 id（不是父名）",
          post.calls == [("/api/protected/tagtwo",
                          {"title": "Pytho", "color": A.color_for_name("Pytho"),
                           "fatherTag": 1})], str(post.calls))

post = _Post("10002")
with patch(_tag_index=_Seq(IDX, IDX), _admin_post=post):
    # 二级标签的名字不能当父（父必须是一级）：拒因要写清「一级标签」，
    # 否则 planner 会以为是名字写错了、去改一个本来就对的名字。
    r = base.create_tag.invoke({"title": "Pytho", "parent_tag": "爬虫"}, config=cfg())
    check("父名指向的是**二级**标签 → unavailable，零 POST，拒因点明「一级标签」",
          r.kind == "unavailable" and post.calls == [] and "一级标签" in r, str(r))

post = _Post("10002")
with patch(_tag_index=_Seq(IDX, IDX), _admin_post=post):
    r = base.create_tag.invoke({"title": "爬虫"}, config=cfg())
    check("同名**二级**已存在时建同名一级 → unavailable（复用只认同层，不把子标签"
          "当一级用；建同名一级会造出歧义）",
          r.kind == "unavailable" and post.calls == [] and "二级" in r, f"{r.kind}: {r}")

post = _Post("10002")
with patch(_tag_index=_Seq(IDX, IDX), _admin_post=post):
    r = base.create_tag.invoke({"title": "Pytho"}, config=cfg())
    check("建后复核找不到 id → unavailable（不把「发出去了」当「建好了」）",
          r.kind == "unavailable" and "复核失败" in r, f"{r.kind}: {r}")

post = _Post("10002")
seq = _Seq(IDX, A.build_tag_index(ONE + [{"tagKey": 10002, "title": "别的名字", "level": 1}],
                                  TWO))
with patch(_tag_index=seq, _admin_post=post):
    r = base.create_tag.invoke({"title": "Pytho"}, config=cfg())
    check("建后复核名字不一致 → unavailable（id 对了、内容不对也是没做成）",
          r.kind == "unavailable", f"{r.kind}: {r}")

with patch(_tag_index=lambda c: None, _admin_post=_Post("1")):
    r = base.create_tag.invoke({"title": "Pytho"}, config=cfg())
    check("读不到标签字典 → unavailable（不冒着重名风险建）",
          r.kind == "unavailable" and "读不到" in r, str(r))

with patch(_tag_index=_Seq(IDX, IDX),
           _admin_post=_Post(base.unavailable("后台接口报错: 标签名重复"))):
    r = base.create_tag.invoke({"title": "Pytho"}, config=cfg())
    check("POST 失败（ToolResult）→ 原样透传 unavailable（绝不包装成 ok）",
          r.kind == "unavailable" and "标签名重复" in r, f"{r.kind}: {r}")

# ── 参数对调守卫（20260921 生产事故）──────────────────────────────────
# planner 把「在 Python 标签下新建 X」填成 title=Python、父也填 Python（父名当成了新标签名）。
# 若放行，库里就会出现「Python」下挂一个也叫「Python」的二级标签——父子同名，
# 此后任何按名字找标签的操作都变歧义，而回复还会说"建好了"。名字通道下两边都是
# 名字，写串了就直接同名，所以这条守卫比 id 通道时更容易撞上、更该留。
post = _Post("10002")
with patch(_tag_index=_Seq(IDX, IDX), _admin_post=post):
    r = base.create_tag.invoke({"title": "Python", "parent_tag": "Python"}, config=cfg())
    check("父标签名 == 新标签名 → unavailable，零 POST（参数多半填反了）",
          r.kind == "unavailable" and post.calls == [], f"{r.kind}: {r}")
    check("  拒因说清是「把父标签名当成了新标签名」（planner 据此改参）",
          "父标签名" in r and "本次未创建" in r, str(r))
    # 对照组：**不同名**的二级标签照常放行，守卫只认"父子同名"这个自身矛盾
    r2 = base.create_tag.invoke({"title": "Pytho", "parent_tag": "Python"}, config=cfg())
    check("  · 对照组：父子不同名照常创建（守卫不误伤正常二级标签）",
          len(post.calls) == 1 and post.calls[0][0].endswith("/tagtwo"),
          str(post.calls))

# ── 弹窗问句要点名父标签（20260921）────────────────────────────────────
_q = A.render_confirm_question([{"tool": "create_tag",
                                 "args": {"title": "分布式", "parent_tag": "架构"}}], IDX)
check("弹窗问句写父标签**名字**（用户才能核对挂在哪）",
      "「架构」" in _q and "二级" in _q and "「分布式」" in _q, _q)
check("  问句里没有 id 这种系统内部编号（用户没法核对）", "id=" not in _q, _q)
_q2 = A.render_confirm_question([{"tool": "create_tag",
                                  "args": {"title": "分布式", "parent_tag": "架构"}}])
check("读不到标签字典 → 仍然弹窗（不因一次读不到就退回死路）",
      "「架构」" in _q2 and "「分布式」" in _q2, _q2)
_q2b = A.render_confirm_question([{"tool": "create_tag",
                                   "args": {"title": "分布式", "parent_tag": "没有这个"}}], IDX)
check("父标签名对不上 → 问句当场写出来（点确定之前用户就该看见爸爸不存在）",
      "没有叫「没有这个」的一级标签" in _q2b, _q2b)
_q3 = A.render_confirm_question([{"tool": "create_tag", "args": {"title": "新的一级"}}], IDX)
check("一级标签的问句不带父标签字样", "挂在" not in _q3, _q3)
_t = A.render_confirm_text([{"tool": "create_tag",
                             "args": {"title": "分布式", "parent_tag": "架构"}}], IDX)
check("气泡正文与问句同源（都点名父标签）", "「架构」" in _t, _t)
# 探针 ⑤ 的现场：主人说摘「摄影」，问句只写「修改文章 1 的标签」——他无从核对
# "要去掉的到底是不是我说的那个"。摘标签这一类的盲签风险比改状态更高（改状态的
# 目标值是枚举，标签名是自由文本）。
_t2 = A.render_confirm_question([{"tool": "set_article_tags",
                                  "args": {"article_id": 1, "remove": ["摄影"]}}])
check("摘标签的问句点名**哪个标签**（不是只说「修改文章 1 的标签」）",
      "摄影" in _t2 and "去掉" in _t2, _t2)
_t3 = A.render_confirm_question([{"tool": "set_article_tags",
                                  "args": {"article_id": 1, "add": ["音乐"],
                                           "remove": ["摄影"]}}])
check("加与摘分别写清（一次动两个标签时不糊成一句）",
      "加上 音乐" in _t3 and "去掉 摄影" in _t3, _t3)
_t4 = A.render_confirm_question([{"tool": "set_article_tags",
                                  "args": {"article_id": 1, "replace": []}}])
check("replace 空列表 = 清空，问句必须直说「清空」（最不可逆的一档）",
      "清空" in _t4, _t4)
check("字段缺失不炸（渲染层对畸形 spec 只退化不加戏）",
      "修改文章 None 的标签" in A.render_confirm_question(
          [{"tool": "set_article_tags", "args": {}}]))


print("\n⑨ set_article_status：只发点名的字段 + 写后复核")

post = _Post("1")
with patch(_read_note=lambda aid, c: note(12), _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "半公开"}, config=cfg())
    check("认不出的状态 → unavailable，零 POST",
          r.kind == "unavailable" and post.calls == [], f"{r.kind}: {r}")
    r = base.set_article_status.invoke({"article_id": "0", "status": "public"}, config=cfg())
    check("article_id 为 0（含字符串形态）→ unavailable",
          r.kind == "unavailable" and "不合法" in r, str(r))
    r = base.set_article_status.invoke({"article_id": 12}, config=cfg())
    check("什么都没点名 → unavailable（不猜要改什么）",
          r.kind == "unavailable" and "没有指出要改什么" in r, str(r))
    r = base.set_article_status.invoke({"article_id": 12, "is_top": 2}, config=cfg())
    check("置顶值越界（2）→ unavailable（不把 2 当成真）",
          r.kind == "unavailable" and "认不出置顶值" in r, str(r))
    r = base.set_article_status.invoke({"article_id": 12, "status": ""}, config=cfg())
    check("空串状态 = 没点名这个字段 → 与其他「没点名」同路径（unavailable）",
          r.kind == "unavailable", str(r))
    check("以上全都没发 POST", post.calls == [], str(post.calls))

post = _Post("1")
with patch(_read_note=lambda aid, c: None, _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "public"}, config=cfg())
    check("文章不在后台列表里（如修改稿影子行）→ unavailable，零 POST",
          r.kind == "unavailable" and post.calls == [] and "修改稿" in r, str(r))

post = _Post("1")
with patch(_read_note=lambda aid, c: note(12), _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "private"}, config=cfg())
    check("已经就是目标值 → ok 但**不发请求**（空改动会无条件刷新 updated_at）",
          r.kind == "ok" and post.calls == [] and "无需改动" in r, f"{r.kind}: {r}")
    check("  这类回执带 noop 标记（可观测：它没写库）", r.meta.get("noop") is True)

post = _Post("1")
seq = _Seq(note(12, "架构", "private", 0), note(12, "架构", "public", 0))
with patch(_read_note=seq, _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "public"}, config=cfg())
    # 载荷里**有没有 title/content** 不只是"少触发一个分支"：Rust 侧 `update_note` 用它
    # 算 `from_editor`，而 `from_editor` 同时判**草稿重定向 / 级联删修改稿 / 署名回填**
    # （`should_backfill_author(user_id IS NULL, from_editor)`）。20261007 那起"文章 23
    # 被记成 uid 721"就是元数据写撞上 `user_id` 为 NULL 的老文章：署名回填当时**不看**
    # `from_editor`，写标签的人被记成了作者，而"只在为空时补写"让它再也改不回来。
    # 所以下面这条断言现在同时是**跨仓署名契约**的守卫（Rust 侧已同步收紧判据）。
    check("正常路径：**只发 status**（不发 title/content/isPublic/updateTime）",
          post.calls == [("/api/protected/notes/12", {"status": "public"})], str(post.calls))
    check("  回执 meta：op / article_id / 前后值（白名单那一半逐键相等）",
          {k: r.meta.get(k) for k in ("op", "article_id", "before", "after")}
          == {"op": "set_status", "article_id": 12, "before": "私密", "after": "公开"},
          str(r.meta))
    check("  人话里带《标题》与前后值（narrator 照它转述）",
          "《架构》" in r and "私密 → 公开" in r and "复核" in r, str(r))
    # F1（20260930）之后标题住在事实信封的 `target.name` 里（信封的语义是「动的是谁」）。
    # **回执侧一个字都没变**：`target` 不在 `_RCPT_META_KEYS` 里（`execute_node` 那个拷贝
    # 循环按白名单取值）⇒ 拷不进 detail、不会随下一轮 `recent_executions` 注回提示词。
    # "带标题 = 我读过这篇"那条来源态纪律的落点就在这条白名单边界上，不是"meta 里没有这串字"。
    check("  **标题不进回执**：顶层没有 title 键，标题只在 target.name 里而 target 不在白名单",
          "title" not in r.meta and "target" not in g._RCPT_META_KEYS
          and (r.meta.get("target") or {}).get("name") == "架构", str(r.meta))

post = _Post("1")
seq = _Seq(note(12, "架构", "private", 0), note(12, "架构", "private", 1))
with patch(_read_note=seq, _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "is_top": 1}, config=cfg())
    check("只点置顶 → 载荷里只有 isTop", post.calls[0][1] == {"isTop": 1}, str(post.calls))
    check("  变更串只讲置顶（没发生的事不进跨轮执行记忆）",
          r.meta["before"] == "未置顶" and r.meta["after"] == "置顶", str(r.meta))

post = _Post("1")
seq = _Seq(note(12, "架构", "private", 0), note(12, "架构", "private", 0))
with patch(_read_note=seq, _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "public"}, config=cfg())
    check("写请求发出但读回仍是原值 → unavailable（「改好了」必须有读回证据）",
          r.kind == "unavailable" and "仍是原值" in r, f"{r.kind}: {r}")

post = _Post("1")
with patch(_read_note=_Seq(note(12, "架构", "private", 0), None), _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "public"}, config=cfg())
    check("写后读不到该行 → unavailable（未确认生效）",
          r.kind == "unavailable" and "未确认生效" in r, str(r))

post = _Post(base.unavailable("后台接口报错: 权限不足"))
with patch(_read_note=lambda aid, c: note(12), _admin_post=post):
    r = base.set_article_status.invoke({"article_id": 12, "status": "public"}, config=cfg())
    check("POST 失败 → 原样透传 unavailable", r.kind == "unavailable", str(r))


print("\n⑩ set_article_tags：只动点名的标签，绝不顺手清空")


def tags_write(payload_args, before="1,10000", after=None, index=IDX):
    """跑一次 set_article_tags：写前读到 before、写后读到 after（默认 = 预期新值）。"""
    post = _Post("1")
    seq = _Seq(note(12, tags=before), note(12, tags=after if after is not None else ""))
    with patch(_tag_index=lambda c: index, _read_note=seq, _admin_post=post):
        return base.set_article_tags.invoke(payload_args, config=cfg()), post


post = _Post("1")
with patch(_tag_index=lambda c: IDX, _read_note=lambda aid, c: note(12, tags="1,10000"),
           _admin_post=post):
    r = base.set_article_tags.invoke({"article_id": 12, "replace": [], "add": ["架构"]},
                                     config=cfg())
    check("replace 与 add/remove 互斥 → unavailable，零 POST",
          r.kind == "unavailable" and post.calls == [] and "同时使用" in r, str(r))
    r = base.set_article_tags.invoke({"article_id": 12}, config=cfg())
    check("什么都没点名 → unavailable（不猜要加什么）", r.kind == "unavailable", str(r))
    r = base.set_article_tags.invoke({"article_id": 12, "add": ["不存在"]}, config=cfg())
    check("站内没有该标签名 → unavailable（**不自动新建**），零 POST",
          r.kind == "unavailable" and post.calls == [] and "不会自动新建" in r, str(r))
    r = base.set_article_tags.invoke({"article_id": 12, "remove": ["不存在"]}, config=cfg())
    check("要摘的标签名不存在 → unavailable（连「去掉哪个」都不确定）",
          r.kind == "unavailable" and post.calls == [], str(r))
    check("以上全都没发 POST", post.calls == [], str(post.calls))

r, post = tags_write({"article_id": 12, "add": ["架构"]}, after="1,10000,2")
check("加标签：未点名的原样保留、新 id 追加在后",
      post.calls == [("/api/protected/notes/12", {"noteTags": "1,10000,2"})],
      str(post.calls))
check("  回执 meta 前后都是可读标签名",
      r.kind == "ok" and r.meta["before"] == "Python、Python / 爬虫"
      and r.meta["after"] == "Python、Python / 爬虫、架构", f"{r.kind} {r.meta}")

r, post = tags_write({"article_id": 12, "remove": ["爬虫"]}, after="1")
check("摘标签：只去掉点名的那个",
      post.calls == [("/api/protected/notes/12", {"noteTags": "1"})], str(post.calls))

r, post = tags_write({"article_id": 12, "replace": []}, after="")
check("**只有** replace=[] 才会产出空串（= 清空，前端也这么读）",
      post.calls == [("/api/protected/notes/12", {"noteTags": ""})], str(post.calls))

r, post = tags_write({"article_id": 12, "replace": ["架构"]}, after="2")
check("replace 是整体替换（没点名的会掉）",
      post.calls == [("/api/protected/notes/12", {"noteTags": "2"})], str(post.calls))

r, post = tags_write({"article_id": 12, "add": ["Python"]})
check("加一个已经挂着的标签 → ok 但零 POST（幂等，不空刷 updated_at）",
      r.kind == "ok" and post.calls == [] and r.meta.get("noop") is True,
      f"{r.kind}: {r} / {post.calls}")

r, post = tags_write({"article_id": 12, "add": ["10001"]}, after="1,10000,10001")
check("数字字符串按 **id** 解析（planner 从回执里抄到的 id，不必凑名字）",
      post.calls[0][1] == {"noteTags": "1,10000,10001"} and "分布式" in r.meta["after"],
      f"{post.calls} / {r.meta}")

r, post = tags_write({"article_id": 12, "add": ["架构"]}, after="1")
check("写后读回与预期不一致 → unavailable（不把「发出去了」当「改好了」）",
      r.kind == "unavailable" and "不一致" in r, f"{r.kind}: {r}")

post = _Post("1")
with patch(_tag_index=lambda c: IDX, _read_note=lambda aid, c: None, _admin_post=post):
    r = base.set_article_tags.invoke({"article_id": 12, "add": ["架构"]}, config=cfg())
    check("文章不在后台列表（修改稿影子行）→ unavailable，零 POST",
          r.kind == "unavailable" and post.calls == [], str(r))

post = _Post("1")
with patch(_tag_index=lambda c: None, _read_note=lambda aid, c: note(12), _admin_post=post):
    r = base.set_article_tags.invoke({"article_id": 12, "add": ["架构"]}, config=cfg())
    check("读不到标签字典 → unavailable（名字对不到 id 就不动手）",
          r.kind == "unavailable" and post.calls == [], str(r))


# ══════════════════════════════════════════════════════════════════
print("\n⑪ checker 三态：没做成不许变成事实")

ok_text = "已修改文章 12《架构》：私密 → 公开（后台已复核读到新值）"
check("ok(非空) → PASS 进回执",
      _check_spec("set_article_status", {"article_id": 12}, True, ok_text, "article_status")
      == ("PASS", "ok"))
check("empty('') → BLOCK empty_result（零写成功的返回也不能是空串）",
      _check_spec("create_tag", {}, True, "", "tag_create", "empty")
      == ("BLOCK", "empty_result"))
check("empty(非空文本) → PASS（「标签已存在，复用 id=13」是真结果）",
      _check_spec("create_tag", {}, True, "标签「X」已经存在（id=13，一级），没有新建。",
                  "tag_create", "empty")[0] == "PASS")
check("unavailable → BLOCK（服务不可用不是事实，不进跨轮执行记忆）",
      _check_spec("create_tag", {}, True, "后台接口请求失败: x", "tag_create", "unavailable")
      == ("BLOCK", "unavailable"))
check("unknown_target 帧 → BLOCK 且原因是 unknown_target（planner 据此先读再写）",
      _check_spec("set_article_status", {"article_id": 12}, True,
                  A.unknown_target_frame("set_article_status"), "article_status")
      == ("BLOCK", "unknown_target"))
check("权限拒绝帧 → BLOCK 且原因码是 denied（如实告知，不是换工具再试）",
      _check_spec("set_article_status", {}, True,
                  authz.denial_frame(authz.check(Principal(1, ROLE_USER),
                                                 "set_article_status"),
                                     Principal(1, ROLE_USER)), "article_status")
      == ("BLOCK", "denied"))
check("未确认帧 → BLOCK 且原因是 consent_required（planner 的应对是**去问**）",
      _check_spec("set_article_status", {}, True,
                  authz.consent_frame("set_article_status", Principal(1, ROLE_ADMIN)),
                  "article_status") == ("BLOCK", "consent_required"))


print("\n⑫ execute 三道门：权限 / 确认 / 目标（零调用 + 带原因码的错误帧）")

CALLS: list = []


class _FakeTool:
    """假写工具：记录参数、返回一个带 meta 的回执形态结果（绝不碰网络）。"""

    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        CALLS.append(args)
        return self.out


def _plan(tools_list, skill="article_status"):
    """改后返回**对象**（20260928 批 C：夹具与生产同路，经 `plan_state` 一次写两态）。"""
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = tools_list
    return obj


def _run(tools_list, msg, config, extra_msgs=(), skill="article_status"):
    CALLS.clear()
    return execute_node({**plan_state(_plan(tools_list, skill)),
                         "plan_rounds": 1, "done": False,
                         "messages": [HumanMessage(content=msg), *extra_msgs]},
                        config)


SPEC_STATUS = 'set_article_status({"article_id": 12, "status": "private"})'
_saved_tool = g._TOOL_MAP.get("set_article_status")
try:
    g._TOOL_MAP["set_article_status"] = _FakeTool(
        base.ok("已修改文章 12：私密 → 公开（后台已复核读到新值）",
                meta={"op": "set_status", "article_id": 12,
                      "before": "私密", "after": "公开"}))

    # ① 身份门：非 admin（authz_enforce=False 的 shadow 下也必须硬拦）
    for role, why in ((ROLE_USER, "普通访客"), (ROLE_SECRETARY, "秘书（后台写不给）"),
                      (None, "身份不明")):
        r = _run([SPEC_STATUS], "把文章 12 设为私密", cfg(9, role))
        frm = str(r["messages"][-1].content)
        check(f"非管理员（{why}）→ 拒绝且零调用（shadow 开关不吃硬拦 scope）",
              CALLS == [] and r["receipts"] == []
              and r["blocked"][0]["reason"] in ("denied", "unknown_role")
              and frm.startswith("__ERROR__"), f"{frm[:70]}")

    # ② 确认门：有权，但本轮没下命令（提问 / 假设 / 陈述）
    for msg in ("把文章 12 设为私密会有什么影响？", "如果我把文章 12 设为私密的话",
                "文章 12 现在是私密吗", "文章 12 设为私密的步骤是什么"):
        r = _run([SPEC_STATUS], msg, cfg())
        check(f"提问/假设/陈述（{msg[:14]}…）→ consent_required 且零调用",
              CALLS == [] and r["receipts"] == []
              and r["blocked"][0]["reason"] == "consent_required", str(r["blocked"]))
    r = _run([SPEC_STATUS], "把文章 12 设为私密", cfg())
    check("同轮命令即确认（不再要第二次确认）→ 执行",
          CALLS == [{"article_id": 12, "status": "private"}], str(CALLS))

    # ③ 目标门：整轮什么都没读过就写一个凭记忆的 id
    r = _run([SPEC_STATUS], "把《架构文档》设为私密", cfg())
    frm = str(r["messages"][-1].content)
    check("目标本轮没读到过 → unknown_target 且零调用",
          CALLS == [] and r["receipts"] == []
          and r["blocked"][0]["reason"] == "unknown_target"
          and frm.startswith("__ERROR__") and "[unknown_target]" in frm, frm[:90])

    # 门序：确认先于目标（先问「要不要做」，再问「哪一篇」）
    r = _run([SPEC_STATUS], "把《架构文档》设为私密可以吗", cfg())
    check("两道门都没过 → 报 consent_required（顺序锁）",
          CALLS == [] and r["blocked"][0]["reason"] == "consent_required",
          str(r["blocked"]))

    # 目标有据：本轮读过的**读类工具帧**里出现过这个 id
    frame = ToolMessage(content="后台文章共 3 篇：\n- id=12 [私密]《架构文档》标签：",
                        tool_call_id="t1", name="list_admin_notes")
    r = _run([SPEC_STATUS], "把《架构文档》设为私密", cfg(), extra_msgs=(frame,))
    check("目标出自本轮读过的读类工具帧 → 放行执行",
          CALLS == [{"article_id": 12, "status": "private"}], str(CALLS))
    rcpt = r["receipts"][0]
    check("  回执带执行角色与结构化的变更前后（跨语言契约）",
          rcpt["principal_role"] == "admin" and rcpt["op"] == "set_status"
          and rcpt["before"] == "私密" and rcpt["after"] == "公开", str(rcpt))
    # `action` 是 20260928 新加的那一个（跨轮执行记忆那一行的**定稿措辞**，Python
    # 写 / Rust 只排版）。加它的正当性：此前那一行由 Rust 独立渲染，同一件事两处
    # 措辞已经漂了（54 条取样只有 31 条逐字相同）——**它是收敛，不是新债**。
    # 除此之外键集仍然固定：这一条的意义正是"想加键就得来改这份清单"。
    check("  回执键集固定（多出来的键是无声的兼容性债）",
          set(rcpt) == {"skill", "tool", "args", "result", "ts", "action",
                        "principal_role", "op", "article_id", "before", "after"},
          str(sorted(rcpt)))
    check("  这一行台账措辞确实定了稿（不是留空等 Rust 兜）",
          rcpt["action"] == "修改文章 12：私密 → 公开", rcpt.get("action"))
    check("  回执里没有 uid（detail 进生产库、还可能被 narrator 念出来）",
          not any("uid" in k for k in rcpt) and "uid" not in json.dumps(rcpt), str(rcpt))
    check("  回执不带《标题》（下一轮会把它读成「我读过这篇」的指代证据）",
          "架构" not in json.dumps(rcpt, ensure_ascii=False), str(rcpt))

    # 写工具自己的回显**不**算证据（否则「刚写过 id=12」自我豁免整个校验）
    echo = ToolMessage(content="已修改文章 12：私密 → 公开", tool_call_id="t2",
                       name="set_article_status")
    r = _run([SPEC_STATUS], "把《架构文档》设为私密", cfg(), extra_msgs=(echo,))
    check("写工具自己的回显不算目标证据 → 仍判 unknown_target",
          CALLS == [] and r["blocked"][0]["reason"] == "unknown_target", str(r["blocked"]))

    # 参数非法：门都过了也不发请求（由工具的 uid≤0 守卫兜住，见 §7）
    r = _run(['set_article_status({"article_id": 0, "status": "private"})'],
             "把文章 0 设为私密", cfg())
    check("article_id=0 → 有据也判无据（非法 id 不猜）→ unknown_target",
          CALLS == [] and r["blocked"][0]["reason"] == "unknown_target",
          str(r["blocked"]))
except BaseException as e:  # noqa: BLE001
    check(f"execute 写路径测试异常：{type(e).__name__}: {e}", False)
finally:
    if _saved_tool is None:
        g._TOOL_MAP.pop("set_article_status", None)
    else:
        g._TOOL_MAP["set_article_status"] = _saved_tool


print("\n⑬ 写去重判据是 args-aware（同名不同参不许被收尾吞掉）")


def _rcpt(tool, **args):
    return {"tool": tool, "args": {k: str(v) for k, v in args.items()}}


SOBJ = {"skill": "article_status",
        "tools": ['set_article_status({"article_id": 12, "status": "private"})']}

check("同一件事重复规划 → 收尾",
      _already_done_writes(SOBJ, [_rcpt("set_article_status", article_id=12,
                                        status="private")]))
check("**同一工具、另一篇** → 不收尾（第二件事必须真执行）",
      not _already_done_writes(SOBJ, [_rcpt("set_article_status", article_id=14,
                                            status="private")]))
check("同一工具、同一篇、另一个值 → 不收尾（来回切换是两次操作）",
      not _already_done_writes(
          {"skill": "article_status",
           "tools": ['set_article_status({"article_id": 12, "is_top": 0})']},
          [_rcpt("set_article_status", article_id=12, is_top=1)]))
check("参数键序不同不影响判据（指纹 sort_keys）",
      _already_done_writes(SOBJ, [_rcpt("set_article_status", status="private",
                                        article_id=12)]))
check("int 与 str 等价（回执侧一律字符串化，计划侧必须走同一归一化——"
      "否则判据静默失效成「从不收尾」）",
      _already_done_writes(SOBJ, [{"tool": "set_article_status",
                                   "args": {"article_id": "12", "status": "private"}}])
      and _already_done_writes(
          {"skill": "article_status",
           "tools": ['set_article_status({"article_id": "12", "status": "private"})']},
          [_rcpt("set_article_status", article_id=12, status="private")]))
check("清单里有一件没做过 → 不收尾（不能只做了一半就说都好了）",
      not _already_done_writes(
          {"skill": "article_status",
           "tools": ['set_article_status({"article_id": 12, "status": "private"})',
                     'set_article_status({"article_id": 14, "status": "private"})']},
          [_rcpt("set_article_status", article_id=12, status="private")]))
check("失败/未确认的写不进回执 ⇒ 改参重试仍放行（判据取 receipts 而非帧名）",
      not _already_done_writes(SOBJ, []))
check("空计划不归这条判据管（收尾与否由别处决定）",
      not _already_done_writes({"skill": "article_status", "tools": []},
                               [_rcpt("set_article_status", article_id=12,
                                      status="private")]))
check("只读技能不吃这条判据（报表去重有自己那条，判据更宽）",
      not _already_done_writes({"skill": "content_query", "tools": SOBJ["tools"]},
                               [_rcpt("set_article_status", article_id=12,
                                      status="private")]))
check("三个写技能都在名单里，且与快照型报表名单不重叠",
      EXECUTED_ONCE_SKILLS == frozenset({"tag_create", "article_status", "article_tags"})
      and not (EXECUTED_ONCE_SKILLS & SNAPSHOT_SKILLS),
      f"{sorted(EXECUTED_ONCE_SKILLS)} / {sorted(SNAPSHOT_SKILLS)}")

gsrc = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("写工具**不进** _CONTENT_TOOLS（否则「建了个标签」会变成「我检索过」的证据）",
      not ({"set_article_status", "set_article_tags", "create_tag"} & _CONTENT_TOOLS)
      and "list_admin_notes" in _CONTENT_TOOLS, str(sorted(_CONTENT_TOOLS)))
# 判据点从 2 → 3（20260921）：新增的第三处在 _confirm_popup——**无权做的写操作
# 不弹窗**（弹了就是承诺一件做不到的事）。同一 decision、同一 enforcing，只减少
# 弹窗、不放宽任何权限。数字仍是硬断言：下一个人加第四处时先回答"会不会放宽"。
check("authz.enforcing(decision.scope) 三处（shadow/硬拦/弹窗前置筛），无新增放宽点",
      gsrc.count("authz.enforcing(decision.scope)") == 3,
      str(gsrc.count("authz.enforcing(decision.scope)")))


print("\n⑭ 参数类型非法：产错误帧如实退回 planner，绝不炸图")

CALLS.clear()
_typed = execute_node({**plan_state(_plan(['set_article_status({"article_id": 12, "is_top": "也许"})'])),
                       "plan_rounds": 1, "done": False,
                       "messages": [HumanMessage(content="把文章 12 置顶")]},
                      cfg())
_ftxt = str(_typed["messages"][-1].content)
check("字符串塞进 int 参数（planner 抄错类型）→ __ERROR__ 帧而不是抛异常",
      _ftxt.startswith("__ERROR__") and _typed["receipts"] == [], _ftxt[:80])
check("  该帧不进回执、进 blocked（planner 按规则 5 改参重试）",
      _typed["blocked"] and _typed["blocked"][0]["reason"] == "error_frame",
      str(_typed["blocked"]))

print("\n⑮ 写目标以「用户本轮点名」为权威（误靶写：点名 1 却取了清单首行 46）")

# 活体探针实证（20260921）：管理员说「把文章 1 置顶」，planner 读了一遍后台列表，
# 把**第一行**（id=46）填进了 article_id。旧判据（本轮读到过 46 ⇒ 有据）放行了它。
# 下面这组是**纯函数 + execute 两级**的回归锁：函数级锁"认出点名"，集成级锁"不一致
# 就零调用"，并且三条互补情形（空集不启用 / 命中即放行 / 计数不算点名）都在。

for msg, want, why in (
        ("把文章 1 的「摄影」标签去掉", {1}, "文章 N"),
        ("帮我把文章#46 置顶", {46}, "文章#N"),
        ("第 3 篇设为私密", {3}, "第 N 篇"),
        ("id=7 那篇隐藏一下", {7}, "id=N"),
        ("把文章 12 和文章 14 都设为私密", {12, 14}, "多点名 → 命中任一即算对上"),
        ("把文章 12 和 14 都设为私密", {12, 14}, "枚举延伸（省了第二个「文章」也算）"),
        ("文章 12、13 置顶", {12, 13}, "顿号枚举"),
        ("文章 12 和 14 还有 15 都要", {12, 14, 15}, "三级枚举"),
        # 20261008：枚举延伸对三种标记一视同仁（此前只接在「文章」后面，序数与 id N
        # 都只认到第一个数）——现场 trace 20261008T064432：主人一句「把 id 13、11、10
        # 设为私密」，13 真改了、11 被判 target_mismatch 拒执行、收尾还否认改过任何一篇。
        ("把 id 13、11、10 设为私密", {13, 11, 10}, "id N 枚举（现场那条）"),
        ("id 13 和 11 都设为私密", {13, 11}, "id N + 连接词枚举"),
        ("第 13 篇、第 11 篇都设成私密", {13, 11}, "序数本身各有标记（第二条独立命中）"),
        ("把第 13 篇和第 11 篇都设成私密", {13, 11}, "两条都带「第」的枚举"),
        ("第 13 篇和 11 篇都设成私密", {13}, "尾项省了「第」⇒ 被量词守卫当计数（保守那一边）"),
        ("把 id 13 和 3 个标签都去掉", {13}, "id 延伸里的量词项仍不算 id"),
        ("文章 12 和 3 个要点", {12}, "枚举里的量词项仍不算 id（3 个要点 ≠ 文章 3）"),
        ("把文章 12 的标签改成 1 和 2", {12}, "连接词不紧跟在 id 后面 → 不延伸"),
        ("站内一共 12 篇文章", set(), "计数形态（数字在名词前）不是点名"),
        ("这篇文章 3 个要点总结一下", set(), "「文章 3 个」后面跟量词 = 计数，不是目标"),
        ("文章 12 篇我都看过了", set(), "「文章 N 篇」显式排除"),
        ("把这篇置顶", set(), "纯指代（没有数字）= 没点名 → 判据不启用"),
):
    got = A.user_named_article_ids(msg)
    check(f"点名识别：{why}（{msg}）→ {sorted(want) or '空集'}",
          got == want, f"{sorted(got)}")

check("空集 = 判据不启用（恒放行）——写操作不能因为「用户没说数字」被全禁",
      A.target_named(999, A.user_named_article_ids("把这篇置顶")) is True
      and A.target_named(1, set()) is True)
check("点名了就不许写成别的 id",
      A.target_named(1, {1}) and not A.target_named(46, {1}))
check("article_id 缺失/非法 → 不放行（fail-closed，有非空点名时）",
      not A.target_named(None, {1}) and not A.target_named("四十六", {1}))
check("原因码取回：两条目标族帧分得开，非本族帧 → None",
      A.target_error_reason(A.target_conflict_frame("set_article_status", {1}, 46))
      == A.REASON_TARGET_MISMATCH
      and A.target_error_reason(A.unknown_target_frame("set_article_status"))
      == A.REASON_UNKNOWN_TARGET
      and A.target_error_reason("__ERROR__: 参数引用无法解析[ref_unresolved]") is None)
_CF = A.target_conflict_frame("set_article_status", {1}, 46)
check("不一致帧把「以主人点名的为准」讲清楚（planner 照它改参，系统不改写参数）",
      "文章 1" in _CF and "46" in _CF and _CF.startswith("__ERROR__"))

# 集成：本轮确实读到过 46（后台列表帧在场，旧判据会放行），但用户点名 1
# （假工具只活在本段——§⑫ 的补丁已还原，这里再挂一次，用完必还原）
LIST_FRAME = ToolMessage(content="后台文章列表：\n1. 私密 46《架构文档》\n2. 公开 12《随笔》",
                         tool_call_id="t0", name="list_admin_notes")
SPEC_46 = 'set_article_status({"article_id": 46, "status": "private"})'
_CALLS2: list = []
_saved2 = g._TOOL_MAP.get("set_article_status")


class _FakeTool2(_FakeTool):
    def invoke(self, args):
        _CALLS2.append(args)
        return self.out


try:
    g._TOOL_MAP["set_article_status"] = _FakeTool2(
        base.ok("已修改文章 46：公开 → 私密（后台已复核读到新值）",
                meta={"op": "set_status", "article_id": 46,
                      "before": "公开", "after": "私密"}))
    r = _run([SPEC_46], "把文章 1 设为私密", cfg(), extra_msgs=[LIST_FRAME])
    _frm = str(r["messages"][-1].content)
    check("点名 1、planner 填 46（46 在本轮帧里）→ 零调用 + target_mismatch",
          _CALLS2 == [] and r["receipts"] == []
          and r["blocked"][0]["reason"] == "target_mismatch"
          and _frm.startswith("__ERROR__"), f"{_frm[:80]}")

    r = _run([SPEC_46], "把文章 46 设为私密", cfg(), extra_msgs=[LIST_FRAME])
    check("点名与填充一致 → 照常执行（判据不是「禁止写」）",
          _CALLS2 == [{"article_id": 46, "status": "private"}], str(_CALLS2))

    _CALLS2.clear()
    r = _run([SPEC_46], "把这篇设为私密", cfg(), extra_msgs=[LIST_FRAME])
    check("没点名（纯指代）→ 判据不启用，行为与改动前完全一致",
          _CALLS2 == [{"article_id": 46, "status": "private"}], str(_CALLS2))
finally:
    if _saved2 is None:
        g._TOOL_MAP.pop("set_article_status", None)
    else:
        g._TOOL_MAP["set_article_status"] = _saved2

print("\n⑰ ②防线：身份必须落在主人这句话里（免弹窗的第三个前提）")

# 判据本身（纯函数）
check("名字原样在主人话里 → 地基成立",
      g._ident_grounded("delete_tag", {"name": "Asyncio"},
                        "把标签 Asyncio 删掉") is True)
check("主人说的是**别的说法**（异步）而参数是 Asyncio → 地基不成立（要人类确认一次）",
      g._ident_grounded("delete_tag", {"name": "Asyncio"},
                        "把那个异步标签删掉吧") is False)
check("空白/换行差异不算不成立（与片段匹配同一口径）",
      g._ident_grounded("delete_tag", {"name": "Rust 异步"},
                        "把标签 Rust\n异步 删掉") is True)
check("父标签名同样要落地基（建标签时挂错爸爸是最贵的一种错）",
      g._ident_grounded("create_tag", {"title": "X", "parent_tag": "编程"},
                        "在「编程」下面建个标签 X") is True
      and g._ident_grounded("create_tag", {"title": "X", "parent_tag": "编程"},
                            "在「异步」下面建个标签 X") is False)
check("留言族同样落地基（quote 就是这个工具的身份）",
      g._ident_grounded("delete_board_comment", {"quote": "泠月喵好笨啊"},
                        "把那条写着「泠月喵好笨啊」的留言删掉吧") is True)
check("不在名字表里的写工具不受这一条约束（文章族走 target_* 三条判据）",
      g._ident_grounded("set_article_status", {"article_id": 12},
                        "把这篇设为私密") is True)


def _popup(spec, msg, uid=7, role=ROLE_ADMIN):
    """跑一次 `_confirm_popup`（真判据 + 真签发，假的是标签字典与后端）。

    计划夹具走 `plan_state`（20260928 批 C）：`_plan_skill` 现在读 `state["plan_obj"]`，
    只喂契约文本的话技能名读成空串 ⇒ `confirm.sign` 拒签 ⇒ 该弹的全变 None
    （看起来"符合预期"，其实是密钥/技能名两件事长得一样）。
    """
    return g._confirm_popup(
        {**g.plan_state({"skill": "tag_delete", "params": {}, "tools": [],
                         "note": "", "reply": "直接回答"})},
        [spec], Principal(uid=uid, role=role), msg,
        {"configurable": {"user_id": uid, "conversation_id": 42}})


with patch(_tag_index=lambda config: A.build_tag_index(
        [{"tagKey": 2, "title": "编程", "level": 1}],
        [{"tagKey": 10000, "title": "Asyncio", "level": 2,
          "fatherTag": "编程", "fatherKey": 2}])):
    SPEC_DEL = 'delete_tag({"name": "Asyncio"})'
    r2 = _popup(SPEC_DEL, "把那个异步标签删掉吧")
    check("命令式但名字不在主人话里（别名/模型自己拣的名字）→ 退回弹窗",
          isinstance(r2, dict) and "pending_confirm" in r2, str(r2)[:80])
    _q = (r2 or {}).get("pending_confirm", {}).get("q", "")
    check("  问句把系统解析到的目标写清楚（主人点的是「Asyncio」这个名字）",
          "Asyncio" in _q, _q[:90])

    r3 = _popup(SPEC_DEL, "把标签 Asyncio 删掉的话，文章上会有什么变化？")
    check("疑问句照旧不弹窗（提问不是下令——这条判据在同意闸里早就有）",
          r3 is None, str(r3)[:60])

    r4 = _popup(SPEC_DEL, "把标签 Asyncio 挪到别的爸爸下面吧")
    check("有意向、名字也在话里，但措辞不是命令 → 照旧弹窗（原行为）",
          isinstance(r4, dict), str(r4)[:60])

    # 快道**真能命中**哪些话：实测过 `authz.consent_granted` 后如实分两栏（这道防线的
    # 咬合面取决于同意闸的动作词表长什么样，不是"所有名字通道写都走弹窗"）。
    # 词表里有 创建/新建/设成/改名… 那一族，**没有** 「删掉标签/移除标签」这一族。
    _p = Principal(uid=7, role=ROLE_ADMIN)
    _reachable = [("update_tag", "把标签 Asyncio 改名叫协程"),
                  ("create_tag", "确认创建标签 Asyncio")]
    _unreachable = [("delete_tag", "把标签 Asyncio 删掉"),
                    ("delete_tag", "删了 Asyncio 那个标签"),
                    ("delete_board_comment", "把那条写着「泠月喵好笨啊」的留言删掉")]
    check("快道**够得着**的名字通道写（动作词表里有「改名/创建」）⇒ 这道防线真会咬人",
          all(authz.consent_granted(_p, _t, _m) is True for _t, _m in _reachable),
          str([(_t, authz.consent_granted(_p, _t, _m)) for _t, _m in _reachable]))
    check("而「删掉标签/删留言」这一族判不成命令 ⇒ 它们一律走弹窗（防线在这里是兜底）",
          all(authz.consent_granted(_p, _t, _m) is False for _t, _m in _unreachable),
          str([(_t, authz.consent_granted(_p, _t, _m)) for _t, _m in _unreachable]))

    SPEC_NEW = 'create_tag({"title": "Asyncio", "parent_tag": "编程"})'
    _MSG_NEW = "确认创建标签 Asyncio，挂在编程下面"
    check("  对照：同一套闸对「确认创建标签 X」是放行的（所以下面两条测得到）",
          authz.consent_granted(Principal(uid=7, role=ROLE_ADMIN), "create_tag", _MSG_NEW) is True)

    r5 = _popup(SPEC_NEW, _MSG_NEW)
    check("快道可达的话 + 身份落地基 → 不弹窗（不是把写一律变成问）",
          r5 is None, str(r5)[:80])

    # ⚠️ 20260926：这一条的 title 从 Asyncio 换成**站里没有的**「协程」——fixture 的
    # 标签树里 `编程/Asyncio` **本来就在**（id=10000），而"状态已达成 ⇒ 不弹卡"上线后
    # 那种 spec 走的是另一条出口（见紧跟的 r6b），问句这一支就测不到了。这里要测的是
    # **卡面**（把 planner 填的那个爸爸印出来、主人当场能对出对不上），得用一件真待办的事。
    r6 = _popup('create_tag({"title": "协程", "parent_tag": "编程"})',
                "确认创建标签 协程，挂在异步下面")
    check("同一条命令，planner 把父标签填成主人**没说过**的名字 → 退回弹窗",
          isinstance(r6, dict) and "pending_confirm" in (r6 or {}), str(r6)[:80])
    _q6 = (r6 or {}).get("pending_confirm", {}).get("q", "")
    check("  问句把**它要挂的那个爸爸**写出来（主人说的是「异步」，问句问的是「编程」——"
          "对不上就能当场取消，而不是被静默挂错）",
          "编程" in _q6 and "协程" in _q6, _q6[:90])

    # 同一句话、planner 照样填错爸爸，但那个爸爸底下**同名标签本来就在站里** ⇒ 这次写
    # 本来就无事可做（工具按重名复用短路、一个字节都不写）⇒ 20260926 起不弹卡，回复直接
    # 把**完整路径**写出来。这是"被静默挂错"的另一种安全落地：路径摆在眼前，零改动。
    r6b = _popup('create_tag({"title": "Asyncio", "parent_tag": "编程"})',
                 "确认创建标签 Asyncio，挂在异步下面")
    check("  而父标签下同名标签已在站里 → **不弹卡、零写**，回复写明完整路径",
          isinstance(r6b, dict) and not r6b.get("pending_confirm")
          and "编程" in str(r6b.get("noop_text") or ""), str(r6b)[:120])

    # 密钥空缺这一支：**fail-closed 但必须留痕**。它一旦生效，所有写确认弹窗静默消失
    # （退回"判不成命令就追问"的死路形态），链路上没有别的信号——20260922 CI 实测就是
    # 这一支在无 .env 的环境里吃掉三条正例、而反例照样绿。
    _seen_rec: list = []
    _saved_record = g.record
    settings.jwt_secret = ""
    try:
        g.record = lambda *a, **k: _seen_rec.append(a)   # trace 钩子：只记调用
        r7 = _popup(SPEC_DEL, "把那个异步标签删掉吧")
    finally:
        g.record = _saved_record
        settings.jwt_secret = _STUB_SECRET
    check("签发密钥空缺 → 不弹窗（宁可走追问，也不发一个验不过的令牌）",
          r7 is None, str(r7)[:60])
    check("  且这一支被记了痕（静默消失是它最危险的形态）",
          any(a[:2] == ("confirm", "token_sign_failed") for a in _seen_rec),
          str(_seen_rec)[:90])

print("\n⑱ 确定性收尾的洞④ 豁免锚（gate 侧接线）")

check("锚常量是**系统**写进注记的那句前缀（不是随意字符串）",
      g._LEDGER_NOTE_PREFIX == "【系统台账核对】", g._LEDGER_NOTE_PREFIX)
_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
# 20260928 架构规范化 ③ 起，零帧轮的豁免判据住在**族表** `_zero_frame_families` 里
# （`_claim_issue` 只按表过一道）⇒ 这一锁跟着看表：锚常量必须**被那条判据读**，
# 而不是只在别处定义了一下（锚写了但判据不认 = 白写）。
_at = _src.index("def _zero_frame_families(")
_tbl = _src[_at:]
_tbl = _tbl[:_tbl.index("\ndef ", 10)]
check("gate 的洞④ 分支真的读了它（锚写了但判据不认 = 白写）",
      "_LEDGER_NOTE_PREFIX in _note" in _tbl)

print("\n⑯ 写技能的描述必须写明「不要自己揽下要不要执行」")

from agent.skills import SKILLS  # noqa: E402

_BY_NAME = {s.name: s for s in SKILLS}
for _n in ("tag_create", "article_status", "article_tags"):
    _d = _BY_NAME[_n].description
    check(f"{_n} 描述含「照常选本技能 + 系统弹确认框」纪律",
          "照常选本技能" in _d and "索要确认" in _d, _d[-60:])
gsrc2 = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("planner 规则里也有同一条（4b 写操作纪律：要不要执行不由你判断）",
      "要不要执行" in gsrc2 and "4b." in gsrc2)

print("\n⑲ 免引号形态的目标名与父标签（②防线续三：描述里的泛称被抄成参数值）")
# 20260922 全量回归现场（`admin_tag_move_popup` 五跑一红）：主人说「帮我把标签 Asyncio
# 挪到「编程」下面」——要挪的名字**没加引号**（唯一一段引号是父标签），planner 抄了技能
# 描述里的泛称：name="标签"（它甚至是这句话的子串，子串级地基放它过去）、
# parent_tag="父标签名"。问句于是变成「要修改标签「标签」：移到「父标签名」下面吗？」——
# 主人核对不出来，点确定就是挂错爸爸。语序在这里是主人给的标记。

check("免引号：名词与动作词之间那一段就是目标名（挪到）",
      g._bare_target_name("帮我把标签 Asyncio 挪到「编程」下面") == "Asyncio",
      repr(g._bare_target_name("帮我把标签 Asyncio 挪到「编程」下面")))
check("免引号：改名叫… 同一条语序也认（名字在动作词之前）",
      g._bare_target_name("把标签 Asyncio 改名叫协程") == "Asyncio",
      repr(g._bare_target_name("把标签 Asyncio 改名叫协程")))
check("免引号：删除语序也认（「把标签 X 删掉」是同一个名字通道）",
      g._bare_target_name("把标签 Asyncio 删掉吧") == "Asyncio",
      repr(g._bare_target_name("把标签 Asyncio 删掉吧")))
check("免引号：没有动作词 → 不认（说不清要干什么就不动参数）",
      g._bare_target_name("站内那个标签 Asyncio 挺好看的") == "")
check("免引号：跨小句 → 不认（多件事的句子分不清哪个名字配哪个动作）",
      g._bare_target_name("把标签 A 删掉，另外把标签 B 挪到「C」下面") == "",
      repr(g._bare_target_name("把标签 A 删掉，另外把标签 B 挪到「C」下面")))
check("免引号：捕获到的就是泛称本身 → 不认",
      g._bare_target_name("把标签 分类 挪到「编程」下面") == "")
check("免引号：引号包着的那段去壳照认（`_owner_target_span` 那条路本就处理，这里只是不误伤）",
      g._bare_target_name("把标签「Asyncio」挪到「编程」下面") == "Asyncio",
      repr(g._bare_target_name("把标签「Asyncio」挪到「编程」下面")))

check("标记词族别：挪到… → move（那段引号是父标签）",
      g._marked_operand("帮我把标签 Asyncio 挪到「编程」下面", ["编程"]) == ("编程", "move"),
      str(g._marked_operand("帮我把标签 Asyncio 挪到「编程」下面", ["编程"])))
check("标记词族别：改名叫… → rename（那段引号是新名字）",
      g._marked_operand("把标签「Asyncio」改名叫「协程」", ["Asyncio", "协程"])
      == ("协程", "rename"))
check("族别跟着**贴着引号的那个**标记词走（前面还有别的动作词也不混）",
      g._marked_operand("把标签 A 挪到 B 改成「C」", ["C"]) == ("C", "rename"))


def _name_plan(params, spec):
    """就一个 spec 的假计划（`_name_target_fix` 只读 skill/params/tools）。"""
    return {"skill": "tag_update", "params": dict(params), "tools": [spec],
            "note": "注记原文", "reply": "直接回答"}


_BAD_SPEC = 'update_tag({"name": "标签", "parent_tag": "父标签名"})'
_bad = _name_plan({"name": "标签", "parent_tag": "父标签名"}, _BAD_SPEC)
g._name_target_fix(_bad, "帮我把标签 Asyncio 挪到「编程」下面")
_s1 = " ".join(_bad["tools"])
check("planner 抄了描述里的泛称（name=「标签」）→ 校正成主人说的那个名字",
      "Asyncio" in _s1 and '"标签"' not in _s1, _s1[:140])
check("  父标签同样校正（它填的「父标签名」不在主人话里，主人只给了「编程」）",
      '"编程"' in _s1 and "父标签名" not in _s1, _s1[:140])
check("  参数表与 TOOLS 行同步（不能各说各的：弹窗问句读的是参数表）",
      _bad["params"].get("name") == "Asyncio"
      and _bad["params"].get("parent_tag") == "编程", str(_bad["params"]))

_ok = _name_plan({"name": "Asyncio", "parent_tag": "编程"},
                 'update_tag({"name": "Asyncio", "parent_tag": "编程"})')
_before = json.dumps(_ok, ensure_ascii=False, sort_keys=True)
g._name_target_fix(_ok, "帮我把标签 Asyncio 挪到「编程」下面")
check("本来就是主人说的名字 → 一个字节都不改（有据的值不动，防线不是重写器）",
      json.dumps(_ok, ensure_ascii=False, sort_keys=True) == _before)

_keep = _name_plan({"name": "标签"}, 'delete_tag({"name": "标签"})')
g._name_target_fix(_keep, "站内那个标签 Asyncio 挺好看的")
check("说不清是哪件事（没有动作词）→ 不猜，参数原样留给弹窗/预检那两条路",
      _keep["params"].get("name") == "标签", str(_keep["params"]))

_cut = _name_plan({"name": "Async"}, 'update_tag({"name": "Async"})')
g._name_target_fix(_cut, "帮我把标签 Asyncio 挪到「编程」下面")
check("planner 把名字**抄短了**（实测 name=「Async」）→ 校正成主人原话里那一段",
      _cut["params"].get("name") == "Asyncio", str(_cut["params"]))

_filler = _name_plan({"name": "Asyncio"}, 'update_tag({"name": "Asyncio"})')
_before_f = json.dumps(_filler, ensure_ascii=False, sort_keys=True)
g._name_target_fix(_filler, "帮我把标签 Asyncio 这个名字挪到「编程」下面")
check("原话里带补语（「Asyncio 这个名字」）→ 捕获段作废，不把补语当名字",
      json.dumps(_filler, ensure_ascii=False, sort_keys=True) == _before_f,
      str(_filler["params"]))


_ren = _name_plan({"name": "Asyncio", "parent_tag": ""},
                  'update_tag({"name": "Asyncio", "new_title": "协程"})')
g._name_target_fix(_ren, "把标签「Asyncio」改名叫「协程」")
check("改名形态：新名字**不会**被当成父标签填进去（族别搞混就是参数对调）",
      not _ren["params"].get("parent_tag"), str(_ren["params"]))

# 20260924：泛称词表补「标题 / 公告标题」（公告族的同形——参数描述里 title=公告标题，
# planner 会把它抄成 `title="标题"`；而"标题"**正是主人这句话的子串**，子串级地基
# 放它过去 ⇒ 弹窗问成了「删除公告「标题」」）
def _ann_plan(spec, title):
    return {"skill": "announcement_delete", "params": {"title": title}, "tools": [spec],
            "note": "注记原文", "reply": "直接回答"}


_ann = _ann_plan('delete_announcement({"title": "标题"})', "标题")
g._name_target_fix(_ann, "把标题是「公告」的那条公告删掉吧")
check("公告族：planner 抄了参数描述里的「标题」→ 校正成主人引号里那个标题",
      _ann["params"].get("title") == "公告", str(_ann["params"]))

_ann_ok = _ann_plan('delete_announcement({"title": "公告"})', "公告")
_before_a = json.dumps(_ann_ok, ensure_ascii=False, sort_keys=True)
g._name_target_fix(_ann_ok, "把标题是「公告」的那条公告删掉吧")
check("公告族：本来就是主人说的标题 → 一个字节都不改",
      json.dumps(_ann_ok, ensure_ascii=False, sort_keys=True) == _before_a,
      str(_ann_ok["params"]))

print("\n⑳ 写参数的值也要来自主人这句话（②防线续五：新建编名 / 抄短 / 抄泛称）")


def _value_case(desc: str, skill: str, params: dict, msg: str, want):
    """跑一次 `_name_arg_fix` 并按形态断言。

    `want` 三态：dict = 要校正成这几项（顺带断言参数表与 TOOLS 行同步）；None =
    一个字节都不动（主人原话里逐字有据）；"REFUSE" = 零写 + 响亮（解不出就绝不猜）。
    """
    pl = instantiate_plan(skill, params)
    pl["params"] = dict(params)
    before = " ".join(pl.get("tools") or [])
    refuse = g._name_arg_fix(pl, msg)
    if want == "REFUSE":
        check(desc, refuse is not None and pl["params"] == params
              and " ".join(pl.get("tools") or []) == before,
              str(refuse)[:90])
        return
    if want is None:
        check(desc, refuse is None and pl["params"] == params,
              json.dumps(pl["params"], ensure_ascii=False)[:90])
        return
    got = {k: pl["params"].get(k) for k in want}
    tools_line = " ".join(pl.get("tools") or [])
    flat = [str(x) for v in want.values() for x in (v if isinstance(v, list) else [v])]
    check(desc, refuse is None and got == want and all(x in tools_line for x in flat),
          json.dumps(got, ensure_ascii=False)[:90])


# 事故现场（20260922 活体探针⑥⑩⑭）：这三条写操作的**新名字**过去没有任何地基——
# `_ident_grounded` 对 create_tag 只查父标签，于是 title 填什么都有据、免弹窗快道
# 又开着 ⇒ planner 编的名字（⑥ 20260922_1345_test_tag）或抄的泛称（⑩⑭ title=「名字」）
# 直接落库，命令式措辞一个确认框都不弹。
_value_case("⑩ 抄了泛称「名字」→ 认免引号的命名标记（主人原话那一段）",
            "tag_create", {"title": "名字", "color": "粉色"},
            "一级标签，名字叫_探针色_0922134601，使用粉色颜色",
            {"title": "_探针色_0922134601"})
_value_case("⑥ 编了个名字 → 认「名字叫「X」」（引号段就是身份）",
            "tag_create", {"title": "20260922_1345_test_tag"},
            "新建一个一级标签，名字叫「_探针_0922134554」",
            {"title": "_探针_0922134554"})
_value_case("⑭ 抄短了名字 → 补回主人原话里那一段（分类同理）",
            "category_create", {"title": "探针分类0922"},
            "新建一个分类，叫「探针分类0922134609」",
            {"title": "探针分类0922134609"})
_value_case("⑭b 泛称同样按标记段校正（名字标记优先于校验）",
            "category_create", {"title": "名字"},
            "新建一个分类，叫「探针分类0922134609」",
            {"title": "探针分类0922134609"})
_value_case("⑤ 列表值也是值：抄了泛称「标签名」→ 认主人引号里那一段",
            "article_tags", {"article_id": 1, "add": ["标签名"]},
            "给文章 1 加上「音乐」标签", {"add": ["音乐"]})
_value_case("  摘标签同理（remove 与 add 同一族，别只修一半）",
            "article_tags", {"article_id": 1, "remove": ["标签名"]},
            "把文章 1 的「音乐」标签去掉", {"remove": ["音乐"]})
_value_case("⑪ 「在「编程」下面」是父标签、不是新名字（两个操作数不许对调）",
            "tag_create", {"title": "父标签名", "parent_tag": "父标签名"},
            "我想在「编程」下面加一个二级标签，名字叫_探针L2_0922134605",
            {"title": "_探针L2_0922134605", "parent_tag": "编程"})
_value_case("改名形态：new_title 认「改名叫「X」」那段",
            "tag_update", {"name": "Asyncio", "new_title": "新名字"},
            "把标签「Asyncio」改名叫「协程」", {"new_title": "协程"})
# 续六（20260922 探针腿⑭ 现场）：改名句里**新名字含目标名词**（`探针分类0922193132R` 里含
# 「分类」）曾被整段判脏 ⇒ 值空缺、`cand_spans[0]` 回落到**目标那段**、planner 写对的值被
# 覆写成目标名 ⇒ 工具照写、回执"X → X"、库一个字节没动（回执读起来像改成功了）。
_value_case("⑭c 新名字里含目标名词（…分类…R）→ 不判脏，planner 写对的值原样保留",
            "category_update",
            {"name": "探针分类0922193132", "new_title": "探针分类0922193132R"},
            "把分类「探针分类0922193132」改名叫「探针分类0922193132R」", None)
_value_case("⑭d 腿⑭ 现场那一跑：planner 把 new_title 写成了**目标自己** → 校正回主人那段",
            "category_update",
            {"name": "探针分类0922193132", "new_title": "探针分类0922193132"},
            "把分类「探针分类0922193132」改名叫「探针分类0922193132R」",
            {"new_title": "探针分类0922193132R"})
_value_case("⑭e 不带「名」的改名词（改成/改为/换成）同样认",
            "tag_update", {"name": "Asyncio", "new_title": "Asyncio"},
            "把标签「Asyncio」改成「协程」", {"new_title": "协程"})
_value_case("⑭f 主人只说了「改名」没说新名字 → 零写 + 响亮（绝不留一次空转的「X → X」）",
            "tag_update", {"name": "摄影", "new_title": "名字"},
            "把标签「摄影」改名", "REFUSE")
_value_case("⑭g 同上但 planner 直接把目标名填成 new_title → 同样零写（空转改名不许放行）",
            "tag_update", {"name": "摄影", "new_title": "摄影"},
            "把标签「摄影」改个名字", "REFUSE")
_value_case("⑭h 移动命令里 planner 顺手带同名 new_title → 一个字节都不动"
            "（「改名意图」是这条判据的闸，不能把一次真移动拦下来）",
            "tag_update",
            {"name": "Asyncio", "parent_tag": "编程", "new_title": "Asyncio",
             "level": "two", "to_level": "two"},
            "把标签「Asyncio」挪到「编程」下面", None)
_value_case("值逐字在主人原话里 → 一个字节都不动（防线不是重写器）",
            "tag_create", {"title": "Redis"}, "帮我建一个 Redis 标签", None)
_value_case("列表值本来就对 → 不动（多段引号各有其主）",
            "article_tags", {"article_id": 1, "add": ["音乐", "摄影"]},
            "给文章 1 加上「音乐」「摄影」标签", None)
_value_case("解不出值（主人没说名字）→ **零写 + 响亮**，绝不拿 planner 的转写凑一个",
            "tag_create", {"title": "临时标签"}, "帮我建一个标签", "REFUSE")
_value_case("引号说不清哪一个才是值（在「编程」和「摄影」下面都建一个）→ 同样拒绝",
            "tag_create", {"title": "标签名"},
            "在「编程」和「摄影」下面都建一个", "REFUSE")

_seen_v: list = []
_saved_v = g.record
try:
    g.record = lambda *a, **k: _seen_v.append(a)
    g._name_arg_fix(instantiate_plan("tag_create", {"title": "临时标签"}) | {"params": {"title": "临时标签"}},
                    "帮我建一个标签")
finally:
    g.record = _saved_v
check("  拒绝这一支要留痕（零写的决定必须能在 trace 里复盘）",
      any(a[:2] == ("planner", "write_value_unresolved") for a in _seen_v),
      str(_seen_v)[:80])

print("\n㉑ 公告正文标记认「改成/改为/换成/变成/更新为」（⑯ 探针：正文被写错的根因）")
for _verb in ("改成", "改为", "换成", "变成", "更新为", "写", "是", "为", "说", "："):
    _b = g._msg_marked_field(f"把公告「X」的内容{_verb}：新的正文", "body")
    check(f"  「内容{_verb}：」认得出标记", _b == "新的正文", str(_b))
_BODY = "维护改到明晚 23 点（探针自动更新）"
_pl_ann = instantiate_plan("announcement_update",
                           {"title": "探针公告", "new_title": "", "content": "维护改到明晚"})
_pl_ann["params"] = {"title": "探针公告", "new_title": "", "content": "维护改到明晚"}
g._announcement_text_fix(_pl_ann, f"把公告「探针公告」的内容改成：{_BODY}")
check("「内容改成：X」→ 正文校正成主人写下的那段"
      "（不认「改成」时标记整条落空，planner 的转写就顶上去落地了）",
      _pl_ann["params"].get("content") == _BODY, str(_pl_ann["params"].get("content"))[:80])
check("  标题不动（这句没给新标题；改公告的 title 是**身份**，不许被正文标记带跑）",
      _pl_ann["params"].get("title") == "探针公告", str(_pl_ann["params"].get("title")))
_pl_new = instantiate_plan("announcement_create",
                           {"title": "探针公告", "content": "探针内容：今晚 23 点维护"})
_pl_new["params"] = {"title": "探针公告", "content": "探针内容：今晚 23 点维护"}
g._announcement_text_fix(_pl_new, "发一条公告，标题叫「探针公告」，"
                                  "正文写：探针内容：今晚 23 点维护（本条由探针自动发出）")
check("  最左匹配落在**主人标的那个**标记上（正文本里再出现「探针内容：」也不许被它抢走）",
      _pl_new["params"].get("content") == "探针内容：今晚 23 点维护（本条由探针自动发出）",
      str(_pl_new["params"].get("content"))[:90])

print("\n㉓ 公告正文：谁成文（两种情形）+ 卡面必须印全文（20261006 改口径）")
# 动机与现场取证见 `docs/问题记录.md` 1.47 的《同类未治》：公告正文**分两种情形**——
# 主人明确给了原文（「正文写：…」）⇒ 一字不改地照录；只给了意思（「以你的口吻发个
# 公告祝大家国庆快乐」）⇒ 由 planner 按他的意思成文、**可以润色**。旧口径把这**两种
# 一起禁**了（技能描述「不许替他润色……他给几个字就写几个字」+ 展开层「原样透传、一个
# 字都不改」+ 工具注「不要自己加戏或改写」+ 阻断文案「一个字都不要改、也不要替他润色」），
# 现场代价是 trace `20261001T061023`：主人点名要"以你的口吻"，模型照旧只回声一句
# 「祝大家国庆节快乐！」，主人下一句就是「太干巴了，而且我要求以你的身份」。
#
# 这一节只锁**契约层**的两件事（措辞归谁有明文；卡面印全文）——行为侧的多遍取证
# 记在问题记录里，这里不重复跑 LLM。
#   ① 「照录」与「只给意思 ⇒ 可由你成文」**两个分支**在给模型看的四处载体里都有明文
#      （同一件事实写四份，是这一族最容易各漂各的地方）；
#   ② 卡面印**全文**——正文改由模型成文之后判据判不了措辞，主人的签字是这一族**唯一**
#      的人眼复核点（与待办卡 20260926「一格都不截」同因）。
import agent.skills as _S  # noqa: E402
from tools import base as _B  # noqa: E402

_ann = _S.SKILL_MAP["announcement_create"]
_ANN_CARRIERS = {
    "技能描述": _ann.description,
    "技能 inputs.content": str(_ann.inputs.get("content") or ""),
    "工具 docstring": str(_B.create_announcement.description or ""),
    "工具 content 注解": str(_B.create_announcement.args_schema.model_json_schema()
                             ["properties"]["content"]["description"]),
}
_ANN_OLD = ("不许替他润色", "不要自己加戏", "原样透传", "他给几个字就写几个字")
check("前置：四处载体都非空（取空 ⇒ 下面几条是空转）",
      all(v.strip() for v in _ANN_CARRIERS.values()),
      str([k for k, v in _ANN_CARRIERS.items() if not v.strip()]))
check("旧口径那几句禁令在四处载体里都消失了"
      "（留一句 = 模型照旧只回声主人那几个词）",
      not [k for k, v in _ANN_CARRIERS.items() if any(p in v for p in _ANN_OLD)],
      str([(k, p) for k, v in _ANN_CARRIERS.items() for p in _ANN_OLD if p in v]))
check("「主人给了原文 ⇒ 照录」在四处都有明文",
      all("照录" in v for v in _ANN_CARRIERS.values()),
      str([k for k, v in _ANN_CARRIERS.items() if "照录" not in v]))
check("「只给了意思 ⇒ 由你成文（可以润色）」在四处都有明文",
      all(("组织措辞" in v or "成文" in v) for v in _ANN_CARRIERS.values()),
      str([k for k, v in _ANN_CARRIERS.items()
           if "组织措辞" not in v and "成文" not in v]))
_authz_ann = authz._CONSENT_WHY_TOOL["create_announcement"][1]
check("阻断文案管的是「卡面要念全」（它是被拦下那一轮的话，不是正文该照抄）",
      "全文" in _authz_ann and "不要再改内容" in _authz_ann
      and "一个字都不要改" not in _authz_ann, _authz_ann)

# ── 展开层：原样放行成文的正文；空正文照旧零工具追问 ─────────────────────
from agent.skills import _expand_write_skill  # noqa: E402

_LONG_ANN = "本次维护安排如下：" + "请提前做好准备" * 12 + "——结束标记"
check("前置：这条正文真的比 60 字长（否则下面几条是空转）",
      len(_LONG_ANN) > 60, str(len(_LONG_ANN)))
_specs_a, _note_a = _expand_write_skill(_ann, {"title": "维护通知", "content": _LONG_ANN})
check("展开层原样放行（不因正文与主人原话不同就拦——那一档本来就是模型成文）",
      len(_specs_a) == 1 and _LONG_ANN in str(_specs_a[0]), str(_specs_a)[:120])
check("  注记写明「正文全文会印在确认卡上由主人核对」（复核点是卡面，不是措辞）",
      "确认卡" in _note_a and "全文" in _note_a, _note_a)
_specs_e, _note_e = _expand_write_skill(_ann, {"title": "维护通知", "content": ""})
check("正文为空 ⇒ 零工具 + 追问（「槽位空着不许往下走」与「可以润色」是两件事）",
      _specs_e == [] and "问清" in _note_e, _note_e)

# ── 卡面：一格都不截（与待办卡 20260926 那条同因）─────────────────────────
_qc = A.render_confirm_question([{"tool": "create_announcement",
                                  "args": {"title": "维护通知", "content": _LONG_ANN}}])
check("发布公告的**卡面**：长正文一字不落（此前裁成 60 字 + 省略号）",
      _LONG_ANN in _qc and "…" not in _qc, _qc[-40:])
check("  卡面写的是「正文：」而不是只报个标题（只写标题 = 让主人盲签一份没看过的公告）",
      f"发布公告「维护通知」，正文：{_LONG_ANN}" in _qc, _qc[:60])
_qu = A.render_confirm_question([{"tool": "update_announcement",
                                  "args": {"title": "维护通知", "content": _LONG_ANN}}])
check("改公告的卡面同样是全文", _LONG_ANN in _qu and "…" not in _qu, _qu[-40:])
check("正文为空时卡面如实写「（没有写正文）」（不是把 None 印出来）",
      "（没有写正文）" in A.render_confirm_question(
          [{"tool": "create_announcement", "args": {"title": "维护通知"}}]))

print("\n㉒ 文章确认框写《标题》与当前状态（20260922 第七轮：全写面唯一的盲签）")
# 事故形态：标签/分类/公告/留言的问句都写了**名字**，只有文章这一类一直只有内部
# 编号（「修改文章 46」）——主人没法从这句话里认出"是不是我说的那篇"，而点确定
# 正是文章写操作唯一的人类兜底。评估意见"有证据 ≠ 目标唯一"落在这里。
_NOTES = {1: {"noteKey": 1, "noteTitle": "Memory Blog 项目文件结构说明",
              "status": "draft", "isTop": 0},
          46: {"noteKey": 46, "noteTitle": "文章向量空间图谱项目文档",
               "status": "public", "isTop": 1}}
_STATUS_SPEC = {"tool": "set_article_status",
                "args": {"article_id": 1, "status": "private"}}
_TAGS_SPEC = {"tool": "set_article_tags",
              "args": {"article_id": 46, "remove": ["摄影"]}}
_q = A.render_confirm_question([_STATUS_SPEC], None, None, None, _NOTES)
check("改状态的问句带上《标题》（此前只有内部编号「修改文章 1」）",
      "《Memory Blog 项目文件结构说明》" in _q and "修改文章 1" in _q, _q)
check("  并写出**现状**（主人点确定前才知道自己改的是不是这件事）",
      "现在：草稿、未置顶" in _q, _q)
_q2 = A.render_confirm_question([_TAGS_SPEC], None, None, None, _NOTES)
check("摘标签的问句同样带标题与现状",
      "《文章向量空间图谱项目文档》（现在：公开、置顶）的标签：去掉 摄影" in _q2, _q2)
check("  带上标题后不补那个分隔空格（「…（现在：…） 的标签」像两个并列短语）",
      "） 的标签" not in _q2, _q2)
check("气泡正文与问句同源（都带标题与现状）",
      "《Memory Blog 项目文件结构说明》" in A.render_confirm_text(
          [_STATUS_SPEC], None, None, None, _NOTES))
check("读不到文章清单（None）→ 退回只写 id，**照样弹窗**"
      "（少说一句 ≠ 不弹：不弹就退回「判成歧义就追问」的死路形态）",
      "修改文章 1：" in A.render_confirm_question([_STATUS_SPEC]) and
      "修改文章 1：" in A.render_confirm_question(
          [_STATUS_SPEC], None, None, None, None))
check("空清单（真的没有这一篇）→ 如实写出来，不装作核对过",
      "（后台清单里没有这一篇）" in A.render_confirm_question(
          [_STATUS_SPEC], None, None, None, {}))
# 现状是**读来的事实**，读不到就该少说：认不出的状态吐回原值、置顶值缺失就不提置顶
# （编一个"公开"出来，主人会以为自己那篇早就是公开的）。
_qbad = A.render_confirm_question(
    [_STATUS_SPEC], None, None, None,
    {1: {"noteKey": 1, "noteTitle": "X", "status": "weird", "isTop": None}})
check("状态值认不出 → 如实吐回「未知状态「weird」」，不编一个已知状态",
      "未知状态「weird」" in _qbad, _qbad)
check("置顶值读不到 → 完全不提置顶（不知道就不说）",
      "置顶" not in _qbad, _qbad)
check("标签写操作不受 notes 影响（同一份快照只服务文章那两类）",
      "《" not in A.render_confirm_question(
          [{"tool": "create_tag", "args": {"title": "新标签"}}],
          {}, None, None, _NOTES))
# 接线（渲染层收到 notes 是一回事，graph 真的去读是另一回事）：只在要点到文章时读，
# 标签/分类写操作不该为此多一次后台往返。
_gsrc = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("接线：只在 picks 里有**要报标题的**文章写工具时才读文章清单"
      "（收藏两件不动标题——它们是普通访客也能用的，读后台清单只会白拿 403）",
      "in _POPUP_TITLE_TOOLS for s in picks" in _gsrc
      and 'authz.check(principal, "list_admin_notes").allowed' in _gsrc
      and "_note_index(config)" in _gsrc
      # 判据必须比 _ARTICLE_WRITE_TOOLS 窄，否则收藏也会去读一次后台清单
      and "_POPUP_TITLE_TOOLS = _ARTICLE_WRITE_TOOLS" not in _gsrc)
from agent.graph import _ARTICLE_WRITE_TOOLS, _POPUP_TITLE_TOOLS  # noqa: E402
check("  收藏两件确实不在标题名单里（名单是**真子集**，不是照抄整份）",
      _POPUP_TITLE_TOOLS < _ARTICLE_WRITE_TOOLS
      and not ({"add_favorite", "remove_favorite"} & _POPUP_TITLE_TOOLS),
      f"{sorted(_POPUP_TITLE_TOOLS)} vs {sorted(_ARTICLE_WRITE_TOOLS)}")
check("  读失败不拦弹窗（note_index 为空时问句退化成 id，不是 return None）",
      'note_index = None' in _gsrc and "_note_index(config)" in _gsrc)
# 跨模块口径：弹窗里的「现在」必须描述**写操作真正会改的那一行**——`_note_index`
# 与写工具的读前值 `_read_note` 必须走同一个端点（谁改成 draft/editor 那条，
# 弹窗描述的就会是另一行：那条对修改稿行会解引用成原文章）。
import inspect  # noqa: E402
from tools import base as _B  # noqa: E402
_rn = inspect.getsource(_B._read_note)
_ni = inspect.getsource(_B._note_index)
check("文章清单与写工具的读前值走同一端点（弹窗里的「现在」= 真正被改的那一行）",
      '"/api/protected/notes/list"' in _rn and '"/api/protected/notes/list"' in _ni
      and "draft/editor" not in _ni, _ni[:60])

# ── 代调令牌的载荷形状（跨仓契约，20260926）────────────────────────────────
# Rust 侧 20260926 给登录令牌加了代次声明 `ver`（改密码/冻结即作废旧令牌），而
# `authz::check_token` 把"没有 ver"单独当一支：跳过代次比对、只判账号冻结。**管理助手
# 这枚 60 秒代调令牌必须停在这一支上**——它代表的是 Rust 本次请求刚认证过的身份；
# 若哪天有人"顺手补全"给它填个 0，那么"改过密码的管理员 + 管理助手"会整体 401
# （对方的 token_version 早就不是 0 了），而这条链路的失败长得像"没权限"，很难查。
import base64 as _b64  # noqa: E402
_tok = _B._sign_local_jwt(721, "admin")
_part = _tok.split(".")[1]
_pad = _part + "=" * (-len(_part) % 4)
_payload = json.loads(_b64.urlsafe_b64decode(_pad).decode())
check("代调令牌的载荷就是 {sub, exp, role} 三个键",
      sorted(_payload) == ["exp", "role", "sub"], f"{sorted(_payload)}")
check("代调令牌**不带 ver**（无 ver 的那一支是刻意保留的，不是漏填）",
      "ver" not in _payload, f"{sorted(_payload)}")
check("代调令牌不带 aud（Rust 用 Validation::default()，多个 aud 会验签失败）",
      "aud" not in _payload, f"{sorted(_payload)}")

print("\n㉔ 一次建多个标签：整批一起问（20261008：弹卡整轮生效，快道那几件不许被静默丢下）")

# 病：`execute_node` 一见 `pending_confirm` 就**一个工具都不执行**（整轮 all-or-nothing），
# 而 `_confirm_popup` 里走了"同轮命令即确认"快道的那几件是 `continue` 掉的——它们既
# 不在 `picks` 里、也不在那张签名令牌的 `specs` 里 ⇒ 主人点了「确定」，它们**这一轮
# 谁都没执行、下一轮也没有任何通道会再办**。而主人那句话是命令式
# （「确认创建标签 Rust，另外一个也建上」）：一条也没办的写被夹在一张只问一半的卡里，
# 正是本批 `tag_create.titles` 要根治的"漏掉的名字不留痕迹"——一次建多个标签把它从
# "理论上的形状"变成了常见形状（`titles` 一次展开好几个 spec）。
# 处方：整批一致——这一批里只要有一件要问，**整批一起问**（并保 TOOLS 行顺序）。
# 方向与本文件其它每一处一致：宁可多问一次，也不静默丢一次主人点名的写。


def _popup_batch(specs, msg, skill="tag_create", uid=7):
    """跑一次多 spec 的 `_confirm_popup`（真判据 + 真签发，假的是标签字典与后端）。"""
    specs = list(specs)
    return g._confirm_popup(
        {**g.plan_state({"skill": skill, "params": {}, "tools": specs,
                         "note": "", "reply": "直接回答"})},
        specs, Principal(uid=uid, role=ROLE_ADMIN), msg,
        {"configurable": {"user_id": uid, "conversation_id": 42}})


def _spec_title(spec):
    """令牌 specs 里一件的标题（结构是 {tool, args}，与卡面渲染读的是同一份）。"""
    return ((spec or {}).get("args") or {}).get("title")


def _token_specs(result):
    """解出卡上那张令牌签下的 `specs`（**只在测试里**解载荷，不验签——验签是 confirm 的事）。"""
    tok = (result or {}).get("pending_confirm", {}).get("token", "")
    part = tok.split(".")[0]
    if not part:
        return []
    pad = part + "=" * (-len(part) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad).decode()).get("specs") or []
    except Exception:
        return []


# 标签树里只有 `编程 / Asyncio`：Rust 与 SVN 都是**站里没有的**（不踩"状态已达成"那条出口）。
with patch(_tag_index=lambda config: A.build_tag_index(
        [{"tagKey": 2, "title": "编程", "level": 1}],
        [{"tagKey": 10000, "title": "Asyncio", "level": 2,
          "fatherTag": "编程", "fatherKey": 2}])):
    SPEC_RUST = 'create_tag({"title": "Rust"})'
    SPEC_SVN = 'create_tag({"title": "SVN"})'
    _P = Principal(uid=7, role=ROLE_ADMIN)
    _MSG_BOTH = "确认创建标签 Rust 和 SVN"
    check("  前提：这一句话对两件都判成命令（否则下面测的不是「整批一致」）",
          all(authz.consent_granted(_P, "create_tag", _MSG_BOTH) is True
              for _ in (SPEC_RUST, SPEC_SVN)))

    r_both = _popup_batch([SPEC_RUST, SPEC_SVN], _MSG_BOTH)
    check("两件都落地基 → **不弹卡**（一句命令一次点击都不多，既有形态一字未动）",
          r_both is None, str(r_both)[:80])

    # 混批：Rust 在主人话里（走免弹窗快道），SVN 不在 ⇒ 只有 SVN 要问。
    _MSG_MIX = "确认创建标签 Rust，另外一个也建上"
    check("  前提：这一句话只给 Rust 落地基（SVN 没落地基 ⇒ 它才是要问的那一件）",
          g._ident_grounded("create_tag", {"title": "Rust"}, _MSG_MIX) is True
          and g._ident_grounded("create_tag", {"title": "SVN"}, _MSG_MIX) is False)

    r_mix = _popup_batch([SPEC_RUST, SPEC_SVN], _MSG_MIX)
    _q_mix = (r_mix or {}).get("pending_confirm", {}).get("q", "")
    check("混批 → 弹卡（走快道那件本来就不该单独执行：弹卡是整轮的）",
          isinstance(r_mix, dict) and "pending_confirm" in (r_mix or {}), str(r_mix)[:80])
    check("  卡面把**快道那件也列上**（修前只列 SVN ⇒ 主人点「确定」它照样一条都没办）",
          "Rust" in _q_mix and "SVN" in _q_mix, _q_mix[:120])
    _tok_specs = _token_specs(r_mix)
    check("  而且**令牌签的就是两件**（只列在卡面上不算——点「确定」执行的是 specs）",
          [_spec_title(s) for s in _tok_specs] == ["Rust", "SVN"], str(_tok_specs)[:160])
    check("  编号两端同源：卡面顺序 = 令牌顺序 = TOOLS 行顺序（「只办第 N 件」才指得准）",
          "Rust" in _q_mix and "SVN" in _q_mix
          and _q_mix.index("Rust") < _q_mix.index("SVN")
          and [o["value"] for o in r_mix["pending_confirm"]["opts"]
               if str(o["value"]).startswith("pick:")] == ["pick:0", "pick:1"],
          str([o["value"] for o in r_mix["pending_confirm"]["opts"]]))

    # 顺序锁：**第一件**才是要问的那一件时，合并后顺序不许倒过来（并进 picks 的入口
    # 是"快道在别的出口 continue 了"，最容易被写成 `picks + fast` 而不排序）。
    r_ord = _popup_batch([SPEC_RUST, SPEC_SVN], "确认创建标签 SVN，另外一个也建上")
    _q_ord = (r_ord or {}).get("pending_confirm", {}).get("q", "")
    _ord_titles = [_spec_title(s) for s in _token_specs(r_ord)]
    check("第一件要问、第二件走快道 → 合并后**仍是 TOOLS 行顺序**（不是「先问的排前面」）",
          _ord_titles == ["Rust", "SVN"], str(_ord_titles)[:120])
    check("  对应地，问句里 Rust 也排在 SVN 前（渲染与令牌同源）",
          "Rust" in _q_ord and "SVN" in _q_ord
          and _q_ord.index("Rust") < _q_ord.index("SVN"), _q_ord[:120])

    # 这一笔要**看得见**：合并改变了"这一轮会不会执行"，事后只能靠 trace 分辨
    # （整批走快道时不合并、tokens 逐字相同，两条路的卡面长得一样）。
    _seen: list = []
    _saved_rec = g.record
    try:
        g.record = lambda *a, **k: _seen.append((a, k))
        _popup_batch([SPEC_RUST, SPEC_SVN], _MSG_MIX)     # 混批：该记一笔
        _popup_batch([SPEC_RUST, SPEC_SVN], _MSG_BOTH)    # 整批走快道（不弹卡）：不该记
    finally:
        g.record = _saved_rec
    _merged = [1 for a, _ in _seen if a[:2] == ("confirm", "batch_fastpath_merged")]
    check("  合并记一笔 trace（`batch_fastpath_merged`），整批走快道那一次**不记**",
          len(_merged) == 1, str([a[:2] for a, _ in _seen])[:120])

    # 反例：被**别的原因**跳过的 spec 一件都不并进来。参数解析不了的那件自有下游
    # 出路（execute 产 `__ERROR__` 帧退回 planner），把它塞进卡里等于让主人去批准
    # 一件系统根本没读懂的事；而"有 picks 才合并"这条也让它落回原有的错误帧链路
    # （picks 空 ⇒ 不弹卡 ⇒ 整轮照常执行，坏的那件照常报错）。
    r_bad = _popup_batch([SPEC_RUST, 'create_tag(not-json)'], _MSG_MIX)
    check("参数解析不了的 spec 不进卡（picks 空 ⇒ 照旧不弹，走既有的错误帧链路）",
          r_bad is None, str(r_bad)[:80])


# ══════════════════════════════════════════════════════════════════
print("\n㉕ 按分类查文章清单（20261009：站内一直有，agent 这一侧此前没有入口）")
# 现场（生产 trace `20261008T234509`）：主人要「把「测试」分类下的文章全部转成私密」，
# 模型手上只有 `list_admin_notes(keyword="测试")`——那是**搜索**（切词匹配标题/正文/
# 标签名），搜出来的是标题带"测试"的 8 篇，与"归在「测试」分类下"是两回事。它如实说
# 没有那份清单、并把「你去后台笔记页按分类筛一遍」这个**本可自动完成**的步骤退给了
# 主人。而站内这个筛选一直在：后台笔记页就是走 `/api/protected/notes/search` 的
# `categories` 字段筛的（`frontend/src/pages/Dashboard/Notes/AllNotes/index.tsx`）。
# 缺的两处都在 agent 侧：① 工具参数没暴露；② 行里连分类名都不印（`categoryTitle`
# 早就在 DTO 里）。这一节把两处都锁住，**外加三条"不许把读不到说成没有"**。

# —— 渲染：每行末尾印分类（键缺席 = 这一路没带分类信息 ⇒ 不印那一格，缺键不编）——
from agent.entities import receipt_digest  # noqa: E402  （跨语言契约：写侧压摘要）

_N_TEST = {**note(12, "架构文档"), "categoryTitle": "测试"}
_N_NULL = {**note(21, "关于欧洲AI"), "categoryTitle": None}
_N_NOKEY = note(19, "Saudade Blog AI Agent（泠月喵）架构文档", "public", 0, "1")
_r = A.render_admin_notes([_N_TEST, _N_NULL, _N_NOKEY], IDX)
check("分类名进得了行尾（数据本来就在 DTO 里，只是没印）",
      "- noteId=12 [私密]《架构文档》标签：（无标签） ｜分类：测试" in _r, _r)
check("键在但为空 = 这篇文章真的没有分类 ⇒ 印「（无）」",
      "｜分类：（无）" in _r, _r)
check("**键缺席 ⇒ 不印这一格**（别的调用方/旧形载荷不带分类信息，不许编一个出来）",
      _r.count("｜分类：") == 2, _r)
check("老形一条没动（新格是行尾追加，`标签：` 前缀逐字不变）",
      "- noteId=19 [公开]《Saudade Blog AI Agent（泠月喵）架构文档》标签：Python\n" in _r
      + "\n" and "｜分类：" not in _r.split("noteId=19")[1], _r)

_r2 = A.render_admin_notes([_N_TEST], IDX, category="测试")
check("按分类筛的头行明写分类（不许让它读成站内总量）",
      _r2.startswith("后台文章里「测试」分类下共 1 篇"), _r2)
_r3 = A.render_admin_notes([_N_TEST], IDX, keyword="测试", category="测试")
check("两样都筛时两个限定都在头行",
      _r3.startswith("后台文章里「测试」分类下匹配「测试」的共 1 篇"), _r3)
_d_cat = receipt_digest("list_admin_notes", A.render_admin_notes([_N_TEST], IDX,
                                                                category="测试"))
check("摘要照同一条口径分流：按分类的数说「该分类下 N 篇」，不说「共 N 篇」",
      "分类「测试」" in _d_cat and "该分类下 1 篇" in _d_cat and "共 1 篇" not in _d_cat,
      _d_cat)
check("两样都筛时仍按搜索口径说「匹配」（限定写进同一条串）",
      "匹配 1 篇" in receipt_digest("list_admin_notes", _r3), _r3)

# —— 提示词文本锁（判据只能锁文本，不可以锁"模型会怎么做"）——
# 20261009 探针实测：3 遍里 1 遍给 category 填了占位词「未确认分类」（那不是站内任何
# 一个分类 ⇒ 工具回"没有这个分类"，白烧一轮，而主人看到的是"没有"）。占位词这类坑
# 本仓有过先例（20260922 tag_create：描述里的〈…〉被当成了真名字）⇒ 契约里必须有一句
# 明写的禁令。同一句话在工具参数描述里也有一份（planner 两处都看得到）。
from agent.skills import SKILLS as _SK  # noqa: E402
_AN = next(s for s in _SK if s.name == "admin_notes")
check("planner 契约里明写「不许填占位词」（契约 + 参数表两处都写，模型两处都看得到）",
      "占位词" in _AN.planner_contract and "未确认分类" in _AN.planner_contract
      and "占位词" in _AN.inputs["category"],
      _AN.inputs["category"][-40:])

# —— 工具：分类名先在本字典上认，认不出就是「读不到」，绝不用空结果冒充「没有」——
_CATS = [{"categoryKey": 9, "categoryTitle": "测试", "pathName": "测试", "noteCount": 0},
         {"categoryKey": 13, "categoryTitle": "编程笔记", "pathName": "编程笔记",
          "noteCount": 8}]
_CATIDX = A.build_category_index(_CATS)
_NOTE_ROW = {"noteKey": 9, "noteTitle": "test 测试111111111", "status": "private",
             "isTop": 0, "noteTags": "", "categoryTitle": "测试"}

with patch(_category_index=lambda c: _CATIDX, _tag_index=lambda c: IDX):
    post = _Post([_NOTE_ROW])
    with patch(_admin_read_post=post):
        r = base.list_admin_notes.invoke({"category": " 测试 "}, config=cfg())
    check("按分类筛走检索端点、分类名（去空白后）原样发给服务端",
          [c[0] for c in post.calls] == ["/api/protected/notes/search"]
          and post.calls[0][1] == {"categories": "测试"}, str(post.calls))
    check("清单照常渲染，且这一行带分类名", "｜分类：测试" in r, str(r))

    post = _Post([_NOTE_ROW])
    with patch(_admin_read_post=post):
        base.list_admin_notes.invoke({"category": "测试", "keyword": "111111"}, config=cfg())
    check("分类与关键词可以同时给（两个键一起发）",
          post.calls[0][1] == {"categories": "测试", "keyword": "111111"}, str(post.calls))

    # ① 认不出的分类名：**零请求** + 措辞是「没有这个分类」，不是「该分类下没有文章」
    post = _Post([_NOTE_ROW])
    with patch(_admin_read_post=post):
        r = base.list_admin_notes.invoke({"category": "编程笔记集"}, config=cfg())
    check("站内没有这个分类 → unavailable 且**一个请求都不发**",
          r.kind == "unavailable" and post.calls == [], f"{r.kind}: {r} / {post.calls}")
    check("  措辞说的是「没有叫…的分类」，绝不许说成「该分类下没有文章」",
          "没有叫「编程笔记集」的分类" in r and "分类下没有文章" not in r, str(r))
    check("  近失候选点名给出来（当一次可核对的追问，不替主人认定）",
          "编程笔记" in str(r) and "完整名字" in str(r), str(r))

    post = _Post([_NOTE_ROW])
    with patch(_admin_read_post=post,
               _category_index=lambda c: A.build_category_index(
                   [{"categoryKey": 9, "categoryTitle": "测试", "noteCount": 1},
                    {"categoryKey": 10, "categoryTitle": "测试", "noteCount": 2}])):
        r = base.list_admin_notes.invoke({"category": "测试"}, config=cfg())
    check("同名两个分类 → unavailable 且零请求（分类名没有唯一约束，重名是真会出现的）",
          r.kind == "unavailable" and post.calls == [] and "无法确定" in r,
          f"{r.kind}: {r}")

    # ② 分类在、篇数也是 0 ⇒ 这才是「该分类下没有文章」，用 empty
    post = _Post([])
    with patch(_admin_read_post=post):
        r = base.list_admin_notes.invoke({"category": "测试"}, config=cfg())
    check("名册 0 篇 + 筛出来空 → empty「该分类下没有文章（篇数 0）」",
          r.kind == "empty" and "分类下没有文章" in r and "篇数 0" in r, f"{r.kind}: {r}")

    # ③ 名册说 N≥1、筛出来却空 ⇒ 这是「这一次没读到」，不是「没有」（缺数 ≠ 零）
    post = _Post([])
    with patch(_admin_read_post=post,
               _category_index=lambda c: A.build_category_index(
                   [{"categoryKey": 9, "categoryTitle": "测试", "noteCount": 6}])):
        r = base.list_admin_notes.invoke({"category": "测试"}, config=cfg())
    check("名册 6 篇却筛出空 → unavailable（明写「不是该分类下没有文章」，是没读到）",
          r.kind == "unavailable" and "写着 6 篇" in r and "这一次没读到" in r
          and "这不是「该分类下没有文章」" in r, f"{r.kind}: {r}")

    # ④ 影子行（编辑修改稿）不是文章：清单端点滤了、检索端点只在「公开文章」页签滤
    post = _Post([_NOTE_ROW,
                  {**_NOTE_ROW, "noteKey": 46, "draftOf": 9, "noteTitle": "编辑修改稿"},
                  {"noteKey": 47, "noteTitle": "没有 draftOf 键的行", "status": "public",
                   "isTop": 0, "noteTags": ""}])
    with patch(_admin_read_post=post):
        r = base.list_admin_notes.invoke({"category": "测试"}, config=cfg())
    check("draftOf 非空（编辑修改稿）不进清单——头行那句「不在其中」才是真话",
          "noteId=46" not in r, str(r))
    check("**键缺席的行留着**（没带这个信息 ⇒ 不拿它当「不是影子行」的反面）",
          "noteId=47" in r, str(r))

    # ⑤ 没给任何筛 → 照旧走清单端点（老路径逐字不变）
    class _Get:
        def __init__(self, ret):
            self.ret = ret
            self.paths: list[str] = []

        def __call__(self, path, config):
            self.paths.append(path)
            return self.ret

    get = _Get([_NOTE_ROW])
    with patch(_admin_get=get):
        r = base.list_admin_notes.invoke({}, config=cfg())
    check("两个筛都不给 → 仍走清单端点 `/notes/list`",
          get.paths == ["/api/protected/notes/list"] and "后台文章共 1 篇" in r,
          f"{get.paths} / {r}")


settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
