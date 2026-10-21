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
③ 接线（纪律进的是**臂的 planner 侧**；贴纸表住**叙述层**，20261021 起）④ 多模态透传的边界。

**这一节不测智能**——"接上之后 `own_*` 是不是真绿了"归 golden A/B（多遍读计数）。
"""


import hashlib
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


from langchain_core.messages import HumanMessage  # noqa: E402

import agent.graph as G  # noqa: E402
import agent.react_arm as R  # noqa: E402
from agent.narrator_discipline import NARRATOR_DISCIPLINE_LOOP  # noqa: E402
from agent.principal import UNKNOWN  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def test_react_narrator_assets():
    _EXECUTOR_SHA256 = "b0083e7542938ffe3e7b327b40dba7effdb0c85680d691e118ea29961acad3ae"
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
    print("\n③ 接线：能力在常量里 ≠ 接上了（`test_source_attribution` ②的同一条道理）")
    R._pending_ledger_frame = lambda *a, **k: ("", {})  # 离线：不读台账
    _sp = R._system_prompt([HumanMessage(content="我有哪些未读通知？")], "admin", UNKNOWN, {}, "")
    check("**叙述纪律（按条裁过的那一份）进了臂的 planner 侧提示词**"
          "（不是只在 graph 里躺着）", NARRATOR_DISCIPLINE_LOOP in _sp)
    # ⚠️ 20261021：贴纸表与那两块**从 planner 侧退场**（主人令：人设是叙述层的事）。
    # 它们住 `_narrator_prompt` 那一份（走生产 `_EXECUTOR_PROMPT` 的 `sticker_guide` 格）
    # ——判据要**同时**钉"planner 侧没有"与"叙述层有"，否则"搬走了"与"删掉了"同形。
    _np = R._narrator_prompt({"messages": [], "plan_obj": {}}, [], set(), {"role": "admin"})
    check("**贴纸名字表住在叙述层**（不接 ⇒ `sticker_*` 结构上产不出）",
          R.STICKER_GUIDE in _np and "害羞" in _np and "每轮最多一个" in _np)
    check("  反控：planner 侧**不再**背它（这一批搬的就是这一格）",
          R.STICKER_GUIDE not in _sp)
    check("`R.STICKER_GUIDE is graph 用的那一份`（同一个对象，不是抄的第二份）",
          R.STICKER_GUIDE is G.STICKER_GUIDE)
    check("臂接的是**按条裁过的**那一份（`NARRATOR_DISCIPLINE_LOOP`，20261021）",
          NARRATOR_DISCIPLINE_LOOP in _sp)
    check("**生产那一份整份不再进臂的提示词**（「接整份 + 作废」那条路已退场）",
          G.NARRATOR_DISCIPLINE not in _sp)
    # ⚠️ 只在**纪律那一块**里找条号：整份提示词里还有别的编号表（planner 的判定规则
    # 1–6、收尾说明 1–5），不圈定范围这条判的是它们、而不是纪律。
    _disc = _sp[_sp.index(NARRATOR_DISCIPLINE_LOOP):
                _sp.index(R._CLOSING_RULES)]
    check("前提为假的那些条**根本不在提示词里**（不是「进来了再声明作废」）",
          all(f"\n{n}. " not in _disc for n in (1, 9, 10, 12, 13, 23, 24)),
          str([n for n in (1, 9, 10, 12, 13, 23, 24) if f"\n{n}. " in _disc]))
    check("  **留下的条一个都不少**（裁的是那几条，不是把整块切短）",
          all(f"\n{n}. " in _disc for n in (2, 4, 5, 7, 8, 11, 14, 15, 16, 17, 19, 20, 21, 22)),
          str([n for n in (2, 4, 5, 7, 8, 11, 14, 15, 16, 17, 19, 20, 21, 22)
               if f"\n{n}. " not in _disc]))
    check("  立场那一句仍在（本循环既是决策者也是回复者，不讲清就是自相矛盾的提示词）",
          "既是决策者也是回复者" in _sp and "一条都不放宽" in _sp)
    check("  且**只裁前提为假的那半句**：3 与 18 的诚实那半照旧在"
      "（空结果 ≠ 没执行 / 只按回执说结果）",
          "空结果就如实说" in _sp and "只按回执说结果" in _sp)
    check("臂的提示词里 `.format` 的槽都填好了（拼的是渲染结果，不是模板）",
          "{persona}" not in _sp and "{sticker_guide}" not in _sp)
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

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_react_narrator_assets()
