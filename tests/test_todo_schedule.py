# -*- coding: utf-8 -*-
"""后台首页待办 / 日程（20260926 第八轮 + 第十轮）：纯函数 + 工具 + 确认闸，零网络零 LLM。

这一族与别的写操作最不一样的地方：**目标是主人随口说的一件事**，站内没有任何
东西可以拿它来核对——标签/分类/公告/留言都能去字典里问"有没有这个名字"，而
"下周三交房租"没处可查。于是判据只剩两条，且都必须是"缺了就零写"：

  1. **正文空 / 超长 → 零工具**（空正文是口误不是待办；超长不替主人截断）；
  2. **排期翻不出来 → 零工具 + 追问**（绝不挑一个日子顶上——错一天的日程会静静
     躺在后台日历的错误格子里，主人不翻到那天根本发现不了）。

第十轮在同一族里加了**第二条写通道**：把某一条勾成完成（§⑨–⑮）。它比"加一条"
少一条判据（没有排期参数），但多一条**定位判据**——这张列表没有行号也没有 id，
正文是唯一能认出是哪一行的东西 ⇒ 判据是"正文逐字相等，且只此一条"，0 条 / 多条
一律零写（§⑨ 用真实工具 + 桩客户端锁住）。

两条都在授权层被放进 `_ALWAYS_CONFIRM_TOOLS`：**每次都弹确认卡**，连"同轮命令
即确认"那条捷径也不走（§⑦ + §⑮ 用真实的 execute 路径锁住这一点）。

另有三处跨语言/跨文件契约在这一层被钉住：
  · 上限 `_TODO_TEXT_LIMIT` / `_TODO_MAX_ROWS` = Rust `MAX_TEXT_CHARS` / `MAX_TODOS`；
  · 写后复核按 **(正文, 排期日)** 认那一条（这张列表的读接口**不回 id**）；
  · 复核判**净变化**（同键条数写后 > 写前）——只判"列表里有这么一条"会在"主人本来
    就记过一模一样的一条"时把一次失败的追加判成成功。

用法：.venv/bin/python tests/test_todo_schedule.py
"""
import contextlib
import datetime
import json
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

import agent.graph as g  # noqa: E402
from agent import adminops as A  # noqa: E402
from agent import authz, confirm  # noqa: E402
from agent.graph import execute_node, plan_state  # noqa: E402
from agent.principal import ROLE_ADMIN, ROLE_USER, Principal  # noqa: E402
from agent.skills import _FREE_TEXT_WRITE_SKILLS, instantiate_plan  # noqa: E402
import tools.base as base  # noqa: E402

# 父仓（Rust）源码的读取统一走它：三态显式（找得到 → 断言；找不到且设了
# SAUDADE_REQUIRE_PARENT → 红；找不到 → 响亮的 ⏭）。此前这里是一行裸 read_text，
# **CI 里直接 FileNotFoundError 把整套炸掉**——而 CI 的名单里没有本套件，谁也不知道。
sys.path.insert(0, str(ROOT / "tests"))
import _parent_repo  # noqa: E402

# ── 密钥桩：settings.jwt_secret 是全局单例（同 test_admin_write）────────────
# `_confirm_popup` 在密钥空缺时**不弹窗**（宁可退回追问，也不发一个验不过的令牌）。
# CI 里没有 .env ⇒ 本地会绿、CI 会静默变成"没弹"（"该弹窗"的正例整体消失）。桩完
# 才是可复现的，且与本套件 §⑦ 那几条正例是同一件事。
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
settings.jwt_secret = "test-secret-for-confirm-tokens"

# 事实信封**只加**的那几个键（F1，20260930）：`tools.base.fact()` 构造，读端在 Python 侧
# （`is_noop` 读 `changed`），**刻意不进** `g._RCPT_META_KEYS`（进去就要同步 Rust 的
# `render_exec_row`）。所以"键必须都在白名单里"这条判据现在写成一个**闭集**：
# 白名单 ∪ 信封四个键，多出来的仍是没登记的键（见 tests/test_write_facts.py 第 ③ 节）。
_ENVELOPE_ONLY = {"changed", "target", "evidence", "noop"}

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


@contextlib.contextmanager
def patch(**kw):
    """临时替换 tools/base 模块级函数（工具调用时按模块全局名解析，故替换生效）。"""
    saved = {k: getattr(base, k) for k in kw}
    for k, v in kw.items():
        setattr(base, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(base, k, v)


class _Seq:
    """按序返回的桩（写前读 / 写后复核是两次独立读数）。"""

    def __init__(self, *vals):
        self.vals = list(vals)
        self.n = 0

    def __call__(self, *a, **k):
        v = self.vals[min(self.n, len(self.vals) - 1)]
        self.n += 1
        return v


class _Post:
    def __init__(self, ret):
        self.ret = ret
        self.calls: list = []

    def __call__(self, method, path, payload, config):
        self.calls.append((method, path, payload))
        return self.ret


def cfg(uid=7, role=ROLE_ADMIN):
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


def todo(text="给猫买罐头", date="2026-09-27", done=False):
    return {"text": text, "done": done, "date": date}


TODAY = datetime.date(2026, 9, 26)          # 固定钟面：相对日期断言才可复现
TOMORROW = "2026-09-27"


class _FrozenDate(datetime.date):
    """钟面固定成 TODAY 的 date（只换 today()，其余原样）。"""

    @classmethod
    def today(cls):
        return TODAY


class _FrozenDatetime:
    """`agent.adminops` 的 datetime 模块替身（它只用 date / timedelta）。"""

    date = _FrozenDate
    timedelta = datetime.timedelta
    datetime = datetime.datetime


# 上面那句"固定钟面"此前只做到**一半**：夹具里显式传 `today=TODAY` 的那半是固定的，
# 而**走真实工具**的那半（`create_dashboard_todo` 内部自己取 today）读的是真实时钟
# ——2026-09-27 那天起，"明天"解析出的 ISO 与夹具写死的 2026-09-27 对不上，⑤ 整段
# 四条转红，而那是**夹具过期**，不是行为回归（工具算出 09-28 是对的）。
# 修法是把工具路径的钟面也钉住，而不是把断言的期望值改成"跟着今天跑"——后者会让
# "相对日期"这组断言失去可复现性，也就失去了它存在的意义。
A.datetime = _FrozenDatetime


# ══════════════════════════════════════════════════════════════════
print("\n① 排期归一：认哪些写法、认不出来绝不猜")

check("标准口径原样通过", A.normalize_due_date("2026-09-27") == "2026-09-27")
check("两位月日补零（发出去的永远是等宽 ISO，字典序 = 时间序）",
      A.normalize_due_date("2026-9-7") == "2026-09-07",
      str(A.normalize_due_date("2026-9-7")))
check("斜杠与中文年月日都认（主人嘴里和 planner 手里都会出现）",
      A.normalize_due_date("2026/9/27") == "2026-09-27"
      and A.normalize_due_date("2026年9月27日") == "2026-09-27")
check("相对词按注入的今天算",
      A.normalize_due_date("今天", today=TODAY) == "2026-09-26"
      and A.normalize_due_date("明天", today=TODAY) == TOMORROW
      and A.normalize_due_date("后天", today=TODAY) == "2026-09-28"
      and A.normalize_due_date("大后天", today=TODAY) == "2026-09-29")
check("繁体/别名同族不作两套（後天=后天、今日=今天）",
      A.normalize_due_date("後天", today=TODAY) == "2026-09-28"
      and A.normalize_due_date("今日", today=TODAY) == "2026-09-26")
check("不带年份的「9月27日」按**当年**解释（是一条规则，不是猜）",
      A.normalize_due_date("9月27日", today=TODAY) == TOMORROW
      and A.normalize_due_date("9-27", today=TODAY) == TOMORROW)

for bad, why in [("下周三", "相对周几（今天是周几才算得出，翻不出来就别猜）"),
                 ("周五", "同上"),
                 ("九月底", "模糊说法"),
                 ("2026-02-30", "存在不了一天"),
                 ("9月31日", "存在不了一天"),
                 ("", "空串"),
                 (None, "没填"),
                 (True, "布尔不是日期（True 别被当成 1）"),
                 ("20260927", "缺分隔符的连写不认（认了会把 8 位数年份读成什么？）")]:
    check(f"认不出来 → None 让调用方零写：{why}",
          A.normalize_due_date(bad, today=TODAY) is None,
          f"{bad!r} → {A.normalize_due_date(bad, today=TODAY)!r}")


# ══════════════════════════════════════════════════════════════════
print("\n② 日期的念法与相对位置（确认卡上主人要能验算）")

check("ISO → 「9月27日」（不写 2026-09-27：主人说的是「明天」）",
      A.due_date_cn("2026-09-27") == "9月27日"
      and A.due_date_cn("2026-09-07") == "9月7日", A.due_date_cn("2026-09-07"))
check("认不出的原样带引号吐回去（不假装它是已知日期）",
      A.due_date_cn("下周") == "「下周」" and A.due_date_cn("") == "（没写日期）")
check("今天标（今天）、过去标（已过期）、以后不标（未来是默认，标了是噪音）",
      A._due_hint("2026-09-26", TODAY) == "（今天）"
      and A._due_hint("2026-09-20", TODAY) == "（已过期）"
      and A._due_hint("2026-09-27", TODAY) == "")
check("形状不对就不标（脏值不硬套一个相对说法）",
      A._due_hint("下周", TODAY) == "" and A._due_hint("", TODAY) == "")


# ══════════════════════════════════════════════════════════════════
print("\n③ 清单渲染（一行一条：哪条 / 哪天 / 完成没有）")

_rows = [todo("逾期的一条", "2026-09-20"), todo("今天要做的", "2026-09-26"),
         todo("没排期的一条", None), todo("做完的一条", "2026-09-26", done=True)]
_out = A.render_todo_list(_rows, today=TODAY)
lines = _out.split("\n")
check("抬头写总数与未完成数（narrator 一眼能答「我有几件事」）",
      lines[0] == "后台首页待办共 4 条（未完成 3 条）：", lines[0])
check("一行一条（揉成一句话会让它自己拆句子，拆错就把已完成读成未完成）",
      len(lines) == 5, f"{len(lines)} 行")
check("每行三样齐全：位次 / 正文 / 排期 / 完成态",
      lines[1] == "1. 逾期的一条 | 排期 9月20日（已过期） | 未完成", lines[1])
check("没排期的那条写「未排期」（不是一个空字段）",
      lines[3] == "3. 没排期的一条 | 未排期 | 未完成", lines[3])
check("完成态是读出来的、不是猜的",
      lines[4] == "4. 做完的一条 | 排期 9月26日（今天） | 已完成", lines[4])
check("空列表如实说空（这是**事实**，checker 照常 PASS 进回执）",
      A.render_todo_list([], today=TODAY) == "后台首页的待办列表是空的（一条都没有）。")
check("畸形行不炸（渲染层只退化不加戏）",
      "1. " in A.render_todo_list([{"weird": 1}], today=TODAY),
      A.render_todo_list([{"weird": 1}], today=TODAY))
check("超长正文截断带省略号（帧不撑爆提示词）",
      "…" in A.render_todo_list([todo("长" * 80)], today=TODAY))

# ── 另两类等着他处理的（留言待审 / 额度重置申请）─────────────────────────────
# 四态各一条。**`None` 与哨兵必须是两句话**：前者 = "这一类本次不适用"（私人清单的
# 调用方），后者 = "适用、但这一次没读到"——把后者渲染成空串，等于让主人听到
# "就这些"，而我们其实一个字都没读到（同族纪律见 `TODO_PENDING_UNREAD` 的注释）。
check("不适用（None）⇒ 一个字都不加（私人清单那一档的既有形态）",
      A.render_todo_pending(None) == "" and
      A.render_todo_list([todo()], today=TODAY, pending=None).count("\n") == 1)
check("两类都是 0 ⇒ 也不加（0 条不是一条待办）",
      A.render_todo_pending({"review": 0, "quota": 0}) == "")
check("有留言待审 ⇒ 说出条数、并写明**不在上面那张私人待办里**",
      "留言待审 3 条" in A.render_todo_pending({"review": 3, "quota": 0})
      and "不在上面这张私人待办里" in A.render_todo_pending({"review": 3, "quota": 0}),
      A.render_todo_pending({"review": 3, "quota": 0}))
_p = A.render_todo_pending({"review": 3, "quota": 2})
check("两类都有 ⇒ 一句里两样齐全（不印成两段，也不漏掉后一样）",
      "留言待审 3 条" in _p and "额度重置申请 2 份" in _p, _p)
check("  只有这一类 > 0 时只印这一类（「额度重置申请 0 份」是一句噪声）",
      "额度重置申请" not in A.render_todo_pending({"review": 1, "quota": 0}), "")
check("⭐ 没读到（哨兵）⇒ 明说「没读到、不确定有几条」，**绝不说成 0 条**",
      "没读到" in A.render_todo_pending(A.TODO_PENDING_UNREAD)
      and "不确定有几条" in A.render_todo_pending(A.TODO_PENDING_UNREAD),
      A.render_todo_pending(A.TODO_PENDING_UNREAD))
check("  畸形入参不炸（渲染层只退化不加戏）",
      A.render_todo_pending("有 3 条") == "" and
      A.render_todo_pending({"review": "三条", "quota": None}) == "")
check("  负数 / 布尔不当条数（`True` 是 int 的子类，会印出「留言待审 True 条」）",
      A.render_todo_pending({"review": True, "quota": -2}) == "")
check("空清单那一支也带上尾注（否则「待办是空的」正好把两类漏掉——那是最容易漏的分支）",
      "留言待审 1 条" in A.render_todo_list([], today=TODAY, pending={"review": 1}),
      A.render_todo_list([], today=TODAY, pending={"review": 1}))
check("  空清单 + 没读到 ⇒ 两句都说（空的是私人清单，不是那两类）",
      "一条都没有" in A.render_todo_list([], today=TODAY,
                                          pending=A.TODO_PENDING_UNREAD)
      and "没读到" in A.render_todo_list([], today=TODAY, pending=A.TODO_PENDING_UNREAD))
check("非空清单 + 有尾注 ⇒ 尾注在最后一行（在它后面加东西会让人读成第 N+1 条待办）",
      A.render_todo_list([todo()], today=TODAY, pending={"review": 1}).split("\n")[-1]
      .startswith("另有两件等着你处理"), "")

_added = A.render_todo_added("给猫买罐头", TOMORROW)
check("追加回执说清加的是什么、排在哪天、去哪儿看",
      "给猫买罐头" in _added and "排期 9月27日" in _added and "后台首页" in _added, _added)
check("追加回执带「后台已复核」（复核是真做过的，见 §⑤）",
      "后台已复核" in _added, _added)
check("未排期时回执直说未排期（不写一个空日子、也不留 2026- 那种半截）",
      "（未排期）" in A.render_todo_added("给猫买罐头", None)
      and "排期 9" not in A.render_todo_added("给猫买罐头", None)
      and "2026" not in A.render_todo_added("给猫买罐头", None),
      A.render_todo_added("给猫买罐头", None))


# ══════════════════════════════════════════════════════════════════
print("\n④ 技能展开：三种「缺了就不写」都在展开层挡住（零工具 + 注记）")


def expand(**params):
    return instantiate_plan("dashboard_todo_add", params)


out = expand()
check("正文缺失 → 零工具（「记一下」三个字本身就是正文时，那是口误不是待办）",
      out["tools"] == [] and "缺少正文" in out["note"], f"{out['tools']} / {out['note'][:50]}")
check("  注记里明写**不许**拿他这句话本身当正文猜一个",
      "不要" in out["note"], out["note"][:60])
out = expand(text="   ")
check("纯空白正文与缺失同路（空白不是一件事）", out["tools"] == [])

out = expand(text="长" * (base._TODO_TEXT_LIMIT + 1))
check("正文超上限 → 零工具（**不截断**：替主人改字是另一种错）",
      out["tools"] == [] and "太长" in out["note"], f"{out['tools']} / {out['note'][:50]}")
check("  上限文案与工具侧同源（同一份常量，写小了会白挡）",
      str(base._TODO_TEXT_LIMIT) in out["note"], out["note"][:60])
out = expand(text="正好" * 100)          # 200 字 = 上限，允许
check("正好到上限仍放行（边界是「超过」而不是「达到」）",
      len(out["tools"]) == 1, out["tools"])

out = expand(text="给猫买罐头", date="下周三")
check("排期翻不出来 → 零工具 + 追问（绝不挑一个日子顶上）",
      out["tools"] == [] and "认不出来" in out["note"], f"{out['tools']} / {out['note'][:60]}")
check("  注记要求如实问清哪一天，不是含糊过去",
      "问清" in out["note"], out["note"][:60])

out = expand(text="给猫买罐头", date="明天")
spec = out["tools"][0] if out["tools"] else ""
check("正例（带排期）：正文与**翻好的**日期一起进 TOOLS 行",
      spec == 'create_dashboard_todo({"text": "给猫买罐头", "date": "'
              + A.normalize_due_date("明天") + '"})', spec)
check("  注记里把排期念成人话（与确认卡同源）",
      f"排期 {A.due_date_cn(A.normalize_due_date('明天'))}" in out["note"],
      out["note"][:70])

out = expand(text="给猫买罐头")
spec = out["tools"][0] if out["tools"] else ""
check("正例（没排期）：spec 里**没有** date 键（不写 null，省得两种「没填」混在一起）",
      spec == 'create_dashboard_todo({"text": "给猫买罐头"})', spec)
check("  注记写「（未排期）」", "（未排期）" in out["note"], out["note"][:70])
check("展开出的工具名在注册表里（否则 execute 只能回「未知工具」错误帧）",
      "create_dashboard_todo" in {t.name for t in base.get_all_tools()})
check("这一族是**自由文本**那一族（不在名字通道、也不在 own 通道——判据错位比没有判据更糟）",
      _FREE_TEXT_WRITE_SKILLS == frozenset({"dashboard_todo_add", "dashboard_todo_done",
                                            "dashboard_todo_reschedule"}),
      str(_FREE_TEXT_WRITE_SKILLS))
# 桶成员资格只说"目标是自由文本"，**展开函数要按技能名三分**（第十轮加"勾完成"、
# 批 G 加"改排期"）：这条钉的是**三个技能名都真的在自己的路径上**——漏了那次分派的
# 后果是静默的，"勾完成"会被 `_expand_todo_skill` 展开成 `create_dashboard_todo`
# （多记一条待办），"改排期"同样会**又记一条**（而列表上多出来的那一行看起来就是主人
# 想要的那条）；所以判据落在"展开出的工具名"上，而不是"桶里有几个名字"。
check("  桶内三个技能各自展开成自己的工具（勾完成/改排期都不会展开成「加一条」）",
      "complete_dashboard_todo" in "".join(
          instantiate_plan("dashboard_todo_done", {"text": "给猫买罐头"})["tools"])
      and "create_dashboard_todo" in "".join(
          instantiate_plan("dashboard_todo_add", {"text": "给猫买罐头"})["tools"])
      and "reschedule_dashboard_todo" in "".join(
          instantiate_plan("dashboard_todo_reschedule",
                           {"text": "给猫买罐头", "date": "明天"})["tools"]),
      str(instantiate_plan("dashboard_todo_done", {"text": "给猫买罐头"})["tools"]))


# ══════════════════════════════════════════════════════════════════
print("\n⑤ create_dashboard_todo：零调用 / 失败如实 / 复核判净变化")

post = _Post("1")
with patch(_admin_get=lambda p, c: [todo()], _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "  "}, config=cfg())
    check("空正文 → unavailable，零网络", r.kind == "unavailable" and post.calls == [],
          f"{r.kind}: {r}")
    r = base.create_dashboard_todo.invoke({"text": "长" * (base._TODO_TEXT_LIMIT + 1)},
                                          config=cfg())
    check("超上限 → unavailable，零网络（服务端还有一道，这里只是不发注定被拒的请求）",
          r.kind == "unavailable" and post.calls == [], f"{r.kind}: {r}")
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头", "date": "下周三"}, config=cfg())
    check("排期认不出 → unavailable，零网络，措辞要求问清哪一天",
          r.kind == "unavailable" and post.calls == [] and "问清" in r, f"{r.kind}: {r}")
    check("以上全都没发 POST", post.calls == [], str(post.calls))

post = _Post("1")
with patch(_admin_get=lambda p, c: base.unavailable("后台读不到"), _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("写前读失败 → unavailable，**零 POST**（读不到基线就不动手）",
          r.kind == "unavailable" and post.calls == [], f"{r.kind}: {r}")

post = _Post("1")
with patch(_admin_get=lambda p, c: {"weird": 1}, _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("写前读回的形态不对 → unavailable，零 POST（不是列表就没法复核）",
          r.kind == "unavailable" and post.calls == [], f"{r.kind}: {r}")

post = _Post("1")
with patch(_admin_get=lambda p, c: [todo(f"第{i}条") for i in range(base._TODO_MAX_ROWS)],
           _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("满员 → unavailable，零 POST（上限与 Rust MAX_TODOS 同源）",
          r.kind == "unavailable" and post.calls == [] and str(base._TODO_MAX_ROWS) in r,
          f"{r.kind}: {r}")

post = _Post("1")
with patch(_admin_get=lambda p, c: [todo("已有一条")],
           _admin_request=lambda m, p, pl, c: base.unavailable("后台 500")):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("POST 失败 → 原样透传 unavailable（不把「发出去了」当「记下了」）",
          r.kind == "unavailable" and "500" in r, f"{r.kind}: {r}")

# 写后复核：三次独立读数（写前 / POST / 读回）——读回里**没多出这一条**就是没生效
post = _Post("1")
seq = _Seq([todo("已有一条")], [todo("已有一条")])
with patch(_admin_get=seq, _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("读回列表里没多出这一条 → unavailable（**不许**声称已记下）",
          r.kind == "unavailable" and "没多出" in r and "不要声称已记下" in r,
          f"{r.kind}: {r}")
check("  发出去的请求形状：POST /api/protected/todos/item（只追加，不整份覆盖）",
      post.calls and post.calls[0][0] == "POST"
      and post.calls[0][1] == "/api/protected/todos/item"
      and post.calls[0][2] == {"text": "给猫买罐头"}, str(post.calls[0][:2]))

post = _Post("1")
seq = _Seq([todo("已有一条")], [todo("已有一条"), todo("给猫买罐头", TOMORROW)])
with patch(_admin_get=seq, _admin_request=post):
    r = base.create_dashboard_todo.invoke(
        {"text": "给猫买罐头", "date": "明天"}, config=cfg())
    check("写后读到了这一条 → ok，回执写明加的是什么、排在哪天",
          r.kind == "ok" and "给猫买罐头" in r and f"排期 {A.due_date_cn(TOMORROW)}" in r,
          f"{r.kind}: {r}")
    check("  回执 meta 是结构化回执（跨轮执行记忆的原料）",
          r.meta.get("op") == "dashboard_todo_add" and r.meta.get("text") == "给猫买罐头"
          and r.meta.get("date") == TOMORROW and r.meta.get("count") == 2, str(r.meta))
    check("  日期是**翻好的** ISO 发出去（「明天」不会原样落到库里）",
          post.calls[0][2] == {"text": "给猫买罐头", "date": TOMORROW}, str(post.calls[0][2]))
    check("  回执不留 uid（detail 进生产库、还可能被 narrator 念出来）",
          "uid" not in json.dumps(r.meta), json.dumps(r.meta, ensure_ascii=False))

post = _Post("1")
seq = _Seq([todo("已有一条")], [todo("已有一条"), todo("给猫买罐头", None)])
with patch(_admin_get=seq, _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("没排期时**不带** date 键（不写 null）",
          r.kind == "ok" and post.calls[0][2] == {"text": "给猫买罐头"},
          f"{r.kind}: {r} / {post.calls[0][2]}")
    check("  回执写「未排期」", "未排期" in r, str(r))

# 净变化才是判据：主人本来就记过一模一样的一条时，一次**失败**的追加不能被判成功
post = _Post("1")
seq = _Seq([todo("给猫买罐头", TOMORROW), todo("已有一条")],
           [todo("给猫买罐头", TOMORROW), todo("已有一条")])
with patch(_admin_get=seq, _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头", "date": "明天"}, config=cfg())
    check("列表里本来就有同内容同排期的一条 → 仍判没生效（只判「有这一条」会假成功）",
          r.kind == "unavailable" and "没多出" in r, f"{r.kind}: {r}")

post = _Post("1")
seq = _Seq([todo("给猫买罐头", TOMORROW)],
           [todo("给猫买罐头", TOMORROW), todo("给猫买罐头", TOMORROW)])
with patch(_admin_get=seq, _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头", "date": "明天"}, config=cfg())
    check("本来就有一条、现在两条 → 净变化 +1，判成功（同键计数，不是简单的有/无）",
          r.kind == "ok" and r.meta.get("count") == 2, f"{r.kind}: {r}")

post = _Post("1")
seq = _Seq([todo("已有一条")], base.unavailable("读不回来了"))
with patch(_admin_get=seq, _admin_request=post):
    r = base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg())
    check("POST 后读不回 → unavailable 且措辞明写「未确认生效」",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

c = _Post("1")
calls: list = []


class _RecClient:
    def request(self, method, url, headers=None, json=None, timeout=None):
        calls.append((method, url))
        return type("R", (), {"status_code": 200,
                              "json": staticmethod(lambda: {"code": 200, "data": "1"})})()


_real = base._client
try:
    base._client = _RecClient()
    base.create_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg(0))
    check("uid ≤ 0 → 一个请求都不发（读写两条通道都由身份哨兵兜住）",
          calls == [], str(calls))
finally:
    base._client = _real


# ══════════════════════════════════════════════════════════════════
print("\n⑥ list_dashboard_todos：空是事实、读不到是故障（两者不能混说）")

with patch(_admin_get=lambda p, c: [], _own_get=lambda p, c: {"pendingReview": 0, "pendingQuota": 0}):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("真的没记过 → kind=empty（**事实**，checker 照常 PASS 进回执）",
          r.kind == "empty" and "空的" in r, f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: base.unavailable("后台读不到"),
           _own_get=lambda p, c: {"pendingReview": 0, "pendingQuota": 0}):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("读不到 → unavailable（**不许**说成「你还没记过待办」）",
          r.kind == "unavailable", f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: {"weird": 1},
           _own_get=lambda p, c: {"pendingReview": 0, "pendingQuota": 0}):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("形态不对 → unavailable", r.kind == "unavailable", f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: [todo("给猫买罐头", TOMORROW)],
           _own_get=lambda p, c: {"pendingReview": 0, "pendingQuota": 0}):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("正常 → ok，给 narrator 的是渲染好的清单（一行一条）",
          r.kind == "ok" and "给猫买罐头" in r and r.meta.get("count") == 1,
          f"{r.kind}: {r.meta}")

# ── 那两类计数真的进了同一帧（主人问「有没有什么事等着我处理」时它说得出来）──
# 读的是**红点那一份汇总**（`/notifications/summary`）——不是第二份计数来源。
_summary = {"pendingReview": 3, "pendingQuota": 1}
_reads: list = []


def _own_spy(p, c):
    _reads.append(p)
    return _summary


with patch(_admin_get=lambda p, c: [todo("给猫买罐头", TOMORROW)], _own_get=_own_spy):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("⭐ 两类计数进同一帧（留言待审 3 条、额度重置申请 1 份）",
          r.kind == "ok" and "留言待审 3 条" in r and "额度重置申请 1 份" in r,
          f"{r.kind}: {r}")
    check("  **只多一次读**、读的是红点那一份汇总（不是自己去数队列："
          "第二份计数来源会与红点各自演化）",
          _reads == ["/api/protected/notifications/summary"], str(_reads))

with patch(_admin_get=lambda p, c: [], _own_get=lambda p, c: _summary):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("⭐ 私人清单空、但那两类有 ⇒ **不是** empty（回一句「什么都没有」正好漏掉它们）",
          r.kind == "ok" and "留言待审 3 条" in r, f"{r.kind}: {r}")

# 汇总读不到：**待办清单本身照常给**，只把那两类如实说成"没读到"——
# 不能因为红点那份挂了就把整次读待办判失败（主人问的是待办，那是读到了的）。
with patch(_admin_get=lambda p, c: [todo("给猫买罐头", TOMORROW)],
           _own_get=lambda p, c: base.unavailable("汇总读不到")):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("⭐ 汇总读不到 ⇒ 待办照常给，另加一句「没读到，不确定有几条」",
          r.kind == "ok" and "给猫买罐头" in r and "没读到" in r and "不确定有几条" in r,
          f"{r.kind}: {r}")
    check("  **不当成 0 条**（那会让主人听到「就这些」，而我们根本没读到）",
          "留言待审 0" not in r and "额度重置申请 0" not in r, str(r))

with patch(_admin_get=lambda p, c: [todo()], _own_get=lambda p, c: {"pendingReview": None}):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("汇总回来了但键缺失（旧后端）⇒ 也不编 0 条",
          r.kind == "ok" and "留言待审" not in r and "额度重置申请" not in r, str(r))


# ══════════════════════════════════════════════════════════════════
print("\n⑦ 授权与确认闸：读走后台读、写走后台写，且**每次都弹卡**")

check("读那条要 admin.console（普通登录用户在前端也打不开后台首页：取 own 会让"
      "授权层说「允许」而 Rust 随后 403）",
      authz.TOOL_SCOPE["list_dashboard_todos"] == authz.SCOPE_ADMIN_CONSOLE
      and authz.TOOL_SCOPE["list_dashboard_todos"] in authz._HARD_SCOPES,
      authz.TOOL_SCOPE["list_dashboard_todos"])
check("写那条要 write.console，且落在同意闸的 scope 里",
      authz.TOOL_SCOPE["create_dashboard_todo"] == authz.SCOPE_WRITE_CONSOLE
      and authz.requires_consent(Principal(uid=7, role=ROLE_ADMIN),
                                 "create_dashboard_todo"))
check("目标没处可核对 ⇒ 进「一律弹窗」族（同轮命令即确认那条捷径被结构性关掉）",
      "create_dashboard_todo" in authz._ALWAYS_CONFIRM_TOOLS)
check("  它在场时任何措辞都不算同意（含命令式）",
      not authz.consent_granted(Principal(uid=7, role=ROLE_ADMIN),
                                "create_dashboard_todo", "帮我记一下明天交房租")
      and not authz.consent_granted(Principal(uid=7, role=ROLE_ADMIN),
                                    "create_dashboard_todo", "记吧"))
check("  「一律弹窗」族的每一件都必须有给主人看的理由（未声明的会被弹窗层兜底成"
      "一句空话）",
      "create_dashboard_todo" in authz._CONSENT_WHY_TOOL)
check("非管理员：写工具对普通访客不放行（权限先于确认）",
      not authz.check(Principal(uid=9, role=ROLE_USER),
                      "create_dashboard_todo").allowed)

_q = A.render_confirm_question([{"tool": "create_dashboard_todo",
                                 "args": {"text": "给猫买罐头", "date": TOMORROW}}],
                               {}, None, None, None)
check("确认卡问句逐字念出正文（主人只能靠这两样核对是不是他要记的那条）",
      "「给猫买罐头」" in _q and "加一条" in _q, _q)
check("排期翻成人话（不写 2026-09-27：主人说的是「明天」，他要能验算这个日子）",
      "排期 9月27日" in _q and "2026-09-27" not in _q, _q)
check("没排期就问句直说未排期", "排期 未排期" in A.render_confirm_question(
    [{"tool": "create_dashboard_todo", "args": {"text": "给猫买罐头"}}],
    {}, None, None, None))
check("认不出的排期原样带引号（不假装它是已知日期）",
      "「下周三」" in A.render_confirm_question(
          [{"tool": "create_dashboard_todo",
            "args": {"text": "给猫买罐头", "date": "下周三"}}], {}, None, None, None))
check("畸形 spec 不炸（渲染层对空参数只退化不加戏）",
      "（没写内容）" in A.render_confirm_question(
          [{"tool": "create_dashboard_todo", "args": {}}], {}, None, None, None))
check("弹窗理由文案点名**是哪份列表**（「后台首页的待办」），不让人以为是公开发布",
      "后台首页的待办列表" in authz._CONSENT_WHY_TOOL["create_dashboard_todo"][0],
      authz._CONSENT_WHY_TOOL["create_dashboard_todo"][0])

# 真实执行路径：写操作卡在同意闸上时，**这一轮就弹卡**（而不是产一个错误帧让它
# 去追问一轮——那正是 20260921 修掉的死路）。「一律弹窗」族连命令措辞都不例外。
CALLS: list = []


class _FakeTool:
    def __init__(self, out):
        self.out = out

    def invoke(self, args):
        CALLS.append(args)
        return self.out


def _run(msg, spec, grant=None):
    CALLS.clear()
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = "dashboard_todo_add"
    obj["tools"] = [spec]
    state = {**plan_state(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content=msg)]}
    if grant:
        state["confirm_grant"] = grant
    return execute_node(state, cfg())


SPEC = ('create_dashboard_todo({"text": "给猫买罐头", "date": "' + TOMORROW + '"})')
# 「这句在同意闸眼里**就是一条命令**」是下面那组断言的前提，得先把它钉住：
# `_console_command` 是同意闸自己那把尺子（句首动词 + 明确目标 + 「确认…」骨架），
# 而「新建待办，叫X」正好落在它的目标形式（`叫X`）与动词表（新建）里。不钉这一条，
# 用一个它本来就判不成的说法去测，"弹卡"会因为**另一个原因**成立——测的是空气。
_CMD_MSG = "新建待办，叫明天交房租"
_saved = g._TOOL_MAP.get("create_dashboard_todo")
try:
    check("（前提）这句在同意闸自己的尺子下就是一条命令",
          authz._console_command(_CMD_MSG, "create_dashboard_todo") is True, _CMD_MSG)
    g._TOOL_MAP["create_dashboard_todo"] = _FakeTool(
        base.ok("已在后台首页的待办里加了一条「给猫买罐头」（排期 9月27日）",
                meta={"op": "dashboard_todo_add", "text": "给猫买罐头",
                      "date": TOMORROW, "count": 1}))
    for msg, why in [(_CMD_MSG, "判成命令的一句（同意闸本来会放行）"),
                     ("帮我记一下明天交房租", "命令式"),
                     ("记一下：明天交房租", "祈使句"),
                     ("给我记个明天交房租的待办", "「给我…」式")]:
        r = _run(msg, SPEC)
        pop = r.get("pending_confirm") or {}
        check(f"{why} → 弹卡且零调用（一律弹窗族不吃「同轮命令即确认」）",
              CALLS == [] and r.get("receipts") == [] and bool(pop), str(sorted(r)))
        check("  卡上念的是**正文与排期**（主人只能靠这两样核对是不是他要记的那条）",
              "「给猫买罐头」" in pop.get("q", "") and "排期 9月27日" in pop.get("q", ""),
              pop.get("q", ""))
        payload = confirm.inspect(pop.get("token") or "") or {}
        check("  令牌载荷里的 skill 与参数就是这一件（卡上写什么就签什么）",
              payload.get("skill") == "dashboard_todo_add"
              and payload.get("specs") == [{"tool": "create_dashboard_todo",
                                            "args": {"text": "给猫买罐头", "date": TOMORROW}}],
              str(payload))
        check("  令牌带失效时刻（前端据此到点自动结算，卡片不会永远停在乐观态）",
              isinstance(pop.get("exp"), int) and pop["exp"] > 0, str(pop.get("exp")))
        check("  这一轮**零执行**（弹卡轮只弹卡）", CALLS == [] and r["receipts"] == [])
    for msg, why in [("你能帮我记个待办吗", "提问"),
                     ("如果记一条明天交房租的话", "假设")]:
        r = _run(msg, SPEC)
        check(f"{why} → 不弹卡（把提问读成意图就错了）→ consent_required 零调用",
              "pending_confirm" not in r or not r.get("pending_confirm"),
              str(sorted(r)))
        check("  与提问/假设同族：走既有追问链路，一个字节都不写",
              CALLS == [] and r.get("receipts") == [])
    r = _run("给猫买罐头", SPEC, grant={"token": "x"})
    check("确认轮（主人点了确定）→ 放行执行（「一律弹窗」不是「永不执行」）",
          CALLS == [{"text": "给猫买罐头", "date": TOMORROW}], str(CALLS))
    check("回执带执行角色与 op（跨轮执行记忆只认结构化回执，不认叙述）",
          r["receipts"] and r["receipts"][0]["principal_role"] == "admin"
          and r["receipts"][0]["op"] == "dashboard_todo_add", str(r["receipts"]))
except BaseException as e:  # noqa: BLE001
    check(f"execute 待办写路径测试异常：{type(e).__name__}: {e}", False)
finally:
    if _saved is None:
        g._TOOL_MAP.pop("create_dashboard_todo", None)
    else:
        g._TOOL_MAP["create_dashboard_todo"] = _saved


# ══════════════════════════════════════════════════════════════════
print("\n⑧ 接线：读技能不进写名单，且后台待办的写通道**全是「只动一行」的最小通道")

from agent.skills import WRITE_SKILL_NAMES  # noqa: E402

check("写技能名单里有 dashboard_todo_add",
      "dashboard_todo_add" in WRITE_SKILL_NAMES, str(sorted(WRITE_SKILL_NAMES)))
check("读技能 dashboard_todo_list **不在**写名单里（它一个字节都不改）",
      "dashboard_todo_list" not in WRITE_SKILL_NAMES)
_gsrc = (ROOT / "agent" / "skills.py").read_text(encoding="utf-8")
check("展开分支真的接在 instantiate_plan 里（漏接就走 fail-closed 那一支：能力结构性不可达）",
      "in _FREE_TEXT_WRITE_SKILLS" in _gsrc and "elif skill.name" in _gsrc)
check("  它排在写通道的 fail-closed 兜底**之前**（排在后面等于永远走不到那一条）",
      _gsrc.index("in _FREE_TEXT_WRITE_SKILLS")
      < _gsrc.index('elif skill.name not in ("article_status", "article_tags")'))
check("工具侧**没有** PUT 整份覆盖这条路（agent 手里没有那份列表：先读再写会把主人"
      "刚做的改动抹掉，发一份自己拼的等于清空他的待办）",
      '"/api/protected/todos"' in (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
      and '_admin_request("PUT", "/api/protected/todos"' not in
      (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
      and '_admin_request("POST", "/api/protected/todos/item"' in
      (ROOT / "tools" / "base.py").read_text(encoding="utf-8"))
# 批 G 起写通道有三条（追加一条 / 翻完成标记 / 改排期日），三条都是"只动一行"的最小
# 通道——这一条锁的是**第三条真的存在且与第二条同源**：两条按正文定位的通道共用一处
# 实现（`_admin_todo_post`），「改一处必须同步另一处」在这里被结构上消掉了。
_bsrc = (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
check("三条最小通道各自点名自己的端点（追加那条在自己的臂里、另两条共用 `_admin_todo_post`）",
      '_admin_request("POST", "/api/protected/todos/item"' in _bsrc
      and '_admin_todo_post("/api/protected/todos/done"' in _bsrc
      and '_admin_todo_post("/api/protected/todos/date"' in _bsrc,
      str(sorted({p for p in ("/api/protected/todos/item", "/api/protected/todos/done",
                              "/api/protected/todos/date") if p in _bsrc})))
check("  按正文定位的两条**共用同一个函数**（`_admin_todo_post` 一处实现：uid/鉴权/HTTP/"
      "非 JSON/业务码五项失败映射不许各写一遍——那是这一族最容易分叉的地方）",
      _bsrc.count("def _admin_todo_post(") == 1
      and _bsrc.count('_admin_todo_post("/api/protected/todos/') == 2,
      str(_bsrc.count('_admin_todo_post("/api/protected/todos/')))

# ══════════════════════════════════════════════════════════════════
print("\n⑨ complete_dashboard_todo：按正文定位（0 条 / 多条零写，1 条才动手）")


class _HResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Http:
    """桩 httpx 客户端：只记 POST（这一族的写通道 `_admin_todo_done_post` 直连
    `_client.post`，不走 `_admin_request`——正因为"非 200 要映射成目标类失败"）。"""

    def __init__(self, post=None, exc=None):
        self.post_ret, self.exc = post, exc
        self.calls: list = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("POST", url, headers or {}, json))
        if self.exc:
            raise self.exc
        return self.post_ret


def _done_ok(text="给猫买罐头"):
    return _HResp(200, {"code": 200,
                        "data": {"text": text, "done": True, "date": TOMORROW}})


_OTHER = todo("已经办完的另一条")


def _call(text="给猫买罐头", uid=7):
    return base.complete_dashboard_todo.invoke({"text": text}, config=cfg(uid))


with patch(_admin_get=lambda p, c: [_OTHER], _client=_Http()):
    r = _call()
    check("列表里没有这一条 → not_found（**不是**服务不可用：主人该做的是改说法，"
          "不是稍后再试）",
          r.kind == "not_found" and "没有「给猫买罐头」这一条" in r, f"{r.kind}: {r}")
    check("  一个字节都不发（本地认不出就不发注定被拒的请求）",
          base._client.calls == [], str(base._client.calls))

with patch(_admin_get=lambda p, c: [], _client=_Http()):
    r = _call()
    check("列表整份是空的 → 如实说「没有可勾的」（与「没有这一条」分开：他要先记一条）",
          r.kind == "not_found" and "空的" in r, f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: [todo("给猫买罐头"), todo("给猫买罐头", None)], _client=_Http()):
    r = _call()
    check("两条同名 → not_found + 如实说分不清（**绝不替主人挑一条**：挑错的那次在"
          "列表上看起来和挑对一模一样）",
          r.kind == "not_found" and "2 条" in r and "分不清" in r, f"{r.kind}: {r}")
    check("  多条同名同样零写", base._client.calls == [], str(base._client.calls))

with patch(_admin_get=lambda p, c: [todo("买 菜"), todo("买菜。")], _client=_Http()):
    r = _call("买菜")
    check("定位是**逐字相等**（模糊匹配会在两条相近的待办里挑错一条）",
          r.kind == "not_found", f"{r.kind}: {r}")

cli = _Http(_done_ok())
with patch(_admin_get=_Seq([todo("给猫买罐头")], [todo("给猫买罐头", done=True)]), _client=cli):
    r = _call()
    check("恰好一条 → ok，回执写明勾的是哪一条、且已复核",
          r.kind == "ok" and "给猫买罐头" in r and "复核" in r, f"{r.kind}: {r}")
    check("  恰好一次 POST，路径与 body 逐字（只发 text/done 两键）",
          len(cli.calls) == 1 and cli.calls[0][1].endswith("/api/protected/todos/done")
          and cli.calls[0][3] == {"text": "给猫买罐头", "done": True},
          str(cli.calls[0][1:]))
    check("  回执 meta 是结构化回执，且 before/after 都在白名单里（漏了是静默丢键）",
          r.meta.get("op") == "dashboard_todo_done" and r.meta.get("before") == "未完成"
          and r.meta.get("after") == "已完成"
          and set(r.meta) <= set(g._RCPT_META_KEYS) | _ENVELOPE_ONLY, str(r.meta))
    check("  零改动的判据是信封里的 changed（F1），不是「有没有走短路」",
          base.is_noop(r.meta) is False and r.meta.get("changed") is True, str(r.meta))
    check("  回执不留 uid、不留正文之外的私货（detail 进生产库、会被 narrator 念出来）",
          "uid" not in json.dumps(r.meta), json.dumps(r.meta, ensure_ascii=False))

# ⭐ 写后复核的判据是"那一行 **done 翻转**"，不是"列表里有没有这么一条"——那一行
# 在写之前就在（这是翻标记不是新增），只判"存在"会把每一次失败都判成成功。
cli = _Http(_done_ok())
with patch(_admin_get=_Seq([todo("给猫买罐头")], [todo("给猫买罐头", done=False)]), _client=cli):
    r = _call()
    check("⭐ 写后复核仍是未完成 → **kind == unavailable**（措辞之外必须判 kind："
          "只断文案是假绿）",
          r.kind == "unavailable" and "不要声称已勾完成" in r, f"{r.kind}: {r}")

cli = _Http(_done_ok())
with patch(_admin_get=_Seq([todo("给猫买罐头")], base.unavailable("读不回来了")), _client=cli):
    r = _call()
    check("写后读不回 → unavailable 且明写「未确认生效」",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

cli = _Http(_done_ok())
with patch(_admin_get=_Seq([todo("给猫买罐头")], [todo("别的")]), _client=cli):
    r = _call()
    check("写后那一行不见了 → unavailable（不能说成勾成了）",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: base.unavailable("后台读不到"), _client=_Http()):
    r = _call()
    check("写前读失败 → unavailable + 「本次未改动」，**零 POST**",
          r.kind == "unavailable" and "本次未改动" in r and base._client.calls == [],
          f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: {"weird": 1}, _client=_Http()):
    r = _call()
    check("写前读回的形态不对 → unavailable，零 POST",
          r.kind == "unavailable" and base._client.calls == [], f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: [todo("给猫买罐头")], _client=_Http()):
    r = base.complete_dashboard_todo.invoke({"text": "  "}, config=cfg())
    check("空正文 → unavailable，零网络（「勾一下」三个字里没有可勾的对象）",
          r.kind == "unavailable" and base._client.calls == [], f"{r.kind}: {r}")
    r = base.complete_dashboard_todo.invoke({"text": "长" * (base._TODO_TEXT_LIMIT + 1)},
                                            config=cfg())
    check("超上限 → unavailable，零网络（**不截断**：截断等于替主人改字，改完就不是同一行了）",
          r.kind == "unavailable" and base._client.calls == [], f"{r.kind}: {r}")

# 幂等**不短路**：写前就是完成态也照发请求（后端那个分支是真 no-op），结论由复核给
cli = _Http(_done_ok())
_seq = _Seq([todo("给猫买罐头", done=True)], [todo("给猫买罐头", done=True)])
with patch(_admin_get=_seq, _client=cli):
    r = _call()
    check("本来就是完成状态 → 仍发一次请求（不短路），回执如实说**未发生变更**",
          r.kind == "ok" and "本来就是完成状态" in r and "这次没有发生任何变更" in r
          and len(cli.calls) == 1, f"{r.kind}: {r}")
    check("  回执 meta 的 before 是已完成（跨轮记忆里能区分「刚勾的」与「本来就是」）",
          r.meta.get("before") == "已完成" and r.meta.get("after") == "已完成", str(r.meta))

_plain = _Http(_done_ok())
_saved_client = base._client
try:
    base._client = _plain
    base.complete_dashboard_todo.invoke({"text": "给猫买罐头"}, config=cfg(0))
    check("uid ≤ 0 → 一个请求都不发（身份不明时不猜「勾谁的」）",
          _plain.calls == [], str(_plain.calls))
finally:
    base._client = _saved_client


# ══════════════════════════════════════════════════════════════════
print("\n⑩ 后端非 200 的映射：目标类失败 → not_found（落进 unavailable 会诱发重试循环）")

_REFUSE = "有 2 条待办都叫「给猫买罐头」，分不清是哪一条（先到后台首页把其中一条改个说法）"
cli = _Http(_HResp(200, {"code": 500, "message": _REFUSE}))
with patch(_admin_get=lambda p, c: [todo("给猫买罐头")], _client=cli):
    r = _call()
    check("后端非 200 业务码 → not_found（**不是** unavailable：政策/定位失败不是"
          "「稍后再试」）", r.kind == "not_found", f"{r.kind}: {r}")
    check("  文案**逐字等于后端那句**（agent 侧任何复述都会在判据变更那天变成假话）",
          str(r) == _REFUSE, str(r))

cli = _Http(_HResp(403))
with patch(_admin_get=lambda p, c: [todo("给猫买罐头")], _client=cli):
    r = _call()
    check("403 → unavailable 且写明「仅管理员可用」（这是身份问题，不是目标问题）",
          r.kind == "unavailable" and "管理员" in r, f"{r.kind}: {r}")

cli = _Http(exc=RuntimeError("boom"))
with patch(_admin_get=lambda p, c: [todo("给猫买罐头")], _client=cli):
    r = _call()
    check("请求抛异常 → unavailable + 「不要声称已改好」",
          r.kind == "unavailable" and "不要声称已改好" in r, f"{r.kind}: {r}")


# ══════════════════════════════════════════════════════════════════
print("\n⑪ 确认闸：勾完成与「加一条」同族——每次都弹卡（判据是行为，不是文案）")

_ADM = Principal(uid=7, role=ROLE_ADMIN)
check("写那条要 write.console（同一道门：/api/protected/todos/done 在 auth_guard 之后）",
      authz.TOOL_SCOPE["complete_dashboard_todo"] == authz.SCOPE_WRITE_CONSOLE
      and authz.requires_consent(_ADM, "complete_dashboard_todo"))
check("进「一律弹窗」族：任何措辞都不算同意（含命令式）",
      "complete_dashboard_todo" in authz._ALWAYS_CONFIRM_TOOLS
      and not authz.consent_granted(_ADM, "complete_dashboard_todo", "把那条待办勾了")
      and not authz.consent_granted(_ADM, "complete_dashboard_todo", "把它标记成完成"))
check("  非管理员不放行（权限先于确认：弹窗都到不了）",
      not authz.check(Principal(uid=9, role=ROLE_USER),
                      "complete_dashboard_todo").allowed)
_why_done = authz._CONSENT_WHY_TOOL["complete_dashboard_todo"][0]
_why_add = authz._CONSENT_WHY_TOOL["create_dashboard_todo"][0]
_why_freeze = authz._CONSENT_WHY_TOOL["freeze_account"][0]
check("  卡上给主人的理由与「加一条」「冻结」**互不同形**（同一句话换动词最容易被读错）",
      _why_done != _why_add and _why_done != _why_freeze and _why_add != _why_freeze,
      _why_done[:40])
check("  理由点明「只翻完成标记、不增不删」（主人要能看出这一下不动列表的其他部分）",
      "不新增也不删除" in _why_done and "正文与排期一个字都不动" in _why_done, _why_done)
_fr = authz.consent_frame("complete_dashboard_todo", _ADM)
check("  未确认帧带的是**这一件**的理由与要求（不是 scope 兜底那句空话）",
      _why_done in _fr and f"待确认[{authz.REASON_CONSENT}]" in _fr, _fr[:80])


# ══════════════════════════════════════════════════════════════════
print("\n⑫ 技能展开：勾完成**绝不**展开成「加一条」（漏了二分就是静默多记一条待办）")

out = instantiate_plan("dashboard_todo_done", {"text": "交房租"})
check("tools 恰为 complete_dashboard_todo 一条，正文原样进 spec",
      out["tools"] == ['complete_dashboard_todo({"text": "交房租"})'], str(out["tools"]))
check("  展开出的工具名**不是** create_dashboard_todo（这条是桶内二分的锁）",
      all("create_dashboard_todo" not in t for t in out["tools"]), str(out["tools"]))
check("  工具名在注册表里（否则 execute 只能回「未知工具」错误帧）",
      "complete_dashboard_todo" in {t.name for t in base.get_all_tools()})
check("  注记写清「只翻完成标记」（跨轮记忆与卡面同源）",
      "勾成完成" in out["note"] and "正文与排期都不动" in out["note"], out["note"])

out = instantiate_plan("dashboard_todo_done", {})
check("缺正文 → 零工具 + 非空注记（要求问清是哪一条，且**不许**替他挑）",
      out["tools"] == [] and "问清" in out["note"] and "不要" in out["note"],
      f"{out['tools']} / {out['note'][:60]}")

out = instantiate_plan("dashboard_todo_done", {"text": "长" * (base._TODO_TEXT_LIMIT + 1)})
check("正文超上限 → 零工具 + 注记（上限与工具侧同源）",
      out["tools"] == [] and "太长" in out["note"]
      and str(base._TODO_TEXT_LIMIT) in out["note"], f"{out['tools']} / {out['note'][:50]}")

check("它在写技能名单里（漏了会落进通用模板分支，产出一次不成形的写）",
      "dashboard_todo_done" in WRITE_SKILL_NAMES, str(sorted(WRITE_SKILL_NAMES)))


# ══════════════════════════════════════════════════════════════════
print("\n⑬ 卡面：正文与排期一字不改地进卡；查无此条**也弹卡**（只如实标注）")

_SNAP = [todo("交房租", "2026-09-28"), todo("写周报", None, done=True)]
_c = A.render_todo_done_action("交房租", _SNAP)
check("卡面念出正文 + 排期 + 当前状态（主人点确定前唯一能核对的三样）",
      "「交房租」" in _c and "排期 9月28日" in _c and "现在：未完成" in _c, _c)
check("已完成的那条如实写「现在：已完成」（不许把现状说反）",
      "现在：已完成" in A.render_todo_done_action("写周报", _SNAP))
check("没排期的写「未排期」",
      "未排期" in A.render_todo_done_action("写周报", _SNAP),
      A.render_todo_done_action("写周报", _SNAP))
check("列表里没有这一条 → 卡面如实标注「没有这一条」（**不是**不弹窗）",
      "没有这一条" in A.render_todo_done_action("不存在的", _SNAP),
      A.render_todo_done_action("不存在的", _SNAP))
check("  列表整份是空的 → 另给一句（他要做的是先记一条）",
      "空的" in A.render_todo_done_action("交房租", []))
check("  多条同名 → 卡面写「分不清是哪一条」并给条数（与「没有这一条」分开："
      "两件事要他做的动作不一样）",
      "2 条" in A.render_todo_done_action("交房租", [todo("交房租"), todo("交房租")]))
check("读不到列表（快照 None）→ 只印正文，**不编也不因此不弹窗**",
      A.render_todo_done_action("交房租", None) == "把待办「交房租」勾成完成",
      A.render_todo_done_action("交房租", None))
_q_done = A.render_confirm_question([{"tool": "complete_dashboard_todo",
                                      "args": {"text": "交房租"}}], None, None, None,
                                    None, None, _SNAP)
check("问句与卡面同源（同一个渲染函数，不是两份实现）",
      "把待办「交房租」勾成完成（排期 9月28日，现在：未完成）" in _q_done, _q_done)
check("  快照在手时问句里带现状、读不到时只带正文（三态透传真的接上了）",
      "现在：" not in A.render_confirm_question(
          [{"tool": "complete_dashboard_todo", "args": {"text": "交房租"}}],
          None, None, None, None, None, None))
check("畸形 spec 不炸（渲染层只退化不加戏）",
      "（没有给出正文）" in A.render_confirm_question(
          [{"tool": "complete_dashboard_todo", "args": {}}], None, None, None, None, None, _SNAP))
_q_add = A.render_confirm_question([{"tool": "create_dashboard_todo",
                                     "args": {"text": "交房租", "date": "2026-09-28"}}],
                                   None, None, None, None, None, _SNAP)
_q_notice = A.render_confirm_question([{"tool": "send_user_notice",
                                        "args": {"name": "guest5",
                                                 "content": "请尽快补齐资料"}}])
check("四张卡互不同形（勾 / 加 / 冻结 / 发通知 各自读起来是不同的事）",
      len({_q_done, _q_add, _q_notice,
           A.render_confirm_question([{"tool": "freeze_account", "args": {"name": "guest5"}}])}) == 4)
check("  发通知这张卡把正文**全文**印出来（模型整理过的、发给第三方且收不回的一段话，"
      "人眼只有这一个复核点）",
      "请尽快补齐资料" in _q_notice and "没有撤回的通道" in _q_notice, _q_notice)
check("回执行区分「刚勾的」与「本来就是」（一次 no-op 不能被读成一个动作）",
      "本来就是完成状态" in A.render_todo_done("交房租", changed=False)
      and "已把待办「交房租」勾成完成" in A.render_todo_done("交房租", changed=True),
      A.render_todo_done("交房租", changed=False))

# ── 长正文：**卡面与回执都印全文**（20260926 修，trace `20260926T094843` 的现场）──
# 此前 `clip(text, 60)` 被用在卡面与回执上：主人点「确定」时核对的是「…」前面那 60 字，
# 而回执（narrator 唯一的取值来源）也是截断版 ⇒ 主人收到的回复就是「…2…」，他问
# "为什么和我要的日程内容不一样、显示截断了"。正文上限 200 字（`_TODO_TEXT_LIMIT`），
# 全文没有一个印不下的地方；`clip` 是**密表/帧**的收口工具，不许拿来量卡面。
_LONG = "分析" + "很长的说明" * 30 + "结尾标记"          # 156 字，> 60 也 > 24
check("前置：这条正文真的比 60 字长（否则下面三条是空转）", len(_LONG) > 60, str(len(_LONG)))
_q_long = A.render_confirm_question([{"tool": "create_dashboard_todo",
                                      "args": {"text": _LONG}}],
                                    None, None, None, None, None, _SNAP)
check("加待办的**卡面**：长正文一字不落（不再被裁成 60 字 + 省略号）",
      _LONG in _q_long and "…" not in _q_long, _q_long[-30:])
check("  **回执行**（下一轮 narrator 的唯一取值来源）同样印全文",
      _LONG in A.render_todo_added(_LONG) and "…" not in A.render_todo_added(_LONG),
      A.render_todo_added(_LONG)[-30:])
check("勾完成的卡面也是全文（它的注释写的就是「一字不改地进卡面」，此前实现却在截）",
      _LONG in A.render_todo_done_action(_LONG, None)
      and "…" not in A.render_todo_done_action(_LONG, None),
      A.render_todo_done_action(_LONG, None)[-30:])
check("  勾完成的回执行同全文", _LONG in A.render_todo_done(_LONG, changed=True))


# ══════════════════════════════════════════════════════════════════
print("\n⑭ 过程行与落库回执：带正文、不带内部工具名（两处措辞逐字一致）")

from agent.action_text import tool_action_text as _tool_action_text  # noqa: E402

_a = _tool_action_text("complete_dashboard_todo", {"text": "交房租"})
check("过程行念出正文（这一行会经 recent_executions 注入下一轮——没有正文就认不出是哪条）",
      "交房租" in _a and "勾成完成" in _a, _a)
check("  不裸露内部工具名（带下划线的名字会被 narrator 照抄）",
      "complete_dashboard_todo" not in _a)
check("  这一行**不写「已完成」**（它是执行前的预告，后端幂等分支上是真 no-op）",
      "已完成" not in _a, _a)
check("  缺正文时退化成动作词，不炸",
      _tool_action_text("complete_dashboard_todo", {}) == "勾完成待办",
      _tool_action_text("complete_dashboard_todo", {}))
# 过程行**可以**比卡面短（它是执行前的灰色预告），但截断必须**看得出来**：裸切一刀
# 读成"系统只记了这半句"（真实现场里主人就是这么问的），而卡面/回执印的是全文。
_a_long = _tool_action_text("create_dashboard_todo", {"text": _LONG})
check("  长正文的过程行是**带省略号的预览**（不是把正文裸切 24 字）",
      _a_long.endswith("」") and "…」" in _a_long and _LONG not in _a_long, _a_long)
check("  短正文的过程行不凭空加省略号（没截就是没截）",
      "…" not in _tool_action_text("create_dashboard_todo", {"text": "交房租"}),
      _tool_action_text("create_dashboard_todo", {"text": "交房租"}))
_r_act = _tool_action_text("reschedule_dashboard_todo",
                           {"text": "发简历给阿里", "date": "2026-10-08"})
check("改排期的过程行：正文与新排期都在这一行里（缺任一项都认不出改的是哪一条、改成几号）",
      _r_act == "把待办「发简历给阿里」的排期改成2026-10-08", _r_act)
check("  日期**不做二次翻译**（就是展开函数归一过的那个值——翻成人话会多出一处与卡面"
      "分叉的说法，而这一行当初是按「两份措辞逐字对账」设计的）",
      "10月8日" not in _r_act, _r_act)
check("  清空那一档读起来不含歧义（同一个句式，值换成契约词）",
      _tool_action_text("reschedule_dashboard_todo",
                        {"text": "发简历给阿里", "date": A._TODO_CLEAR_WORD})
      == "把待办「发简历给阿里」的排期改成未排期")
check("  日期缺席 ⇒ 少说一句排期，**不补默认值**（猜一个日子比不说更坏）",
      _tool_action_text("reschedule_dashboard_todo", {"text": "发简历给阿里"})
      == "把待办「发简历给阿里」改排期")
check("  正文也缺 ⇒ 只留动作词，且不裸露带下划线的内部工具名",
      _tool_action_text("reschedule_dashboard_todo", {}) == "改待办排期")

_rsrc = _parent_repo.read(
    "src/routes/chat.rs",
    why="跨轮执行记忆的动作行**自 20260928 起由 Python 写时渲染定稿**"
        "（`agent/action_text.py::receipt_action`，落 `rcpt[\"action\"]`）⇒ 这里要钉的是"
        "Rust 那半**认得出这个字段**，而不是它自己也有一张同名表")
if _rsrc is not None:
    check("Rust 那半读回执顶层的 `action`（不读的话这一行仍是老表渲染的旧措辞）",
          'row["action"]' in _rsrc, "chat.rs")
    # 老表的**唯一**活路径：回执里**没有 `action`** 的行——只可能是 agent 回滚到
    # 20260928 之前送进来的回执行，或 Python 侧至今没有臂的工具（两件死工具）。
    # 已落库的行不走它（`execution_log.detail` 存的就是渲染后的字）。
    check("  老表仍在（回执缺 `action` 时才走它——那条路只留给回滚与两件无臂死工具）",
          '"complete_dashboard_todo" =>' in _rsrc, "chat.rs")
    check("  改排期**不在**老表里（新工具的动作词只加在 Python 那侧：给它加臂是永远"
          "走不到的死代码，还会让「两份措辞互相对账」看着比实际更严）",
          '"reschedule_dashboard_todo" =>' not in _rsrc, "chat.rs")


# ══════════════════════════════════════════════════════════════════
print("\n⑮ 真实 execute 路径：弹卡轮**零执行**，令牌载荷就是这一件")

_SPEC_DONE = 'complete_dashboard_todo({"text": "交房租"})'
_saved_done = g._TOOL_MAP.get("complete_dashboard_todo")
try:
    g._TOOL_MAP["complete_dashboard_todo"] = _FakeTool(
        base.ok("已把待办「交房租」勾成完成（后台已复核：列表里这一条现在就是完成状态）",
                meta={"op": "dashboard_todo_done", "before": "未完成", "after": "已完成"}))
    with patch(_admin_get=lambda p, c: _SNAP):
        for msg, why in [("把交房租那条勾了", "命令式"),
                         ("交房租办完了", "陈述式（同意闸本就判不出）")]:
            CALLS.clear()
            obj = instantiate_plan("navigate", {"target": "物联网平台"})
            obj["skill"] = "dashboard_todo_done"
            obj["tools"] = [_SPEC_DONE]
            state = {**plan_state(obj), "plan_rounds": 1, "done": False,
                     "messages": [HumanMessage(content=msg)]}
            r = execute_node(state, cfg())
            pop = r.get("pending_confirm") or {}
            check(f"{why} → 弹卡且零调用（一律弹窗族不吃「同轮命令即确认」）",
                  CALLS == [] and r.get("receipts") == [] and bool(pop), str(sorted(r)))
            check("  卡上带正文、排期与现状（与 ⑬ 同源）",
                  "「交房租」" in pop.get("q", "") and "排期 9月28日" in pop.get("q", "")
                  and "现在：未完成" in pop.get("q", ""), pop.get("q", ""))
            payload = confirm.inspect(pop.get("token") or "") or {}
            check("  令牌载荷里的 skill 与参数就是这一件（卡上写什么就签什么）",
                  payload.get("skill") == "dashboard_todo_done"
                  and payload.get("specs") == [{"tool": "complete_dashboard_todo",
                                                "args": {"text": "交房租"}}], str(payload))
            check("  这一轮**零执行**（弹卡轮只弹卡）", CALLS == [] and r["receipts"] == [])

        # 读不到列表时**仍要弹卡**（弹窗是这类写唯一的人类兜底；少一句现状比不弹轻得多）
        CALLS.clear()
        obj = instantiate_plan("navigate", {"target": "物联网平台"})
        obj["skill"] = "dashboard_todo_done"
        obj["tools"] = [_SPEC_DONE]
        state = {**plan_state(obj), "plan_rounds": 1, "done": False,
                 "messages": [HumanMessage(content="把交房租那条勾了")]}
        with patch(_admin_get=lambda p, c: base.unavailable("读不到")):
            r = execute_node(state, cfg())
        pop = r.get("pending_confirm") or {}
        check("台账读不到 → 卡照弹，只是卡上没有现状（**绝不因此不弹窗**）",
              bool(pop) and "「交房租」" in pop.get("q", "") and "现在：" not in pop.get("q", ""),
              pop.get("q", ""))

    # 主人点了确定 → 放行执行（「一律弹窗」不是「永不执行」）
    CALLS.clear()
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = "dashboard_todo_done"
    obj["tools"] = [_SPEC_DONE]
    state = {**plan_state(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content="交房租办完了")],
             "confirm_grant": {"token": "x"}}
    r = execute_node(state, cfg())
    check("确认轮 → 放行执行（工具收到的是**原样正文**）",
          CALLS == [{"text": "交房租"}], str(CALLS))
    check("  回执带 op 与执行角色（跨轮执行记忆只认结构化回执，不认叙述）",
          r["receipts"] and r["receipts"][0]["op"] == "dashboard_todo_done"
          and r["receipts"][0]["principal_role"] == "admin", str(r["receipts"]))
except BaseException as e:  # noqa: BLE001
    check(f"execute 勾完成写路径测试异常：{type(e).__name__}: {e}", False)
finally:
    if _saved_done is None:
        g._TOOL_MAP.pop("complete_dashboard_todo", None)
    else:
        g._TOOL_MAP["complete_dashboard_todo"] = _saved_done


# ══════════════════════════════════════════════════════════════════
print("\n⑯ 报待办时连带报出后台首页上并排的两类「等着你处理」"
      "（留言待审 + 额度重置申请，两个读都带 status=pending，且都是后台读）")

from agent.skills import SKILL_MAP  # noqa: E402

_todo_read = SKILL_MAP["dashboard_todo_list"]
_tp = instantiate_plan("dashboard_todo_list", {}, role="admin")
check("技能展开成**三个**工具：待办清单 + 留言审核状况 + 额度重置申请",
      _tp["tools"] == ['list_dashboard_todos({})',
                       'get_moderation_status({"status": "pending"})',
                       'list_quota_requests({"status": "pending"})'],
      str(_tp["tools"]))
check("后两个读**都必须带 status=pending**（报表的 focus 只展开待审那一类 ⇒ 帧不膨胀；"
      "不带参数的形态一次列三份名单）",
      any("pending" in t for t in _tp["tools"] if t.startswith("get_moderation_status"))
      and any("pending" in t for t in _tp["tools"] if t.startswith("list_quota_requests")))
check("它是**只读**技能（不在写名单里、清单里没有写工具）",
      "dashboard_todo_list" not in WRITE_SKILL_NAMES
      and not any(t.split("(")[0] in {"create_dashboard_todo", "complete_dashboard_todo"}
                  for t in _tp["tools"]))
check("完成判定要求**三个**工具都返回（只等前两个 ⇒ 额度那个没跑也当收尾轮，"
      "而它正是主人报「额度重置也在日程里面，他也不查」时缺的那一路）",
      "list_dashboard_todos" in _todo_read.complete_when
      and "get_moderation_status" in _todo_read.complete_when
      and "list_quota_requests" in _todo_read.complete_when,
      _todo_read.complete_when)
check("回复契约分开写**三段**口径（待办逐条照抄 / 审核只报待人工复批那部分，明细不展开 / "
      "额度申请逐份说清，不许只念计数）",
      "逐条" in _todo_read.reply_contract
      and "待人工复批" in _todo_read.reply_contract
      and "moderation_report" in _todo_read.reply_contract
      and "额度重置申请" in _todo_read.reply_contract
      and "只念一句计数" in _todo_read.reply_contract)
check("  审核段写明 0 条也要说出来、读不到不许当成 0 条（两句缺一，"
      "「没查」与「查了没有」在答复里就同形）",
      "0 条" in _todo_read.reply_contract and "不许**当成 0 条" in _todo_read.reply_contract)
check("  额度段同一形状（0 份要说、读不到不许当 0 份）——两段的空/读不到纪律必须对称，"
      "否则弱的那一段会变成「读不到 = 没有」的暗门",
      "0 份" in _todo_read.reply_contract and "不许**当成 0 份" in _todo_read.reply_contract)
check("技能仍只对管理员开放（读的是后台留言管理视图；普通用户拿不到这几个读）",
      _todo_read.roles and ROLE_ADMIN in _todo_read.roles
      and ROLE_USER not in _todo_read.roles,
      str(sorted(_todo_read.roles)))


# ══════════════════════════════════════════════════════════════════
print("\n⑰ planner 侧目标门：正文登记成**目标名**，查的是后台待办台账（20260927）")

# 现场（golden `admin_todo_done_popup` 那条红）：主人说「把待办「给多肉浇水」勾成完成」，
# planner 把正文填成了**另一条**待办（列表里真有的那条，格式完全合法）——于是卡片上
# 印的是别人的行。待办没有 id 也没有标题，**正文是主人唯一能核对的字**：填错 = 让他
# 盲签。修法 = 把 `text` 登记成目标名字段（`_WRITE_NAME_FIELDS` + `_NAME_TARGET_TOOLS`），
# 于是引号通道把正文校正回主人引号里那一段、台账通道再回答「你列表里有没有这一条」。
#
# 这条链**在本节之前没有任何判据**：登记位缺失时上面 ①–⑯ 全绿、而这条链整条不存在
# （登记表是这几道门的唯一开关，`name not in _WRITE_NAME_FIELDS` 直接早退）——所以第
# 一条断言就是「登记在位」，没有它，后面每一条都会变成零断言的绿。
check("⭐ `complete_dashboard_todo` 已登记为目标名字段（漏登记 ⇒ 本节其余断言全部测不到）",
      g._WRITE_NAME_FIELDS.get("complete_dashboard_todo") == ("text", None)
      and "complete_dashboard_todo" in g._NAME_TARGET_TOOLS,
      str(g._WRITE_NAME_FIELDS.get("complete_dashboard_todo")))

_MSG_TODO = "把待办「给多肉浇水」勾成完成"
_ROWS_TODO = [todo("给多肉浇水", date="2026-09-28"), todo("买猫粮", date=None)]


def _todo_plan(text):
    """真实展开路径产出的计划体（与 planner 同形：params 与 TOOLS 行都要有）。"""
    return instantiate_plan("dashboard_todo_done", {"text": text}, "admin")


def _run_target_gates(plan_obj, user_msg, rows):
    """planner 侧那三道门的**真实调用序**（见 `planner_node` 里 fixer 链的注）。

    返回 `(拒绝说明 or None, 计划里此刻的正文)`——用的是那三个真函数本身，不重写判据。
    正文读的是 **TOOLS 行**（execute 真会执行的那一份），不是 `params`：`instantiate_plan`
    的产物里根本没有 `params` 键（那是 planner 那份计划体才有的形状），而"这两处同步"
    恰恰是要锁的性质之一。
    """
    g._name_target_fix(plan_obj, user_msg, "admin")
    ref = g._target_grounding_refusal(plan_obj, user_msg)
    if ref is None:
        with patch(_admin_get=lambda p, c: rows):
            ref = g._write_target_refusal(plan_obj, cfg(), user_msg, "admin")
    args, _ok = g._tool_args((plan_obj.get("tools") or [""])[0])
    return ref, str((args or {}).get("text") or "")


_plan_wrong = _todo_plan("买猫粮")
_ref_fix, _got_fix = _run_target_gates(_plan_wrong, _MSG_TODO, _ROWS_TODO)
check("正例：planner 填的是列表里另一条 ⇒ 正文被校正回主人引号里那一段",
      _got_fix == "给多肉浇水", _got_fix)
check("  TOOLS 行**同步重建**（弹卡印的与执行的是同一份参数，不是两处各算一遍）",
      _plan_wrong.get("tools") == ['complete_dashboard_todo({"text": "给多肉浇水"})'],
      str(_plan_wrong.get("tools")))
check("  校正后**来源态判据放行**（顺序锁：校正必须在 `_target_grounding_refusal` 之前，"
      "否则它判的是 planner 那个错值）",
      _ref_fix is None, str(_ref_fix))

# 台账三态 + 读不到（每一态都用真实的三道门跑一遍）
ref_hit, _ = _run_target_gates(_todo_plan("给多肉浇水"), _MSG_TODO, _ROWS_TODO)
check("台账里有这一条 ⇒ 放行到弹窗（本层只回答这件事现在做不做得成，签字由主人点）",
      ref_hit is None, str(ref_hit))

ref_miss, _ = _run_target_gates(_todo_plan("给多肉浇水"), _MSG_TODO,
                                [todo("买猫粮", date=None)])
check("台账里没有这一条 ⇒ 零写 + 如实拒绝", ref_miss is not None, str(ref_miss))
check("  说明里说的是**后台待办**（措辞错成「站内没有叫「X」的标签」是最坏的一种："
      "它长得像一句诚实拒绝，查的却是另一本台账）",
      ref_miss is not None and "待办" in ref_miss[1] and "标签" not in ref_miss[1],
      str(ref_miss))
check("  说明里带上主人说的那个字与列表条数（他要能照着改口）",
      ref_miss is not None and "给多肉浇水" in ref_miss[1] and "1 条" in ref_miss[1],
      str(ref_miss))

ref_dup, _ = _run_target_gates(_todo_plan("给多肉浇水"), _MSG_TODO,
                               [todo("给多肉浇水"), todo("给多肉浇水", date=None)])
check("多条同名 ⇒ 零写（**歧义即零写**：挑错的那次在列表上看起来和挑对一模一样）",
      ref_dup is not None and "分不清" in ref_dup[1], str(ref_dup))

ref_empty, _ = _run_target_gates(_todo_plan("给多肉浇水"), _MSG_TODO, [])
check("空列表 ⇒ 零写 + 说明是**事实**（「一条都没记」）而不是故障",
      ref_empty is not None and "一条都没记" in ref_empty[1], str(ref_empty))

ref_down, _ = _run_target_gates(_todo_plan("给多肉浇水"), _MSG_TODO,
                                base.unavailable("后台读不到"))
check("⭐ 台账**读不到** ⇒ 放行（读不到 ≠ 没有：把一次网络故障说成"
      "「你列表里没有这一条」是本族最坏的错法；golden 那条 uid=0 的用例"
      "「卡照弹」也正是靠这一格）",
      ref_down is None, str(ref_down))

# 待办族**不走近失校正**：主人引号里就是短的那一截，而台账里恰好有一条以它开头 ⇒ 若走
# 标签族那条校正，目标会被改成台账全名。待办正文是主人自己写在清单上的自由文本、没有
# id，「以它开头」根本没有指认力（「给多肉」与「给多肉浇水」是两件事）。
_short_plan = _todo_plan("给多肉")
_ref_short, _got_short = _run_target_gates(_short_plan, "把待办「给多肉」勾成完成",
                                           [todo("给多肉浇水", date=None)])
check("⭐ 待办族不做「抄短了就补全」的校正（引号里写什么就是什么，退回如实拒绝）",
      _ref_short is not None and _got_short == "给多肉", f"{_ref_short} / {_got_short}")

# ── ⑰b 待办的**引用式**唯一命中（20260929 批 G，D2）────────────────────────────
# 现场：主人说「把简历那条挪到 10 月 8 号」，台账里**唯一**一行含「简历」，可定位判据是
# 逐字相等 ⇒ 系统反问"请你点名是哪一件"，主人只好把台账原文抄一遍。这一支补的不是新
# 判据，是**候选来源**：从主人**没加引号**的那部分说法出发（2-gram 包含），恰好命中一行
# 才把目标校正成台账那一行的逐字正文，再走上面那条既有的重建机制。
#
# 与上面那条 20260927 的锁**不是同一件事**（别把这两段合并）：那一条锁的是"引号里的短
# 字面不许被补全"，这一支把引号段整段排除在外，所以它一个字都没放松——本节第三条断言
# 就是这个边界本身。
print("\n⑰b 待办的引用式唯一命中：主人自己的字唯一指向一行 ⇒ 校正成台账原文（批 G）")

_MSG_REF = "把简历那条勾成完成"
_ROWS_REF = [todo("更新简历（国庆后）", date="2026-10-08"), todo("买猫粮", date=None)]

_plan_ref = _todo_plan("简历")
_ref_r, _got_r = _run_target_gates(_plan_ref, _MSG_REF, _ROWS_REF)
check("⭐ 主人没加引号的说法**唯一**指向台账一行 ⇒ 正文校正成那一行的逐字正文、放行到弹卡",
      _ref_r is None and _got_r == "更新简历（国庆后）", f"{_ref_r} / {_got_r}")
check("  TOOLS 行与 `params` **同步**重建（弹卡印的与 execute 执行的是同一份参数）",
      _plan_ref.get("tools") == ['complete_dashboard_todo({"text": "更新简历（国庆后）"})']
      and (_plan_ref.get("params") or {}).get("text") == "更新简历（国庆后）",
      f"{_plan_ref.get('tools')} / {_plan_ref.get('params')}")

_plan_none = _todo_plan("那件事")
_ref_n, _got_n = _run_target_gates(_plan_none, "把那条勾成完成", _ROWS_REF)
check("这批字在台账里**一行都指向不到** ⇒ 今天的拒绝原样保留（不许凭空认领一个是目标）",
      _ref_n is not None and _got_n == "那件事", f"{_ref_n} / {_got_n}")

_plan_two = _todo_plan("简历")
_ref_two, _got_two = _run_target_gates(
    _plan_two, _MSG_REF, [todo("更新简历", date=None), todo("简历附件", date=None)])
check("⭐ 被引用的行有**两条** ⇒ 零写（歧义即零写：两行都含「简历」，判据认得出多个候选）",
      _ref_two is not None and _got_two == "简历", f"{_ref_two} / {_got_two}")

_plan_q = _todo_plan("简历")
_ref_q, _got_q = _run_target_gates(_plan_q, "把「简历」那条勾成完成", _ROWS_REF)
check("⭐ 引号里写的是短的那一截 ⇒ 仍走今天的如实拒绝（这一支只看主人**没加引号**的说法，"
      "20260927 那条纪律一个字没放松）",
      _ref_q is not None and _got_q == "简历", f"{_ref_q} / {_got_q}")

_plan_one = _todo_plan("那个菜")
_ref_one, _got_one = _run_target_gates(_plan_one, "把菜那条勾成完成", [todo("菜")])
check("只有**一个字**的正文永远不被认领（凑不出 2-gram ⇒ 一个字没有指认力），退回如实拒绝",
      _ref_one is not None and _got_one == "那个菜", f"{_ref_one} / {_got_one}")

# 负锁（两条）：这一支只改「我们找哪一行」，**不改**「怎么找」——
# ① 工具侧仍是逐字相等（校正出来的那一行，换个近似说法就找不到它）；
# ② Rust `pick_todo` 与它同口径（`r.text == text`，不是 contains/startswith）。
check("⭐ 执行端定位判据仍是**逐字相等**（近似说法找到 0 条，原文才找到那一条）",
      base._todo_text_hits([{"text": "更新简历（国庆后）"}], "更新简历") == []
      and len(base._todo_text_hits([{"text": "更新简历（国庆后）"}],
                                   "更新简历（国庆后）")) == 1)
_rs_todos = _parent_repo.read(
    "src/routes/todos.rs",
    why="待办定位是**跨语言同口径**：Python 侧 `_todo_text_hits` 与 Rust 侧 `pick_todo`"
        "都必须是逐字相等；这一支只改候选来源，两边判据都不许被放宽")
if _rs_todos is not None:
    check("  Rust `pick_todo` 也是逐字相等（含 `trim`，与写入口径同源）",
          "r.text == text" in _rs_todos and ".trim()" in _rs_todos)

# ══════════════════════════════════════════════════════════════════
# ⑱ 第三条窄写：改某一条的**排期日**（20260929 批 G，P4）
#   为什么这一族还要有第三条通道：主人说「简历那条挪到 10 月 8 号」时，系统手里只有
#   「加一条」与「勾完成」——前者会把一次改动记成一条**新**待办（列表上多出来的那一行
#   看起来就是主人想要的那条），后者会把一件**没办完**的事勾掉。这一件补的是"只动
#   那一行的日期"这个动作，与另两件共用同一条定位判据（正文逐字相等 + 唯一命中）。
print("\n⑱ 改排期（第三条窄写）：展开四态 / 登记位 / 工具三态 / 卡面（批 G）")


def rex(**params):
    """真实展开路径（与 planner 同形）。"""
    return instantiate_plan("dashboard_todo_reschedule", params)


out = rex()
check("正文缺失 → 零工具 + 注记（这张列表没有行号，正文是唯一的指认方式）",
      out["tools"] == [] and "缺少正文" in out["note"], f"{out['tools']} / {out['note'][:50]}")
check("  注记明写**不要替他挑一条**（本族恒弹卡，但「挑错的那条看起来一样」)",
      "不要" in out["note"], out["note"][:70])

out = rex(text="给猫买罐头", date="")          # 「帮我改个日子」，一个字都没说
check("没说日期 → 零工具 + 追问「要改到哪一天」（与「翻不出来」分开：两件事要他做的动作不同）",
      out["tools"] == [] and "没给日期" in out["note"], f"{out['tools']} / {out['note'][:60]}")
check("  同一句注记里给出清空那个契约词（主人说「不用排期了」时 planner 该往里填什么）",
      A._TODO_CLEAR_WORD in out["note"], out["note"][:90])

out = rex(text="给猫买罐头", date="下周三")
check("⭐ 日期翻不出来 → 零工具 + 问清哪一天（**绝不挑一天顶上**：错一天的日程会静静躺在"
      "后台日历的错误格子里，而「改好了」这三个字会让主人以为系统听懂了他的说法）",
      out["tools"] == [] and "认不出来" in out["note"] and "不许" in out["note"],
      f"{out['tools']} / {out['note'][:70]}")
check("  零写那一句落到 status 上（不是只写在注记里——gate/planner 读的是键）",
      out.get("status") == "param_missing", str(out.get("status")))

out = rex(text="长" * (base._TODO_TEXT_LIMIT + 1), date="明天")
check("正文超上限 → 零工具（**不截断**：截断等于替主人改字，改完就不是同一行了）",
      out["tools"] == [] and "太长" in out["note"], f"{out['tools']} / {out['note'][:50]}")

out = rex(text="给猫买罐头", date="明天")
_due = A.normalize_due_date("明天")
check("正例：正文原样 + **翻好的** ISO 日期进 TOOLS 行",
      out["tools"] == [f'reschedule_dashboard_todo('
                       f'{json.dumps({"text": "给猫买罐头", "date": _due}, ensure_ascii=False)})'],
      str(out["tools"]))
check("  注记把日期念成人话，且写明**只动排期**（正文与完成标记一个字都不动）",
      f"排期改成{A.due_date_cn(_due)}" in out["note"] and "只动排期" in out["note"],
      out["note"][:80])

# 清空排期：主人那几种说法（「不用排期了」「把日子清掉」）**收敛到同一个契约词**，
# 落进 TOOLS 行的永远是那一个字面 —— 工具与卡面都只认它（单一来源 `_TODO_CLEAR_WORD`）。
_cleared = {tuple(rex(text="给猫买罐头", date=w)["tools"])
            for w in sorted(A._TODO_CLEAR_DATES)}
_want_clear = [f'reschedule_dashboard_todo('
               f'{json.dumps({"text": "给猫买罐头", "date": A._TODO_CLEAR_WORD}, ensure_ascii=False)})']
check("清空排期的几种说法**收敛成同一份 TOOLS 行**（同义词只在入口收，往里传的永远是契约词）",
      _cleared == {tuple(_want_clear)}, str(_cleared))
check("  注记写「排期清掉」（不是「改成未排期」——清空是主人自己那几种说法的意思）",
      "排期清掉" in rex(text="给猫买罐头", date="清空")["note"],
      rex(text="给猫买罐头", date="清空")["note"][:60])

# 登记位（三条）：缺任何一条的后果都是**静默的**
check("⭐ 登记成目标名字段（漏登记 ⇒ 口径里的「正文必须能从他原话里抽出来」那道闸整个不作用）",
      g._WRITE_NAME_FIELDS.get("reschedule_dashboard_todo") == ("text", None)
      and "reschedule_dashboard_todo" in g._NAME_TARGET_TOOLS,
      str(g._WRITE_NAME_FIELDS.get("reschedule_dashboard_todo")))
check("⭐ 在 `_TODO_TOOLS` 里（漏了 ⇒ 弹卡那一轮**不读台账**、卡面印不出「现在：排期…」，"
      "主人只能盲签——而这张卡的全部意义就是让他核对「改的是不是这一条、它现在排在几号」）",
      "reschedule_dashboard_todo" in g._TODO_TOOLS, str(g._TODO_TOOLS))
check("  `date` **不**登记成目标名（它是要写进去的值，不是身份——登记了会让"
      "「名字必须能从原话里抽出来」这条判据作用在一个日期串上）",
      g._WRITE_NAME_FIELDS["reschedule_dashboard_todo"][1] is None)
check("  工具参数说明里那个清空关键词与契约词是同一个字面（两处各写一份的那天，"
      "「清空」会在一条通道上成立、另一条上变成格式错误）",
      A._TODO_CLEAR_WORD in json.dumps(base.reschedule_dashboard_todo.args,
                                       ensure_ascii=False),
      str(base.reschedule_dashboard_todo.args.get("date"))[:40])

# ── 工具四态（真实工具 + 桩客户端；写前读 / 写后复核是两次独立读数）──────────
_TOMORROW = A.normalize_due_date("明天")
_ROWS_R = [todo("更新简历（国庆后）", date="2026-10-08"), todo("买猫粮", date=None)]


def _rsch(text="更新简历（国庆后）", date="明天", uid=7):
    return base.reschedule_dashboard_todo.invoke({"text": text, "date": date}, config=cfg(uid))


def _date_ok(text="更新简历（国庆后）", date=_TOMORROW):
    return _HResp(200, {"code": 200, "data": {"text": text, "done": False, "date": date}})


with patch(_admin_get=lambda p, c: _ROWS_R, _client=_Http(_date_ok())):
    r = _rsch(date="下周三")
    check("日期翻不出来 → unavailable + 问清哪一天，**零 POST**（一个字节都不发）",
          r.kind == "unavailable" and "认不出排期日" in r and base._client.calls == [],
          f"{r.kind}: {r}")
    r = _rsch(text="  ")
    check("正文空 → unavailable，零 POST", r.kind == "unavailable" and base._client.calls == [],
          f"{r.kind}: {r}")
    r = _rsch(text="长" * (base._TODO_TEXT_LIMIT + 1))
    check("正文超上限 → unavailable，零 POST",
          r.kind == "unavailable" and base._client.calls == [], f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: [todo("买猫粮", date=None)], _client=_Http(_date_ok())):
    r = _rsch()
    check("写前读：列表里没有这一条 → not_found（**不是**服务不可用：他要做的是照那一行"
          "现在的正文说，不是稍后再试），且零 POST",
          r.kind == "not_found" and "没有「更新简历（国庆后）」这一条" in r
          and base._client.calls == [], f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: [todo("更新简历", date=None), todo("更新简历", "2026-10-08")],
           _client=_Http(_date_ok())):
    r = _rsch(text="更新简历")
    check("写前读：两条同名 → not_found + 说清分不清（**歧义即零写**，绝不替主人挑一条）"
          "且零 POST",
          r.kind == "not_found" and "2 条" in r and "分不清" in r
          and base._client.calls == [], f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: base.unavailable("读不到"), _client=_Http(_date_ok())):
    r = _rsch()
    check("写前读失败 → unavailable + 「本次未改动」，零 POST（读不到 ≠ 没有）",
          r.kind == "unavailable" and "本次未改动" in r and base._client.calls == [],
          f"{r.kind}: {r}")

# 正例：一次 POST 到**新端点**，body 只发 text/date 两键、日期是 ISO
cli = _Http(_date_ok())
with patch(_admin_get=_Seq(_ROWS_R, [todo("更新简历（国庆后）", date=_TOMORROW)]),
           _client=cli):
    r = _rsch()
    check("恰好一条命中 → ok，回执行写明改成了哪一天且已复核",
          r.kind == "ok" and "更新简历（国庆后）" in r and "复核" in r, f"{r.kind}: {r}")
    check("  恰好一次 POST：路径是新端点、body 只发 text/date（date 是归一后的 ISO）",
          len(cli.calls) == 1
          and cli.calls[0][1].endswith("/api/protected/todos/date")
          and cli.calls[0][3] == {"text": "更新简历（国庆后）", "date": _TOMORROW},
          str(cli.calls[0][1:]))
    check("  回执 meta 是结构化回执：op + 新旧排期，且键都在白名单里（漏了是静默丢键）",
          r.meta.get("op") == "dashboard_todo_reschedule"
          and r.meta.get("before") == "排期 10月8日"
          and r.meta.get("after") == f"排期 {A.due_date_cn(_TOMORROW)}"
          and set(r.meta) <= set(g._RCPT_META_KEYS) | _ENVELOPE_ONLY, str(r.meta))
    check("  回执不留 uid（detail 进生产库、会被 narrator 念出来）",
          "uid" not in json.dumps(r.meta), json.dumps(r.meta, ensure_ascii=False))

# 写后复核的判据是"那一行的**日期**变了"——那一行在写之前就在（这是改不是新增），
# 只判"列表里有没有这么一条"会把每一次失败都判成成功。
cli = _Http(_date_ok())
with patch(_admin_get=_Seq(_ROWS_R, [todo("更新简历（国庆后）", date="2026-10-08")]),
           _client=cli):
    r = _rsch()
    check("⭐ 写后复核仍是旧日期 → **kind == unavailable**（措辞之外必须判 kind，"
          "只断文案是假绿）",
          r.kind == "unavailable" and "未确认生效" in r and "不要声称已改好" in r,
          f"{r.kind}: {r}")

cli = _Http(_date_ok())
with patch(_admin_get=_Seq(_ROWS_R, base.unavailable("读不回来了")), _client=cli):
    r = _rsch()
    check("写后读不回 → unavailable 且明写「未确认生效」",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

cli = _Http(_date_ok())
with patch(_admin_get=_Seq(_ROWS_R, [todo("别的", date=_TOMORROW)]), _client=cli):
    r = _rsch()
    check("写后那一行不见了 → unavailable（不能说成改好了）",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

# 幂等**不短路**（同勾完成族）：目标日期与写前相同也照发一次（服务端那个分支是真 no-op），
# 结论由复核给——回执必须**如实区分**"刚改的"与"本来就是这一天"。
cli = _Http(_date_ok(date=_TOMORROW))
with patch(_admin_get=_Seq([todo("更新简历（国庆后）", date=_TOMORROW)],
                          [todo("更新简历（国庆后）", date=_TOMORROW)]), _client=cli):
    r = _rsch()
    check("本来就是这一天 → 仍发一次请求（不短路），回执行如实说**未发生任何变更**",
          r.kind == "ok" and "本来就排在" in r and "这次没有发生任何变更" in r
          and len(cli.calls) == 1, f"{r.kind}: {r}")
    check("  meta 的 before/after 都写出来了（跨轮记忆里能区分「刚改的」与「本来就是」）",
          r.meta.get("before") == f"排期 {A.due_date_cn(_TOMORROW)}"
          and r.meta.get("after") == f"排期 {A.due_date_cn(_TOMORROW)}", str(r.meta))

# 清空那一次：body 里的 date 是**空串**（Rust 侧 `null`/空串 = 清空，与前端 `allowClear`
# 同语义），回执行同样区分"刚清掉的"与"本来就没排期"。
cli = _Http(_HResp(200, {"code": 200, "data": {"text": "更新简历（国庆后）", "done": False,
                                               "date": None}}))
with patch(_admin_get=_Seq(_ROWS_R, [todo("更新简历（国庆后）", date=None)]), _client=cli):
    r = _rsch(date="未排期")
    check("清空排期：body 的 date 是空串（**不是缺键**——缺键在后端是 400）",
          cli.calls and cli.calls[0][3] == {"text": "更新简历（国庆后）", "date": ""},
          str(cli.calls[0][3]) if cli.calls else "零 POST")
    check("  回执行说「排期改成未排期」且已复核",
          r.kind == "ok" and "排期改成未排期" in r and "复核" in r, f"{r.kind}: {r}")

cli = _Http(_HResp(200, {"code": 200, "data": {"text": "更新简历（国庆后）", "done": False,
                                               "date": None}}))
with patch(_admin_get=_Seq([todo("更新简历（国庆后）", date=None)],
                          [todo("更新简历（国庆后）", date=None)]), _client=cli):
    r = _rsch(date="未排期")
    check("本来就没排期 → 「**本来就没有排期**」（「本来就排在未排期」不是中文；"
          "与幂等臂同一句，两处共用同一份措辞）",
          r.kind == "ok" and "本来就没有排期" in r and "变更" in r, f"{r.kind}: {r}")

# uid 不明 → 一个请求都不发（同族纪律）
_plain = _Http(_date_ok())
_saved_c = base._client
try:
    base._client = _plain
    base.reschedule_dashboard_todo.invoke({"text": "更新简历（国庆后）", "date": "明天"},
                                          config=cfg(0))
    check("uid ≤ 0 → 一个请求都不发（身份不明时不猜「改谁的」）",
          _plain.calls == [], str(_plain.calls))
finally:
    base._client = _saved_c

# ── 卡面与问句（与勾完成那张卡同形：正文一字不改 + 现状那一格换成排期）──────
_c = A.render_todo_reschedule_action("更新简历（国庆后）", "2026-10-09", _ROWS_R)
check("卡面念出正文 + 改到哪天 + **它现在排在几号**（主人点确定前唯一能核对的三样）",
      "「更新简历（国庆后）」" in _c and "排期改成10月9日" in _c and "现在：排期 10月8日" in _c, _c)
check("清空那一次卡面说「改成未排期」（不是「改成 None」也不是「改成」）",
      "排期改成未排期" in A.render_todo_reschedule_action("买猫粮", A._TODO_CLEAR_WORD, _ROWS_R),
      A.render_todo_reschedule_action("买猫粮", A._TODO_CLEAR_WORD, _ROWS_R))
check("列表里没有这一条 / 有多条同名 / 列表是空的 → 三种如实标注各不相同"
      "（**不是**不弹窗）",
      "没有这一条" in A.render_todo_reschedule_action("不存在", _TOMORROW, _ROWS_R)
      and "2 条" in A.render_todo_reschedule_action("买猫粮", _TOMORROW,
                                                   [todo("买猫粮"), todo("买猫粮")])
      and "空的" in A.render_todo_reschedule_action("买猫粮", _TOMORROW, []),
      A.render_todo_reschedule_action("不存在", _TOMORROW, _ROWS_R))
check("读不到列表（快照 None）→ 只印正文与目标日期，**不编也不因此不弹窗**",
      A.render_todo_reschedule_action("买猫粮", _TOMORROW, None)
      == f"把待办「买猫粮」的排期改成{A.due_date_cn(_TOMORROW)}",
      A.render_todo_reschedule_action("买猫粮", _TOMORROW, None))
_q_r = A.render_confirm_question([{"tool": "reschedule_dashboard_todo",
                                   "args": {"text": "更新简历（国庆后）", "date": "2026-10-09"}}],
                                 None, None, None, None, None, _ROWS_R)
check("问句与卡面同源（同一个渲染函数，不是两份实现）",
      "把待办「更新简历（国庆后）」的排期改成10月9日（现在：排期 10月8日）" in _q_r, _q_r)
check("  改排期这张卡与勾完成那张卡**不同形**（同一行正文、两件不同的事——"
      "主人不能把「挪个日子」读成「办完了」）",
      _q_r != _q_done, _q_r)
check("畸形 spec 不炸（渲染层只退化不加戏）",
      "（没有给出正文）" in A.render_confirm_question(
          [{"tool": "reschedule_dashboard_todo", "args": {}}], None, None, None, None, None, _ROWS_R))
check("回执行区分「刚要改」与「本来就排在」（一次 no-op 不能被读成一个动作）",
      "本来就排在10月9日" in A.render_todo_rescheduled("更新简历（国庆后）", "2026-10-09",
                                                    changed=False)
      and "已把待办「更新简历（国庆后）」的排期改成10月9日" in A.render_todo_rescheduled(
          "更新简历（国庆后）", "2026-10-09", changed=True),
      A.render_todo_rescheduled("更新简历（国庆后）", "2026-10-09", changed=False))

# 「状态已达成 ⇒ 不弹卡」的判据（`reached_specs`）：比的是**两个日期**，只在"不会真改变
# 什么"时才免弹卡；判不出来（快照读不到 / 同名多条 / 认不出的值）一律照弹。
_spec_r = {"tool": "reschedule_dashboard_todo",
           "args": {"text": "更新简历（国庆后）", "date": "2026-10-08"}}
_kept_r, _already_r = A.reached_specs([_spec_r], todos=_ROWS_R)
check("⭐ 要改成的正是它现在的排期 ⇒ 判成已达成（主人看到的是「本来就在那天」，不是一张白点的卡）",
      _kept_r == [] and len(_already_r) == 1 and "本来就排在10月8日" in _already_r[0]["why"],
      str(_already_r))
_spec_r2 = {"tool": "reschedule_dashboard_todo",
            "args": {"text": "更新简历（国庆后）", "date": "2026-10-09"}}
_kept_r2, _already_r2 = A.reached_specs([_spec_r2], todos=_ROWS_R)
check("  要改成的是别的一天 ⇒ **照弹**（这一下真会改变什么）",
      len(_kept_r2) == 1 and _already_r2 == [], str((_kept_r2, _already_r2)))
_kept_r3, _already_r3 = A.reached_specs([_spec_r2], todos=None)
check("  台账读不到 ⇒ 照弹（判不出来不等于已达成）",
      len(_kept_r3) == 1 and _already_r3 == [], str((_kept_r3, _already_r3)))
_kept_r4, _already_r4 = A.reached_specs([_spec_r2], todos=[todo("更新简历（国庆后）"),
                                                          todo("更新简历（国庆后）")])
check("  同名多条 ⇒ 照弹（分不清是哪一条，更判不出「已在不在那天」）",
      len(_kept_r4) == 1 and _already_r4 == [], str((_kept_r4, _already_r4)))

# ── 真实 execute 路径：弹卡轮**零执行**，令牌载荷就是这一件 ──────────────
_SPEC_RSCH = ('reschedule_dashboard_todo('
              '{"text": "更新简历（国庆后）", "date": "' + _TOMORROW + '"})')
_saved_rsch = g._TOOL_MAP.get("reschedule_dashboard_todo")
try:
    g._TOOL_MAP["reschedule_dashboard_todo"] = _FakeTool(
        base.ok(A.render_todo_rescheduled("更新简历（国庆后）", _TOMORROW),
                meta={"op": "dashboard_todo_reschedule",
                      "before": "排期 10月8日",
                      "after": f"排期 {A.due_date_cn(_TOMORROW)}"}))
    with patch(_admin_get=lambda p, c: _ROWS_R):
        for msg, why in [("把简历那条挪到 10 月 8 号", "命令式"),
                         ("简历那条的日子我想挪一下", "陈述式（同意闸本就判不出）")]:
            CALLS.clear()
            obj = instantiate_plan("navigate", {"target": "物联网平台"})
            obj["skill"] = "dashboard_todo_reschedule"
            obj["tools"] = [_SPEC_RSCH]
            state = {**plan_state(obj), "plan_rounds": 1, "done": False,
                     "messages": [HumanMessage(content=msg)]}
            r = execute_node(state, cfg())
            pop = r.get("pending_confirm") or {}
            check(f"{why} → 弹卡且零调用（一律弹窗族不吃「同轮命令即确认」）",
                  CALLS == [] and r.get("receipts") == [] and bool(pop), str(sorted(r)))
            check("  卡面印台账原文 + 新旧两个排期（漏了 `_TODO_TOOLS` 这一格就会少后半句）",
                  "「更新简历（国庆后）」" in pop.get("q", "")
                  and f"排期改成{A.due_date_cn(_TOMORROW)}" in pop.get("q", "")
                  and "现在：排期 10月8日" in pop.get("q", ""), pop.get("q", ""))
            payload = confirm.inspect(pop.get("token") or "") or {}
            check("  令牌载荷里的 skill 与参数就是这一件（卡上写什么就签什么）",
                  payload.get("skill") == "dashboard_todo_reschedule"
                  and payload.get("specs") == [{"tool": "reschedule_dashboard_todo",
                                                "args": {"text": "更新简历（国庆后）",
                                                         "date": _TOMORROW}}], str(payload))

    # 主人点了确定 → 放行执行
    CALLS.clear()
    obj = instantiate_plan("navigate", {"target": "物联网平台"})
    obj["skill"] = "dashboard_todo_reschedule"
    obj["tools"] = [_SPEC_RSCH]
    state = {**plan_state(obj), "plan_rounds": 1, "done": False,
             "messages": [HumanMessage(content="简历那条的日子我想挪一下")],
             "confirm_grant": {"token": "x"}}
    r = execute_node(state, cfg())
    check("确认轮 → 放行执行（工具收到的是**原样正文 + 归一后的日期**）",
          CALLS == [{"text": "更新简历（国庆后）", "date": _TOMORROW}], str(CALLS))
    check("  回执带 op 与执行角色（跨轮执行记忆只认结构化回执，不认叙述）",
          r["receipts"] and r["receipts"][0]["op"] == "dashboard_todo_reschedule"
          and r["receipts"][0]["principal_role"] == "admin", str(r["receipts"]))
except BaseException as e:  # noqa: BLE001
    check(f"execute 改排期写路径测试异常：{type(e).__name__}: {e}", False)
finally:
    if _saved_rsch is None:
        g._TOOL_MAP.pop("reschedule_dashboard_todo", None)
    else:
        g._TOOL_MAP["reschedule_dashboard_todo"] = _saved_rsch

# ══════════════════════════════════════════════════════════════════
# ⑲ 待办正文的**出处**：planner 不许替主人编那一件事（20261006 事故）
#
# **现场**（trace `20261006T093633`，uid=1 超管，主人原话见 `_INCIDENT_MSG`）：主人在
# 第一句里就把两件事连编号带标点写得清清楚楚，planner 选了 `dashboard_todo_add`、
# 格式合法、正文却是**另一件事**——「给晶宝恢复身份后跟进功能测试是否恢复正常」，
# 那句话来自**更早的上下文**，这句话里一个字都没有。卡片照弹，主人照着这张卡签字，
# 系统就会把一件他从没说过的事记进他的待办清单。
#
# **为什么跑了这么多轮全量回归都没抓到**（三层各漏一格，缺一层都不该漏）：
#   ① `_WRITE_NAME_FIELDS` 登记了 `complete_dashboard_todo` / `reschedule_dashboard_todo`
#      （`⑰`/`⑱` 那两条），**没有** `create_dashboard_todo`——"加一条"的目标不是站内
#      既有台账里的一行，"名字通道"与"台账通道"都天然不覆盖它；
#   ② 这件技能的 `planner_contract` 是空的，提示词正文里没有一个字提醒模型"照抄"；
#   ③ golden `admin_todo_add_popup` 的 `_note` **明写**不做正文逐字断言（理由：参数
#      由 planner 采样填），于是这一格在评测层**没有任何判据**。
#   实测命中率：同一条输入连跑 12 次，只有 3 次把正文关联对（25%）。
#
# 修法（`graph.py::_todo_text_fix`）：与 `⑰`/`⑱` 同一条纪律——**主人自己标出来的那
# 一段才是正文**。三态，边界在"绝不替他编"：
#   · 有据不动：planner 填的正文能在主人这句话里找到 → 一个字不改；
#   · 无据 + 主人标了标记 → 校正成标记后那一段（`todo_text_correct`），TOOLS 行重建；
#   · 无据 + 没标记 → 零工具 + 如实请他把那件事原样再说一次（不许猜一件顶上）。
print("\n⑲ 待办正文的出处闸：主人标出来的那一段才是正文（20261006 事故）")

import agent.skills as _S  # noqa: E402
from _native_stub import bind_tools_stub, native_reply  # noqa: E402

_skill_map = _S.SKILL_MAP
# 接线判据读的是**源码字面**（同 `test_plan_channel` 钉 `plan_encode` 调用点那条）：
# 这几句是"这道闸到底有没有被 `planner_node` 用上"的唯一可离线断言的东西——
# 换成调用计数就得跑整流，而这里要锁的恰恰是"链上有没有这一步"。
_graph_src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")


class _ScriptedLLM:
    """按脚本吐计划、留提示词（同 `test_confirm_leftovers` 那台）。"""

    bind_tools = bind_tools_stub

    def __init__(self, replies):
        self.replies = list(replies)

    def invoke(self, prompt):
        return native_reply(self.replies.pop(0))


def _planner_cfg():
    """uid=1 超管 + 那条事故 trace 的会话号（`planner_node` 只从 config 取身份与会话）。"""
    return {"configurable": {"user_id": 1, "principal": Principal(uid=1, role="superadmin"),
                             "conversation_id": 324, "stop_event": None}}


_INCIDENT_MSG = ("闺女，给我加一条今天的待办，1.agent开发：探讨引入JEV等决策模式的修改面"
                 "和后续评估升级。2.后台面板移动端适配是灾难级别的，亟待优化。")
_INCIDENT_BODY = ("1.agent开发：探讨引入JEV等决策模式的修改面和后续评估升级。"
                  "2.后台面板移动端适配是灾难级别的，亟待优化。")
# 逐字取自那条 trace 的 `planner/decision` 事件——**不许改写**：判据就是照着它定的。
_HALLUCINATED = "给晶宝恢复身份后跟进功能测试是否恢复正常"

# ⭐ **接线在位**是本节其余全部断言的前提（同 `⑰` 的第一条）：抽取器/校正器写好了却
# 没接进 `planner_node` 的 fixer 链，下面每一条都会静默变成测一个没人调用的函数。
# 三条一起判：调用点在链上、拒绝来源有独立取值、`value_tail` 认得它（漏了最后一条则
# 拒绝轮的叙述会把"系统自己的参数值"说成"主人点名的名字"）。
check("⭐ `_todo_text_fix` 已接进 planner 的 fixer 链（漏接线 ⇒ 本节其余全部空转）",
      "todo_refuse = _todo_text_fix(" in _graph_src)
check("⭐ 拒绝来源有独立取值 `todo_text`（与其他五类出处分得开：报表要能按它计数）",
      '"todo_text" if todo_refuse' in _graph_src)
check("  且它进了 `value_tail` 的那一组（否则拒绝轮的叙述会把系统自己的参数值"
      "说成主人点名的名字）",
      "(value_refuse or grounded_refuse or todo_refuse)" in _graph_src)

# ── 抽取器本身：抽不出就是 `None`，**从不编造** ────────────────────────────
for _msg, _want, _why in [
        (_INCIDENT_MSG, _INCIDENT_BODY, "整句：编号连标点原样带走（一个字节都不许整理）"),
        ("帮我记一条待办：明天交房租，别忘了", "明天交房租，别忘了", "标记后紧跟冒号"),
        ("记一下，明天要买牛奶", "明天要买牛奶", "标记后紧跟逗号"),
        ("帮我在待办里加一条「把上周那篇配图换掉」", "把上周那篇配图换掉", "引号段整体"),
        # ⚠️ 这一条是**收紧过的**：放成"任意 ≤8 字 + 逗号"时它会被抽出「具体是什么
        #    我到时候再说」——那**是主人的原话**（出处闸放行），却根本不是一件事。
        #    出处闸只回答"是不是他说的字"，回答不了"这是不是那件事"。
        ("安排一下下周要办的事，具体是什么我到时候再说", None, "标记后面的尾巴不是那件事"),
        ("我对下个月有点想法，先记下来", None, "没有标记"),
        ("记录一下，下周三体检", None, "近似但不是标记（标记表是**闭集**）"),
]:
    check(f"抽取：{_why} → {_want!r}", g._msg_todo_text(_msg) == _want,
          repr(g._msg_todo_text(_msg)))

# ── 三态（用真的校正器，不重写判据）────────────────────────────────────────
def _add_plan(text):
    """planner 那一份计划体的形状（`params` 与 TOOLS 行两态都要在）。"""
    obj = instantiate_plan("dashboard_todo_add", {"text": text}, "admin")
    obj["params"] = {"text": text}
    return obj


def _add_gate(plan_obj, user_msg):
    """真校正器 + 捕获 trace 事件（不重写判据：`record` 换成记账的那个）。"""
    _ev: list = []
    _saved_rec = g.record
    g.record = lambda node, event, **data: _ev.append((node, event, data))
    try:
        ref = g._todo_text_fix(plan_obj, user_msg, "admin")
    finally:
        g.record = _saved_rec
    got = (g._tool_args((plan_obj.get("tools") or [""])[0])[0] or {}).get("text")
    return ref, got, plan_obj, _ev


_ref_g, _got_g, _p_g, _ev_g = _add_gate(_add_plan(_HALLUCINATED), _INCIDENT_MSG)
check("无据 + 主人标了标记 ⇒ 校正成主人标的那一段（**不拒绝**：那件事他真的说了）",
      _ref_g is None and _got_g == _INCIDENT_BODY, repr(_got_g))
check("  TOOLS 行**同步重建**（弹卡印的与 execute 执行的是同一份参数，不是两处各算一遍）",
      _p_g.get("tools") == [f'create_dashboard_todo({json.dumps({"text": _INCIDENT_BODY}, ensure_ascii=False)})'],
      str(_p_g.get("tools")))
check("  `params` 也同步（弹卡载荷读的是它）",
      (_p_g.get("params") or {}).get("text") == _INCIDENT_BODY,
      str(_p_g.get("params")))
check("  校正事件进 trace（否则「系统改写了 planner 填的正文」在事后只能靠肉眼看文本）",
      [e[1] for e in _ev_g] == ["todo_text_correct"]
      and _ev_g[0][2].get("used") == _INCIDENT_BODY
      and _ev_g[0][2].get("got") == _HALLUCINATED, str(_ev_g))

_ref_ok, _got_ok, _p_ok, _ev_ok = _add_gate(_add_plan(_INCIDENT_BODY), _INCIDENT_MSG)
check("**有据不动**：planner 填的就是主人那一段 ⇒ 一个字节不改、不拒绝",
      _ref_ok is None and _got_ok == _INCIDENT_BODY
      and _p_ok.get("tools") == [f'create_dashboard_todo({json.dumps({"text": _INCIDENT_BODY}, ensure_ascii=False)})']
      and _ev_ok == [], repr(_got_ok))

_ref_no, _got_no, _p_no, _ = _add_gate(_add_plan(_HALLUCINATED), "我对下个月有点想法，先记下来")
check("无据 + 主人**没**标标记 ⇒ 零写 + 如实追问（绝不猜一件顶上）",
      isinstance(_ref_no, str) and "原样再说一次" in _ref_no, str(_ref_no))
check("  说明里**原样印出**系统填的那个字面（主人要能看出系统编了什么）",
      isinstance(_ref_no, str) and _HALLUCINATED in _ref_no)
check("  空正文归展开层（`_expand_todo_skill` 那条「缺少正文」），**不**在这里抢答",
      g._msg_todo_text("加一条待办") is None
      and g._todo_text_fix(_add_plan(""), "加一条待办", "admin") is None)

# ── 不动边界：这几格只要动一下，就是"系统改别人的活"──────────────────────
_p_multi = _add_plan(_HALLUCINATED)
_p_multi["tools"] = _p_multi["tools"] + ["list_dashboard_todos({})"]
check("多 spec ⇒ 退回下一道门（`_todo_text_fix` 只管**单条加待办**这一格）",
      g._todo_text_fix(_p_multi, _INCIDENT_MSG, "admin") is None
      and _p_multi.get("tools")[0] != f'create_dashboard_todo({json.dumps({"text": _INCIDENT_BODY}, ensure_ascii=False)})')
_p_ref = _add_plan("$tool[0].text")
check("带 `$ref` 的参数不校正（那是**上一轮工具产出**的指代，不是主人这句话里的字）",
      g._todo_text_fix(_p_ref, _INCIDENT_MSG, "admin") is None)
_p_other = instantiate_plan("dashboard_todo_done", {"text": _HALLUCINATED}, "admin")
_p_other["params"] = {"text": _HALLUCINATED}
check("别的工具（勾完成/改排期走的是 `⑰` 那条台账通道）⇒ 本闸一个字都不动",
      g._todo_text_fix(_p_other, _INCIDENT_MSG, "admin") is None
      and (_p_other.get("params") or {}).get("text") == _HALLUCINATED)
_p_cs = _add_plan(_HALLUCINATED)
_p_cs["skill"] = "review_inbox"      # 变更集族：params 是 `{"calls": [...]}`，没有 `text` 槽
_p_cs["params"] = {"calls": [{"tool": "create_dashboard_todo",
                              "args": {"text": _HALLUCINATED}}]}
check("⭐ 变更集那一族不碰：单条 `calls` 里出现同一件工具时，只按工具名判会往 "
      "`{\"calls\": …}` 里塞一个没人读的 `text` 并拿它重建整份计划（丢的是一批裁决）"
      "——所以技能名也要判",
      g._todo_text_fix(_p_cs, _INCIDENT_MSG, "admin") is None
      and _p_cs.get("params") == {"calls": [{"tool": "create_dashboard_todo",
                                             "args": {"text": _HALLUCINATED}}]},
      str(_p_cs.get("params")))
check("  「加一条」**不**进 `_WRITE_NAME_FIELDS`：它的正文不在任何站内台账里，"
      "登记上去等于让台账通道去查一本查不到的书（它只会回一句「没有」= 假话）",
      "create_dashboard_todo" not in g._WRITE_NAME_FIELDS)

# ── 端到端（真 `planner_node` + 桩 LLM）：拒绝真的落到"零工具零帧"──────────
_saved_llm = g.get_llm
try:
    _llm = _ScriptedLLM([f'SKILL: dashboard_todo_add\nPARAMS: '
                         f'{json.dumps({"text": _HALLUCINATED}, ensure_ascii=False)}'])
    g.get_llm = lambda **kw: _llm                                     # noqa: ARG005
    _out = g.planner_node({"messages": [HumanMessage(content="我对下个月有点想法，先记下来")],
                           "plan_rounds": 0, "done": False}, _planner_cfg())
    _po = _out.get("plan_obj") or {}
    check("端到端：planner 编了正文而主人没标标记 ⇒ 本轮**零工具**（一个写都不发）",
          _po.get("tools") == [], str(_po.get("tools")))
    check("  结构化产出物带 `source=todo_text`（下游 `_no_popup_fact` 按它选支，"
          "不必再猜这一段散文在说什么）",
          (_po.get("refusal") or {}).get("source") == "todo_text",
          str(_po.get("refusal")))

    _llm2 = _ScriptedLLM([f'SKILL: dashboard_todo_add\nPARAMS: '
                          f'{json.dumps({"text": _HALLUCINATED}, ensure_ascii=False)}'])
    g.get_llm = lambda **kw: _llm2                                    # noqa: ARG005
    _out2 = g.planner_node({"messages": [HumanMessage(content=_INCIDENT_MSG)],
                            "plan_rounds": 0, "done": False}, _planner_cfg())
    _po2 = _out2.get("plan_obj") or {}
    check("端到端：主人标了标记 ⇒ **不拒绝**，照常走弹卡（那件事他说了，只差抄对）",
          _po2.get("refusal") is None and _po2.get("tools") == [
              f'create_dashboard_todo({json.dumps({"text": _INCIDENT_BODY}, ensure_ascii=False)})'],
          str(_po2.get("tools")))
finally:
    g.get_llm = _saved_llm

# ── 第二本账：短应答重提交上一轮那张卡上的事（20261006 16:51 那个循环）──────────
# 现场（生产 trace `20261006T165209` / `T165231`）：主人 16:51 让系统记一条待办，
# 卡弹出来了；他接着回「排期到今天」、再回一句「嗯」——两轮都被**本闸**判成针对
# "编造"（正文对不回**这一轮**的话）⇒ 零写、卡收回，那一行永远 pending，而 narrator
# 只能重复上一轮卡上那句「点「确定」我就去办」⇒ gate 洞⑥ 再把它换成一句自我纠正。
# 根因不是那句拒绝文案，是**出处只有一本账**：系统自己的规则（planner rule 1 /
# rule 21）要求"照 pending_action 原样重新提交"，而那个正文只活在台账那一行里
# （Rust `render_pending_action` 渲染：动作行原文 + `参数` 那一格是落库的 args JSON）。
# 这一段钉的就是"那一行进得来"：台账在场 ⇒ 放行；台账缺席/里面没有这个值 ⇒ 照旧拒。
_LEDGER_LINE = ("在后台首页的待办里加一条「" + _INCIDENT_BODY + "」，排期 未排期"
                "（那是你自己那份列表，加完随时能改能删）；动作 create_dashboard_todo"
                "（技能 dashboard_todo_add）；参数 "
                + json.dumps([{"args": {"text": _INCIDENT_BODY},
                               "tool": "create_dashboard_todo"}], ensure_ascii=False)
                + "；提出于 10-06 16:51；状态 awaiting（等主人点头，尚未执行）")


def _cid(ledger=None):
    """真 `planner_node` 跑一轮短应答（脚本化 LLM：planner 照台账那一行重提交）——
    返回 `(plan_obj, trace 事件名列表)`。"""
    _saved_llm2, _saved_rec2 = g.get_llm, g.record
    _ev2: list = []
    _llm = _ScriptedLLM([f'SKILL: dashboard_todo_add\nPARAMS: '
                         f'{json.dumps({"text": _INCIDENT_BODY}, ensure_ascii=False)}'])
    g.get_llm = lambda **kw: _llm                                         # noqa: ARG005
    g.record = lambda node, event, **data: _ev2.append((node, event, data))
    try:
        st = {"messages": [HumanMessage(content="嗯")], "plan_rounds": 0, "done": False}
        if ledger is not None:
            st["ledger"] = ledger
        return (g.planner_node(st, _planner_cfg()).get("plan_obj") or {},
                [e[1] for e in _ev2])
    finally:
        g.get_llm, g.record = _saved_llm2, _saved_rec2


_po_led, _ev_led = _cid({"pending": _LEDGER_LINE})
check("⭐ 短应答重提交：台账那一行在场 ⇒ **不拒绝**（正文照抄台账原文就是对的）",
      _po_led.get("refusal") is None
      and _po_led.get("tools") == [
          f'create_dashboard_todo({json.dumps({"text": _INCIDENT_BODY}, ensure_ascii=False)})'],
      str(_po_led.get("tools")))
check("  且留痕（事后要能分辨「这一轮为什么没判它编造」——这条闸的误判都是静默的）",
      "todo_text_from_ledger" in _ev_led, str(_ev_led))
_po_nol, _ = _cid()
check("反向对照：台账缺席（旧调用点/无待办）⇒ 照旧零写追问（一个字都不许松）",
      (_po_nol.get("refusal") or {}).get("source") == "todo_text"
      and _po_nol.get("tools") == [], str(_po_nol.get("refusal")))
_po_oth, _ = _cid({"pending": "在后台首页的待办里加一条「买牛奶」；动作 create_dashboard_todo"})
check("反向对照二：台账在、但那一行里**没有**这个正文 ⇒ 仍然拒（模型新编的走不了后门）",
      (_po_oth.get("refusal") or {}).get("source") == "todo_text",
      str(_po_oth.get("refusal")))

# ── 契约在位：描述与参数说明里那句"照抄"不许被后来的改动挤掉──────────────
_add_skill = _skill_map.get("dashboard_todo_add")
check("技能描述仍写着「照抄主人说的」与「不许润色」（提示词正文那一层是**唯一**"
      "还在提醒模型照抄的地方——这一句掉了，校正器就变成事后补漏）",
      _add_skill is not None and "照抄主人说的" in _add_skill.description
      and "不许润色" in _add_skill.description)
check("  参数说明同款（`text` 那一格写的是「原样，不要改写」）",
      _add_skill is not None and "原样" in str(_add_skill.inputs.get("text"))
      and "改写" in str(_add_skill.inputs.get("text")),
      str(_add_skill and _add_skill.inputs.get("text")))

# ── 评测层那一格（这才是「全量回归跑了这么多轮没发现」的答案）────────────────
# golden `admin_todo_add_popup` 现在带 `require_confirm_payload.args_from_input: ["text"]`：
# 载荷里那一格必须是**主人这句话的子串**。这一节的最后三条就是它的**判据的判据**——
# 一条判据不加反向对照就等于没加（本仓纪律）：喂一个编出来的正文必须当场红、
# 喂一个主人原话里的片段必须放行、拿不到原话时必须**响亮判不了**（不是静默给绿）。
sys.path.insert(0, str(ROOT / "eval"))
import run_golden as rg  # noqa: E402

_GOLD = {"require_confirm_payload": {"skill": "dashboard_todo_add",
                                     "args_from_input": ["text"]}}


def _cp_result(text):
    """一条 `__CONFIRM__` 帧的解析结果（形状照 `run_one` 的产物，只留判据要读的键）。"""
    return {"text": "好呀，这一步要动到站内数据，我先跟你确认一下：", "commands": [],
            "tool_calls": [], "exec_rows": [], "exec_tools": [],
            "frames": [], "confirm_tokens": ["tok"], "resets": 0, "resets_resets": [],
            "resets_reasons": [], "reset_scopes": [], "fallback_reasons": [], "error": None,
            "confirm_payloads": [{"skill": "dashboard_todo_add",
                                  "specs": [{"tool": "create_dashboard_todo",
                                             "args": {"text": text}}]}]}


_GOLD_MSG = "帮我记一条待办：明天交房租，别忘了"
check("⭐ 反向对照：编出来的正文（`20261006` 那条 trace 的原话）⇒ 判据**当场红**"
      "（不红的话这一格等于没加）",
      bool(rg.check_gold(_GOLD, _cp_result(_HALLUCINATED), user_input=_GOLD_MSG)),
      str(rg.check_gold(_GOLD, _cp_result(_HALLUCINATED), user_input=_GOLD_MSG)))
check("  主人原话里的片段 ⇒ 放行（不锁措辞、不锁采样长度：主人给几个字就是几个字）",
      rg.check_gold(_GOLD, _cp_result("明天交房租，别忘了"), user_input=_GOLD_MSG) == []
      and rg.check_gold(_GOLD, _cp_result("交房租"), user_input=_GOLD_MSG) == [])
check("  空白差异不算抄错（模型抄写时换行/加全角空格 ⇒ 归一后仍是原话）；"
      "但**标点**变了就是抄错了字，不在豁免里",
      rg.check_gold(_GOLD, _cp_result("明天 交房租，别忘了"), user_input=_GOLD_MSG) == []
      and bool(rg.check_gold(_GOLD, _cp_result("明天交房租 别忘了"), user_input=_GOLD_MSG)))
check("⭐ 拿不到主人原话 ⇒ **响亮判不了**（不是静默放行——判据的前提住在调用点手里，"
      "前提没到就不许给绿）",
      bool(rg.check_gold(_GOLD, _cp_result("交房租"), user_input="")))

# ── 第二本账（20261006 晚，判据层的那一半）────────────────────────────────
# 上一格判的是"载荷里的字出不出自**这一轮**主人这句话"。而系统自己的规划纪律
# （rule 1 / rule 21）要求"短应答照 pending_action 原样重提"——那件事的正文**不在
# 他这一轮的话里**，在上一轮那张卡的台账行里。只认 `user_input` 的判据会把系统自己
# 规定的那条路判成编造：**行为侧**当天已经修过（`_todo_text_fix` 那一族四道闸都接了
# `ledger_src`），**判据侧**当时没动（判据的改动要点头）——这一格就是补它的孪生。
# 不补的后果有两条，第二条比第一条贵：① 谁写一条"回『嗯』重提"的用例，**做对的
# 那一轮会红**；② 为了让那条用例变绿，人自然会去改行为——等于把刚修好的洞按回去。
check("⭐ 正控：先确认这一格**本来**会红（不先亮这一条，下面那条『放行』是假的——"
      "绿色可能只是判据根本没跑）",
      bool(rg.check_gold(_GOLD, _cp_result(_INCIDENT_BODY), user_input="嗯")))
check("⭐ 台账那一行在场 ⇒ **放行**：主人这一轮只说了一个「嗯」，正文照抄台账原文就是对的",
      rg.check_gold(_GOLD, _cp_result(_INCIDENT_BODY), user_input="嗯",
                    pending_src=_LEDGER_LINE) == [])
check("  反向对照：台账在、但那一行里**没有**这个正文 ⇒ 仍然判红"
      "（模型新编的走不了第二本账这个后门）",
      bool(rg.check_gold(_GOLD, _cp_result(_HALLUCINATED), user_input="嗯",
                         pending_src=_LEDGER_LINE)))
check("  反向对照二：两本账都拿不到 ⇒ 照旧**响亮判不了**"
      "（第二本账不许把『前提没到』变成静默给绿）",
      any("判不了" in f for f in rg.check_gold(_GOLD, _cp_result(_INCIDENT_BODY),
                                              user_input="", pending_src="")))
check("  加第二本账**没有松掉第一本**：台账在场时，只出现在主人原话里的正文照旧放行"
      "（两支是并列的『或』，台账不是「只有它才算数」）",
      rg.check_gold(_GOLD, _cp_result("交房租"), user_input=_GOLD_MSG,
                    pending_src="在后台首页的待办里加一条「买牛奶」") == [])
# 接线锁：两本账都得由**调用点**喂进来，而喂进来的那根字符串必须与真发给模型的
# 逐字同一份——`build_request` 与 `check_case` 各展开一次 `expand_now`，
# 两处各漂各的正是这一族最容易犯的错（行首是 `{now…}` 占位符，展开时刻不同 ⇒
# 同一行在两边长得不一样，判据与模型看到的东西从根上就不是一份）。
_rgsrc = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
check("⭐ 接线在位：`check_case` 把这一轮的 `context.pending_action` 喂给判据"
      "（漏了它 ⇒ 第二本账恒空，上面那条『放行』测的是个没人调用的参数）",
      'pending_src=expand_now((rnd.get("context") or {}).get(' in _rgsrc)
check("  且判据点与请求构造点用的是**同一个** `expand_now`（两处各漂各的 ⇒ "
      "判据比对的字符串与模型看到的那一行不是同一份）",
      _rgsrc.count("expand_now(") >= 3 and '"pending_action") or ""))' in _rgsrc)

settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
