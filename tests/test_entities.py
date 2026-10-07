# -*- coding: utf-8 -*-
"""实体摘要单测（纯函数、零网络、零 LLM，秒级）。

被测 = agent/entities.py 的 receipt_digest()：把数据工具返回压成一行可引用实体摘要
（20260920 探针驱动：指代解析已正确但取值靠重跑工具；摘要随回执落 execution_log，
下轮作为 recent_executions 注入 ⇒ 指代可零调用取值）。

样本用的是**线上真实返回形态**（2026-09-20 采样 /api/public/{board,talk,category,
tagone,topnotes,announcements}），字段名照抄，防"改了字段名测试还绿"。

契约：
  · 条目类必须带序号（"第二条"要能对号）；
  · 计数类必须带真实数字（不得凑整/改写）；
  · 未支持的工具与解析失败的返回一律空摘要（退化为改动前行为，绝不猜）；
  · 总长 ≤ 150（Rust detail 列 varchar(300)，动作行 + 摘要一起截断）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

from agent.entities import receipt_digest  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


GUESTBOOK = """[{'talkKey': 85, 'talkTitle': '诉', 'content': '测试260905', 'cat': '诉', 'v': 1, 'author': '', 'mine': False, 'approved': 1, 'createTime': '2026-09-05 14:06:57'}, {'talkKey': 45, 'talkTitle': '诉', 'content': '泠月喵好笨啊', 'cat': '诉', 'v': 2, 'author': '', 'mine': False, 'approved': 1, 'createTime': '2026-09-05 11:30:02'}, {'talkKey': 30, 'talkTitle': '寄', 'content': '泠月喵真棒！', 'cat': '寄', 'v': 0, 'author': '', 'mine': False, 'approved': 1, 'createTime': '2026-09-02 23:26:00'}]"""
TALKS = """[{'talkKey': 43, 'talkTitle': '', 'content': '折磨了我这么久的前端性能优化终于找到根因了。', 'cat': '愿', 'v': 0, 'mine': False}, {'talkKey': 22, 'talkTitle': '???', 'content': 'meow', 'cat': '愿', 'v': 0, 'mine': False}]"""
CATEGORIES = """[{'categoryKey': 9, 'categoryTitle': '测试', 'pathName': '测试', 'icon': '📷', 'noteCount': 5}, {'categoryKey': 11, 'categoryTitle': '本项目介绍', 'noteCount': 7}, {'categoryKey': 12, 'categoryTitle': '摄影', 'noteCount': 0}, {'categoryKey': 13, 'categoryTitle': '编程', 'noteCount': 8}, {'categoryKey': 14, 'categoryTitle': 'Web3', 'noteCount': 0}]"""
TAGS = """[{'tagKey': 10, 'title': '摄影', 'color': '#3255c6', 'level': 1}, {'tagKey': 11, 'title': '音乐', 'level': 1}, {'tagKey': 13, 'title': '嵌入式', 'level': 1}, {'tagKey': 14, 'title': '编程', 'level': 1}]"""
NOTES = """[{'noteKey': 14, 'key': 14, 'noteTitle': 'ESP32-S3-OBC固件接入参考', 'description': '本固件是 saudade.site IoT 平台的参考设备实现'}, {'noteKey': 22, 'key': 22, 'noteTitle': 'IoT 设备接入物联网平台指南', 'description': 'x'}]"""
ANNOUNCE = """[{'id': 10, 'title': '公告', 'content': '请务必认真仔细阅读此公告，因为他会浪费你珍贵的10秒钟。', 'createdAt': '2026-08-07 12:24:28'}]"""

print("① 条目类（留言/说说）：序号 + 内容，序号必须数得出来")
g = receipt_digest("list_guestbook", GUESTBOOK)
check("留言摘要含『3条』", "3条" in g, g)
check("第 1 条 = 测试260905（顺序即返回顺序）", g.index("测试260905") < g.index("泠月喵好笨啊") < g.index("泠月喵真棒"), g)
check("序号前缀存在（第N条对号）", g.count("1.") >= 1 and "2." in g and "3." in g, g)
check("带分类字（诉/寄）", "〔诉〕「" in g and "〔寄〕「" in g, g)
# 20261001：分类必须**看得见是分类**。裸印 `4.诉「博主是大笨狗」` 被读成"某人说了某话"
# ——trace 20260930T235232 里模型回的就是"那条留言是访客**诉**发的"。
check("★ 分类带 〔〕 外壳（与正文「」分开，不再被当成人名）",
      "1.〔诉〕「测试260905」" in g, g)
check(" 分类缺失时不硬凑一个壳出来（没有就没有）",
      receipt_digest("list_guestbook",
                     "[{'talkKey': 1, 'content': '无分类的留言'}]") == "最近1条: 1.「无分类的留言」",
      receipt_digest("list_guestbook", "[{'talkKey': 1, 'content': '无分类的留言'}]"))
t = receipt_digest("list_talks", TALKS)
check("说说摘要含 2 条与首条内容", "2条" in t and "前端性能优化" in t, t)

print("② 计数类（分类/标签）：数字必须是真的")
c = receipt_digest("list_categories", CATEGORIES)
for want in ("5 个分类", "编程 8 篇", "摄影 0 篇", "本项目介绍 7 篇"):
    check(f"分类摘要含 {want}", want in c, c)
check("名称与数字之间有分隔（防 Web3+0 读成 Web30）", "Web3 0 篇" in c, c)
check("分类总数 = 5（不是条目数拼凑）", "5 个分类" in c, c)
tg = receipt_digest("list_tags", TAGS)
check("标签摘要含 4 个与名称", "4 个标签" in tg and "嵌入式" in tg, tg)
# 两级标签（20260921）：list_tags 已改为 /tagone + /tagtwo 合并成一张表（此前只读
# /tagone ⇒ 结构上看不见二级标签，线上实测答"站内没有二级标签"）。计数必须分开报，
# 二级以 `父/子` 出现——"编程下面有几个二级标签"全靠这一行。
TAGS2 = """[{'tagKey': 10, 'title': '摄影', 'color': '#3255c6', 'level': 1}, {'tagKey': 14, 'title': '编程', 'color': '#e798d7b5', 'level': 1}, {'tagKey': 5, 'title': 'Python', 'color': '#49ba54b5', 'level': 2, 'fatherTag': '编程', 'fatherKey': 14}, {'tagKey': 6, 'title': 'Rust', 'color': '#ed6040bf', 'level': 2, 'fatherTag': '编程', 'fatherKey': 14}]"""
tg2 = receipt_digest("list_tags", TAGS2)
check("两级标签：总数与分级计数分开报", "4 个标签（一级 2/二级 2）" in tg2, tg2)
check("二级写成 父/子（下轮指代对得上号）", "编程/Rust" in tg2 and "编程/Python" in tg2, tg2)
check("一级不带父前缀", "摄影" in tg2 and "/摄影" not in tg2, tg2)
# 每标签文章数（20260921，Rust 侧 `noteCount`）：口径 = 公开可见文章，与 /categories 的
# noteCount 同形（见上面的 "Web3 0 篇"）。**只认 int**——字段缺失时只写名字、不写"0 篇"，
# 因为"0"是一个结论而"缺字段"不是（错答成 0 会被下轮指代当事实照抄）。
TAGS3 = """[{'tagKey': 10, 'title': '摄影', 'level': 1, 'noteCount': 0}, {'tagKey': 14, 'title': '编程', 'level': 1, 'noteCount': 8}, {'tagKey': 5, 'title': 'Python', 'level': 2, 'fatherTag': '编程', 'fatherKey': 14, 'noteCount': 3}]"""
tg3 = receipt_digest("list_tags", TAGS3)
check("标签带文章数（0 也要写出来）", "摄影 0 篇" in tg3 and "编程 8 篇" in tg3, tg3)
check("二级标签 = 父/子 + 篇数", "编程/Python 3 篇" in tg3, tg3)
check("缺 noteCount 字段时**不编造** 0 篇", "篇" not in tg2, tg2)

print("③ 候选类（检索/列表/置顶）：id《标题》")
n = receipt_digest("search_notes", NOTES)
check("检索摘要含 id《标题》", "14《ESP32-S3-OBC固件接入参考》" in n, n)
check("候选类同样支持 list_notes/get_top_notes", receipt_digest("list_notes", NOTES).startswith("文章列表:")
      and receipt_digest("get_top_notes", NOTES).startswith("置顶文章:"))
a = receipt_digest("get_announcements", ANNOUNCE)
check("公告摘要含标题与日期", "公告" in a and "2026-08-07" in a, a)

print("③b 标题截断不留半个括号（20260921 线上回执实测）")
_LONG_TITLE = """[{'noteKey': 19, 'key': 19, 'noteTitle': 'Saudade Blog AI Agent（泠月喵）架构文档', 'description': 'x'}]"""
lt = receipt_digest("search_notes", _LONG_TITLE)
check("长标题截断后不留悬空的「（」", "（》" not in lt and "（" not in lt, lt)
check("截断处仍是完整词（退回括号前）", "19《Saudade Blog AI Agent》" in lt, lt)
check("短标题原样（不受影响）", "14《ESP32-S3-OBC固件接入参考》" in receipt_digest("search_notes", NOTES),
      receipt_digest("search_notes", NOTES))
_BAL = """[{'noteKey': 30, 'key': 30, 'noteTitle': '一个括号（带说明）完整闭合的超长标题示例文本', 'description': 'x'}]"""
_bl = receipt_digest("search_notes", _BAL)
check("括号已闭合的标题照常按字数截断", "30《" in _bl and "（》" not in _bl, _bl)

print("④ 不该有摘要的一律空串（退化为改动前行为，绝不猜）")
for tool, payload in [("navigate_to", "NAVIGATE:/talk"), ("list_devices", "设备1（在线）\n设备2（离线）"),
                      ("get_current_time", "2026年09月20日 星期日 19:30"),
                      ("list_guestbook", "UPSTREAM_DOWN"), ("list_categories", ""),
                      ("list_guestbook", "[{'talkKey': 1, 'content': ''}]"),
                      ("list_guestbook", "[")]:
    check(f"{tool} / {payload[:22]!r} → 空摘要", receipt_digest(tool, payload) == "",
          receipt_digest(tool, payload))

print("⑤ 长度上限与异常安全")
long_rows = "[" + ",".join(
    "{'talkKey': %d, 'cat': '诉', 'content': '这是一条很长的留言内容用来把摘要撑长第%d条'}" % (i, i)
    for i in range(30)) + "]"
lg = receipt_digest("list_guestbook", long_rows)
check("超长输入摘要 ≤150 字", 0 < len(lg) <= 150, f"{len(lg)} 字")
check("截断发生在条目边界（不留半条）", lg.rstrip('」').count("「") == lg.count("」"), lg[:60])
check("None/非字符串不炸", receipt_digest("list_guestbook", None) == "")

print("⑥ planner 契约在位（规则 6b + 回执字段名与 Rust 侧一致）")
src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
check("planner 提示词含规则 6b", "6b. 指代取值优先于重查" in src)
check("规则 6b 点了 recent_executions 实体摘要", "实体摘要" in src)
check("回执写入 digest 字段（Rust render_exec_row 读同名键）", 'rcpt["digest"] = digest' in src)
# 双侧契约（20260921）：规则 6b 末条在 planner 侧只决定"这轮不重查"，而**句子是 narrator
# 写的**——纪律 15 缺失时同一条输入会时对时错（真实 trace：19:59 追问对 / 20:08 直接
# 答「编程 8 篇」）。两处必须同时在位，缺一条就等于没有这条行为。
check("planner 规则 6b 点名「不唯一 → 追问澄清」", "指代对象在摘要里本身就**不唯一**" in src)
# 20261001：6b/6c 的分界原先只长在 6c 的尾巴上（"取值指代走 6b"），而**判错的那一轮
# 恰好是照着 6b 在走**——模型把"编程那个分类下面有几篇文章"当成 6c 的现时状态去重查了。
# 这条锁判的不是"那句话在不在"，是**它在不在 6b 的位置上**（句子在谁的位置上，就属于
# 谁的判据）。顺手把旧版那句绝对化的话一起拦掉：分类文章数这类量会变，"照抄摘要是
# 历史事实、永远对"是把一个会变的量说成了不会变。
_6b = src.index("6b. 指代取值优先于重查")
_6c = src.index("6c. 现时状态类询问")
check("6b/6c 的分界句长在 6b 里（不在 6c 的尾巴上）",
      _6b < src.index("第一步先分清问的是哪一件事", _6b) < _6c, f"6b@{_6b} 6c@{_6c}")
check("分界句把量词明确算进 6b（「有几篇文章」不是现时状态）",
      "量词不改变归属" in src[_6b:_6c])
check("旧版绝对化的那句已删净（graph.py）", "照抄摘要永远是对的" not in src)
check("旧版绝对化的那句已删净（server.py）",
      "照抄摘要永远是对的" not in (ROOT / "server.py").read_text(encoding="utf-8"))
check("narrator 叙述纪律 15（指代不唯一先追问）在位", "15. 指代不唯一时先追问" in src)
check("纪律 15 要求点名候选（不是笼统反问）", "追问要**点名候选**" in src)
check("纪律 15 含反面（带序号/已点名 → 直接答，不反问）", "就是多此一举" in src)

print("⑦ 后台文章清单也有摘要了（20261007：跨轮取值通道缺的那一格）")
# 生产实证（trace 20261007T191440 → 20261007T191543）：19:14 真调了后台文章清单、
# 照帧答"共 21 篇"；下一轮用户只说句闲聊（零工具，planner 判得对），narrator 却**主动
# 撤回了自己那句话**——因为回执行里**只有动作、没有数**，那个数只活在它上一轮的散文里，
# 而"零帧不得声称"的诚实纪律于是只能让它否认自己。这一条锁的是"数真的进得了台账"。
# 样本**用真渲染器产出**（不手写文本）：两侧形状一旦漂开，这里就红。
from agent.adminops import render_admin_notes  # noqa: E402


def _an(i: int, status: str, title: str, tags: str = "5") -> dict:
    return {"noteKey": i, "noteTitle": title, "status": status,
            "isTop": 0, "noteTags": tags}


_NOTES = [_an(54, "public", "我，管理员！"),
          _an(46, "public", "文章向量空间图谱项目文档", ""),
          _an(23, "public", "Python asyncio 异步并发"),
          _an(21, "private", "关于欧洲AI产业落后中美", ""),
          _an(20, "draft", "第一次评估")]
_dig = receipt_digest("list_admin_notes", render_admin_notes(_NOTES, None))
check("抽出了总数（动作行之外真的有了数）", "共 5 篇" in _dig, _dig)
check("带状态分布（公开 3 / 私密 1 / 草稿 1）", "公开 3 / 私密 1 / 草稿 1" in _dig, _dig)
check("摘要 ≤150 字", 0 < len(_dig) <= 150, f"{len(_dig)} 字")
_kw = receipt_digest("list_admin_notes",
                     render_admin_notes([_NOTES[2]], None, keyword="Python"))
# 渲染器自己的头注写明了：带词的那支是**搜索口径**，不是站内总量。摘要是跨轮取值的
# 来源，把"匹配 N 篇"写成"共 N 篇"，下轮问"后台一共几篇"就会拿一次搜索的条数作答。
check("带 keyword 的那支说『匹配』不说『共』（搜索条数 ≠ 站内总量）",
      "匹配 1 篇" in _kw and "共 1 篇" not in _kw, _kw)
_td = receipt_digest("list_admin_notes", render_admin_notes(_NOTES, None, limit=2))
check("清单被截断时不报状态分布（那是前 N 条的分布，不是全量）",
      "共 5 篇" in _td and "（公开" not in _td, _td)
_ud = receipt_digest("list_admin_notes", render_admin_notes([_an(1, "weird", "x", "")], None))
check("状态认不出时不报分布（三项之和 ≠ 总数 ⇒ 不拿局部冒充全量）",
      "共 1 篇" in _ud and "（公开" not in _ud, _ud)
check("头行读不出来时照旧空摘要（退化为只有动作行，绝不猜）",
      receipt_digest("list_admin_notes", "这是一段没有头行的话") == "")

print("⑦b 叙述纪律：『本轮没查』不等于『撤回上一轮』（同一现场的另一半）")
# 两半缺一不可：数据面补了摘要，纪律面还得允许它把上一轮的读数当成有据的。
_psrc = (ROOT / "agent/prompts.py").read_text(encoding="utf-8")
check("来源清单把『上一轮自己照帧说出口的读数』算作有据",
      "上一轮你自己照帧说出口的那些读数" in _psrc)
check("明写『本轮没有工具返回、不构成撤回上一轮那句话的理由』",
      "不构成撤回上一轮那句话的理由" in _psrc)
check("明写不许主动翻旧账（没被问到不动自己的旧账）",
      "不要主动去复盘或修正自己的旧账" in _psrc)
check("被问到/被质疑时照旧核对（这条纪律没把真相询问吃掉）",
      "被问到或被质疑时照旧逐条核对" in _psrc)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
