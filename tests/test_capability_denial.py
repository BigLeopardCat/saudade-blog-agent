# -*- coding: utf-8 -*-
"""零帧轮的"**有这件能力却说成站内没有**"声称（gate 洞⑫，20261003）。

**为什么单起一套**：这条网的依据与前几族**不在一处**。洞①/⑨/② 问的是"这一轮做过
没有"（依据在工具帧 / 跨轮回执里），洞④ 问的是"这条**结论**本轮查过没有"（依据仍在
本轮），而本族问的是"**这件能力**站内有没有"——依据在**技能注册表**里
（`visible_skills(role)`）。这意味着两件前几族没有的性质：

  · **它带角色**：同一句「站内没有删除留言的通道」，普通用户说是**实话**
    （board_delete 对他不可见），管理员说才是假话；
  · **判据片段必须同源**：动词取自该技能 `plan` 里那些工具的
    `action_text.WRITE_CLAIM_ROOTS`（写技能）/ `skills.CAPABILITY_DENIAL_VERBS`
    （读技能），对象取自 `skills.CAPABILITY_DENIAL_OBJECTS`，两半必须落在**同一件
    能力**上——否则「站内没有删除已发通知的功能」（**实话**，它逐字印在 notice_send
    的确认卡面上）会被误判。

两处现场（都是判据诞生前真实发生过的）：
  ① trace `20260928T032411`：管理员要删一条被驳回的留言，回复写「系统这边没有删除
     被驳回留言的通道……这一步只能你自己进后台手动处理」——主人被**劝退**，而
     `board_delete` 就在他的能力清单里；
  ② 20261003 复扫：「我这边现在还没有办法直接搜索全站文章里的具体关键词呢」——
     站内明明有检索（`content_query`）。那一例当时按假红修的是**洞④ 的动词表**
     （不再把它当"站内没有这个内容"判），可**放行不等于这句话是对的**。

本套件锁六条：
  ① 正例必中（两处现场原句 + 同族变体）；
  ② 负例必不中——**说真话的那几类**（卡片上印着的"没有撤回已发通知的通道"、
    能力清单里真的没有的"发邮件"、别人的专属权限、管理域里真的看不见的登录 IP、
    "批量"这种站内确实做不到的范围词）；
  ③ **角色锁**：同一句话按角色的两种结局（普通用户/杂鱼放行、管理员必判）；
  ④ **两半同源锁**：动词与对象必须来自**同一件**能力（跨技能串台 = 误伤）；
  ⑤ 接线锁：族在 `_zero_frame_families` 里、排在洞④ **之前**、`role` 真的从
     `_claim_issue` 传下来、gate 调用点真的传身份；且这一族在 `_REPLAN_ISSUES` 里
     （打回口径必须避开洞④ 的**检索味**建议——主人要的是"删"，让他再搜一遍是错的下一步）；
  ⑥ 兜底文案只否认**被点名的那件事**（不许说成"这件事已经办了"——它这一轮什么都没做）。

两条判据**在诞生后各修过一次**（20261003，都是"生产原话打不到"）：
  · ④b **给予义谓词槽**——「没有**提供**「直接删除一个标签」的能力」的「提供」原来不在
    任何一条形状的窗口里；
  · ④c **引号规则**——只加槽还是打不到，因为那句话把动词与对象**一起写进了引号**，而
    声称闸的通用预处理 `_strip_quoted_spans` 会把引号内容整段删掉。
两处都要按**生产原话**（连标点带引号）验，只拿裸句试会得到"已经修好了"的错觉。

用法：.venv/bin/python tests/test_capability_denial.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import graph as G  # noqa: E402
from agent import skills as S  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


_PLAN = {"skill": "chat", "note": "", "status": ""}


def judge(text: str, role: str | None) -> bool:
    return G._capability_absent_claim(text, role)


# ══════════════════════════════════════════════════════════════════
print("\n① 正例：技能表里明明有的能力，被说成「站内没有」——必判")

POS = [
    # 现场 ①（trace 20260928T032411）原句
    ("系统这边没有删除被驳回留言的通道，这一步只能你自己进后台手动处理", "admin"),
    # 现场 ②（20261003 复扫）原句
    ("我这边现在还没有办法直接搜索全站文章里的具体关键词呢", "user"),
    # 同族变体：换否定词 / 换能力名词 / 换对象的说法
    ("站内并不存在查看留言的功能", "admin"),
    ("我这边没有审核留言的入口", "admin"),
    ("系统没有新建标签的能力", "admin"),
    ("抱歉，站内没有删除分类这个功能", "admin"),
    ("我这个身份没有办法把文章设成置顶", "admin"),
    # 现场 ③（20261003 落地）：否定词与动词之间插了**给予义谓词**「提供」——
    # `admin_write_intent_tag_remove_popup` 慢性红（12/35 = 34%）给出的正是这一句
    ("系统这边没有提供「直接删除一个标签」的能力", "admin"),
    # 同一个槽的其余词形（给出/开放）与"槽前还有塞词"的位置
    ("系统这边没有给出删除标签的能力", "admin"),
    ("系统没有开放删除标签的接口", "admin"),
    ("很抱歉，我没有给你删除留言的通道", "admin"),
]
for t, role in POS:
    check(f"必判（{role}）：{t[:26]}…", judge(t, role), t)

# ══════════════════════════════════════════════════════════════════
print("\n② 负例：**说真话的那几类** —— 一律放行（零帧轮误伤的代价是整轮回复被吞）")

NEG = [
    # 卡片正面印着的那句实话（notice_send 的确认卡；20260926 那条 golden 的 `_note`
    # 记着它曾经自命中一条断言）——"删除"属删类技能、"通知"属发通知技能，凑不成一对。
    # 加过「提供」槽的词形也要照样放行（槽只让匹配往前多吃两字，同源那半边判的是**同一次**
    # 命中的整段文本，所以两张守网（同源 / 范围词）都不受新槽影响）
    ("站内没有删除已发通知的功能", "admin"),
    ("站内没有提供删除已发通知的功能", "admin"),
    # 能力清单里真的没有的（`admin_capability_absent_honest` 的现场）
    ("我的能力清单里没有「发邮件」这一项", "user"),
    ("站内没有查看登录 IP 的能力", "admin"),
    ("这个能力不在我的清单里", "admin"),
    # 别人的专属权限（对普通用户是实话；角色那一半由 ③ 单独锁）
    ("发布公告是博客主人 Sora 的专属权限", "user"),
    # 站内确实做不到的那件事：范围词
    ("站内没有批量删除留言的功能", "admin"),
    ("站内没有提供批量删除留言的功能", "admin"),
    ("没有一次性清空所有留言的通道", "admin"),
    # 裸"文章"不是 article_status 的对象（"发布新文章"站内真的没有：没有写正文的通道）
    ("站内没有发布新文章的功能", "admin"),
    # 纯内容结论（归洞④ 管，不是本族）
    ("站内没有关于这个话题的文章喵", "user"),
]
for t, role in NEG:
    check(f"放行（{role}）：{t[:26]}…", not judge(t, role), t)

# ── 本族最近一次放宽（20261003，主人点名后落地）：**给予义谓词槽** `_CAP_FAIL_GIVE` ──
#
# 起因是 golden `admin_write_intent_tag_remove_popup` 慢性红 12/35 = 34%（当前最红一条）
# 里那句：「系统这边**没有提供**「直接删除一个标签」的**能力**」——事实相反，`tag_delete`
# 就在管理员的能力清单里。它从判据底下漏过去纯属**形状**：乙支要求否定词后 ≤3 字就接动词，
# 而这句在否定词与动词之间插了「提供「直接」5 个字。
#
# 落地前按"动闸门要拍板 + 多遍 A/B"的纪律挂了一轮（正例里那句就是当时的**已知漏**）。
# 复扫口径与结果（1082 份有回复的 trace；uid=1 记超管、其余记普通用户，与本族运行时同一
# 份 `visible_skills` 判据）：原形状命中 1 组、放宽后**新增 0 组**；再按**超管身份**过一遍
# 同一份语料（"最坏情况"上界，把所有管理能力都放开）也**新增 0 组** ⇒ 生产误伤面为零。
# 放宽后的行为是"打回重规划一次"而非兜底吞轮：本族早在 `_REPLAN_ISSUES` 里、
# `_REPLAN_ADVICE` 那段也已按写族写好（"该动手就去动手"，不是洞④ 的检索味建议）。
#
# 下面的负例里那些"带「提供」槽"的条目就是这次放宽的**反向对照**：新槽只让匹配往前多
# 吃两字，同源（动词/对象必须属于同一件能力）与范围词两张守网**都不受影响**。

# ══════════════════════════════════════════════════════════════════
print("\n③ 角色锁：同一句话，按角色两种结局（依据只能是 `visible_skills`）")

_SAME = "系统这边没有删除留言的通道，你自己进后台手动删吧"
check("  admin ⇒ 判（他真能删）", judge(_SAME, "admin"))
check("  superadmin ⇒ 判", judge(_SAME, "superadmin"))
check("  user ⇒ 放行（board_delete 对他不可见 = 实话）", not judge(_SAME, "user"))
check("  zako ⇒ 放行（只准闲聊族）", not judge(_SAME, "zako"))
# 反向那一条：普通用户自己的数据能力对他是**可见**的（拿"收藏"试）
_FAV = "站内没有收藏文章的功能"
check("  反向：普通用户说「没有收藏功能」⇒ 判（favorite_add 对他是可见的）", judge(_FAV, "user"))

# ══════════════════════════════════════════════════════════════════
print("\n④ 两半同源：动词必须来自*这一件*能力（跨技能串台 = 误伤）")

_V = {k: G._capability_denial_verbs(S.SKILL_MAP[k]) for k in S.CAPABILITY_DENIAL_OBJECTS}
check("  每个对象词条都对应一件真技能、且动词非空",
      all(S.SKILL_MAP.get(k) and _V[k] for k in S.CAPABILITY_DENIAL_OBJECTS))
check("  读技能那份 `CAPABILITY_DENIAL_VERBS` 的键都在对象表里（两边不许各写一份）",
      set(S.CAPABILITY_DENIAL_VERBS) <= set(S.CAPABILITY_DENIAL_OBJECTS))
check("  board_delete 的动词里**不含**发通知的词根（这就是 ② 第一句放行的原因）",
      not re.search(r"发|通知", _V["board_delete"]), _V["board_delete"])
check("  `content_query` 走显式表（它的 plan 里没有写工具可借）",
      _V["content_query"] == S.CAPABILITY_DENIAL_VERBS["content_query"])

# ══════════════════════════════════════════════════════════════════
print("\n④b 给予义谓词槽（20261003 放宽）：两副形状**都要**接，不许只补一半")

_DENIED_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
_AT = _DENIED_SRC.index("def _capability_denied(")
_SHAPES = _DENIED_SRC[_AT:_DENIED_SRC.index("\ndef ", _AT + 10)]
check("新槽定义在场，且是**可选**的（不给也接老形状）",
      "_CAP_FAIL_GIVE" in _DENIED_SRC and "_CAP_FAIL_GIVE = " in _DENIED_SRC
      and G._CAP_FAIL_GIVE.endswith(")?"), G._CAP_FAIL_GIVE)
check("  两种词形（提供/给出/支持/开放）都在槽里",
      all(w in G._CAP_FAIL_GIVE for w in ("提供", "给出", "支持", "开放")), G._CAP_FAIL_GIVE)
check("  甲支接了槽（`_CAP_FAIL_LEAD` 与名词之间）",
      "{_CAP_FAIL_LEAD}{_CAP_FAIL_GIVE}{_CAP_FAIL_GAP}{{0,4}}{_CAP_FAIL_NOUN}" in _SHAPES)
check("  乙支也接了槽（缺少它 = 这次放宽要修的那句仍然漏）",
      "{_CAP_FAIL_LEAD}{_CAP_FAIL_GIVE}{_CAP_FAIL_GAP}{{0,3}}" in _SHAPES)
# 行为锁：拿**定义**（不是仓内现成的常量）重算一遍，防"定义了却忘了接"
from agent.skills import CAPABILITY_DENIAL_OBJECTS  # noqa: E402

_V_TAG = G._capability_denial_verbs(S.SKILL_MAP["tag_delete"])
check("  重算（用 `_CAP_FAIL_GIVE` 现算的正则）仍判那句原话",
      G._capability_denied("系统这边没有提供「直接删除一个标签」的能力",
                           _V_TAG, CAPABILITY_DENIAL_OBJECTS["tag_delete"]))
check("  同一次命中里，槽前塞词 ≤2 字（「没有向你提供…」也要接得住）",
      G._capability_denied("我们没有向你提供删除标签的能力",
                           _V_TAG, CAPABILITY_DENIAL_OBJECTS["tag_delete"]))
check("  槽**不是**万能前缀：别的动词不会因为前置了「提供」就被认成能力否定",
      not G._capability_denied("我们没有提供摄影小贴士的说明",
                               _V_TAG, CAPABILITY_DENIAL_OBJECTS["tag_delete"]))

# ══════════════════════════════════════════════════════════════════
print("\n④c 引号规则（20261003 第二处修补）：能力名在引号里要**留着**，转述整句要**剥掉**")

# 这一处是 ④b 落地后**实测踩出来**的：光加谓词槽仍然打不到那句原话——因为生产里的写法
# 把动词与对象**一起写进了引号**，而声称闸的通用预处理 `_strip_quoted_spans` 会把整段
# 引号内容**删掉**，那句话于是变成「系统这边没有提供的能力」。洞⑫ 专用一份引号处理。
_RAW = "系统这边没有提供「直接删除一个标签」的能力"
check("  引号里的能力名**留着**（两半都在引号里）",
      "直接删除一个标签" in G._quotes_dropped_but_named_kept(_RAW))
check("  通用预处理仍会把它剥掉（这就是它打不到的原因，锁住防回退）",
      "删除" not in G._strip_quoted_spans(_RAW))
check("  转述整句**仍要剥掉**（否定词在引号里 = 那是别人的话，不是它的声称）",
      "没有删除留言的通道" not in G._quotes_dropped_but_named_kept(
          "留言里有人写「站内没有删除留言的通道」。"))
check("  引号外还有否定词时照旧判（引号规则不是一刀切放行）",
      G._capability_absent_claim(
          G._quotes_dropped_but_named_kept("系统这边没有提供「直接删除一个标签」的能力"),
          "admin"))
# 端到端：**诞生现场那份 trace 的原话**（20261003_180024 那条慢性红）
_REAL = ("主人，这件事我做不到喵呜……\n\n系统这边没有提供「直接删除一个标签」的能力"
         "——现有的标签操作只支持**新建、改名、改颜色、换父级、一级↔二级互转**，"
         "唯独没有「删掉某个标签」这一项。")
_R = G._claim_issue(_REAL, "chat", _PLAN, False, role="admin")
check("  端到端（零帧）：真红那一句现在判得出来，原因码是洞⑫",
      bool(_R) and _R[0] == "capability_absent_though_registered", str(_R)[:60])
check("  端到端（零帧）：同一句换成普通用户 ⇒ 放行（tag_delete 对他不可见 = 实话）",
      G._claim_issue(_REAL, "chat", _PLAN, False, role="user") is None)
check("  端到端（零帧）：转述别人那句整句否认 ⇒ 放行",
      G._claim_issue("留言里有人写「站内没有删除留言的通道」。",
                     "chat", _PLAN, False, role="admin") is None)
# 接线锁：零帧族要挂 `name_quotes=True`，有帧那一半（5f2）要用同一个 helper——
# 漏一处就是"补了一半"（本族的两副面孔必须同规则）。
check("  零帧族挂了 `name_quotes=True`",
      re.search(r'_ClaimFamily\("capability_absent_though_registered".{0,400}?'
                r'name_quotes=True', _DENIED_SRC, re.S) is not None)
_A5F2 = _DENIED_SRC.index("# 5f2.")
check("  有帧那一半（5f2）用的是同一个 helper（不是通用剥引号）",
      "_capability_absent_clause(_quotes_dropped_but_named_kept(reply)" in
      _DENIED_SRC[_A5F2:_A5F2 + 4000])
check("  `_ClaimFamily` 真的加了这一格、且循环里真的按它换文本",
      "name_quotes" in _DENIED_SRC[: _DENIED_SRC.index("def _zero_frame_families(")]
      and "if fam.name_quotes else own" in _DENIED_SRC)

# ══════════════════════════════════════════════════════════════════
print("\n⑤ 接线锁：族在表里、排在洞④ 之前、role 真的传下来了、且挂号重规划")

_SRC = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")


def _fn_src(name: str) -> str:
    at = _SRC.index(f"def {name}(")
    body = _SRC[at:]
    return body[:body.index("\ndef ", 10)]


_BODY = _fn_src("_zero_frame_families")
_RUNNER = _fn_src("_claim_issue")
_FAM = '"capability_absent_though_registered"'
check("这一族在零帧族表里", _FAM in _BODY and "_capability_absent_claim" in _BODY)
check("  排在洞④ `site_absence_claim_without_tool` **之前**（那句话不再按结论无依据记）",
      _BODY.index(_FAM) < _BODY.index('"site_absence_claim_without_tool"'))
check("  在洞② 检索声称**之后**（自称「查过了」的仍按谎称检索记）",
      _BODY.index('"search_claim_without_tool"') < _BODY.index(_FAM))
check("  子句版也接了（trace 要能指出判的是哪句话）",
      "_capability_absent_clause" in _BODY)
check("  吃的是与洞④ 同一份收尾轮豁免（`refused` 那档说「我没有权限」是实话）",
      "_absence_exempt" in _BODY[_BODY.index(_FAM) - 900:_BODY.index(_FAM)])
check("  族表签名收了 `role`、且判据拿到了它",
      "role" in _BODY.split("\n", 1)[0]
      and "role" in _BODY[_BODY.index(_FAM):_BODY.index(_FAM) + 600])
check("  `_claim_issue` 把 role 传进族表", "_zero_frame_families(plan, skill, role)" in _RUNNER)
check("  且过表仍在 `if frames_exist: return None` **之后**（有帧轮不查）",
      _RUNNER.index("if frames_exist") < _RUNNER.index("_zero_frame_families("))
# gate 调用点：身份只许从 `_principal_of` 取一处（别再各算一份）
check("  gate 调用点真的传了本轮身份（`role=_principal_of(config).known_role`）",
      "role=_principal_of(config).known_role" in _SRC)
# 打回口径：这一族必须挂号，否则会落到洞④ 那份**检索味**的建议上
check("  在 `_REPLAN_ISSUES` 里（打回提示按族分，见下一条）",
      "capability_absent_though_registered" in G._REPLAN_ISSUES)
check("  且有自己那份打回建议（`_REPLAN_ADVICE`）与否定说明（`_REPLAN_WHY`）",
      "capability_absent_though_registered" in G._REPLAN_ADVICE
      and "capability_absent_though_registered" in G._REPLAN_WHY)
_ADV = " ".join(G._REPLAN_ADVICE["capability_absent_though_registered"])
check("  建议里给的是**动手/去查**两条路（不是洞④ 那句「再去检索一遍」）",
      "该动手就去动手" in _ADV and "检索类技能" in _ADV)
# ── 有帧轮那一半（5f2）：**本族唯一一次生产命中就是有帧轮**，只挂零帧等于漏掉它
#    （`frames` 是 turn-scoped，第 4 节整族在 `if not frames:` 下面进不去）
_A4 = _SRC.index("# 5f2.")
_A4B = _SRC[_A4:_A4 + 4000]
check("有帧那一半挂在 `gate_node` 里（5f2）", "_capability_absent_clause" in _A4B)
check("  用的是**同一个原因码**（两副面孔一份打回口径与兜底文案）", _FAM in _A4B)
check("  且**不在** `if not (executed_names & _CONTENT_TOOLS)` 里面"
      "（事故那一轮跑的正是内容类工具，放进去就是哑判据）",
      _SRC.index("if not (executed_names & _CONTENT_TOOLS):") < _A4)
check("  有帧那一半吃同一份收尾轮豁免（与零帧同源同值）",
      "PLAN_STATUS_ABSENCE_EXEMPT" in _A4B and "_LEDGER_NOTE_PREFIX" in _A4B)
# 引号处理：这一半与零帧**同一条规则**（`_quotes_dropped_but_named_kept`，接线锁在 ④c），
# 转述那句的**行为**锁在下一节（⑥ 的 `_O4`：引号里只有别人的否定 ⇒ 放行）。
# （这里原来挂的是子串 `_strip_quoted_spans(reply)`——它一直绿，可绿的是它**下面**十几行
#  5f3 那处 `_false_negative_claim`；5f2 换成新 helper 之后，那条断言锁的东西就不在场了。）

# 行为锁（比子串锁抗重构）：走完整判据链，原因码是新的那个，不是洞④
_R = G._claim_issue("系统这边没有删除被驳回留言的通道，这一步只能你自己进后台处理。",
                    "chat", _PLAN, False, role="admin")
check("  端到端：原因码是 `capability_absent_though_registered`（不是洞④）",
      bool(_R) and _R[0] == "capability_absent_though_registered", str(_R)[:80])
check("  端到端：被否掉的那一句就是那句原话（trace 里指得出）",
      bool(_R) and "没有删除被驳回留言的通道" in _R[2], _R[2] if _R else "")

# ══════════════════════════════════════════════════════════════════
print("\n⑥ 端到端（有帧轮那一半）：照着诞生现场那一轮的形状跑一次 `gate_node`")

# 现场形状（trace 20260928T032411 **逐字照抄**）：planner 先跑了 `get_moderation_status`
# （内容类工具，frames=1），最后一轮落 chat 零 TOOLS，narrator 顺手把审核结果读成
# "站内没有删除被驳回留言的通道"。本族必须**在有帧轮也判**——否则零帧那一半在全量
# 真实语料上永远响不了（见 `tests/test_effect_truthfulness.py` 头注那种"零帧 0 命中"）。
from langchain_core.messages import ToolMessage  # noqa: E402

from agent.principal import Principal  # noqa: E402

_CFG = {"configurable": {"principal": Principal(uid=1, role="admin")}}
_REPLY = ("主人，这个我得跟你如实说清楚喵 :委屈:\n\n"
          "系统这边**没有删除被驳回留言的通道**。我能删的是展示在留言板上的那些……")
_ST = {
    "plan": G.plan_encode(G.instantiate_plan("chat", {})),
    "messages": [
        G.HumanMessage(content="[System: user_id=1, page=/dashboard/users; "
                               "current_effects=none; current_darkmode=on]"),
        G.HumanMessage(content="被驳回的呢，你不能删除吗"),
        ToolMessage(content="河灯留言审核状况：待审 0、已通过 21、已驳回 4",
                    name="get_moderation_status", tool_call_id="t1"),
        G.AIMessage(content=_REPLY),
    ],
    "done": False, "plan_rounds": 2, "gate_replan": True,
    "receipts": [{"skill": "content_query", "tool": "get_moderation_status",
                  "args": {"status": "ai_rejected"}, "result": "…"}],
}
_O = G.gate_node(_ST, _CFG)
check("有帧轮命中 → 打回/兜底（不是放行）",
      _O.get("done") is True and _O.get("fallback_text") == G._FALLBACK_CAPABILITY_ABSENT,
      str(_O)[:70])
# 首次打回要交回 planner（不是直接道歉）——本族在 `_REPLAN_ISSUES` 里的用处就在这
_ST2 = dict(_ST, gate_replan=False)
_O2 = G.gate_node(_ST2, _CFG)
check("  首次打回是**交回 planner 重规划**（不是直接道歉收尾）",
      _O2.get("gate_replan") is True and _O2.get("done") is False, str(_O2.get("done")))
_NOTE = "".join(str(getattr(m, "content", "")) for m in (_O2.get("messages") or []))
check("  打回提示里那句否定是**这件能力站内有**（不是「这一轮没执行工具」）",
      "这件能力站内是有的" in _NOTE and "一个工具都没有执行" not in _NOTE,
      _NOTE[:80])
check("  打回提示给的两条路是办/查（不是洞④ 那份检索味建议）",
      "该动手就去动手" in _NOTE)
# 反向：同一句话、同一个有帧轮，**身份是普通用户**时不许判（他真没这条通道）
_O3 = G.gate_node(dict(_ST, gate_replan=False),
                  {"configurable": {"principal": Principal(uid=722, role="user")}})
check("  对照：同一个有帧轮换成普通用户 ⇒ 放行（那句对他是实话）",
      _O3.get("done") is True and not _O3.get("fallback_text"), str(_O3)[:60])
# 反向之二：**转述**。同一个有帧轮、同一个管理员身份，否定词只长在引号里
# （那是留言里别人写的话，不是它的声称）⇒ 放行。这条锁的是"引号规则不是一刀切放行"
# 在**有帧那一半**也成立——零帧那一半的同一情形锁在 ④c 的端到端里。
_O4 = G.gate_node(dict(_ST, gate_replan=False, messages=list(_ST["messages"][:-1]) + [
    G.AIMessage(content="留言里有人写「站内没有删除被驳回留言的通道」，"
                        "我这边可以直接帮你删掉。")]),
    _CFG)
check("  对照：转述别人的整句否认（否定词在引号里）⇒ 放行，不当成它的声称",
      _O4.get("done") is True and not _O4.get("fallback_text"), str(_O4)[:60])

# ══════════════════════════════════════════════════════════════════
print("\n⑦ 兜底文案只否认被点名的那件事（不许说成「已经办了」）")

_FB = G._FALLBACK_CAPABILITY_ABSENT
check("文案在（非空）", bool(_FB))
check("  落点是「这件事我有办法办」", "有办法办" in _FB)
check("  不许出现完成式（这一轮它什么都没做）",
      not re.search(r"已经(办|做|删|改|完成)|办好了|搞定", _FB))
check("  保留人设与贴纸约定", "喵" in _FB and ":犯错:" in _FB)

print(f"\n{'全部通过' if not FAILS else f'失败 {len(FAILS)} 项'}")
for f in FAILS:
    print("  - " + f)
sys.exit(1 if FAILS else 0)
