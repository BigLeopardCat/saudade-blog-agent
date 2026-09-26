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
from agent.graph import execute_node, plan_encode  # noqa: E402
from agent.principal import ROLE_ADMIN, ROLE_USER, Principal  # noqa: E402
from agent.skills import _FREE_TEXT_WRITE_SKILLS, instantiate_plan  # noqa: E402
import tools.base as base  # noqa: E402

# ── 密钥桩：settings.jwt_secret 是全局单例（同 test_admin_write）────────────
# `_confirm_popup` 在密钥空缺时**不弹窗**（宁可退回追问，也不发一个验不过的令牌）。
# CI 里没有 .env ⇒ 本地会绿、CI 会静默变成"没弹"（"该弹窗"的正例整体消失）。桩完
# 才是可复现的，且与本套件 §⑦ 那几条正例是同一件事。
from config.settings import settings  # noqa: E402

_SAVED_SECRET = settings.jwt_secret
settings.jwt_secret = "test-secret-for-confirm-tokens"

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
      _FREE_TEXT_WRITE_SKILLS == frozenset({"dashboard_todo_add", "dashboard_todo_done"}),
      str(_FREE_TEXT_WRITE_SKILLS))
# 桶成员资格只说"目标是自由文本"，**展开函数要按技能名二分**（第十轮加了"勾完成"）：
# 这条钉的是**两个技能名都真的在自己的路径上**——漏了二分的后果是静默的，"勾完成"
# 会被 `_expand_todo_skill` 展开成 `create_dashboard_todo`（多记一条待办），
# 所以判据落在"展开出的工具名"上，而不是"桶里有几个名字"。
check("  桶内两个技能各自展开成自己的工具（勾完成不会展开成「加一条」）",
      "complete_dashboard_todo" in "".join(
          instantiate_plan("dashboard_todo_done", {"text": "给猫买罐头"})["tools"])
      and "create_dashboard_todo" in "".join(
          instantiate_plan("dashboard_todo_add", {"text": "给猫买罐头"})["tools"]),
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

with patch(_admin_get=lambda p, c: []):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("真的没记过 → kind=empty（**事实**，checker 照常 PASS 进回执）",
          r.kind == "empty" and "空的" in r, f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: base.unavailable("后台读不到")):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("读不到 → unavailable（**不许**说成「你还没记过待办」）",
          r.kind == "unavailable", f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: {"weird": 1}):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("形态不对 → unavailable", r.kind == "unavailable", f"{r.kind}: {r}")

with patch(_admin_get=lambda p, c: [todo("给猫买罐头", TOMORROW)]):
    r = base.list_dashboard_todos.invoke({}, config=cfg())
    check("正常 → ok，给 narrator 的是渲染好的清单（一行一条）",
          r.kind == "ok" and "给猫买罐头" in r and r.meta.get("count") == 1,
          f"{r.kind}: {r.meta}")


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
    state = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
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
print("\n⑧ 接线：读技能不进写名单，且后台待办**只有**追加这一条写通道")

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
          and set(r.meta) <= set(g._RCPT_META_KEYS), str(r.meta))
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
check("三张卡互不同形（勾 / 加 / 冻结 各自读起来是不同的事）",
      len({_q_done, _q_add,
           A.render_confirm_question([{"tool": "freeze_account", "args": {"name": "guest5"}}])}) == 3)
check("回执行区分「刚勾的」与「本来就是」（一次 no-op 不能被读成一个动作）",
      "本来就是完成状态" in A.render_todo_done("交房租", changed=False)
      and "已把待办「交房租」勾成完成" in A.render_todo_done("交房租", changed=True),
      A.render_todo_done("交房租", changed=False))


# ══════════════════════════════════════════════════════════════════
print("\n⑭ 过程行与落库回执：带正文、不带内部工具名（两处措辞逐字一致）")

import server as _srv  # noqa: E402

_a = _srv._tool_action_text("complete_dashboard_todo", {"text": "交房租"})
check("过程行念出正文（这一行会经 recent_executions 注入下一轮——没有正文就认不出是哪条）",
      "交房租" in _a and "勾成完成" in _a, _a)
check("  不裸露内部工具名（带下划线的名字会被 narrator 照抄）",
      "complete_dashboard_todo" not in _a)
check("  这一行**不写「已完成」**（它是执行前的预告，后端幂等分支上是真 no-op）",
      "已完成" not in _a, _a)
check("  缺正文时退化成动作词，不炸",
      _srv._tool_action_text("complete_dashboard_todo", {}) == "勾完成待办",
      _srv._tool_action_text("complete_dashboard_todo", {}))
_rsrc = (ROOT.parent / "src" / "routes" / "chat.rs").read_text(encoding="utf-8")
check("Rust 那半有同名臂（漏了会把 `complete_dashboard_todo` 这种带下划线的内部名"
      "写进 execution_log 被下一轮照抄）",
      '"complete_dashboard_todo" =>' in _rsrc, "chat.rs")
check("  两侧措辞逐字一致（预告帧与落库回执是同一件事的两处渲染）",
      "把待办「{}」勾成完成" in _rsrc, "chat.rs")


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
            state = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
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
        state = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
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
    state = {"plan": plan_encode(obj), "plan_rounds": 1, "done": False,
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


settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
