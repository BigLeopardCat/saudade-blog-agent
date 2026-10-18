# -*- coding: utf-8 -*-
"""管理助手写工具：站内公告（代发 / 改 / 删）—— 20261018 拆 `tools/base.py`。

**为什么单独成模块**：`tools/base.py` 是全仓第二大体量文件，而这一族是**连续的 233 行、
8 个模块级名字**（两个上限常量 + 四个帮手 + 三个 `@tool`），scope 全是 `write.console`，
目标全是**公告标题**。族内自洽、出边只有三个共享帮手，切得干净。

## 为什么是这三件，以及实现为什么长这样
与标签/分类同一套纪律（名字通道 + fail-closed + 读回复核），三处**公告独有**的
事实决定了实现的形状：
  ① 公告**没有唯一约束、没有"已发布/草稿"字段**（`src/entity/announcement.rs` 只有
     id/title/content/时间戳），所以目标只能按**标题**认，且重名一律拒绝——按名字
     挑一条出来改/删，选错就是改了别人的公告；
  ② 新建端点**不回 id**（返回字符串 "Created"）⇒ 复核只能靠"读回清单、按标题+
     正文认出新增的那条"，与 `create_category` 同族（那条也是按名字回头认）；
  ③ 改端点要求 title 与 content **都发**（Rust 侧 `UpsertAnnouncement` 两个字段
     都必填）⇒ 只改正文时必须把现有标题原样带上，不能发空标题把公告改成没名字。
删除是 `ON DELETE` 真删（公告没有外键），所以**读不回就等于没删掉**，不猜。

## 搬家时守的两条
⚠️ **四个帮手一律写成 `_base.<名>(...)`，不许按名 import**：`_announcement_index`
与 `_admin_request` 都被套件**直接打桩**（`tests/test_tag_admin.py` 的 `patch()`
就是 `setattr(tools.base, 名, 桩)`，实测 6 处 `_announcement_index` / 3 处
`_admin_request`）。函数对象取 globals 取的是**它自己所在模块**的 `__dict__`
⇒ 按名 import 会把打桩前的旧对象钉死在这里，桩静默失效、代码真去发网络请求，
而症状只是"断言换了个理由红"（本仓最贵的失效：看着在跑、其实没跑）。
`_base` 这个属性查找发生在**调用那一刻**，桩才生得效。
⚠️ **本模块一个名字都不按名从 `tools.base` 取**（连 `_near_miss_names` 也走属性查找）：
模块级 `from tools.base import …` 会让 mypy 在这个环上判不出被 re-export 的名字的
类型，`tools/base.py` 的 `_TOOL_REGISTRY` 会当场多出一批 `Cannot determine type`
（20261018 在 `tools/admin_read.py` 那一刀实测 7 条、`tools/base.py` 13→16）。
属性查找同时解决了"本模块在 base 的原位置就被 import"的时序问题。

## `__all__` 只列三个工具
两个上限常量与四个帮手是**实现细节**：常量外面只在注释里被提到，`_announce_change`
只在本模块内用。`_announcement_index` / `_find_named_announcement` 确实被外面按名取
（`agent/graph.py`），但它们是**下划线名**——按社区惯例不进 `__all__`，
由 `tools/base.py` 按原名显式 re-export 供给（显式 import 不受 `__all__` 约束）。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, fact, ok, tgt, unavailable

__all__ = ["create_announcement", "delete_announcement", "update_announcement"]

MAX_ANNOUNCE_TITLE = 80
MAX_ANNOUNCE_BODY = 2000


def _announcement_index(config: RunnableConfig) -> dict[int, dict] | None:
    """读公告清单 → `{id: 公告行}`；读不到返回 None（≠"没有公告"，同 _tag_index）。

    读的是**公开**端点 `/api/public/announcements`：公告本来人人都看得见，后台没
    第二份清单（`src/routes/announcements.rs` 只有这一个读接口）。仍然带发起人
    身份去打（`_admin_get` 统一签名），写工具的身份判据不受影响。
    """
    data = _base._admin_get("/api/public/announcements", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            aid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        row["id"] = aid                    # id 就地归一成 int：下游 `hit["id"]` 直接可用
        out[aid] = row
    return out


def _find_named_announcement(title, config, index=None):
    """按**标题**在公告清单里找一条 → `(行, None)` 或 `(None, 拒绝文本)`。

    与 `_find_named_tag` 同取向（也是同一个"名字通道"的公告版）：唯一命中才动手；
    命中多条（同名公告）**不替用户选一条**——把候选连同 id/时间列出来让他指认；
    一条完全同名的都没有就如实说没有，**并附上名字最接近的候选**（`_near_miss_names`，
    20260925：被截短的标题在这里会撞成一句"站内没有"的假话）；字典读不到则单独一种
    说法（"读不到" ≠ "没有"，把一次网络故障说成"站内没这条公告"是最坏的错法）。
    **模糊匹配只用来提问、不用来动手**：写操作仍然只认标题完全相等。
    `index` 是可选快照（一次操作要读同一份清单两回时用）。
    """
    if index is None:
        index = _base._announcement_index(config)
    if index is None:
        return None, "读不到现有的公告列表，无法把标题对应到公告，本次未改动"
    want = str(title or "").strip()
    if not want:
        return None, "公告标题为空，本次未改动"
    hits = [r for r in index.values() if str(r.get("title") or "").strip() == want]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        cands = "、".join(f"id={r.get('id')}（{str(r.get('createdAt') or '')[:16]}）" for r in hits)
        return None, (f"站内有 {len(hits)} 条标题都叫「{want}」的公告（{cands}）："
                      f"无法确定要动的是哪一条，本次未改动——请先说明是哪一条"
                      f"（或让我按别的说法列出全部公告）")
    near = _base._near_miss_names(want, [(r.get("id"), str(r.get("title") or ""))
                                   for r in index.values()])
    if near:
        # 近失（20260925 现场，见 _near_miss_names 长注）：主人点名的标题被节选
        # 截短 ⇒ 完全相等查不到。此时说"站内没有这条公告"是**假话**——站内有，
        # 只差三个字。把候选连同 id 摆出来，让"没有"变成一次可核对的追问。
        cands2 = "、".join(f"id={cid}「{nm}」" for cid, nm in near)
        return None, (f"站内没有标题**完全等于**「{want}」的公告；名字最接近的是 {cands2}。"
                      f"本次未改动——若就是其中一条，请照它的**完整标题**再说一遍"
                      f"（或直接给 id），我按那个标题动手")
    return None, f"站内没有标题是「{want}」的公告，本次未改动"


@tool
def create_announcement(
    title: Annotated[str, "公告标题（一句话，会显示在公告里）"],
    content: Annotated[str, "公告正文（主人明确给了原文就一字不改地照录；"
                            "只给了意思则由你按他的意思成文）"],
    config: RunnableConfig,
) -> str:
    """发一条站内公告（全站访客都会在首页看到）。
    **正文分两种情形**：主人**明确给出原文**（「正文写：…」）时**照录**——改一个字
    都算改了他的意思；他**只给了意思**（「发个公告祝大家国庆快乐，以你的口吻」）时
    由你**按他的意思组织措辞**成一句得体的公告。两种情形都**不许编一件他没让你说的事**。
    正文**全文**会印在确认卡上，他点了确定才真发出。
    需要管理员身份，且要经主人确认才会真正发出。"""
    from agent import adminops_board as _board
    t = str(title or "").strip()
    c = str(content or "").strip()
    if not t:
        return unavailable("公告标题为空，未发布")
    if not c:
        return unavailable("公告正文为空，未发布")
    if len(t) > MAX_ANNOUNCE_TITLE:
        return unavailable(f"公告标题过长（{len(t)} 字，上限 {MAX_ANNOUNCE_TITLE}），未发布")
    if len(c) > MAX_ANNOUNCE_BODY:
        return unavailable(f"公告正文过长（{len(c)} 字，上限 {MAX_ANNOUNCE_BODY}），未发布")

    before = _base._announcement_index(config)
    if before is None:
        return unavailable("读不到现有的公告列表，无法确认发布结果，本次未发布")
    data = _base._admin_request("POST", "/api/protected/announcements",
                          {"title": t, "content": c}, config)
    if isinstance(data, ToolResult):
        return data

    # 建后复核：端点不回 id，只能读回清单按"新旧 id 差集 + 标题对上"认人。
    # 认不出（读不回 / 差集为空 / 新行标题对不上）一律 unavailable——宁可让主人
    # 去后台看一眼，也不能把"可能没发出去"说成"已发布"。
    after = _base._announcement_index(config)
    if after is None:
        return unavailable("发布请求已发出，但读不回公告列表、无法确认是否真的发出去了"
                           "——请到后台公告页核对")
    fresh = [r for i, r in after.items() if i not in before
             and str(r.get("title") or "").strip() == t
             and str(r.get("content") or "").strip() == c]
    if len(fresh) != 1:
        return unavailable(f"发布请求已发出，但读回公告列表里没有找到标题为「{t}」的新公告"
                           f"（找到 {len(fresh)} 条），本次改动未确认生效——请到后台核对")
    row = fresh[0]
    return ok(_board.render_announcement_created(row),
              meta=fact("announcement_create", changed=True,
                        target=tgt("announcement", row.get("id"), t),
                        before="不存在", after="已发布", evidence=t,
                        announcement_id=row.get("id"), announcement_title=t))


@tool
def update_announcement(
    title: Annotated[str, "要改的那条公告的**标题**（用现在标题来指认它，不是新标题）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么标题；用户没说要改标题就不填"] = None,
    content: Annotated[str | None, "正文改成什么；用户没说要改正文就不填"] = None,
) -> str:
    """修改一条已经发出去的公告（改标题、改正文，或两者一起改）。
    **只改用户点名的那几项**，没点名的保持不变（改端点要求标题与正文都发，这里会
    把没点名的那个原样带上，不会把标题清空）。标题对不上就什么都不做。
    需要管理员身份，且要经主人确认才会真正改。"""
    from agent import adminops_board as _board
    hit, err = _find_named_announcement(title, config)
    if err:
        return unavailable(err)

    nt = str(new_title or "").strip()
    nc = str(content or "").strip()
    if not nt and not nc:
        return unavailable("没有指出要改什么（标题还是正文），本次未改动")
    cur_t = str(hit.get("title") or "").strip()
    cur_c = str(hit.get("content") or "")
    final_t = nt or cur_t                  # 未点名的字段原样带上（见函数头注 ③）
    final_c = nc or cur_c.strip()
    if len(final_t) > MAX_ANNOUNCE_TITLE:
        return unavailable(f"新标题过长（{len(final_t)} 字，上限 {MAX_ANNOUNCE_TITLE}），未改动")
    if len(final_c) > MAX_ANNOUNCE_BODY:
        return unavailable(f"正文过长（{len(final_c)} 字，上限 {MAX_ANNOUNCE_BODY}），未改动")
    if final_t == cur_t and final_c == cur_c.strip():
        # changed=False ⇒ `fact()` 自动补上 `noop: True`（零改动的机器可读证书）。
        # 20260930 之前这个键是**手写**的，而全仓只有 8 处 write.own 的短路回执记得写
        # ——本处与 board_audit 那处是同一件事却没带：少一个键，两个读端（gate 的洞⑩、
        # planner 的零改动裁剪）就会把"与现在一致"当成"真的改了"。现在它是从 `changed`
        # 派生的一处实现（详见 `fact()` 头注），漏写不再可能。
        return ok(_board.render_announcement_noop(hit),
                  meta=fact("announcement_update", changed=False,
                            target=tgt("announcement", hit.get("id"), final_t),
                            before="与现在一致", after="与现在一致",
                            change="与现在一致，无需改动",
                            announcement_id=hit.get("id"), announcement_title=final_t))

    aid = hit.get("id")
    data = _base._admin_request("PUT", f"/api/protected/announcements/{aid}",
                          {"title": final_t, "content": final_c}, config)
    if isinstance(data, ToolResult):
        return data

    after = _base._announcement_index(config)
    if after is None:
        return unavailable("修改请求已发出，但读不回公告列表、无法确认是否真的改上了"
                           "——请到后台公告页核对")
    got = after.get(int(aid))
    if got is None:
        return unavailable(f"修改请求已发出，但读回公告列表里找不到 id={aid} 这条公告，"
                           f"本次改动未确认生效")
    if (str(got.get("title") or "").strip() != final_t
            or str(got.get("content") or "").strip() != final_c):
        return unavailable(f"修改请求已发出，但读回 id={aid} 的公告与预期不一致，"
                           f"本次改动未确认生效")
    return ok(_board.render_announcement_updated(hit, got),
              meta=fact("announcement_update", changed=True,
                        target=tgt("announcement", aid, final_t),
                        before=cur_t, after=final_t, evidence=final_t,
                        announcement_id=aid, announcement_title=final_t,
                        change=_announce_change(cur_t, final_t, cur_c, final_c)))


def _announce_change(cur_t: str, final_t: str, cur_c: str, final_c: str) -> str:
    """变更摘要（进回执行的那句人话，跨轮执行记忆读它）。**不写正文内容**——
    公告正文可以很长，回执行只留列宽（300 字符），把正文塞进去会把摘要挤掉。

    改名的措辞是「改名（原「旧名」）」而不是「标题改为「新名」」：回执行的动作行
    已经用**新**名字做主语（`修改公告「新名」：…`），change 里再写一遍新名就成了
    「修改公告「维护改期」：标题改为「维护改期」」——同一句里同一个名字出现两次，
    跨轮读到也读不出改之前叫什么。
    """
    bits = []
    if final_t != cur_t:
        bits.append(f"改名（原「{cur_t}」）")
    if final_c != cur_c.strip():
        bits.append("正文已更新")
    return "、".join(bits)


@tool
def delete_announcement(
    title: Annotated[str, "要删掉的那条公告的**标题**（用现在标题指认它）"],
    config: RunnableConfig,
) -> str:
    """删除一条站内公告（删除后访客在首页看不到了，**删掉取不回来**）。
    标题对不上、或站内有好几条同名标题时什么都不做，并如实说明原因。
    需要管理员身份，且要经主人确认才会真正删。"""
    from agent import adminops_board as _board
    hit, err = _find_named_announcement(title, config)
    if err:
        return unavailable(err)

    aid = int(hit.get("id"))
    data = _base._admin_request("DELETE", "/api/protected/announcements", [aid], config)
    if isinstance(data, ToolResult):
        return data

    after = _base._announcement_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回公告列表、无法确认是否真的删掉了"
                           "——请到后台公告页核对")
    if aid in after:
        return unavailable(f"删除请求已发出，但读回公告列表里 id={aid}（{hit.get('title')}）还在，"
                           f"本次改动未确认生效")
    title_now = str(hit.get("title") or "").strip()
    return ok(_board.render_announcement_deleted(hit),
              meta=fact("announcement_delete", changed=True,
                        target=tgt("announcement", aid, title_now),
                        before="已存在", after="已删除", change="已删除",
                        announcement_id=aid, announcement_title=title_now))
