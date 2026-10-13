# -*- coding: utf-8 -*-
"""第二条臂接 narrator 的三件资产：叙述纪律、贴纸名字表、多模态消息（20261005）。

**被锁的缺陷**（三条都实测过，不是推测）：

  ① `own_*` 族整族慢性红（react 归档 3/3 次全量跑）。现场回复是**诚实的**——
     "系统这边没有读到你的登录身份""一笔收藏都没记到你名下"——但后面**又多派了一句**
     "你可以先到登录页（/login）登一下账号"。`own_*` 的负断言 `(?:先|去|到|要|需|得|请)
     …(?:登录|登陆)|/login` 判的就是这句：能跟 agent 说上话的人**一定是登录着的**
     （chat 链路 uid>0 恒成立），"没携带身份"只可能是系统这一侧出了异常，叫主人去登录
     既没用又误导——把系统的账算在他头上。生产 narrator 有纪律 20 明令禁止，而本臂
     **一条纪律都没接**。
  ② `sticker_praise_shy` 慢性红：语料只认 12 个贴纸名（`:害羞:` …），而名字表的唯一
     来源是 `agent/prompts.STICKER_GUIDE`——不接进系统提示，那些记号**结构上产不出来**。
  ③ `image_color_red` / `image_two_colors` 慢性红：主人消息是 `[{"type":"text"},{"type":
     "image_url"}]`，而适配器此前写死 `HumanMessage(content=user_msg)`——**按文本重建
     等于把图丢了**，模型看不见图，答不出颜色，还顺手去调 `get_blog_info`（那条用例
     的 `no_tool_calls` 跟着红）。

判据分四段：① 拆分逐字节（生产提示词一个字节都没变）② 纪律块本体完好
③ 接线（贴纸名字表 / 人设 / 循环机制真的进了臂的 system prompt，且 **20261013 起
规划契约整块不在了**，见 ③b）④ 多模态透传的边界。

**这一节不测智能**——"接上之后 `own_*` 是不是真绿了"归 golden A/B（多遍读计数）。
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
import agent.react_arm as R  # noqa: E402
from agent.principal import UNKNOWN  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 拼之前算出来的那份（`20261005`，拆之前 `_EXECUTOR_PROMPT` 的 sha256）。
# **它是闸门不是文物**：有人日后改纪律（改一个字也算），这里立刻红——那条红的意思是
# "生产 narrator 和第二条臂共用同一份文本，你改了它，请把两边的读数一起重新评一遍"。
# 直接改这个数字等于宣布"我评过了"；不评就改，改的就是一个没人看过的新提示词。
#
# **20261005 改过一次（换的是下面那个数字，理由记在这里）**：主人拍板"写族也退出代印"
# （`agent/factblock.py` 的 `BLOCK_FAMILIES` 空集），`_EXECUTOR_PROMPT` 里那一格的**标题与
# 括注**随之改口——原样写着「[本轮已由系统印出的事实]（**已经印在气泡最前面**…）」，
# 而那一刻起系统一行都不印，标题本身成了假话。纪律 23 条**一个字没动**（上面 ② 那一节
# 逐句锁着它们）。读数影响：两臂共用这份文本 ⇒ **改动前跑的任何 golden 读数都不代表改后的
# 行为**，两臂都要重跑（这条闸门存在的全部意义）。
#
# **20261009 改过一次（纪律 20 的后半段）**：`own_mark_read_not_logged_in` 两跑都红，
# 抓下来是同一件事的两种说法——一条把话头指向主人（"确认你是登录"），一条把系统这侧
# 写成条件句（"需要系统这边能识别到…才行"）。上一版在这里**举了反例句**（"你先去登录"
# "需要先登录博客账号"），而实测模型是**照着反例句换词复用**的 ⇒ 这一版改成只说
# **不许说什么形状**：这一格「登录」「登陆」二字一次都不许出口、系统这侧的事不许写成
# 条件句/前置条件；反例句整句删掉。断言（下面 ②）随之换字面，**实质一个字没放宽**
# （读不到 ≠ 空、不许把系统的账算在主人头上——两条都还在）。
_EXECUTOR_SHA256 = "e58c361f613cc847d6501c4fdbdc1eb6af18adc28f9a1a6ecba57d796d274001"

# ── ① 拆分逐字节 ─────────────────────────────────────────────────────
print("① 拆分逐字节：`_EXECUTOR_PROMPT` 仍是原来那一串（生产行为零变化）")
_t = G._EXECUTOR_PROMPT
_sha = hashlib.sha256(_t.encode()).hexdigest()
check("**模板 sha256 与拆分前一致**（红 = 有人动了共享纪律，两边的读数都得重评）",
      _sha == _EXECUTOR_SHA256, _sha)
check("纪律段**逐字**嵌在成品里（拆分没丢字、没重复）",
      G.NARRATOR_DISCIPLINE in _t, f"纪律 {len(G.NARRATOR_DISCIPLINE)} 字")
check("纪律段是成品里**唯一**的一份（不是又抄了一遍）",
      _t.count(G.NARRATOR_DISCIPLINE) == 1)
check("纪律段前面是 persona+audience 那两格（拼装顺序没变）",
      _t.startswith(G._EXECUTOR_HEAD + G.NARRATOR_DISCIPLINE))
check("`.format` 的八个槽一个不少（既有的四条测试就是这么调的）",
      all("{%s}" % k in _t for k in ("persona", "audience", "plan", "tool_frames",
                                     "exec_receipts", "fact_block", "page_ctx",
                                     "sticker_guide")))
check("**纪律段自己没有花括号**（它会被原样拼进臂的提示词，留个 `{` 就是对模型可见的垃圾）",
      "{" not in G.NARRATOR_DISCIPLINE and "}" not in G.NARRATOR_DISCIPLINE)

# ── ② 纪律本体：那两条实质句还在 ─────────────────────────────────────
print("\n② 纪律本体：`own_*` 那两条实质句在纪律段里（拆出去的那一份没被削弱）")
_d = G.NARRATOR_DISCIPLINE
check("纪律 20 在（读不到 ≠ 空 / 不许派主人去登录那一条）", "20." in _d)
check("  且明写这一格**「登录」「登陆」两个字一次都不许出口**（20261009 收口）",
      "「登录」「登陆」这两个字一次都不许出口" in _d)
check("  且明写系统这一侧的事**不许写成条件句/前置条件**（「才行／才能／要先确认」那族）",
      "不许写成条件句或前置条件" in _d and "才行" in _d)
check("  且明写这是**系统这一侧**的事实（把系统的账算在他头上是反面）",
      "没携带身份" in _d and "系统这一" in _d)
check("「读到了确实是空」才准说没有（`own_*` 两条负断言的正面对手）",
      "读到了确实是空" in _d)
check("纪律里仍有不许编造的底线（拆段不是把纪律切成只留 20 条）",
      "不得编造" in _d or "不许编造" in _d)

# ── ③ 接线：留下的几件真的进了臂的 system prompt ───────────────────────
print("\n③ 接线：能力在常量里 ≠ 接上了（`test_source_attribution` ②的同一条道理）")
R._pending_ledger_frame = lambda *a, **k: ("", {})  # 离线：不读台账
_sp = R._system_prompt([HumanMessage(content="我有哪些未读通知？")], "admin", UNKNOWN, {}, "")
check("**贴纸名字表进了臂的系统提示**（不接 ⇒ `sticker_*` 结构上产不出）",
      R.STICKER_GUIDE in _sp and "害羞" in _sp and "每轮最多一个" in _sp)
check("`R.STICKER_GUIDE is graph 用的那一份`（同一个对象，不是抄的第二份）",
      R.STICKER_GUIDE is G.STICKER_GUIDE)
check("人设与受众也在（`BLOG_ASSISTANT_PROMPT` / `audience_block(role)`）",
      R.BLOG_ASSISTANT_PROMPT in _sp and R.audience_block("admin") in _sp)
check("**循环机制进了系统提示**（怎么走、怎么结束——本循环里没有 planner 的规则行兜着）",
      R._LOOP_MECHANICS in _sp)
check("  且它写明收尾那条消息是**收束信号**、正文由系统组织"
      "（P6 起正文归独立 narrator 重写；不写明就是提示词在骗模型）",
      "收束信号" in R._LOOP_MECHANICS)
check("  且它写明「别把活派回给主人」（`own_*` 那族的正面对手，20261005 那一族的继任者）",
      "派回给主人" in R._LOOP_MECHANICS)
check("  且它写明「读不到 ≠ 空」（那一族两条负断言的正面对手）",
      "读不到 ≠ 空" in R._LOOP_MECHANICS)
check("**固定那几块排在事实格之前**（前缀缓存：逐轮变的那一段必须在尾部，"
      "否则 `cache_read` 会从三万多掉到几千）",
      _sp.index(R.STICKER_GUIDE) < _sp.index(R._HEAD_PAGE_CTX))
check("臂的提示词里 `.format` 的槽都填好了（拼的是渲染结果，不是模板）",
      "{persona}" not in _sp and "{sticker_guide}" not in _sp)

# ── ③b 规划契约整块摘掉（20261013 批次 B 的判据本身）──────────────────
# 上一版第一块逐字拼的是 `graph._render_planner_prompt(...)`：技能表 + 判定规则 1..7 +
# 输出契约，实测占整份提示词的 70%。那一族是给「一次吐完整计划」的**解析器**写的
# （`SKILL=`/`PARAMS=` 契约行、`TODO:` 行），本循环里模型直接调函数 ⇒ 一句都不需要它读。
# 这一节判两个方向，**缺一个都不算数**：契约真的不在了 **且** 它确实还在别处活着
# （否则"删干净了"与"这份文本早就没了"长得一样）。
print("\n③b 规划契约不在决策循环里了，但它仍活在生产 narrator 资产里（两个方向都判）")
_contract = G._PLANNER_OUTPUT_CONTRACT_NATIVE
check("控：输出契约**确实带着 `SKILL=`/`PARAMS=` 这类指纹**"
      "（断言它们不在别处之前，先证这几个探针真能认出契约）",
      "SKILL=" in _contract and "PARAMS=" in _contract, f"{len(_contract)} 字")
check("**输出契约整块不在臂的提示词里**", _contract not in _sp)
check("  且两个契约指纹一个都没漏进来", "SKILL=" not in _sp and "PARAMS=" not in _sp)
check("  且技能表的文本渲染也不在了（技能描述已经在函数 schema 里，这是第二份渲染器）",
      "PARAMS.calls" not in _sp and "TODO:" not in _sp)
check("控：叙述纪律**仍在**生产 narrator 资产里（只是不再进决策循环——"
      "它本来就是给零工具节点写的）",
      G.NARRATOR_DISCIPLINE in G._EXECUTOR_PROMPT)
check("**叙述纪律不在臂的提示词里**（本循环的模型手上有工具，"
      "纪律 1「你没有任何可以直接调用的工具」对它恒假）",
      G.NARRATOR_DISCIPLINE not in _sp)
check("  立场改写那段也不再需要（它存在的唯一理由是给那份纪律换指称）",
      not hasattr(R, "_DISCIPLINE_STANCE") and not hasattr(R, "_CLOSING_RULES"))
check("**提示词总量上锁**（改前 admin 档 35,762 字符；契约长回去 = 有人把它抄回来了）",
      len(_sp) < 12_000, f"{len(_sp)} 字")

# 台账那一格必须留：`planner.ledger_frame` 的留痕是三条 `require_ledger_*` 判据的
# **唯一**证据，而台账本身得先真的摆进提示词（只记不留 = 证据与事实两回事）。
R._pending_ledger_frame = lambda *a, **k: ("[待办台账] 留言审核 3 条", {"ids": [1, 2, 3]})
_sp2 = R._system_prompt([HumanMessage(content="我有哪些未读通知？")], "admin", UNKNOWN, {}, "")
check("台账帧摆进了提示词（它带着 `id`，是「主人说的是哪一条」的判据来源）",
      _sp2.index(R._HEAD_LEDGER) < _sp2.index("[待办台账] 留言审核 3 条"))
R._pending_ledger_frame = lambda *a, **k: ("", {})

# ── ④ 多模态透传 ─────────────────────────────────────────────────────
print("\n④ 多模态：文本口径不变，非文本部件原样带上")
_IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_mm = HumanMessage(content=[{"type": "text", "text": "这张图是什么颜色"}, _IMG])
_m = R._inner_message([_mm], "这张图是什么颜色")
check("**图片部件带过去了**（丢掉它 = 模型看不见图，「这张图是什么颜色」无从答起）",
      isinstance(_m.content, list) and _IMG in _m.content, repr(_m.content)[:90])
check("文本仍是 `user_msg`（末 500 字那条口径不变）",
      _m.content[0] == {"type": "text", "text": "这张图是什么颜色"})
check("纯文本消息**不被改形状**（仍是 `content=str`，不是只有一个 text 部件的数组）",
      R._inner_message([HumanMessage(content="你好")], "你好").content == "你好")
check("没有 HumanMessage 时退回文本（身份通道缺省也不炸）",
      R._inner_message([], "你好").content == "你好")
check("只有文本部件的多模态消息也不改形状（避免给每条用例都换一种消息形状）",
      R._inner_message([HumanMessage(content=[{"type": "text", "text": "x"}])], "x"
                       ).content == "x")

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
