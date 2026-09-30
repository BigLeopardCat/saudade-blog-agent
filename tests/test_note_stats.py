# -*- coding: utf-8 -*-
"""文章流量（阅读/点赞/收藏）报表单测（纯函数、零网络、零 LLM，秒级）。

被测四块：
  · `agent/reports.py::render_note_stats` —— 三张榜的渲染与**三态**话术；
  · `agent/entities.py::_note_traffic_digest` —— 落进跨轮执行记忆的那一行；
  · `tools/base.py` —— 列表瘦身（`_slim_note_rows`）与单篇读数（`_note_public_counts`）；
  · 接线（scope / 菜单 / 动作词 / 技能）与**跨语言守卫**（Rust 侧键名与 `TOP_N`）。

这一批与别的报表最大的不同是**数据是"很多行 + 名次"**，于是判据多出四条：

  1. **名次按下标印**：后端给的数组顺序就是名次，Python 侧只按下标编号，绝不重排
     ——排名是后端算好的事实，重排就等于替站内重新裁决；
  2. **三张榜各自独立**：同一篇文章在两张榜上名次可以不同，标题与计数**不许串榜**
     （点赞榜首的行里也印着"阅读 N"，按整段文搜数就会把点赞第一名报成阅读榜首）；
  3. **"名次只列到前 N" ≠ "全站就这些"**：榜给满时报表必须写明"最多只列到这里"，
     否则 narrator 会照着十行说"全站就这十篇"（那是替站内下一个假的"没有"结论）；
  4. **缺键 ≠ 0**（跨语言同一条取向）：读不到的项整段不印或写"本次没读到"，绝不写 0。

⚠️ 本文件不连网、不碰生产：`_get` 那一层用假 httpx 客户端桩边界（同 test_reports.py ⑩）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from agent import authz  # noqa: E402
from agent import reports as R  # noqa: E402
from agent.entities import receipt_digest  # noqa: E402
import tools.base as base  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
import _parent_repo  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _row(nid, title, views=0, likes=0, favorites=0):
    return {"noteId": nid, "title": title, "views": views, "likes": likes,
            "favorites": favorites}


def _report(**over):
    """一份"三项齐全"的报表（形态抄自 Rust `NoteStatsReportDto` 的序列化结果）。

    注意三张榜的**榜首是三篇不同的文章**、且点赞榜首那一行里也带着"阅读 40"
    ——这是刻意的：串榜类缺陷（按整段文找数）会在这里露馅。
    """
    data = {
        "generatedAt": "2026-09-30 12:00",
        "totalViews": 1234,
        "totalLikes": 56,
        "totalFavorites": 7,
        "topViewed": [_row(1, "架构", 120, 40, 3), _row(2, "B", 90, 5, 1)],
        "topLiked": [_row(3, "C", 40, 9, 1)],
        "topFavorited": [_row(4, "D", 5, 2, 5)],
        "daily": [{"date": "2026-09-28", "views": 7, "likes": 0},
                  {"date": "2026-09-29", "views": 10, "likes": 1},
                  {"date": "2026-09-30", "views": 0, "likes": 0}],
    }
    data.update(over)
    return data


def _line(text, head):
    """取以 `head` 开头的那一行（取不到返回空串，让断言报 ❌ 而不是抛栈）。"""
    for ln in (text or "").split("\n"):
        if ln.startswith(head):
            return ln
    return ""


def _block(text, head):
    """取以 `head` 开头的**一整块**（头行 + 紧随其后的 `  · ` 行）。

    串榜类断言必须按块比：`_line` 只回头行，而"榜的第 1 名是谁"在行里。
    """
    lines = (text or "").split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(head):
            j = i + 1
            while j < len(lines) and lines[j].startswith("  · "):
                j += 1
            return "\n".join(lines[i:j])
    return ""


# ══════════════════════════════════════════════════════════════════
print("\n① 渲染：合计行（用服务端给的聚合值，绝不拿榜上的行相加）")

txt = R.render_note_stats(_report(), now=None)
total = _line(txt, "- 全站合计")
check("合计行印三个聚合值（阅读/点赞/收藏）",
      "阅读 1234" in total and "点赞 56" in total and "收藏 7" in total, total)
check("★ 合计**不是**榜上行相加（榜只有前 10 名，加起来只会比真值小）",
      "1234" in total and str(120 + 90) not in total, total)

txt = R.render_note_stats(_report(totalViews=9999), now=None)
check("合计与榜上的行不一致时，印的是合计那个数",
      "阅读 9999" in _line(txt, "- 全站合计"), _line(txt, "- 全站合计"))

txt = R.render_note_stats({"topViewed": []}, now=None)
check("三个合计键都缺席 → 「本次没读到合计值」（不是 0）",
      "本次没读到合计值" in _line(txt, "- 全站合计") and "阅读 0" not in txt,
      _line(txt, "- 全站合计"))

txt = R.render_note_stats(_report(totalViews=True, totalLikes="56", totalFavorites=None),
                          now=None)
check("bool 与字符串不是计数（`True` 是 `int` 的子类，会伪装成 1）",
      "本次没读到合计值" in _line(txt, "- 全站合计"), _line(txt, "- 全站合计"))


print("\n② 渲染：三张榜（顺序即名次，逐条印「第 N 名」）")

txt = R.render_note_stats(_report(), now=None)
check("阅读榜逐条印名次与标题、noteId",
      "第 1 名 《架构》（noteId 1）" in txt and "第 2 名 《B》（noteId 2）" in txt,
      _line(txt, "- 阅读榜"))
check("★ 名次就是数组下标（顺序原样透传，不重排）",
      txt.index("第 1 名 《架构》") < txt.index("第 2 名 《B》"))
check("每行带上三个数（看榜的人下一个问题必然是「那篇的赞/收藏呢」）",
      "阅读 120／点赞 40／收藏 3" in txt, _line(txt, "  · 第 1 名"))
check("榜按哪个数排，那个数就排在该行最前（阅读榜行首是「阅读」，点赞榜是「点赞」）",
      "点赞 9／阅读 40／收藏 1" in txt, _line(txt, "  · 第 1 名 《C》"))
check("三张榜都在", all(f"- {b}榜" in txt for b in ("阅读", "点赞", "收藏")))

check("★ 不满 N 行时如实说「全站共 N 篇」（这时才是真的只有这些）",
      "全站共 2 篇有阅读记录" in txt, _line(txt, "- 阅读榜"))
check("  给满 N 行的榜**不出现**「全站共」那句",
      "全站共 1 篇有点赞记录" in txt, "（点赞榜只有 1 行，本就该出现）")
_full = _report(topViewed=[_row(i, f"N{i}", 100 - i) for i in range(1, R._RANK_TOP + 1)])
txt = R.render_note_stats(_full, now=None)
check(f"★ 给满 {R._RANK_TOP} 行 → 「下列前 {R._RANK_TOP} 名，报表最多只列到这里」",
      f"下列前 {R._RANK_TOP} 名，报表最多只列到这里" in txt, _line(txt, "- 阅读榜"))
check("  给满时**不说**「全站共 N 篇」（那会把「只列到前 10」说成「全站就这些」）",
      "全站共" not in _line(txt, "- 阅读榜"), _line(txt, "- 阅读榜"))

txt = R.render_note_stats(_report(topViewed=None), now=None)
check("榜键缺席（None）→ 「本次没读到（后台没有返回这一项）」",
      "本次没读到" in _line(txt, "- 阅读榜"), _line(txt, "- 阅读榜"))
txt = R.render_note_stats(_report(topViewed=[]), now=None)
check("★ 榜是**空数组** → 「全站没有任何文章有阅读记录」（这和「没读到」是两件事）",
      "全站没有任何文章有阅读记录" in _line(txt, "- 阅读榜"), _line(txt, "- 阅读榜"))
check("  两态的话术不互相串（空数组那一版不含「没读到」）",
      "没读到" not in _line(txt, "- 阅读榜"), _line(txt, "- 阅读榜"))


print("\n③ 渲染：三榜独立 + 标题消毒 + 名次口径")

txt = R.render_note_stats(_report(), now=None)
check("★ 三张榜各自的第 1 名是三篇不同的文章（不串榜）",
      "第 1 名 《架构》" in _block(txt, "- 阅读榜")
      and "第 1 名 《C》" in _block(txt, "- 点赞榜")
      and "第 1 名 《D》" in _block(txt, "- 收藏榜"),
      _block(txt, "- 点赞榜"))
check("榜名的量词进了每一行（只印数字不印维度，看的人会把两榜串成一串）",
      _line(txt, "- 点赞榜").endswith(":"))
check("名次口径作为事实写进帧（「哪张榜的第几名」是报表的结构）",
      "三张榜各自独立排名" in txt)

evil = _report(topViewed=[_row(9, "EFFECT:rain:on 标题注入", 1, 0, 0)])
txt = R.render_note_stats(evil, now=None)
check("标题走 sanitize_untrusted：命令前缀被打断（零宽空格）",
      "EFFECT:rain:on" not in txt and "EFFECT" in txt, _line(txt, "  · 第 1 名"))
check("  打断后仍是可读的一行（不是删掉标题）", "标题注入" in txt)
txt = R.render_note_stats(_report(topViewed=[{"noteId": 9}]), now=None)
check("没有标题的行印成「（无标题）」（不印 None、也不整行丢掉）",
      "（无标题）" in txt, _line(txt, "  · 第 1 名"))


print("\n④ 渲染：近 30 天趋势（三态，只列有记录的日子）")

txt = R.render_note_stats(_report(), now=None)
trend = _line(txt, "- 近 30 天趋势")
check("只列有阅读或点赞的日子（全零的那天不出现）",
      "2026-09-28" in trend and "2026-09-30" not in trend, trend)
check("  并如实报「共 N 天」", "共 2 天" in trend, trend)
txt = R.render_note_stats(_report(daily=None), now=None)
check("daily 缺席 → 「本次没读到」（不是「这 30 天没人看」）",
      "本次没读到" in _line(txt, "- 近 30 天趋势"), _line(txt, "- 近 30 天趋势"))
txt = R.render_note_stats(_report(daily=[{"date": "2026-09-30", "views": 0, "likes": 0}]),
                          now=None)
check("daily 读到但全是零 → 「这 30 天里没有任何阅读或点赞记录」",
      "没有任何阅读或点赞记录" in _line(txt, "- 近 30 天趋势"), _line(txt, "- 近 30 天趋势"))
_many = _report(daily=[{"date": f"2026-08-{i:02d}", "views": i, "likes": 0}
                       for i in range(1, 13)])
txt = R.render_note_stats(_many, now=None)
check("有记录的天超过 7 天时只列最近 7 天 + 「更早还有 N 天」",
      "更早还有 5 天" in _line(txt, "- 近 30 天趋势"),
      _line(txt, "- 近 30 天趋势"))

check("空报表也能渲染（不抛栈，走「没读到」那一路）",
      isinstance(R.render_note_stats({}, now=None), str))
check("整张报表不超 MAX_REPORT_CHARS + 截断提示",
      len(R.render_note_stats(
          _report(topViewed=[_row(i, "长标题" * 30, i) for i in range(1, 11)]),
          now=None)) <= R.MAX_REPORT_CHARS + 40)


print("\n⑤ 实体摘要 `_note_traffic_digest`（跨轮唯一能照抄的取值来源）")

d = receipt_digest("get_note_stats", R.render_note_stats(_report(), now=None))
check("★ 摘要是「合计 + 三张榜各自的榜首」（榜首带标题，下一轮问「第一名那篇」才答得出）",
      d == "文章流量: 阅读合计 1234、点赞 56；阅读榜首《架构》120；点赞榜首《C》9；"
           "收藏榜首《D》5", d)
check("★ 三榜不串（点赞榜首的行里也印着「阅读 40」，按整段文搜数会把 C 报成阅读榜首）",
      "阅读榜首《架构》" in d and "阅读榜首《C》" not in d, d)
d2 = receipt_digest("get_note_stats", R.render_note_stats(_report(topViewed=None), now=None))
check("★ 缺一张榜时**不为它编一句**（只有点赞/收藏两个榜首）",
      "阅读榜首" not in d2 and "点赞榜首《C》9" in d2, d2)
d3 = receipt_digest("get_note_stats",
                    R.render_note_stats(_report(topViewed=[], topLiked=[], topFavorited=[]),
                                        now=None))
check("榜为空（真的没记录）时也只留合计，不编榜首",
      d3 == "文章流量: 阅读合计 1234、点赞 56", d3)
check("合计也读不到 → 空摘要（退化为只有动作行，不编）",
      receipt_digest("get_note_stats", "文章流量报表（2026-09-30 12:00）\n- 阅读榜：本次没读到") == "")
check("空输入 → 空摘要", receipt_digest("get_note_stats", "") == "")
check("摘要不超 150 字（Rust detail 列约束）", len(d) <= 150, str(len(d)))


print("\n⑥ 列表瘦身 `_slim_note_rows`：三个计数原样带上，缺键不带、脏值不转")

slim = base._slim_note_rows([{"noteKey": 1, "noteTitle": "A", "views": 120, "likes": 8,
                              "favorites": 2}])
check("三个计数进了精简行", slim and slim[0].get("views") == 120
      and slim[0].get("likes") == 8 and slim[0].get("favorites") == 2, str(slim))
slim = base._slim_note_rows([{"noteKey": 1, "noteTitle": "A", "views": 120}])
check("★ 缺的键**不出现**（不是补 0——「没读到」与「是 0」必须分得开）",
      slim and "likes" not in slim[0] and "favorites" not in slim[0]
      and "views" in slim[0], str(slim))
slim = base._slim_note_rows([{"noteKey": 1, "noteTitle": "A", "views": True,
                              "likes": "8", "favorites": None}])
check("bool / 字符串 / None 都不是数（把 \"8\" 转成 8 属于编数）",
      slim and "views" not in slim[0] and "likes" not in slim[0]
      and "favorites" not in slim[0], str(slim))
check("非 note 行（没有 noteKey）→ 不套这把刀（返回 None 原样透出）",
      base._slim_note_rows([{"talkKey": 1, "content": "留言"}]) is None)


print("\n⑦ 单篇读数 `_note_public_counts`：读它不计数，读不到就是空 dict")


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """按 URL 路由的假客户端（`/notes/:id/stats` 与 `/notes/:id` 各回一份）。"""

    def __init__(self, by_path):
        self.by_path = by_path
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for frag, resp in self.by_path.items():
            if url.endswith(frag):
                return resp
        return _Resp(404, None)


real_client = base._client
try:
    base._client = _Client({"/notes/7/stats": _Resp(200, {"code": 200, "data": {
        "noteId": 7, "views": 120, "likes": 8, "favorites": 2, "liked": True}})})
    counts = base._note_public_counts(7)
    check("读到三个计数", counts == {"views": 120, "likes": 8, "favorites": 2}, str(counts))
    check("★ `liked` 丢掉（本工具以服务身份读，带上它只会是恒 false 的假事实）",
          "liked" not in counts)

    base._client = _Client({"/notes/7/stats": _Resp(404, None)})
    check("★ 404（草稿/私密文章在这个公开端点上就是查无此篇）→ 空 dict，"
          "调用方据此让键**不出现**（不是 0）",
          base._note_public_counts(7) == {})

    base._client = _Client({"/notes/7/stats": _Resp(200, {"code": 500, "message": "boom"})})
    check("业务码非 200（故障）→ 空 dict（读数失败绝不冒充成 0）",
          base._note_public_counts(7) == {})

    base._client = _Client({"/notes/7/stats": _Resp(200, {"code": 200, "data": {
        "views": True, "likes": "8"}})})
    check("脏值同样一个都不带", base._note_public_counts(7) == {})

    # 详情：note 分支把读数并进去（**同一份 data 上**，不是另起一个字段）
    base._client = _Client({
        "/notes/7/stats": _Resp(200, {"code": 200, "data": {
            "views": 120, "likes": 8, "favorites": 2, "liked": False}}),
        "/notes/7": _Resp(200, {"code": 200, "data": {
            "noteKey": 7, "noteTitle": "架构", "content": "正文"}}),
    })
    out = str(base.get_article_detail.invoke({"article_id": 7, "doc_type": "note"}))
    # 帧是 `_shape` 出来的 Python 字面量（单引号），不是 JSON——按实际形态断言
    check("详情帧带上三个读数", "'views': 120" in out and "'likes': 8" in out
          and "'favorites': 2" in out, out[:200])
    check("  且不带 `liked`（假事实不出版）", "liked" not in out)

    # 读数端点读不到时，详情照常给（键不出现 = 没读到，不是"三个 0"）
    base._client = _Client({
        "/notes/7/stats": _Resp(404, None),
        "/notes/7": _Resp(200, {"code": 200, "data": {
            "noteKey": 7, "noteTitle": "架构", "content": "正文"}}),
    })
    out = str(base.get_article_detail.invoke({"article_id": 7, "doc_type": "note"}))
    check("★ 读数读不到时详情**照常返回正文**，只是三个键一个都不出现",
          "架构" in out and "'views'" not in out and "'likes'" not in out
          and "'favorites'" not in out, out[:200])
    check("  也**没有**把故障说成「没有读数」之外的话（不拦截、不报错）",
          "站内没有" not in out)
finally:
    base._client = real_client


print("\n⑧ 接线：scope / 菜单 / 白名单 / 动作词 / 技能（改坏了这几处就静默失效）")

from agent.action_text import tool_action_text  # noqa: E402
from agent.graph import _CONTENT_TOOLS, _TOOL_MENU_LINES, _tools_desc  # noqa: E402
from agent.skills import SKILL_MAP, build_planner_context, callable_query_tools  # noqa: E402
from agent.principal import ADMIN_ROLES  # noqa: E402

TOOL = "get_note_stats"
check("scope 是 admin.console（数据在 protected_routes 后，agent 以发起人身份代调）",
      authz.TOOL_SCOPE.get(TOOL) == authz.SCOPE_ADMIN_CONSOLE, str(authz.TOOL_SCOPE.get(TOOL)))
check("访客/普通用户结构上点不到（不在公开点名白名单里）",
      not any(authz.check(p, TOOL).allowed for p in (None,)) and
      TOOL not in callable_query_tools(None))
check("管理员可经点名通道直接调用", TOOL in callable_query_tools("admin"))
check("在注册表里", TOOL in {t.name for t in base._TOOL_REGISTRY})
check("进了 _CONTENT_TOOLS（否则报表轮的『暂无』会被判成凭空结论）",
      TOOL in _CONTENT_TOOLS)
check("planner 菜单里有一行（否则模型不知道有这件工具）",
      bool(_TOOL_MENU_LINES.get(TOOL)) and TOOL in _tools_desc("admin"))
check("动作词是中文（不是 `执行 get_note_stats`）",
      tool_action_text(TOOL, {}) != f"执行 {TOOL}", tool_action_text(TOOL, {}))

sk = SKILL_MAP.get("traffic_report")
check("技能 traffic_report 在位，计划就是这一件工具",
      sk is not None and [t for t, _ in sk.plan] == [TOOL], str(sk and sk.plan))
check("技能能力措辞点明三张榜（capability 是判据的一部分，见批 G 的教训）",
      sk is not None and "排行" in sk.capability, sk and sk.capability)
check("技能只对管理员族可见（admin + superadmin）",
      sk is not None and sk.roles == ADMIN_ROLES, str(sk and sk.roles))
check("非 admin 的 planner 上下文里看不到这张技能",
      "traffic_report" not in build_planner_context("user")
      and "traffic_report" not in build_planner_context(None))
check("admin 看得到", "traffic_report" in build_planner_context("admin"))
check("reply_contract 不点**别的工具名**（点名兄弟工具会被工具名锁判红，"
      "也会把模型引到那条路上）",
      sk is not None and not any(t in sk.reply_contract
                                 for t in ("get_article_detail", "list_notes", "search_notes")))


print("\n⑨ 跨语言守卫：Rust 侧键名与榜长（改了那边这边就静默错）")

_rs = _parent_repo.read(
    "src/routes/note_stats.rs",
    why="报表的**键名**是 Python 与 Rust 之间的契约：`render_note_stats` 按 "
        "`totalViews`/`topViewed` 取值，Rust 改一次 rename 就会让整张报表变成"
        "「本次没读到」（静默，两端各自测试都绿）")
if _rs is not None:
    for key in ("generatedAt", "totalViews", "totalLikes", "totalFavorites",
                "topViewed", "topLiked", "topFavorited"):
        check(f"Rust 报表 DTO 有键 `{key}`", f'rename = "{key}"' in _rs)
    for key in ("noteId", "views", "likes", "favorites"):
        check(f"Rust 榜行有键 `{key}`",
              f'rename = "{key}"' in _rs or f"pub {key}:" in _rs)
    check("`daily` 行有 date/views/likes",
          "pub date: String" in _rs and "pub views: i64" in _rs and "pub likes: i64" in _rs)
    import re as _re
    m = _re.search(r"const TOP_N: usize = (\d+);", _rs)
    check(f"★ Rust `TOP_N` 与 Python `_RANK_TOP` 同值（报表那句「下列前 N 名」按它写）",
          m is not None and int(m.group(1)) == R._RANK_TOP,
          f"Rust={m and m.group(1)}；Python={R._RANK_TOP}")
    check("三张榜各截到 TOP_N（`rank()` 一处截断，三榜同一个函数）",
          _rs.count("rows.truncate(TOP_N)") == 1)

_notes = _parent_repo.read(
    "src/routes/notes.rs",
    why="列表接口的 `views`/`likes`/`favorites` 是**可选键**（`skip_serializing_if`）"
        "——Python 侧据此把「没读到」与「是 0」分开；若那边改成必填并补 0，"
        "看板娘就会把「这个数没读到」说成「就是 0」")
if _notes is not None:
    check("★ 列表行的三个计数都是 Option + skip_serializing_if（缺键就不出现）",
          _notes.count('skip_serializing_if = "Option::is_none"') >= 3
          and "pub favorites: Option<i64>" in _notes)
    check("attach_stats 三个数一起填（读不到就三个都不填——失败只记一行 warn）",
          "dto.views = Some(c.views)" in _notes and "dto.likes = Some(c.likes)" in _notes
          and "dto.favorites = Some(c.favorites)" in _notes)

_dto = _parent_repo.read(
    "src/routes/note_stats.rs", why="单篇读数 DTO（`GET /notes/:id/stats`）的三个计数键")
if _dto is not None:
    i = _dto.find("pub struct NoteStatsDto")
    seg = _dto[i:i + 900] if i >= 0 else ""
    check("NoteStatsDto 有 views/likes/favorites 三个计数",
          all(f"pub {k}: i64" in seg for k in ("views", "likes", "favorites")), seg[:120])
    check("  `liked` 也在这个 DTO 里（Python 侧刻意丢掉它——它是「这位访客点过没有」）",
          "pub liked: bool" in seg)


print(f"\n{'全部通过' if not FAILS else f'{len(FAILS)} 项不符'}")
for f in FAILS:
    print(f"  ✗ {f}")
sys.exit(1 if FAILS else 0)
