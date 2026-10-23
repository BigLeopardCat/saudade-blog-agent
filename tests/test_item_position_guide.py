"""Verify source-backed item links, parameter binding and actual navigation routes."""


import re
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根（测试统一在 tests/）


sys.path.insert(0, str(ROOT))


import agent.entity_digest as E  # noqa: E402
import agent.prompt_context as C  # noqa: E402
from agent import authz  # noqa: E402
from agent.workflow_defs import WORKFLOWS, visible_workflows  # noqa: E402
from agent.workflow_defs.nav_map import NAV_MAP  # noqa: E402
from agent.workflow_plan import instantiate_plan  # noqa: E402
from agent.workflow_tools import _EXPLICIT_TOOLS  # noqa: E402
from tools.base import (  # noqa: E402
    _FRAME_ID_KEYS,
    _NAV_EXACT_PATHS,
    _NAV_PREFIX_PATHS,
    get_all_tools,
    nav_path_valid,
    nav_pure_path,
    navigate_to,
)

FAILS: list[str] = []
check = collector(FAILS)


_FORM_CMT = "/article/<文章 id>?cid=<评论 id>"


_FORM_LID = "/guestbook?lid=<留言 id>"


_CID = "/article/54?cid=9"        # 端到端那一半用的具体实例


_LID = "/guestbook?lid=109"


def _nav_inputs_desc() -> str:
    nav = [s for s in WORKFLOWS if s.name == "navigate"][0]
    return str(nav.inputs["target"])


def _tool_names() -> set[str]:
    return {getattr(t, "name", "") for t in get_all_tools()}


def _test_fact_is_in_the_shared_page_context():
    print("\n[供给] 事实进的是 planner 与 narrator 共用的那份页面上下文")
    fact = C._item_link_fact()
    check("事实句非空且够长（不是一句空壳）", len(fact) >= 40, f"{len(fact)} 字")
    for role in (None, "user", "admin"):
        check(f"[{role}] 事实在 site_guide(role) 里", fact in C.site_guide(role))
        check(f"[{role}] 收束句仍是最后一句（清单的收束句不许被挤走）",
              C.site_guide(role).endswith(C._SITE_GUIDE_CLOSING))

    # 无条件追加：出问题的那几轮里有一轮就站在首页（/），不是文章页、也不是留言板
    for url in ("/", "/article/54", "/guestbook", "/dashboard"):
        check(f"★ current_url={url} 这一页也拿得到这句",
              fact in C._attach_page_guide(f"current_url={url}；page_title=x", "admin"))

    # 事实锚点：删掉哪个词都会让这句失去意义（防止有人把它改写成一句空话）
    for kw in ("评论", "文章", "通知", "?cid=", "?lid=", "talkId", "阅读进度"):
        check(f"事实里逐字带着「{kw}」", kw in fact)
    check("★ 事实里写明「评论没有独立页面」这一层（否则「带我去评论」仍可被理解成某个板块）",
          "没有独立的评论页" in fact)
    check("★ 事实里点出文章页 `#…` 的**真实**含义（阅读进度锚点）——这是防再次编锚点的那半句",
          "阅读进度" in fact and "#" in fact)
    check("★ 留言那半句点出「待审/未通过的灯也定得到」（否则模型只会去公开池找，找不到）",
          "待审" in fact and "未通过" in fact)
    check("两个形态都写明是「照抄站内通知里那条链接」（取值从哪来）",
          fact.count("通知") >= 1 and "照抄" in fact)


def _test_fact_is_a_standalone_sentence_not_a_capability():
    print("\n[供给] 它是**站内事实**、单独成句——不混进能力枚举")
    fact = C._item_link_fact()
    caps = [s.capability or "" for s in visible_workflows("admin")]
    check("不在任何技能的 capability 文案里（否则「能不能做」的边界跟着漂）",
          all(fact not in c for c in caps))
    check("不在收束句里", fact not in C._SITE_GUIDE_CLOSING)
    check("不在板块清单/管理引导语里",
          fact not in C._SITE_GUIDE_HEAD and fact not in C._ADMIN_GUIDE_HEAD)
    check("与留言板那条事实各自独立（两条不许被并成一条）",
          fact != C._SITE_GUIDE_BOARD_FACT
          and C._SITE_GUIDE_BOARD_FACT not in fact and fact not in C._SITE_GUIDE_BOARD_FACT)


def _test_fact_does_not_teach_the_wrong_shape():
    print("\n[供给] 事实里**不许**出现错形状的例句（可抄句纪律）")
    fact = C._item_link_fact()
    check("★ 没有 `#comment` 这个具体错形状（写了就成了可抄的例句）",
          "#comment" not in fact)
    check("没有把「评论」写成一个可跳转的板块名（例如 `/comments`）",
          "/comments" not in fact and "/comment " not in fact)
    # `#` 只允许出现在"它是阅读进度锚点"那一句里——别的 `#` 都是可抄的锚点形状
    check("`#` 只在那句解释里露面一次", fact.count("#") == 1, f"{fact.count('#')} 处")


def _test_param_doc_names_the_literal_path_grid():
    print("\n[参数通道] navigate.target 的说明书里写着字面路径与两个位置形态")
    desc = _nav_inputs_desc()
    check("说明书里点出「可以直接填字面路径」这一格（此前只写别名，与实现不一致）",
          "字面路径" in desc)
    check(f"说明书里带着评论位置形态 {_FORM_CMT!r}", "?cid=" in desc and "/article/" in desc)
    check(f"说明书里带着留言位置形态 {_FORM_LID!r}", "?lid=" in desc and "/guestbook" in desc)
    check("说明书里点出「照抄站内通知里那条链接」（取值从哪来）",
          "查看" in desc or "通知" in desc)
    # 说明书那半句必须是**正向**描述（不许只写"别用 #"——那会把错形状留在上下文里）
    check("说明书里没有出现 `#` 锚点的错形状", "#" not in desc)


def _test_impl_actually_accepts_both_forms():
    print("\n[参数通道] instantiate_plan 与 navigate_to 真的放行**两个**形态")
    for target in (_CID, _LID, "/article/54"):
        plan = instantiate_plan("navigate", {"target": target})
        tools = plan.get("tools") or []
        check(f"{target} → 产出一条 navigate_to 调用",
              len(tools) == 1 and tools[0].startswith("navigate_to("), str(tools))
        check(f"{target} → 调用里逐字带着这个路径", target in (tools[0] if tools else ""))
        check(f"{target} → 没被降级成「目标页不存在」", "不存在" not in (plan.get("note") or ""))

    # 工具层那一侧：**端到端**必须接受，回的是同一条地址（不是「路径无效」）
    for lit in (_CID, _LID):
        out = str(navigate_to.invoke({"path": lit, "confirm": False}))
        check(f"★ navigate_to 端到端接受 {lit}、回的是同一条地址",
              lit in out and "无效" not in out, out[:90])

    # 白名单的**单一事实来源**：校验只看路径，定位参数随页面一起放行
    check("★ `nav_path_valid` 是判据本体（校验前先丢掉 `?…`/`#…`）",
          nav_pure_path(_LID) == "/guestbook" and nav_path_valid(_LID))
    check("★ 带 id 的路径仍走「前缀 + 至少一个 id 段」那一支（别把带 id 的路径塞进精确表）",
          nav_pure_path(_CID) == "/article/54"
          and _CID.split("?")[0] not in _NAV_EXACT_PATHS
          and _CID.split("?")[0].startswith(_NAV_PREFIX_PATHS))
    check("白名单外的路径**仍被拒**（这次修的是「带上定位参数」，不是「放宽白名单」）",
          not nav_path_valid("/nope") and not nav_path_valid("/guestbook/../admin"))


def _test_lid_value_has_a_read_channel():
    print("\n[取值通道] 「我自己那几条（含待审/未通过）」有读通道，值就是 `talkId`")
    check("★ list_my_board 在工具注册表里（不在表里 = planner 拿不到）",
          "list_my_board" in _tool_names())
    check("★ 它的 scope 是 read.own（三档角色都能读自己的）",
          authz.TOOL_SCOPE.get("list_my_board") == "read.own")
    check("★ 进了 planner 的点名白名单（content_query 的 PARAMS.tools）",
          "list_my_board" in _EXPLICIT_TOOLS)
    check("★ 帧里的 id 是 `talkId`（`?lid=` 要填的正是它）",
          _FRAME_ID_KEYS.get("talkKey") == "talkId")
    check("★ 跨轮执行记忆有它的摘要器（否则下一轮「带我去那条」得重查）",
          "list_my_board" in E._DIGESTERS)

    # 公开池那半个影子：`mine` 在公开池里结构上恒假 ⇒ 必须被摘掉，不能留着让人误读
    from tools.base import _drop_dead_mine
    rows = [{"content": "x", "mine": False, "approved": 1}]
    check("★ 公开池帧里的 `mine` 被摘掉（留着就是「这条不是你的」这种系统级的假话）",
          "mine" not in _drop_dead_mine(rows)[0])

    # 摘要器：状态与 talkId 都要在（这两样是这条通道独有的）
    digest = E._my_board_digest([
        {"talkId": 109, "content": "今天的月亮很好看", "cat": "诉", "approved": 1},
        {"talkId": 108, "content": "还没过审的那条", "cat": "忆", "approved": 0},
        {"talkId": 107, "content": "被驳回的那条", "cat": "愿", "approved": 2},
    ])
    check("摘要带着 talkId（下一轮零工具取值）", "109" in digest and "talkId" in digest, digest)
    for word in ("通过", "待审", "未通过"):
        check(f"摘要里逐字带着「{word}」（三种审核态各一个词，不合并）", word in digest)
    check("三态不塌成一个词（「未通过」不许被读成「待审」）",
          digest.count("通过") >= 2 and "待审" in digest and "未通过" in digest)

    # 未知状态不许兜成"待审"（读不到 ≠ 一条审核结论）
    unknown = E._my_board_digest([{"talkId": 1, "content": "x", "approved": 7}])
    check("★ 读不出的状态写「状态未知」，不许兜成「待审」",
          "状态未知" in unknown and "待审" not in unknown, unknown)


def _test_comment_has_no_nav_alias():
    print("\n[反向锁] 「评论」不许进 NAV_MAP（有别名 = 把'带去留言板'写进系统数据）")
    for alias in ("评论", "评论区", "评论页", "我的评论"):
        check(f"NAV_MAP 里没有「{alias}」", alias not in NAV_MAP)
    # 留言板那几条**照旧**在（本锁只针对"评论"，不许顺手把留言板删了）
    check("留言板/河灯集的别名照旧在（本锁不许误伤）",
          NAV_MAP.get("留言板") == "/guestbook" and NAV_MAP.get("河灯") == "/guestbook")


def _test_forms_table_is_the_single_source():
    print("\n[单一事实来源] 形态表（tools.base._ITEM_POSITION_FORMS）逐行自洽")
    from tools.base import _ITEM_POSITION_FORMS as FORMS
    fact = C._item_link_fact()
    check("★ 表在（事实句从这里渲染；手写回常量 = 条数又会各漂各的）",
          len(FORMS) >= 3 and all(len(r) == 3 for r in FORMS), f"{len(FORMS)} 行")
    seen: set[str] = set()
    for what, form, src in FORMS:
        example = re.sub(r"<[^>]+>", "1", form)
        check(f"[{what}] 形态代入真实编号后可导航",
              nav_path_valid(example), example)
        check(f"[{what}] 未绑定参数的模板不能当成真实导航目标",
              not nav_path_valid(form), form)
        check(f"[{what}] 形态带查询参数（定位形态必须有 `?`）", "?" in form, form)
        check(f"[{what}] 形态串逐字进了事实句（渲染，不是另写一份）", form in fact)
        check(f"[{what}] 取值来源那句也进了事实句", src in fact)
        check(f"[{what}] 形态名不重复", what not in seen)
        seen.add(what)
    # 反向：事实句里不许再有**手写的条数**——「站内只有两个形态」正是一句可抄的错句
    # （站内实际三类）。条数只能由表的行数决定。
    check("★ 事实句里没有「只有两个」这类手写条数（那是可抄的错句的入口）",
          "只有两个" not in fact and "两个形态" not in fact)
    # 每一类都得在白名单里有一个**页面**落脚（定位参数挂在真页面上）
    for what, form, _ in FORMS:
        check(f"[{what}] 形态的页面在导航白名单里（不是凭空造的页面）",
              nav_path_valid(nav_pure_path(re.sub(r"<[^>]+>", "1", form))))


def main() -> int:
    for fn in (_test_fact_is_in_the_shared_page_context,
               _test_fact_is_a_standalone_sentence_not_a_capability,
               _test_fact_does_not_teach_the_wrong_shape,
               _test_param_doc_names_the_literal_path_grid,
               _test_impl_actually_accepts_both_forms,
               _test_lid_value_has_a_read_channel,
               _test_comment_has_no_nav_alias,
               _test_forms_table_is_the_single_source):
        fn()
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 条不通过：")
        for f in FAILS:
            print("   · " + f)
        return 1
    print("✅ 全部通过")
    return 0


def test_item_position_guide():
    main()

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_item_position_guide()
