# -*- coding: utf-8 -*-
"""后台首页待办 / 日程的确定性层。

批 3a 从 `agent/adminops.py` 按域拆出（原文逐字节保留）。日期在这一层翻：
主人嘴里的时间词翻不出来就返回 `None` 让调用方零写 + 追问，绝不挑默认日期顶上。"""

from __future__ import annotations

import datetime
import re

from agent.adminops_tags import (
    clip,
)

# ── 后台首页待办 / 日程（20260926）──────────────────────────────────────
# 这张列表的存储口径（线上 `{text, done, date}`、`date` = `YYYY-MM-DD` 或 null）
# 见 src/routes/todos.rs 与 scripts/migration/dashboard_todo_20260924.sql：**前端
# 拥有那份列表的顺序与身份**（拖拽换位、空行回收、行文本就地编辑），服务端只存
# "此刻的这份列表"。agent 手里没有那份列表，所以它只有**追加一条**这一条通道
# （POST /api/protected/todos/item）——整份覆盖会抹掉主人刚做的改动。
#
# 日期为什么在这一层翻：主人嘴里说的时间词（"明天""后天"）是最常见的形态，而
# 日期一旦错一天，这条日程会静静地躺在错误的格子里（后台首页的日历就是按它画点
# 的）。翻不出来就**返回 None 让调用方零写 + 追问**，绝不挑一个默认日期顶上。
_DUE_REL_DAYS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2,
                 "後天": 2, "大后天": 3, "大後天": 3}
# 末尾那个 `[日号]?` 里的「号」是 20261008 补的：**「日」与「号」在这句人话里同义**，
# 而「X月X号」才是主人嘴里更常见的形态——`workflow_defs/` 的能力文案自己举的例句就是
# 「把〈…〉挪到 10 月 8 号」，旧表却不认它 ⇒ 主人明明说了日子，助手反问「你说是哪一天」。
# 只在这两个形态的**日位**加一个字：裸的「8号」仍不认（那要另立一条"按当月"的规则，
# 与「不带年份按当年」不是同一件事，别顺手扩）。
_DUE_DAY_SUFFIX = r"[日号]?"
# 带年份：分隔符宽容（`2026-09-27` / `2026/9/27` / `2026年9月27日` / `2026年9月27号`）
_DUE_FULL_RE = re.compile(r"^(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*"
                          + _DUE_DAY_SUFFIX + r"$")
# 不带年份：`9月27日` / `9月27号` / `9-27`
_DUE_MONTH_DAY_RE = re.compile(r"^(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*"
                               + _DUE_DAY_SUFFIX + r"$")
# 「认哪些写法」这句人话**只写一次**（四处报错文案共用）：它此前手抄在
# `workflow_defs/`×2 与 `tools/base.py`×2，四处已经开始各漂各的——改一处漏一处，就是
# "系统告诉模型的面"与"真正认的面"不是同一张（同族病：`_ITEM_POSITION_FORMS`）。
DUE_DATE_FORMS_CN = ("年-月-日 / 年/月/日 / X月X日（X月X号也算） / 今天·明天·后天"
                     " / N天之内·N天后 / 下周X·周X / 月底·下个月X号")


def normalize_due_date(value, *, today=None) -> str | None:
    """排期日 → `YYYY-MM-DD`；认不出来 → None（**不猜**，调用方零写 + 追问）。

    认这几种写法（都是主人嘴里或 planner 手里真会出现的形态）：
      · `YYYY-MM-DD`（标准口径——planner 手里有 current_time，能算就该直接给这个）
      · `YYYY/M/D`、`YYYY年M月D日`（日位写「号」同样认：`2026年9月27号`）
      · 今天 / 今日 / 明天 / 明日 / 后天 / 大后天
      · `M月D日` / `M月D号` / `M-D`：**按当年**解释。这是条确定的规则、不是猜——无年份时
        分辨不出"去年的 9 月 27"和"今年的 9 月 27"，跨年必须写年份；真解释错了
        还有一道兜底：确认框那一行会把翻出来的日子写出来（「排期 9月27日」），
        主人点确定之前就看得到。
      · **裸的「27号」不认**：「按当月」与上面的「按当年」不是同一条规则，且
        「3号楼」这类东西不带「月」时和日期长得一样——要收它得另立一条规则，
        不在这条日位的同义字里顺手扩。

    `today` 只为测试注入（默认取本地今天，与全站 +08:00 钟面约定一致）。
    """
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in _DUE_REL_DAYS:
        base = today or datetime.date.today()
        return (base + datetime.timedelta(days=_DUE_REL_DAYS[s])).isoformat()
    m = _DUE_FULL_RE.match(s)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)),
                                 int(m.group(3))).isoformat()
        except ValueError:
            return None                      # 2026-02-30 这类：存在不了一天
    m = _DUE_MONTH_DAY_RE.match(s)
    if m:
        base = today or datetime.date.today()
        try:
            return datetime.date(base.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    return None


# ── 排期日的**推导**（20261020）──────────────────────────────────────────
# 上一版到这里就没了：`normalize_due_date` 认不出 ⇒ 调用方零写 + 追问。而主人嘴里
# 最自然的排期说法恰恰在闭集之外——生产 trace `20261020T084447`/`T084521`：「猫咪加条
# 待办，**两天之内**发布泠月喵正式版第一版」，planner 技能选对、正文抄对，只因为
# 「两天之内」不在那张闭集里，**整轮一件事都没办成**（零工具 → 纠偏空转 → 确定性收尾）。
# 而那句话是完全清楚的：今天 10-20，两天之内就是 10-22。**系统算得出来，却去问主人**
# ——这不是"不猜"，这是把一件机器能算的事推给主人。
#
# 于是补一条**推导**路径（`derive_due_date`），闭集不动：
#   · 闭集管"主人报了一个日子"（照抄即可），推导管"主人给了一个相对/指示说法"
#     （要用钟面算）。**两者分开是承重的**：`tests/test_todo_schedule.py:186-189` 逐字
#     锁着「裸的 27号 / 3号楼 / 5号窗口 一律不认」，把推导并进归一会当场把它推翻。
#   · 推导出来的值**不是主人的话**，所以它必须**标出处**（调用方往计划里写
#     `derived_values`，卡面与 narrator 注记都写明「系统替你排的」）——主人的日子与
#     系统的日子在卡面上必须看得出区别，否则等于系统替主人改了字。
#   · **完全推不出来的仍走零写 + 追问**（用户 20261020 裁决：主人压根没提日子 ⇒ 留空，
#     不硬塞一个）。两条判据见函数内那两段。
#
# 收哪些说法是**封闭判据**（不是"什么算日期"的开放推断），每一条都能在钟面上算出唯一
# 一个日子，且都不与闭集重叠。
_DUE_DAY_NUM = r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})"


class _NoMatch:
    """「正则压根没匹配上」的**哨兵**——与 `None` 严格区分。

    这一层是承重的：`_derive_month` 的两个 `None` 出口意思是"**匹配上了、但那天不
    存在**"（9月31日 / 31号 而那个月没有 31 天），调用方必须**当场终局**；而"压根
    没匹配上"要继续往下试别的形状。把两者都写成 `None`，`9月31日` 就会一路落到
    "是排期说法但算不出 ⇒ 默认今天"那一格——主人写错的日子被悄悄改成今天，卡面上
    还一切正常。本仓 20261020 那条纪律（"缺键绝不编 0"）在这里的另一副面孔就是它。
    """

    __slots__ = ()


_DUE_NO_MATCH = _NoMatch()
_DUE_MONTH_NUM = r"(?:\d{1,2}|[一二三四五六七八九十]{1,3})"
_DUE_OFFSET_RE = re.compile(
    r"^(" + _DUE_DAY_NUM + r")\s*[天日]\s*(?:之内|以内|内|里|后|之后|以后)$")
# 「今天之内」「明天以前」这类：基底词闭集已经认，加上界限词之后整串不在闭集里
# （闭集是 `^…$` 全匹配），于是「明天以前」此前也翻不出来。
_DUE_BASE_LIMIT_RE = re.compile(
    r"^(今天|今日|明天|明日|后天|後天|大后天|大後天)\s*(?:之内|以内|内|里|之前|以前|前)$")
# 周几：`周一` / `星期三` / `礼拜5` / `下周五` / `这周日`。映射按 `date.weekday()`
# （周一 = 0 … 周日 = 6），与 `utils/clock.WEEKDAYS` 同一套下标。
_DUE_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
                 "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_DUE_WEEKDAY_RE = re.compile(r"^(下下|下个|下|这个|本个|这|本)?\s*(?:周|星期|礼拜)\s*"
                             r"([一二三四五六日天1-7])$")
# 「下周」这种不带周几的：落**下周一**（"下周"就是一个可算的区间起点）。
_DUE_WEEK_ONLY_RE = re.compile(r"^(下下|下个|下)\s*(?:周|星期|礼拜)$")
# 月份那一截有**两种**说法，别混成一个槽：
#   · 相对（「下个月」「这个月」「本月」）——基数就是现在这个月，句子里**没有**月份数字；
#   · 绝对（「9月」「九月」）——主人报的是月份本身，按**当年**解释（同闭集那条规则）。
# 前者带一个「个」字（下个月），所以两条正则共用一个 `_DUE_MONTH_HEAD`：组①=相对词
# （绝对形态时为 None），组②=绝对月份数字。
_DUE_MONTH_HEAD = r"(?:(下个|下|这个|本个|这|本)个?月|(" + _DUE_MONTH_NUM + r")月)"
_DUE_MONTH_DAY_WORD_RE = re.compile(r"^" + _DUE_MONTH_HEAD + r"\s*(" + _DUE_DAY_NUM
                                    + r")\s*[号日]$")
_DUE_MONTH_END_RE = re.compile(r"^" + _DUE_MONTH_HEAD + r"\s*(?:底|末)$")
# 裸的「月底」/「月末」（不带月份那一截）：就是**这个月**的月底。
_DUE_MONTH_END_BARE_RE = re.compile(r"^(?:这个|本|这)?月(?:底|末)$")
# 裸的「下个月」/「下月」（不带日号）：落**下个月 1 号**（同「下周」→ 下周一）。
_DUE_MONTH_ONLY_RE = re.compile(r"^(下个|下|这个|本个|这|本)个?月$")
# 裸的「27号」：主人说了日子、只是没说是哪个月 ⇒ 按**下一次**的那个日号算（同"周几"
# 那条规则）。`normalize_due_date` 仍不认它（那条锁不动，见它自己那段注）；这里是
# **推导**：说得出日号就是排期意图，而"算不出哪个月"不该让整轮什么都办不成。
_DUE_BARE_DAY_RE = re.compile(r"^(" + _DUE_DAY_NUM + r")\s*[号日]$")
# 「尽快」这类：主人表达了排期意图，但**没有可算的锚**——按 20261020 裁决落默认今天
# （卡面会把日子印出来让他核对）。`这两天` 字面上也指今天起的两天内。
_DUE_SOON_WORDS = ("尽快", "尽早", "赶紧", "这两天", "这两天里", "这两天之内",
                   "马上", "立刻", "现在", "今天之内", "今日之内")
# 「像某个具体日子、但那天不存在」（9月31日 / 2026-02-30）：**不许落默认今天**——
# 那是"那一天不存在"，不是"算不出来"，静默改成今天等于替主人改字。
_DUE_DATED_LIKE_RE = re.compile(r"\d\s*[-/.年月]\s*\d")
# 有没有**排期语素**（决定"算不出来时能不能落默认今天"）：一个都没有 ⇒ planner 往
# date 槽里填的压根不是时间（幻觉/串了上文），那不是排期意图 ⇒ 仍零写 + 追问。
# ⚠️ **「号」刻意不在表里**：它是"日号"那个字的同形字，`3号楼` / `5号窗口` 会因此
# 被误判成排期说法、静默落成今天——而那两个串压根不是时间。日号那一路由上面
# `_DUE_BARE_DAY_RE`（全匹配 `^\d+[号日]$`）接走，不需要在这里再兜一层。
_DUE_TIME_MORPHEMES = ("天", "日", "周", "星期", "礼拜", "月", "底", "末",
                       "尽快", "尽早", "赶紧", "之内", "以内", "之后", "以后")


def _due_day_number(text: str) -> int | None:
    """`N天` 里的那个 N：阿拉伯数字或中文数字（`两天` → 2）。认不出 → None。

    中文数字借 `adminops_account._cn_number`（`禁他三天` 那一族用的同一个）——
    **不另写一份**：同一门语言的同一段规则抄两遍，就是本仓点过名的那种病。
    """
    text = str(text or "").strip()
    if text.isdigit():
        return int(text)
    from agent.adminops_account import _cn_number      # 局部导入：避免模块级成环
    return _cn_number(text)


def _due_month(lead: str | None, num: str | None, base: datetime.date) -> tuple[int, int]:
    """月份那一截 → `(年, 月)`：相对说法（下个月/这个月）按基数月推，绝对说法按当年。

    「下个月」跨年由这里兜（12 月说「下个月」⇒ 明年 1 月）。
    """
    if lead in ("下", "下个"):
        year, month = base.year, base.month + 1
        return year + (month - 1) // 12, (month - 1) % 12 + 1
    if lead:
        return base.year, base.month          # 这个月 / 本月
    return base.year, _due_day_number(num) or 0


def _month_end(year: int, month: int) -> str:
    """那一个月的最后一天。"""
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    return (nxt - datetime.timedelta(days=1)).isoformat()


def derive_due_date(value, *, today=None) -> str | None:
    """排期日 → `YYYY-MM-DD`；**闭集认不出的相对说法算一个**；实在算不出 → None。

    与 `normalize_due_date` 的分工（这一条决定了两个函数为什么不是一个）：
      · `normalize_due_date` = **照抄**：主人报了一个日子，系统只做格式归一；
      · `derive_due_date` = **算**：主人给的是相对/指示说法，要拿钟面推。
    本函数**先跑一遍归一**（认得就直接返回它）⇒ 它是"排期日的总解析器"，调用方不必
    自己判断该走哪一个；认得的那些写法与闭集**逐字节同结论**。

    三种出口，调用方按它区分出处（见模块顶部那段）：
      · 算得出（含闭集认得的）⇒ 一个 ISO 日子；
      · 算不出、但**是**排期说法（「过几天」「有空的时候」）⇒ **今天**（用户裁决：
        系统自己挑一个，卡面印出来让主人核对）；
      · 算不出、且**不是**排期说法（往 date 槽里填了不相干的词）⇒ None，调用方零写 + 追问。

    `today` 只为测试注入（默认取本地今天，与全站 +08:00 钟面约定一致）。
    """
    iso = normalize_due_date(value, today=today)
    if iso:
        return iso
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().strip("「」『』“”\"'").strip()
    if not s:
        return None
    base = today or datetime.date.today()
    if s in _DUE_SOON_WORDS:
        return base.isoformat()
    m = _DUE_BASE_LIMIT_RE.match(s)
    if m:
        return normalize_due_date(m.group(1), today=base)   # 基底词闭集一定认得
    m = _DUE_OFFSET_RE.match(s)
    if m:
        n = _due_day_number(m.group(1))
        if n:
            return (base + datetime.timedelta(days=n)).isoformat()
    v = _derive_week(s, base)
    if v is _DUE_NO_MATCH:
        v = _derive_month(s, base)
    if v is not _DUE_NO_MATCH:
        # 可能是个真日子，也可能是 `None`（"9月31日"这类——正则匹配上了、那天不存在）。
        # 两种都是**终局**：不许从这里再落到下面"默认今天"那一格。
        return v
    if _DUE_DATED_LIKE_RE.search(s):
        return None                          # 像日子却不存在：不许落默认今天
    if any(w in s for w in _DUE_TIME_MORPHEMES):
        return base.isoformat()              # 是排期说法但算不出 ⇒ 默认今天
    return None                              # 压根不是排期说法 ⇒ 交回调用方去问


def _derive_week(s: str, base: datetime.date) -> str | None | _NoMatch:
    """周几族（`下下周五` / `下周五` / `周五` / `下周`）→ ISO；都不匹配 → `_DUE_NO_MATCH`。

    抽出来只因**复杂度预算**（C ≤ 30，`tests/test_complexity_budget.py`）：这一族四条
    出口各自要算一次"离目标周几还有几天"，与月族、日号族在正则与算式上都不相干，
    合在一个函数里只是把三个互不相干的判据串成一条长链。

    三个分支的语义差别（别把它们统一成一句"下一个周五"）：
      · `下下周五` = 隔一整周的那一周的周五；
      · `下周五` = **下一个自然周**的周五（不是"七天后的周五"）；
      · 裸的 `周五` / `这周五` = **下一次**的那个周几（今天就是周五 ⇒ 今天）。
        已过去的"这周五"不落一个过去的日期——主人说这句话时心里想的一定是下一次。
    """
    m = _DUE_WEEKDAY_RE.match(s)
    if m:
        lead, day = m.group(1) or "", _DUE_WEEKDAYS[m.group(2)]
        if lead in ("下下",):
            return (base + datetime.timedelta(days=7 - base.weekday() + 7 + day)).isoformat()
        if lead in ("下", "下个"):
            return (base + datetime.timedelta(days=7 - base.weekday() + day)).isoformat()
        return (base + datetime.timedelta(days=(day - base.weekday()) % 7)).isoformat()
    if _DUE_WEEK_ONLY_RE.match(s):
        return (base + datetime.timedelta(days=7 - base.weekday())).isoformat()
    return _DUE_NO_MATCH


def _derive_month(s: str, base: datetime.date) -> str | None | _NoMatch:
    """月/日号族 → ISO 或 `None`（正则匹配上了、那天不存在）；都不匹配 → `_DUE_NO_MATCH`。

    与 `_derive_week` 同一条抽出理由（复杂度预算）。五个分支由**粗到细**排：先"某月
    某号"、再"某月底"、再"裸的月底/某月"、最后"裸的日号"——顺序不能换，`27号` 与
    `27日月` 这类形状的宽窄就靠这个顺序定。

    两个 `None` 出口是**承重**的：`9月31日` / `31号`（那个月没有 31 天）这类"像日子
    却不存在"必须当场终局，**绝不许**落回调用方那格"默认今天"——那会把主人写错的
    日子悄悄改成今天，而卡面上看起来一切正常。
    """
    m = _DUE_MONTH_DAY_WORD_RE.match(s)
    if m:
        year, month = _due_month(m.group(1), m.group(2), base)
        n = _due_day_number(m.group(3))
        if month and n:
            try:
                return datetime.date(year, month, n).isoformat()
            except ValueError:
                return None                  # 9月31日 / 下个月 31 号这类：那天不存在
    m = _DUE_MONTH_END_RE.match(s)
    if m:
        year, month = _due_month(m.group(1), m.group(2), base)
        if month:
            return _month_end(year, month)
    if _DUE_MONTH_END_BARE_RE.match(s):
        return _month_end(base.year, base.month)
    m = _DUE_MONTH_ONLY_RE.match(s)
    if m:
        year, month = _due_month(m.group(1), None, base)
        if month and m.group(1) in ("下", "下个"):
            return datetime.date(year, month, 1).isoformat()
        return base.isoformat()          # 「这个月」= 现在就在这个月里
    m = _DUE_BARE_DAY_RE.match(s)
    if m:
        n = _due_day_number(m.group(1))
        if n and 1 <= n <= 31:
            # 下一次的那个日号：本月还没到就是本月，已经到了就是下个月。
            year, month = base.year, base.month + (1 if n < base.day else 0)
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
            try:
                return datetime.date(year, month, n).isoformat()
            except ValueError:
                return None                  # 「31号」而那个月没有 31 天
    return _DUE_NO_MATCH


def due_date_cn(iso) -> str:
    """`YYYY-MM-DD` → 「9月27日」；认不出的原样带引号吐回去（不假装它是已知日期）。"""
    s = str(iso or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if not m:
        return f"「{s}」" if s else "（没写日期）"
    return f"{int(m.group(2))}月{int(m.group(3))}日"


def _due_hint(iso: str, today) -> str:
    """排期相对今天的位置 → `（今天）` / `（已过期）` / 空串。

    与 `due_date_cn` 分开：那个只管"怎么念这个日子"，这个管"它相对今天是什么"。
    两个都不是猜——`iso` 是库里读出来的、`today` 是钟面，等宽 ISO 串直接比大小
    （`YYYY-MM-DD` 字典序 = 时间序，这也是 `normalize_due_date` 归一成这个形状的
    理由之一）。判不出来（形状不对）就**不标**，不硬套一个相对说法。
    """
    if len(iso) != 10 or iso[4] != "-" or iso[7] != "-":
        return ""
    now = today or datetime.date.today()
    if iso == now.isoformat():
        return "（今天）"
    return "（已过期）" if iso < now.isoformat() else ""


# 「这两类这一次**没读到**」的哨兵（见 `render_todo_pending`）。
#
# 为什么需要一个哨兵而不是用 `None`：`None` 要表达的其实是两种完全不同的事——
# ① **这一类信息本次不适用**（只渲染那份私人清单的调用方，比如单测与卡面预览）；
# ② **适用、但这一次没读到**（汇总接口挂了）。两者在下游**措辞相反**：①一个字都不印，
# ②必须明说"没读到、不确定"（**且不许顺口断言那两类存在**）。合并成一个值就会让 ① 那一侧凭空多出一句
# "没读到"（而它根本没去读），或者让 ② 静默成"没有待处理的事"——后者正是主人
# 最需要知道有活儿等着他的时候。哨兵让这两种形态在调用点**必须写出来**。
TODO_PENDING_UNREAD = object()


def render_todo_pending(pending) -> str:
    """待处理的那几类（留言待审 / 评论待审 / 额度重置申请）→ **一行**；`None` ⇒ 空串。

    **为什么这几类要在这一帧里**（20260929 用户要求「让 agent 能感知到待审核留言和
    对话额度批准、问待办日程它能说出来」；20261019 补**评论**——它在后台首页是**第三
    行**、与留言并排，所以 agent 少读它一次就少一件主人眼里"等着我处理的事"）：后台首页
    把它们与私人待办**并排**显示，主人眼里那就是"我日程上等着我处理的事"；但它们**不是**
    `dashboard_todo` 表的行（不落库、不进 todos，见父仓
    `frontend/src/pages/Dashboard/Home/index.tsx` 那几行的注释——父仓的路径）。它们真正的来源是同一份
    红点汇总（`/notifications/summary` 的 `pendingReview` / `pendingComments` /
    `pendingQuota`）——所以这一帧读的是**红点那个源**，不另造一份计数
    （"红点与列表是两套数据源"那件事的教训：能同源就同源）。

    ⚠️ **留言与评论是两行、不是一行**（20261019）：两张表（`talk` / `note_comment`）、
    两个后台页签（`?tab=review` / `?tab=comment`）、两个字段名。合并成一个数就会让
    narrator 把"评论还有 3 条"说成"留言还有 3 条"——而主人点进去看到的是另一个页面。

    四态，与 `_quota_pair` 同一条纪律——**读不到 ≠ 0**：
      · `pending is None` ⇒ 空串（这一类信息本次不适用，调用方没提供）；
      · `pending is TODO_PENDING_UNREAD` ⇒ 明说"没读到、不确定"，
        **绝不**留空（留空会被读成"没有待处理的事"）；
      · 各类都是 0 ⇒ 空串（这时"没有"就是事实，不必占一行）；
      · 有一类 > 0 ⇒ 只印 > 0 的那几类（印"留言待审 0 条"是噪声，且会让 narrator
        开始念零）。
    """
    if pending is None:
        return ""
    if pending is TODO_PENDING_UNREAD:
        # ⚠️ **不许断言那两类存在**（20261019 修）。旧版写的是「另有两类等你处理的待办
        # ——留言待审与额度重置申请——这一次没读到，不确定有几条」：它一边说没读到、
        # 一边**把"确实有这两类在等你"说死了**。主人在额度申请为 0 的时候反复读到
        # 「有两类待办」，正是从这句来的（生产 trace 20261018 那几轮）。
        # 没读到就只陈述"没读到"这一件事，存在性留给真读到的那一次说。
        return ("（后台汇总这一次没读到，**不确定**有没有待审留言、待审评论或额度重置"
                "申请——这是「没读到」，不是「没有」；要报给主人时如实说没读到）")
    if not isinstance(pending, dict):
        # 形态不对：**不猜**（当成"没提供"），同 `_todo_rows` 的取向。
        return ""
    review = _nonneg_int(pending.get("review"))
    comments = _nonneg_int(pending.get("comments"))
    quota = _nonneg_int(pending.get("quota"))
    parts = []
    if review:
        parts.append(f"留言待审 {review} 条")
    if comments:
        parts.append(f"评论待审 {comments} 条")
    if quota:
        parts.append(f"额度重置申请 {quota} 份")
    if not parts:
        return ""
    # 件数**按实际印出来的那几类数**，不写死"两件"（20261019 修）：旧版恒印「另有两件」，
    # 而额度那一类为 0 时下面只列出一条 ⇒ 模型照着念成「另有两件…留言待审 1 条」，
    # 主人读到的是"还有两件事"却只看见一件（生产 trace 20261018T232550 就是这个形状）。
    # 中文量词按**类数**取（一类/两类/三类），仍然不写死——20261019 加评论族之后
    # 三类的组合真的会出现，所以这里是一张表而不是三元表达式。
    lead = {1: "另有一件", 2: "另有两件", 3: "另有三件"}[len(parts)]
    return f"{lead}等着你处理（不在上面这张私人待办里）：" + "、".join(parts) + "。"


def _nonneg_int(v) -> int:
    """`v` → 非负整数；不是整数/读不出 ⇒ 0（**只用于"印不印这一项"**）。

    不接受字符串数字：这一格的两端都是 Python 自己塞的 int（`tools.base` 从汇总里
    取 `pendingReview`/`pendingQuota`），出现字符串说明有人在编数而不是在读。
    """
    if isinstance(v, bool) or not isinstance(v, int):
        return 0
    return v if v > 0 else 0


def render_todo_list(rows, *, today=None, pending=None) -> str:
    """后台待办清单 → narrator 读的一行一条。

    **一行一条 + 字段用 ` | ` 分开**（同 tag 列表族的帧瘦身口径）：narrator 要能
    一眼看出"哪条、哪天、完成没有"，而把这三样揉成一句话会让它开始自己拆句子
    （拆错就把"已完成"读成"未完成"）。序号是**列表里的位次**——它只是给人读的，
    不是 id（这张列表没有稳定的行 id，见 src/routes/todos.rs 头注；任何按序号
    指认某一条的后续动作都做不到，所以 narrator 也**不该**用它指认）。

    `pending` 见 `render_todo_pending`（默认 `None` = 不提供这一类信息）：
    后台首页那两类"等着你处理"的计数（留言待审 / 额度申请），**空清单时也照印**
    ——"一条待办都没记"与"有两件等着你"是两件同时成立的事，不能因为前者为空
    就把后者吞掉（`list_dashboard_todos` 那一侧也正是这么分出口的）。
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    tail = render_todo_pending(pending)
    if not rows:
        head = "后台首页的待办列表是空的（一条都没有）。"
        return f"{head}\n{tail}" if tail else head
    undone = sum(1 for r in rows if not r.get("done"))
    lines = [f"后台首页待办共 {len(rows)} 条（未完成 {undone} 条）："]
    for i, r in enumerate(rows, 1):
        iso = str(r.get("date") or "").strip()
        when = f"排期 {due_date_cn(iso)}{_due_hint(iso, today)}" if iso else "未排期"
        lines.append(f"{i}. {clip(str(r.get('text') or ''), 60)} | {when} | "
                     f"{'已完成' if r.get('done') else '未完成'}")
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def render_todo_added(text: str, date: str | None = None) -> str:
    """追加成功的回执行。**正文一字不改地进回执**（20260926 修，见本节上方那段
    "正文是唯一的指认方式"）——回执行是 narrator 唯一的取值来源，它截到 60 字，
    narrator 就**只能**照着那个截断版念给主人（真实现场：trace `20260926T094843`，
    主人要记的那条 150 字，回执给的是「…/20260926/2…」、回复里也是「…2…」，
    主人问"为什么和我要的内容不一样、显示截断了"）。`_TODO_TEXT_LIMIT` = 200 字，
    回执行整行 ~240 字，没有撑爆谁的问题；要截也必须像 `render_notice_status`
    那样**标注节选**，不能截了还读成完整的。
    **说清加的是什么、排在哪天**，并点明它在哪（后台首页的待办卡）——这条是主人
    自己那份列表，不是对外可见的东西，回执行不必吓唬他，但必须让他知道去哪儿看。"""
    when = f"排期 {due_date_cn(date)}" if date else "未排期"
    return (f"已在后台首页的待办里加了一条「{str(text or '').strip()}」（{when}）"
            f"（后台已复核：列表里读到了这一条）")


# ── 待办"勾完成"（20260926 第十轮）────────────────────────────────────────
# 这一件的卡面有**一条硬要求**（不是文风问题，同账号族那三条）：`text` 一字不改地
# 进卡面。主人点「确定」之前唯一能核对的就是"要勾的是不是这一条"，而这张列表没有
# 行号、没有标题，正文就是他认得的全部；把它润色/截短等于让他盲签（"交房租"被写成
# "交房"仍点得下去）。
def render_todo_when(row) -> str:
    """一行待办的排期短语（`排期 9月28日` / `未排期`）。

    **只此一处**：列表帧、卡面、多条同名时的区分串走的是同一句话——系统对同一行
    的两种表述不一致时，主人在卡上认出的东西与他事后在列表里看到的对不上。
    """
    iso = str((row or {}).get("date") or "").strip()
    return f"排期 {due_date_cn(iso)}" if iso else "未排期"


def _todo_face_row(todos, text) -> tuple[dict | None, int, bool, int]:
    """渲染用的待办快照查询 → `(唯一命中行, 命中条数, 快照是否在手, 列表总条数)`。

    与 `tools.base._todo_text_hits` 同一判据（正文逐字相等），但**只用于渲染**：
    这里是"卡面上写不写得出具体是哪一条"，不是"能不能动"（那是工具侧独立判的
    一次，同 `_match_board` 那条注）。快照不在手（读不到列表）时不编、也不因此
    不弹窗——只印正文。总条数用来把"列表里没有这一条"与"你还没有任何待办"分开
    （要主人做的事不一样：一个是改正文，一个是先去记一条）。
    """
    if todos is None:
        return None, 0, False, 0
    want = str(text or "").strip()
    rows = [r for r in todos if isinstance(r, dict)]
    hits = [r for r in rows if str(r.get("text") or "").strip() == want] if want else []
    return (hits[0] if len(hits) == 1 else None), len(hits), True, len(rows)


def render_todo_done_action(text: str, todos=None) -> str:
    """`把待办「交房租」勾成完成（排期 9月28日，现在：未完成）`——卡面、问句、
    跨轮待办的目标**共用这一行**（同 `render_account_action` 的分工）。

    `todos` 三态（全表同一条纪律：读不到就少说，**不因此不弹窗**）：
      · 快照在手、恰好一条同名 → 印 `排期 …，现在：未完成/已完成`；
      · 快照在手、没有这条 → 印「（你后台首页的待办里没有这一条）」——主人点确定
        **之前**就该看到，而不是点完才被告知没做成；
      · 快照在手、多条同名 → 如实印「（有 N 条都叫这个，分不清是哪一条）」：
        这一条**点下去也办不成**（后端与工具侧都是歧义即零写），而"没有这一条"
        与"分不清是哪一条"要他做的事不一样（一个改正文、一个先去改个说法）；
      · 快照没有（读不到列表）→ 只印正文。
    """
    want = str(text or "").strip()
    # **一格都不截**（20260926 修）：上面那三行注释写的就是这条要求，此前实现的却是
    # `clip(want, 60)`——注释与代码互相打脸了一个月，而"卡面印的是截断版"这件事在
    # 长正文上是**真的会让人盲签**（主人核对的是「…」前面那 60 字，系统勾的是整条）。
    act = f"把待办「{want}」勾成完成"
    row, n, have, total = _todo_face_row(todos, want)
    if not have:
        return act
    if row is None:
        if n > 1:
            return f"{act}（有 {n} 条待办都叫这个，分不清是哪一条）"
        if not total:
            return f"{act}（你后台首页的待办列表现在是空的）"
        return f"{act}（你后台首页的待办里没有这一条）"
    return (f"{act}（{render_todo_when(row)}，现在："
            f"{'已完成' if row.get('done') else '未完成'}）")


def render_todo_done(text: str, *, changed: bool = True) -> str:
    """勾完成成功后的回执行（工具侧用；与卡面同源同事实）。

    `changed=False` = 写前它就是完成态（后端那次是真 no-op，走的是幂等分支）——
    **必须与"刚勾的"分开说**（同 `account_change_phrase`）：主人这一下什么都没
    发生，短路成"已勾成完成"会让回执读成一个动作。

    正文同样**不截**（与卡面同源同事实，20260926）：回执行是下一轮 narrator 的
    取值来源，截了它 narrator 就只能念截断版（`render_todo_added` 里那次真实现场
    的同一个道理）。
    """
    want = str(text or "").strip()
    if not changed:
        return (f"待办「{want}」**本来就是完成状态**，这次没有发生任何变更（没有重复勾）")
    return (f"已把待办「{want}」勾成完成"
            f"（后台已复核：列表里这一条现在就是完成状态）")


# ── 待办"改排期"（20260929 批 G）──────────────────────────────────────────
# 「清空排期」这条通道上的**契约写法**：改排期与新增不一样——新增那次"没给 date"就是
# 没排期，而这里**必须**分出三种处境：给了日子 / 说了不要排期 / 压根没说（第三种要
# 回头问主人，见 `workflows._expand_todo_reschedule_workflow`）。所以"清掉"要有一个**说得出
# 口**的写法，就是这一个词；工具侧另收几个明显同义的词兜底（封闭小集，见
# `tools.base._TODO_CLEAR_DATES`），但契约只有这一个。
_TODO_CLEAR_WORD = "未排期"
# 工具侧**兜底**收的同义词（封闭小集、不做模糊匹配）：planner 没照契约写那个词、而是
# 按主人原话填了「清空」「取消」时，认它比回一句"认不出这个日子"对主人更有用——而漏收
# 一个的代价是"把那条的日子清掉"被答成日期格式错误。**认不出来的一律不是清空**：它们
# 会走"翻不出日期 ⇒ 零写 + 问清哪一天"那条既有的路。
_TODO_CLEAR_DATES = frozenset({_TODO_CLEAR_WORD, "无", "清空", "取消"})


def todo_due_phrase(date) -> str:
    """目标排期 → 「10月8日」/「未排期」（卡面、回执行、跨轮目标行**共用这一行**）。

    与 `render_todo_when` 分工：那个读的是**台账里一行**的现状（`date` 是库里的
    `YYYY-MM-DD` 或空），这个读的是**计划里要写进去的那个值**（可能是清空那个契约词、
    也可能是没翻过的脏值）。两者都必须落到同一句人话上——主人在卡上看到的"改成几号"
    与事后在列表里看到的"排期几号"是同一件事的两种说法。
    """
    s = str(date or "").strip()
    if not s or s in _TODO_CLEAR_DATES:
        return "未排期"
    return due_date_cn(s)


def render_todo_reschedule_action(text: str, date, todos=None) -> str:
    """`把待办「更新简历（国庆后）」的排期改成10月8日（现在：排期 9月28日）`——卡面、问句、
    跨轮待办的目标**共用这一行**（同 `render_todo_done_action` 的分工）。

    与勾完成那张卡同一套三态（快照在手且唯一命中 → 印现状；没有这一条 / 有多条同名 →
    如实说清；快照读不到 → 只印正文，**不因此不弹窗**），差在"现状"那一格印的是
    **当前排期**（`render_todo_when`）而不是完成态——主人核对的是"要改的是不是这一条、
    它现在排在几号"。现状那一格的写法与勾完成那张卡同形（`现在：…`）：两张卡印的是
    同一件事的两种问法，主人不必学两套读法。正文同样**一格都不截**（见
    `render_todo_done_action` 里那段）。
    """
    want = str(text or "").strip()
    act = f"把待办「{want}」的排期改成{todo_due_phrase(date)}"
    row, n, have, total = _todo_face_row(todos, want)
    if not have:
        return act
    if row is None:
        if n > 1:
            return f"{act}（有 {n} 条待办都叫这个，分不清是哪一条）"
        if not total:
            return f"{act}（你后台首页的待办列表现在是空的）"
        return f"{act}（你后台首页的待办里没有这一条）"
    return f"{act}（现在：{render_todo_when(row)}）"


def render_todo_rescheduled(text: str, date, *, changed: bool = True) -> str:
    """改排期成功后的回执行（工具侧用；与卡面同源同事实）。

    `changed=False` = 写前它就排在那一天（后端那次是真 no-op，走的是幂等分支）——
    **必须与"刚改的"分开说**（同 `render_todo_done`）：主人这一下什么都没发生，短路成
    "已改成…"会让回执读成一个动作，而下一轮 narrator 就照着它讲。

    正文**不截**（与卡面同源同事实，同 `render_todo_done` 的论证）。
    """
    want = str(text or "").strip()
    when = todo_due_phrase(date)
    # 「未排期」不能套进「排在…」这个句式（「本来就排在未排期」不是中文）——清空那一路
    # 的说法与 `_reached_one` 的幂等臂共用同一句（两处印的是同一个事实，不许各写一套）。
    was = f"本来就排在{when}" if when != _TODO_CLEAR_WORD else "本来就没有排期"
    if not changed:
        return (f"待办「{want}」**{was}**，这次没有发生任何变更（没有重复改）")
    return (f"已把待办「{want}」的排期改成{when}"
            f"（后台已复核：列表里这一条现在的排期就是 {when}）")
