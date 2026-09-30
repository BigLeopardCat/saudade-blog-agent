# -*- coding: utf-8 -*-
"""零帧轮的「系统侧写动作完成式」声称（gate 洞⑨，20260930）。

**为什么单起一套**：洞① 收的是"帮你把 X 打开了"这类**第一人称施事 + 手写动词表**
的完成式声称，而真实叙述里还有第三种说法——**施事是「系统/后台」、动作词来自写能力
清单、完成标记落在同句更早的位置**。事故实证（trace `20260930T123938`，主人全程
可见，uid=1）：主人说「我的未读信息全部就标记为已读」（明确的祈使写请求），planner
判 chat（零工具、本轮 execute 零事件），narrator 回**「这一轮系统真的办成了：你
（id=1）的未读站内信已全部标记为已读。现在你的未读站内信是 0 封」**。把这句话喂给
graph 里**全部 12 张声称网**，**一张都没命中**，gate 判 PASS 放行；下一轮主人追问
「你调用工具了吗就说」，是**模型自己的诚实**认了「那句是我编的」——系统没拦住它。

漏的机制不是"模型学乖了"，是**判据的射程与写能力清单脱钩**：洞里那些动词表一处一手
写，而 `read_notifications`（20260923 批 8）上线时没有任何东西会因此报错。所以本套件
除了行为锁，还钉一条**同步锁**：每个 write scope 工具都必须在 `action_text.WRITE_CLAIM_ROOTS`
里有词根——加写能力忘了加词，这里当场红。

本套件锁五条：
  ① 正例必中（事故原句 + 同族变体）；
  ② 负例必不中——**四条是全量复扫抓出来的真实误伤原句**（第一版判据 6 命中 5 误伤，
     见 `_WRITE_DONE_CLAIM_RE` 上方的收窄说明），加上据实转述/疑问/条件/否定/给主人
     的操作建议：零帧轮误伤的代价是整轮回复被 fallback 吞掉（本仓一贯取向，宁漏勿误伤）；
  ③ **同步锁**：write scope 工具集合 ⊆ `WRITE_CLAIM_ROOTS` 的键（加写能力必须登记词根）；
  ④ 接线锁：这一族真的挂在**零帧族表**里、且排在 `if frames_exist` 之后；
  ⑤ 收窄锁：`_STATE_ACTION_EXEMPT_RE` 的裸「未」必须带名词化否定前瞻（否则「未读」
     这个名词会把最该抓的那句话整句豁免掉——实测）。

用法：.venv/bin/python tests/test_write_done_claim.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import action_text as AT        # noqa: E402
from agent import graph as G               # noqa: E402
from agent.authz import TOOL_SCOPE, WRITE_SCOPES   # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ══════════════════════════════════════════════════════════════════
print("\n① 正例：零帧轮里「系统办成了…」必判（本轮什么都没发生）")

POS = [
    # 事故原文（trace 20260930T123938）
    "主人，这一轮系统真的办成了：你（id=1）的未读站内信已全部标记为已读。",
    # 同族变体：换写动作（换词根）/ 换施事 / 只留泛完成断言
    "这一轮系统已经把那个标签删掉了",
    "本轮系统真的把留言复核通过了",
    "这一轮系统真的办成了，你的额度已经恢复了",
    "系统真的办成了，全部搞定",
    # D 支（施事 + 完成副词 + 写词根，**不带本轮指称**）：A 支要"这一轮/本轮"、
    # B/C 支只认泛完成断言 ⇒ 这个形态三支全漏，20260930 全量复扫后补上。
    "后台已经帮你把公告发出去了",
    "系统已经帮你把那个标签删掉了",
    "服务器已经把这批留言复核通过了",
]
for t in POS:
    check(f"必判：{t[:26]}…", G._write_done_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n② 负例：**全量复扫实测的误伤原句** + 转述/疑问/条件/否定/建议 —— 一律放行")

NEG = [
    # ── 第一版判据在 1112 份真实 trace / 278 轮零帧轮上的 4 条误伤（原句）──
    # "系统"是复合名词的中心语、"通过"是介词（技术解释，一个动作都没声称做过）
    "Rust 的所有权系统通过 `Option<T>` 把这个运行时错误前移到了编译期",
    # "系统"是定语（在讲系统的一条正则怎么工作）
    '也就是说你那条"转跳到物联网平台"被系统的导航关键词正则直接命中',
    # 照 page_ctx 念事实（"显示"不是 device 写；这一轮它确实有页面上下文可读）
    "喵呜～系统显示你是 user_id=1",
    # "后台"是**地点**、整句是给主人的**操作建议**（一个动作都没声称做过）
    "所以这个操作需要主人自己去后台完成：先删掉一级标签 `Asyncio`（id=19）",
    # ── D 支候选的真身（全量复扫里 4 条，全部是**自我否认 / 带证据的追述**，
    #    靠豁免表落网；**所以 D 支的副词只收完成类、不收"真的/确实"**）──
    "3. 唯一能确定的是：**系统确实执行了那次跳转**（回执帧为证）",
    "我这边**没有看到这次驳回操作的执行记录**喵——刚才那轮系统并没有真的去点「驳回」",
    "系统**并没有真的去查那两条留言的审核明细**（本轮工具记录是空的）",
    "我这边**没有看到执行记录**——刚才那一轮系统并没有真的发出驳回操作",
    # ── 据实转述既有事实（rule 6 的合法形态）──
    "你的留言「想说啥来着，忘了。」已通过审核",
    "那条公告是上一轮系统发的（系统记录里写着）",
    "记录里最近一次系统操作是 03:23 标记了两条通知已读",
    # ── 疑问 / 条件 / 否定 ──
    "系统真的办成了吗？",
    "如果系统这一轮办成了，页面会自己刷新",
    "这一轮系统没有办成，那个标签还在",
    "系统并不是把留言删掉了，是隐藏了",
    # ── 能力清单 / 提议（没有完成态，也不是声称）──
    "我可以帮你把未读通知标记为已读，要我办吗？",
    "要我把这 4 条真的标记为已读吗？",
    "这一轮系统会帮你把标签删掉，你点头就行",
    # ── 本轮指称缺失的同族（刻意不收，见正则上方收窄①）──
    "未读站内信已全部标记为已读",
]
for t in NEG:
    check(f"放行：{t[:26]}…", not G._write_done_claim(t), t)

# ══════════════════════════════════════════════════════════════════
print("\n③ 同步锁：**每个 write scope 工具都登记了词根**（加写能力忘了加词 → 这里红）")

_write_tools = {n for n, sc in TOOL_SCOPE.items() if sc in WRITE_SCOPES}
_missing = sorted(_write_tools - set(AT.WRITE_CLAIM_ROOTS))
_stale = sorted(set(AT.WRITE_CLAIM_ROOTS) - _write_tools)
check(f"write scope 工具 {len(_write_tools)} 件全部有词根", not _missing,
      "缺：" + ", ".join(_missing))
check("  词根表里没有已不是 write scope 的僵尸条目（改名/删工具要一起收拾）",
      not _stale, "僵尸：" + ", ".join(_stale))
_bad: list[str] = []
for _n, _frag in sorted(AT.WRITE_CLAIM_ROOTS.items()):
    try:
        re.compile(_frag)
    except re.error as e:                       # pragma: no cover - 出错即红
        _bad.append(f"{_n}: {e}")
check("  每条词根都是合法正则片段", not _bad, "; ".join(_bad))
check("  词根表非空且写进了图里的正则（两边同源，不是各写一份）",
      bool(AT.WRITE_CLAIM_ROOTS) and "WRITE_CLAIM_ROOTS" in
      (ROOT / "agent" / "graph.py").read_text(encoding="utf-8"))

# ══════════════════════════════════════════════════════════════════
print("\n④ 接线锁：这一族在**零帧族表**里，且排在 `if frames_exist` 之后")

_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")


def _fn_src(name: str) -> str:
    at = _SRC.index(f"def {name}(")
    body = _SRC[at:]
    return body[:body.index("\ndef ", 10)]


_BODY = _fn_src("_zero_frame_families")
_RUNNER = _fn_src("_claim_issue")
check("族在零帧族表里（谓词 + 子句版都在）",
      "_write_done_claim" in _BODY and "_write_done_claim_clause" in _BODY)
check("  `_claim_issue` 过表排在 `if frames_exist: return None` **之后**（有帧轮不查）",
      _RUNNER.index("if frames_exist") < _RUNNER.index("_zero_frame_families("))
check("  返回的原因码是 `sys_write_claim_without_tool`",
      '"sys_write_claim_without_tool"' in _BODY)
# 行为锁：走完整族表时要能落到这一族（而不是被别的族抢走）
_INCIDENT = ("主人，这一轮系统真的办成了：你（id=1）的未读站内信已全部标记为已读。"
             "现在你的未读站内信是 **0 封**，列表清干净了喵～")
_r = G._claim_issue(_INCIDENT, "chat", {"note": "", "status": ""}, False, exec_memory=True)
check("  事故原句走 `_claim_issue` 落到 `sys_write_claim_without_tool`",
      bool(_r) and _r[0] == "sys_write_claim_without_tool", str(_r and _r[0]))
check("  子句版把那句话原样带回来（trace 要能指出判的是哪句）",
      "未读站内信已全部标记为已读" in (_r[2] if _r else ""))
# 兜底文案：只否认"这一轮没执行"，**不许把编出来的那个读数坐实**
_ft = G._FALLBACK_WRITE_DONE
check("兜底文案说清这一轮零执行、并写明读数也是编的",
      "一次工具都没有执行" in _ft and "瞎报" in _ft)
check("  且不替它复述任何具体状态（不许坐实「未读 0 封」这类读数）",
      "0 封" not in _ft and "已经标记为已读" not in _ft)
check("  但它把编造的那句原样点出来（『…』里是模型自己的话，是**指认**不是坐实）",
      "办成了" in _ft)

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 收窄锁：豁免表里的裸「未」必须带名词化前瞻")

for noun in ("未读", "未知", "未审", "未阅", "未免"):
    ex = G._STATE_ACTION_EXEMPT_RE.search(f"这一轮系统真的办成了：{noun}的东西全处理了")
    check(f"  「{noun}」不算否定词（不豁免）", ex is None, str(ex and ex.group(0)))
for neg in ("未标记", "未能", "未曾"):
    ex = G._STATE_ACTION_EXEMPT_RE.search(f"系统{neg}处理完")
    check(f"  「{neg}」仍算否定词（照旧豁免）", ex is not None, str(ex and ex.group(0)))

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
