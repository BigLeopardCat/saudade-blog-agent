# -*- coding: utf-8 -*-
"""帧里 id 带命名空间（20260926 批 4）：裸 id 一律不许出现在模型读得到的渲染文本里。

**为什么值得单开一个套件**：帧里同时住着好几个 id 命名空间——文章 `noteId`、
留言 `talkId`、账号 `userId`、通知 `notifId`、站内信 `mailId`——而它们的形态
全是裸数字。trace `20260924T030031` 实证：通知帧同时给出留言 id `96` 和一个链接
`/guestbook?lid=96`，帧里没有任何东西说明 `96` 是**留言**的 id，planner 于是拿它
去调 `get_article_detail`，回来只能说"这两条拿不到内容"。这不是模型笨，是**帧没
说清**——id 不带来源时，"这个数字是哪种物件"只能靠猜。

三组锁：
  ① 渲染值：每个渲染点都产出 `命名空间:id`（逐点一条，漏一个就是留一处猜测）；
  ② 反向：**同一个数字不带命名空间时不许出现**。只做正向的话，"把名字去掉"
     这种回退照样绿（测试里那条 `talkId:96` 是字符串包含，去掉名字后的 `#96`
     恰好不被任何断言盯住）——`_bare` 用负向后顾把这条钉住；
  ③ 源码级：这几处渲染的实现里必须写着命名空间字面量，且没有 `f"#{` 这种旧形。

**注意 `noteId=19` 里那个是大写 I**：所以 "id=19" 不是它的子串，拿旧形做负断言
会**恒真/恒假而不报错**（本批实测：test_skills 里那批负断言改名后全变成空转）。
本套件里所有 id 比对都走 `_bare()`，它按"紧邻字符是不是命名空间分隔符"判，
不靠子串。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

from agent import adminops as A  # noqa: E402
from agent import context as C  # noqa: E402
from agent import reports as R  # noqa: E402
from agent.entities import receipt_digest  # noqa: E402
from tools.base import _board_label  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _bare(text: str, n) -> bool:
    """数字 n 在这段文本里**没有**命名空间——紧邻其左的字符既不是 `:`/`=`
    （命名空间分隔符）也不是单词字符。

    `noteId:12` → 前一个字符是 `:` ⇒ 不算裸；`#12` / `12《标题》` / `12 小舟`
    ⇒ 算裸（旧形，回退时抓住它）。用负向后顾而不是子串，是因为 `noteId:12` 本身
    就含子串 `12`（甚至含 `Id:12`），子串法两个方向都判不准。
    """
    return re.search(r"(?<![\w:=])" + re.escape(str(n)) + r"(?![\w])", text) is not None


# ══════════════════════════════════════════════════════════════════
print("\n① 留言指称（问句/明细/回执行）：talkId / userId")

ROW = {"talkKey": 96, "userId": 3, "author": "小舟", "createTime": "2026-09-21 21:00:00",
       "content": "谢谢站长的分享！"}

lab = _board_label(ROW)
check("list_guestbook 的问句指称带 talkId", "talkId:96 " in lab, lab)
check("  且裸 96 不再出现（同一个数字只以一种身份露面）", not _bare(lab, 96), lab)

NOBODY = {"talkKey": 97, "userId": 42, "author": "", "nickname": "",
          "createTime": "2026-09-21 21:00:00"}
lab2 = _board_label(NOBODY)
check("作者名缺失时的兜底是 userId:42（不是「用户#42」）",
      "userId:42" in lab2 and not _bare(lab2, 42), lab2)

det = R._detail_line(ROW, "AI通过")
check("报表明细行带 talkId", "  · talkId:96 " in det, det)
check("  且裸 96 不再出现", not _bare(det, 96), det)

det2 = R._detail_line({**ROW, "author": ""}, "人工已驳回")
check("明细行的作者兜底同样具名", "userId:3" in det2 and not _bare(det2, 3), det2)

ref = A._board_ref(ROW)
check("回执行里的留言指称带 talkId（跨轮记忆唯一的认物）",
      ref.startswith("talkId:96 ") and not _bare(ref, 96), ref)

from datetime import datetime  # noqa: E402

urow = {"id": 1, "name": "", "role": "admin", "conversations": 120, "messages": 3000,
        "lastActiveAt": "2026-09-21 11:30:00"}
usr = R.render_user_stats({"totalUsers": 1, "users": [urow]},
                          now=datetime(2026, 9, 21, 13, 5))
_uline = [ln for ln in usr.splitlines() if ln.startswith("  · ")][0]
check("用户明细行带 userId（名字为空时是 userId:1，不是「用户#1」）",
      "  · userId:1 " in _uline and not _bare(_uline, 1), _uline)

# ══════════════════════════════════════════════════════════════════
print("\n② 实体摘要（跨轮取值来源）：noteId / notifId / mailId")

fav = receipt_digest("list_my_favorites", str([
    {"noteId": 12, "title": "留言板怎么用", "status": "published"}]))
check("收藏摘要带 noteId", "noteId:12" in fav and not _bare(fav, 12), fav)

noti = receipt_digest("list_notifications", str({"unread": 1, "items": [
    {"id": 7, "title": "国庆维护公告", "isRead": False}]}))
check("通知摘要带 notifId（与信的 id 不是同一个空间）",
      "notifId:7" in noti and not _bare(noti, 7), noti)

unread = receipt_digest("get_unread_summary", str(
    {"notifications": 3, "messages": 1, "total": 4,
     "unread_items": [{"id": 7, "title": "国庆维护公告"}]}))
check("未读汇总的条目带 notifId", "notifId:7" in unread and not _bare(unread, 7), unread)

box = receipt_digest("list_my_messages", str({"unread": 1, "outbox": [], "inbox": [
    {"id": 11, "peerName": "小猫咪", "title": "河灯集那篇", "isRead": False}]}))
check("站内信摘要带 mailId", "mailId:11" in box and not _bare(box, 11), box)

box2 = receipt_digest("list_my_messages", str({"unread": 0, "outbox": [], "inbox": [
    {"id": 9, "peerName": "阿岚", "title": None, "isRead": True}]}))
check("无标题信也带 mailId（「（无标题信）」不至于让 id 丢掉）",
      "mailId:9（无标题信）" in box2 and not _bare(box2, 9), box2)

# 缺 id 的条目仍然是"不写编号"（缺字段绝不编）——具名不该把这条例律带偏
noid = receipt_digest("list_notifications", str({"unread": 0, "items": [
    {"title": "没有 id 的通知", "isRead": True}]}))
check("缺 id 仍不编号（具名不等于给每个条目编一个 id）",
      "notifId" not in noid and "没有 id 的通知" in noid, noid)

# ══════════════════════════════════════════════════════════════════
print("\n③ 文档锚点（planner 被授权「直接采用」的那一个 id）：noteId=")

_orig = C._doc_id_lookup
C._doc_id_lookup = lambda t: "13" if t == "TEST8" else ""
try:
    anc = C._doc_anchors([])
    from langchain_core.messages import AIMessage
    anc = C._doc_anchors([AIMessage(content="点这里看：[《TEST8》](https://saudade.site/article/99)")])
finally:
    C._doc_id_lookup = _orig
check("锚点行带 noteId（且语料优先于链接里的 id——这里语料说 13）",
      "《TEST8》 noteId=13" in anc and not _bare(anc, 13), anc)
check("  裸 id= 不再出现（旧形 `《标题》 id=N` 是大写/小写混着的坑）",
      " id=13" not in anc and " id=" not in anc, anc)

# ══════════════════════════════════════════════════════════════════
print("\n④ 接线锁（源码级）：渲染实现里必须写着命名空间，且没有旧形")

_SRC = {
    "tools/base.py": (ROOT / "tools" / "base.py").read_text(encoding="utf-8"),
    "agent/reports.py": (ROOT / "agent" / "reports.py").read_text(encoding="utf-8"),
    "agent/adminops.py": (ROOT / "agent" / "adminops.py").read_text(encoding="utf-8"),
    "agent/entities.py": (ROOT / "agent" / "entities.py").read_text(encoding="utf-8"),
    "agent/context.py": (ROOT / "agent" / "context.py").read_text(encoding="utf-8"),
}


def _body(src: str, fn: str) -> str:
    """顶格函数体（到下一个顶格 def 为止）。"""
    body = src[src.index(f"\ndef {fn}"):]
    return body[:body.index("\ndef ", 10)]


_WIRING = (
    ("tools/base.py", "_board_label", 'f"talkId:'),
    ("tools/base.py", "_board_label", 'f"userId:'),
    ("agent/reports.py", "_detail_line", 'f"  · talkId:'),
    ("agent/adminops.py", "_board_ref", 'f"talkId:'),
    ("agent/entities.py", "_note_digest", 'f"noteId:'),
    ("agent/entities.py", "_notification_digest", 'f"notifId:'),
    ("agent/entities.py", "_unread_digest", 'f"notifId:'),
    ("agent/entities.py", "_mailbox_digest", 'f"mailId:'),
    ("agent/context.py", "_doc_anchors", " noteId="),
)
for _f, _fn, _lit in _WIRING:
    check(f"{_f}::{_fn} 里写着 {_lit}", _lit in _SRC[_f])

# 用户明细那行在 render_user_stats 里（不在 _detail_line）
check("agent/reports.py::render_user_stats 里写着 userId:",
      'f"  · userId:' in _SRC["agent/reports.py"])

# 旧形不许回来：留言族渲染里不得再有 `f"#{` 插值（`#{` 只会是裸 id）
for _f, _fn in (("tools/base.py", "_board_label"), ("agent/reports.py", "_detail_line"),
                ("agent/adminops.py", "_board_ref")):
    _b = _body(_SRC[_f], _fn)
    check(f"{_f}::{_fn} 里没有裸 `#` 插值（回退即红）", 'f"#{' not in _b)
# 子串法在这里判不准（`f"noteId:{nid}《` 里本来就含 `{nid}《`）⇒ 钉的是"模板以 id 起头"
_ENT = _SRC["agent/entities.py"]
check("实体摘要里没有以 id 起头的条目模板（`f\"{nid}《` 这种旧形，回退即红）",
      re.search(r'f"\{(?:nid|mid)\}《', _ENT) is None)

# 卡面刻意不具名：确认弹窗的抬头上给人看的是 `#12「原文」`（tools/base.py 的
# `_board_label` 是给模型看的、adminops 的弹窗抬头是给人看的——两处形态不同是对的，
# 但这条差异得写在测试里，否则下一个人会"顺手统一"）
check("弹窗卡面仍是 `#{talkKey}`（人眼看的 UX 面，本批刻意不动）",
      'head = f"#{hit' in _SRC["agent/adminops.py"])

print("\n⑤ 帧出口改名（20260928）：同一个数字只以一种身份露面")

from tools import base as _B  # noqa: E402

_saved_get = _B._get
try:
    _B._get = lambda p, **kw: [{"talkKey": 96, "noteKey": 96, "author": "小舟",
                               "content": "谢谢站长的分享！", "approved": 1}]
    _frame = _B.list_guestbook.invoke({})
finally:
    _B._get = _saved_get

check("公开留言帧里是 `talkId` 而不是上游字段名 `talkKey`",
      "'talkId': 96" in _frame and "talkKey" not in _frame, _frame[:120])
check("  **递归**改名：包了一层的响应也不会漏（漏掉的那处又变成两个名字）",
      "noteId" in str(_B._label_id_keys({"data": {"records": [{"noteKey": 7}]}}))
      and "noteKey" not in str(_B._label_id_keys({"data": {"records": [{"noteKey": 7}]}})))
check("只改键名，不动值（值仍是上游给的那个）",
      _B._label_id_keys({"talkKey": 96}) == {"talkId": 96})
check("取值失败哨兵原样透出（ToolResult 不是行数据，不许被当成 dict 拆）",
      _B._shape(_B.UPSTREAM_DOWN) is _B.UPSTREAM_DOWN)
import ast as _ast  # noqa: E402

_note = _ast.literal_eval(_frame)
check("帧仍然 `literal_eval` 得动（注记在 list **里面**，不是拼在文本外面）",
      isinstance(_note, list) and isinstance(_note[0], dict), str(type(_note)))
check("注记确实在帧里（narrator 读的是全文，边界要有出处）",
      any("已通过审核" in str(x) for x in _note), str(_note)[-90:])
check("注记**不占下标**：`[0]` 仍是第一条留言",
      isinstance(_note[0], dict) and _note[0].get("talkId") == 96)
_rows_kept = [x for x in _note if isinstance(x, dict)]
from agent.entities import _rows as _ent_rows  # noqa: E402
check("实体摘要器跳过注记元素（非 dict 元素天然被过滤）",
      _ent_rows(_note) == _rows_kept, str(_ent_rows(_note)))
# ⚠ 模型读的是 `.description` 而**不是** `.__doc__`：`@tool` 把函数包成 StructuredTool
# 实例后，实例的 `__doc__` 是 pydantic 那个类自己的文档（实测恰好是
# `"Tool that can operate on any number of inputs."`）。钉 `__doc__` 等于钉一句
# 谁也没读过的话——本批第一版就写错了，是这条断言自己红出来的。
_LS_DESC = (_B.list_guestbook.description or "")
check("列表工具的说明（**模型读的那份 `.description`**）里写着「只列已通过审核」",
      "只列已通过审核的留言" in _LS_DESC, _LS_DESC[:40])
check("  且写着「查无此条不等于不存在」（planner 那一侧要有边界，不能只在注记里）",
      "不等于" in _LS_DESC and "待审" in _LS_DESC)
check("  说说那边同样披露（两个数据源不同的工具，边界不能只写一个）",
      "只列已通过审核" in (_B.list_talks.description or ""))

from agent.refs import parse_data, resolve_one  # noqa: E402

_td = [{"tool": "list_guestbook", "data": parse_data(_frame)}]
check("引用按**新名**取值", resolve_one("$list_guestbook[0].talkId", _td) == (96, None),
      str(resolve_one("$list_guestbook[0].talkId", _td)))
check("引用按**旧名**也取得到（模型的历史措辞不许被改名判成 ref_path_missing）",
      resolve_one("$list_guestbook[0].talkKey", _td) == (96, None),
      str(resolve_one("$list_guestbook[0].talkKey", _td)))
check("两个名字都不在时才报 ref_path_missing",
      resolve_one("$list_guestbook[0].nope", _td)[1] == "ref_path_missing")
check("后台文章清单用的是 `noteId=` 而不是裸 ` id=`",
      "- noteId=" in _SRC["agent/adminops.py"] and "- id={" not in _SRC["agent/adminops.py"])

print("\n⑥ 物件名与出口覆盖（20260928）：同一个数字不只以一种名字露面，也不只以一种身份露面")

# 上面五节管的是"这个数字**叫什么**"，这一节管"这个数字**是什么**"。
# 事故实证（trace `20260928T032411` / `20260928T032502`）：`get_article_detail` 是
# **四个数据源共用的一件工具**，而动作词恒为"读取**文章**"⇒ 跨轮执行记忆里写着
# 「读取文章 100」，而 100 是一条**留言**；下一轮 narrator 照抄，主人看到的就是
# "把留言编号当成文章读"。
from tools import base as _B  # noqa: E402
from tools.base import DOC_TYPE_CN  # noqa: E402
import server as _SRV  # noqa: E402

check("物件名只有一份（`DOC_TYPE_CN`），四个源都在",
      set(DOC_TYPE_CN) == {"note", "talk", "board", "announcement"}
      and DOC_TYPE_CN["board"] == "留言" and DOC_TYPE_CN["talk"] == "说说",
      str(DOC_TYPE_CN))
for _dt, _noun in (("board", "留言"), ("talk", "说说"), ("announcement", "公告"),
                   ("note", "文章")):
    _line = _SRV._tool_action_text("get_article_detail",
                                   {"article_id": 100, "doc_type": _dt})
    check(f"过程行按 doc_type 说话：{_dt} → 读取{_noun} 100", _line == f"读取{_noun} 100", _line)
check("  缺 doc_type 时回落到「文章」（存量行为与确定性快道一个字节不变）",
      _SRV._tool_action_text("get_article_detail", {"article_id": 19}) == "读取文章 19",
      _SRV._tool_action_text("get_article_detail", {"article_id": 19}))
check("  `$ref` 形态同样按 doc_type 取名词",
      _SRV._tool_action_text("get_article_detail",
                             {"article_id": "$search_notes[0].noteId",
                              "doc_type": "board"}).startswith("读取留言（"), "")

# ⑥b 出口覆盖：这一支此前是裸 `str(it)`，绕过 `_shape` ⇒ 同一份帧里 article 族的键
# 叫 `noteId`、留言族却仍是上游的 `talkKey`（读者回复里 `talkId 99` 与 `talkKey 100`
# 混着出现，当时被误判成"模型编了个不存在的字段名"）。
_saved_get2 = _B._get
try:
    _B._get = lambda p, **kw: [{"talkKey": 100, "talkTitle": "诉",
                                "content": "博主是大笨狗", "approved": 1}]
    _board_frame = _B.get_article_detail.invoke({"article_id": 100, "doc_type": "board"})
finally:
    _B._get = _saved_get2
check("留言详情帧走出口改名：`talkId` 在、上游的 `talkKey` 不在",
      "'talkId': 100" in _board_frame and "talkKey" not in _board_frame,
      _board_frame[:80])

# ⑥c 换措辞的**第二重收益**：文章指代锚点只认「读取文章 N《标题》」行，而留言读取
# 行从今天起不再冒充它（此前若那条留言恰好在公开列表里、标题又非空，就会被当成本
# 会话已点名的**文章** anchor）。判据钉在正反两侧。
from agent.context import _DOC_READ_ROW_RE as _DRR  # noqa: E402
check("跨轮执行记忆里，文章读取行仍被认成文档锚点",
      bool(_DRR.search("09-20 21:03 读取文章 19《Saudade Blog AI Agent（泠月喵）架构文档》")))
check("  留言读取行**不再**冒充文章锚点（换措辞顺带堵掉的旧耦合）",
      not _DRR.search("09-28 03:23 读取留言 100") and not _DRR.search("09-28 03:23 读取说说 88"))

# ⑥d Rust 侧是同一个散文契约的另一半（父仓独立 checkout 时跳过，同 test_user_notice
# 的做法）：`render_exec_row` 必须按 doc_type 分名词，否则线上落库的台账又会是"读取文章"。
sys.path.insert(0, str(ROOT / "tests"))
import _parent_repo  # noqa: E402

_rsrc = _parent_repo.read(
    "src/routes/chat.rs",
    why="跨轮执行记忆是 Rust 渲染定稿的 ⇒ 只改 Python 一侧，线上台账仍写「读取文章」")
if _rsrc:
    check("父仓 `render_exec_row` 按 doc_type 分名词（board/talk/announcement 三臂）",
          '"board" => "留言"' in _rsrc and '"talk" => "说说"' in _rsrc
          and '"announcement" => "公告"' in _rsrc)
    check("  且注明名词与 Python 侧 `DOC_TYPE_CN` 同源（改一处必须同步另一处）",
          "DOC_TYPE_CN" in _rsrc)

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
