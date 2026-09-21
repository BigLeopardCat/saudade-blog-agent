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

用法：.venv/bin/python test_admin_write.py
"""
import base64
import contextlib
import json
import sys
import time
from pathlib import Path

from langchain_core.messages import HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent.graph as g  # noqa: E402
from agent import adminops as A  # noqa: E402
from agent import authz  # noqa: E402
from agent.graph import (EXECUTED_ONCE_SKILLS, SNAPSHOT_SKILLS,  # noqa: E402
                         _CONTENT_TOOLS, _already_done_writes, _check_spec,
                         execute_node, plan_encode)
from agent.principal import ROLE_ADMIN, ROLE_SECRETARY, ROLE_USER, Principal  # noqa: E402
from agent.skills import instantiate_plan  # noqa: E402
import tools.base as base  # noqa: E402

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


print("\n⑤ 纯函数：渲染（清单喂 id、变更只讲真动了的字段）")

check("只改置顶 → 前后串里不出现状态",
      A.render_change([("未置顶", "置顶")]) == ("未置顶", "置顶"))
check("状态+置顶 → 两条都在", A.render_change([("私密", "公开"), ("未置顶", "置顶")])
      == ("私密 / 未置顶", "公开 / 置顶"))
check("回执字段截断带省略号（detail 列宽有限）",
      A.clip("x" * 80) == "x" * 60 + "…" and A.clip("ok") == "ok")
_list = A.render_admin_notes([note(12, "架构文档", "draft", 1)], IDX)
check("后台清单带 id 与状态标记（planner 只有这一轮真读到 id 才有据可写）",
      _list.startswith("后台文章共 1 篇") and "id=12 [草稿/置顶]《架构文档》" in _list, _list)
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
    def __init__(self, resp=None, exc=None):
        self.calls = []
        self.resp, self.exc = resp, exc

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append((url, headers or {}, json))
        if self.exc:
            raise self.exc
        return self.resp


real_client = base._client
try:
    c = _Client(_Resp(200, {"code": 200, "data": "10002"}))
    base._client = c
    out = base._admin_post("/api/protected/tagone", {"title": "X"}, cfg(7))
    check("成功 → 返回 data 字段", out == "10002", str(out))
    url, hdrs, payload = c.calls[0]
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
    # 字符串型非法值到不了这一层（pydantic 先按 int|None 校验，见 §⑭）——这里测的是
    # 类型合法但取值非法的 0/负数。
    r = base.create_tag.invoke({"title": "X", "parent_id": 0}, config=cfg())
    check("父 id 为 0 → unavailable（同一套「必须是正整数」判据）",
          r.kind == "unavailable" and "不合法" in r and post.calls == [], str(r))

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
    r = base.create_tag.invoke({"title": "Pytho", "parent_id": 1}, config=cfg())
    check("新建二级：走 tagtwo、fatherTag 是父 id（不是父名）",
          post.calls == [("/api/protected/tagtwo",
                          {"title": "Pytho", "color": A.color_for_name("Pytho"),
                           "fatherTag": 1})], str(post.calls))

post = _Post("10002")
with patch(_tag_index=_Seq(IDX, IDX), _admin_post=post):
    r = base.create_tag.invoke({"title": "Pytho", "parent_id": 10000}, config=cfg())
    check("父 id 不是一级标签 → unavailable，零 POST",
          r.kind == "unavailable" and post.calls == [] and "不是一级标签" in r, str(r))

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
    check("正常路径：**只发 status**（不发 title/content/isPublic/updateTime）",
          post.calls == [("/api/protected/notes/12", {"status": "public"})], str(post.calls))
    check("  回执 meta：op / article_id / 前后值",
          r.meta == {"op": "set_status", "article_id": 12,
                     "before": "私密", "after": "公开"}, str(r.meta))
    check("  人话里带《标题》与前后值（narrator 照它转述）",
          "《架构》" in r and "私密 → 公开" in r and "复核" in r, str(r))
    check("  **标题不进 meta**（回执注入下一轮，带标题会被读成「我读过这篇」）",
          "架构" not in json.dumps(r.meta, ensure_ascii=False), str(r.meta))

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
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = skill
    obj["tools"] = tools_list
    return plan_encode(obj)


def _run(tools_list, msg, config, extra_msgs=(), skill="article_status"):
    CALLS.clear()
    return execute_node({"plan": _plan(tools_list, skill),
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
    check("  回执键集固定（多出来的键是无声的兼容性债）",
          set(rcpt) == {"skill", "tool", "args", "result", "ts",
                        "principal_role", "op", "article_id", "before", "after"},
          str(sorted(rcpt)))
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

gsrc = (Path(__file__).resolve().parent / "agent" / "graph.py").read_text(encoding="utf-8")
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
_typed = execute_node({"plan": _plan(['set_article_status({"article_id": 12, "is_top": "也许"})']),
                       "plan_rounds": 1, "done": False,
                       "messages": [HumanMessage(content="把文章 12 置顶")]},
                      cfg())
_ftxt = str(_typed["messages"][-1].content)
check("字符串塞进 int 参数（planner 抄错类型）→ __ERROR__ 帧而不是抛异常",
      _ftxt.startswith("__ERROR__") and _typed["receipts"] == [], _ftxt[:80])
check("  该帧不进回执、进 blocked（planner 按规则 5 改参重试）",
      _typed["blocked"] and _typed["blocked"][0]["reason"] == "error_frame",
      str(_typed["blocked"]))

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
