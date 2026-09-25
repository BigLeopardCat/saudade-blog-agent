# -*- coding: utf-8 -*-
"""后台首页待办 / 日程（20260926 第八轮）：纯函数 + 工具 + 确认闸，零网络零 LLM。

这一件与别的写操作最不一样的地方：**目标是主人随口说的一件事**，站内没有任何
东西可以拿它来核对——标签/分类/公告/留言都能去字典里问"有没有这个名字"，而
"下周三交房租"没处可查。于是判据只剩两条，且都必须是"缺了就零写"：

  1. **正文空 / 超长 → 零工具**（空正文是口误不是待办；超长不替主人截断）；
  2. **排期翻不出来 → 零工具 + 追问**（绝不挑一个日子顶上——错一天的日程会静静
     躺在后台日历的错误格子里，主人不翻到那天根本发现不了）。

因为没处可核对，它在授权层被放进 `_ALWAYS_CONFIRM_TOOLS`：**每次都弹确认卡**，
连"同轮命令即确认"那条捷径也不走（§⑦ 用真实的 execute 路径锁住这一点）。

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
check("它是**自由文本**那一族（不在名字通道、也不在 own 通道——判据错位比没有判据更糟）",
      _FREE_TEXT_WRITE_SKILLS == frozenset({"dashboard_todo_add"}), str(_FREE_TEXT_WRITE_SKILLS))


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

settings.jwt_secret = _SAVED_SECRET   # 收尾：把这个全局单例还原成进来时的样子

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
