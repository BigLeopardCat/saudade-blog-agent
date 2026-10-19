# -*- coding: utf-8 -*-
"""管理助手写工具：**文章**（后台清单 / 改状态 / 打标签）—— 20261018 拆 `tools/base.py`。

**为什么单独成模块**：这一族是 `tools/base.py` 里**离散的两段、5 个模块级名字**——
前面一段是后台文章的两个读口径（`_read_note` 单篇现状、`_note_index` 整份清单），
后面一段是三件工具（`list_admin_notes` 读后台清单 / `set_article_status` 改状态 /
`set_article_tags` 打标签）。scope 是 `admin.console` + `write.console`，目标通道是
**文章 id**（不是名字——与标签/分类/公告那几族的根本差别）。

## 这一族独有的事实（实现为什么长这样）
  ① **读的行与写的行必须是同一行**：`_read_note` 刻意不走
     `/api/protected/draft/editor/:id`——那一条对「编辑修改稿」行会**解引用成原文章**，
     而 `update_note` 只发 `{status,isTop}` 时写的是**被点的那一行本身** ⇒ 前值全假；
     更坏的是把修改稿行写成 `status=public` 后，公开列表（只滤 `is_public`/`draft`、
     不滤 `draft_of`）会多出一篇同标题文章。`/api/protected/notes/list` 过滤
     `draft_of is null`，于是修改稿 id 在这里**读不到** ⇒ 调用方如实拒绝（"这不是一篇
     文章本体"），而不是猜。
  ② **`_read_note` / `_note_index` 是写操作的唯一读写口径**：确认弹窗里那句「现在是
     公开还是草稿」必须与写操作自己读前值的口径**同一份数据源**，否则弹窗写的现状
     与真正被改的那一行可能不是同一行。
  ③ **`None` 与 `{}` 的区分是刻意的**：`{}` = 后台确实一篇文章都没有（"清单里没有
     这一篇"是**事实**），`None` = 这一次读不到（只能说"没核对上"，不许说成"站内没有"）。

## 搬家时守的一条
⚠️ **所有共享帮手一律写成 `_base.<名>(...)`**：`_admin_get`（套件打桩 50 处）、
`_admin_post`（28 处）、`_admin_read_post`（7 处）、`_tag_index`（47 处）、
`_category_index`（15 处）、`_read_note`（**12 处**）、`_note_index`（1 处）都是
**被套件直接打桩**的模块全局（`patch()` 就是 `setattr(tools.base, 名, 桩)`）；
`_near_miss_names` 与 `_as_article_id` 这两个共享原语**留在 `tools/base.py` 的原位置**
（前者供给标签/分类/公告/账号四族，后者被 `tools/own_data.py` 与
`agent/adminops_noop.py` 按名 import）。
⚠️⚠️ **连本模块自己的 `_read_note` / `_note_index` 也走 `_base.<名>(...)`**：它们各自
被套件打桩 12 处 / 1 处，而函数对象按**自己所在模块**的 `__dict__` 解析全局名 ⇒ 裸名
调用会让 `tools.base` 上那份桩**静默失效**（第十块那条教训的同款）。
本模块一个名字都不按名从 `tools.base` 取（模块级 `from tools.base import <名>` 会让
mypy 在这个环上判不出被 re-export 的那些名字的类型，见 `tools/admin_read.py` 头注）。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, fact, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外）：
# 三件 `@tool`。`_read_note` / `_note_index` 是下划线名，由 `tools/base.py` 按原名
# 显式 re-export 供给（`agent/graph.py` 按名取 `_note_index`；套件更是直接
# `setattr(tools.base, "_read_note", 桩)`）。
__all__ = ["list_admin_notes", "set_article_status", "set_article_tags"]


def _read_note(article_id: int, config: RunnableConfig) -> dict | None | ToolResult:
    """读一篇文章的后台现状（**写操作的唯一读写口径**）。

    为什么不用 `/api/protected/draft/editor/:id`：那一条对「编辑修改稿」行会**解引用
    成原文章**，而 `update_note` 只发 `{status,isTop}` 时写的是**被点的那一行本身**
    ——读的行和写的行不是同一行，前值全假；更坏的是把修改稿行写成 status=public 后，
    公开列表（只滤 is_public/draft，不滤 draft_of）会多出一篇同标题文章。
    `/api/protected/notes/list` 过滤 `draft_of is null`，于是修改稿 id 在这里**读不到**
    → 调用方如实拒绝（"这不是一篇文章本体"），而不是猜。
    """
    data = _base._admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return data
    for n in (data or []):
        if isinstance(n, dict) and n.get("noteKey") == article_id:
            return n
    return None



def _note_index(config: RunnableConfig) -> dict[int, dict] | None:
    """后台文章清单 → `{noteKey: 行}`；读不到返回 None（**≠"一篇文章都没有"**，同 _tag_index）。

    与 `_read_note` 同一份数据源、同一口径（`/api/protected/notes/list`，含草稿/私密、
    不含「编辑修改稿」行）——确认弹窗要写的「现在是公开还是草稿」必须和写操作自己读
    前值的口径一致，否则弹窗里那句现状可能和真正被改的那一行不是同一行。

    None 与 `{}` 的区分是刻意的：`{}` = 后台确实一篇文章都没有（"后台清单里没有这一篇"
    是**事实**），None = 这次读不到（只能说"没核对上"，不许说成"站内没有"）。
    """
    data = _base._admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return None
    out: dict[int, dict] = {}
    for row in data or []:
        if not isinstance(row, dict):
            continue
        try:
            nid = int(row.get("noteKey"))
        except (TypeError, ValueError):
            continue
        row["noteKey"] = nid          # 就地归一成 int：下游 notes.get(id) 直接可用
        out[nid] = row
    return out




@tool
def list_admin_notes(
    config: RunnableConfig,
    keyword: Annotated[str | None,
                       "关键词收窄（匹配标题/正文/标签名）；只在文章很多、"
                       "要定位某几篇时填。与公开站内搜索同一套切词口径，"
                       "区别是**这里连草稿与私密文章一起搜**"] = None,
    category: Annotated[str | None,
                        "分类名**一字不差**（先用 list_categories 核对全部分类名）："
                        "只要归在这个分类下的文章。主人说「某个分类下的文章」时就填它，"
                        "**不要用 keyword 去凑**——keyword 搜的是标题/正文/标签名，"
                        "搜不出「这篇归在哪个分类」，两者不是一回事。"
                        "**这个值只能照抄主人原话或 list_categories 的名单，"
                        "绝不填「未确认分类」「待定」这类占位词**（名字还没到手就"
                        "先调 list_categories 再回来调本工具）"] = None,
) -> str:
    """查看后台文章清单：**包含未公开的草稿与私密文章**（公开接口一律看不到它们），
    每行给出 id、状态（公开/私密/草稿）、是否置顶、标签、**所属分类**。要改某篇文章
    的状态或标签，先用它拿到**确切的 id**；要一次拿到**某个分类下的全部文章**
    （比如"把这个分类的文章都转成私密"），用 category 筛一遍就有了确切清单。

    可选 keyword 按关键词收窄——**它是搜索而不是过滤**（切词匹配标题/正文/标签名，
    口径与公开站内搜索一致），所以「按这个词没搜到」只是这一次没搜到，不等于站内
    没有。可选 category 按分类筛（服务端按分类名精确匹配，与后台笔记页的
    「分类」筛同一口径）；**分类名认不出来时返回的是"读不到"，不是"该分类下没有
    文章"**。需要管理员身份。"""
    from agent import adminops_tags as _tags
    kw = str(keyword or "").strip()
    cat = str(category or "").strip()
    payload: dict = {}
    hit = None
    if cat:
        # 名字→分类在**本地字典**上解析，再把认准的那个名字交给服务端筛。为什么不
        # 直接把它丢给服务端：`search_all_notes` 对认不出的分类名回的是 **200 + 空
        # 数组**（`notes.rs` 那段），与"这个分类下确实没有文章"长得一模一样 ⇒ 一句
        # "没有"就把"站内没有这个分类"说成了"该分类下没有文章"（缺数 ≠ 零）。
        # 复用 `A.find_category`（精确相等、重名返回候选、绝不模糊匹配）——与写通道
        # 同一个判据，只是措辞分开（写通道那几句说"本次未改动/动手"，读通道不能照抄）。
        index = _base._category_index(config)
        if index is None:
            return unavailable("读不到现有的分类列表，无法确认这个分类存不存在，这一次没有查")
        hit, cands = _tags.find_category(index, cat)
        if hit is None:
            if cands:
                return unavailable(
                    f"站内有 {len(cands)} 个叫「{cat}」的分类（"
                    + "、".join(f"id={c.id}" for c in cands)
                    + "）：无法确定你说的是哪一个，这一次没有查")
            near = _base._near_miss_names(cat, [(c.id, c.name) for c in index.values()])
            tail = ("；名字最接近的是 "
                    + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                    + "——若就是其中一个，照它的完整名字再说一遍") if near else ""
            return unavailable(f"站内没有叫「{cat}」的分类，这一次没有查{tail}"
                               f"（`list_categories` 能看到全部分类名）")
        payload["categories"] = hit.name
    if kw:
        payload["keyword"] = kw
    # 有筛就走检索端点（**筛选在服务端做**，口径与公开 `search_notes` / 后台分类页
    # 同源，见 `notes.rs::search_all_notes`——两处各写一套"什么算命中"就是第二个
    # 真相源）；没筛走清单端点。两个端点回的是同一个 DTO，渲染侧不必分叉。
    if payload:
        data = _base._admin_read_post("/api/protected/notes/search", payload, config)
    else:
        data = _base._admin_get("/api/protected/notes/list", config)
    if isinstance(data, ToolResult):
        return data
    notes = data if isinstance(data, list) else []
    # 「编辑修改稿」是编辑某篇文章时自动保存落下的**影子行**（`draftOf` 指向原文章），
    # 不是文章。清单端点自己滤掉了它（`notes.rs::list_all_notes`），而**检索端点只在
    # 「公开文章」页签那一支滤**（`only_public`，本工具不发这个参数）⇒ 带筛的这两路
    # 会把影子行一起带回来，头行那句"「编辑修改稿」不在其中"就成了假话。按 DTO 自己
    # 的 `draftOf` 在这里滤（与渲染头行同一承诺）；**键缺席 = 这一路没带这个信息 ⇒ 留**
    # （缺键不编，不拿"没带"当"不是影子行"的反面）。
    notes = [n for n in notes
             if not (isinstance(n, dict) and n.get("draftOf") is not None)]
    if not notes:
        # 筛空与"站里一篇文章都没有"是两件事（缺数 ≠ 零的老纪律）：带筛时只许说
        # "按这个条件没搜到"，说成"后台没有文章"就是替站内下一个假结论。
        if hit is not None:
            # 分类确实在（上面刚核对过），可它名册上的篇数与筛出来的空手对不上：
            # 分类的 noteCount 口径 = 该分类下**非修改稿**的行数（`categories.rs`），
            # 与本工具滤掉影子行之后的口径同一套 ⇒ 名册说 N≥1、筛出来却是空的，
            # 只可能是"这一次没读到"，不是"该分类下没有文章"。
            if hit.note_count:
                return unavailable(
                    f"「{hit.name}」分类在名册上写着 {hit.note_count} 篇，"
                    f"但这一次按分类筛出来是空的——这不是「该分类下没有文章」，"
                    f"是这一次没读到，再试一次或换个说法")
            return empty(f"「{hit.name}」分类下没有文章（分类本身在，篇数 0）")
        if kw:
            return empty(f"按关键词「{kw}」没有搜到文章（后台文章清单本身不是空的；"
                         f"这是这一次的搜索结果，换个词或去掉关键词再看）")
        return empty("后台文章列表是空的（一篇文章都没有）")
    # `data` 是渲染用的那**同一份**（批 G5）：跨轮实体摘要此前只能从渲染出来的中文
    # 表头里正则抠"共 N 篇"、再从行文本里数状态分布——改一句措辞就静默变空。
    return ok(_tags.render_admin_notes(notes, _base._tag_index(config), keyword=kw, category=cat),
              meta={"count": len(notes), "keyword": kw or None,
                    "category": hit.name if hit is not None else None},
              data={"notes": notes, "keyword": kw, "category": cat,
                    "limit": _tags.ADMIN_NOTES_LIMIT})




@tool
def set_article_status(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「文章 12」）就**直接用点名的那个**，"
                               "不必先读；没说 id 只说特征（「那篇讲 OTA 的」）时必须先用 "
                               "list_admin_notes 读回确切 id，不许凭记忆猜"],
    config: RunnableConfig,
    status: Annotated[str | None, "改成什么状态：public=公开 / private=私密 / draft=草稿"] = None,
    is_top: Annotated[int | None, "置顶开关：1=置顶 / 0=取消置顶"] = None,
) -> str:
    """修改一篇文章的状态（发布/隐藏/转草稿）或置顶开关。**只改你点名的字段**，
    没点名的保持不动（不会顺手改标题、正文或可见性）。需要管理员身份。"""
    from agent import adminops_tags as _tags
    aid = _base._as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")
    has_status = status is not None and str(status).strip() != ""
    has_top = is_top is not None and str(is_top).strip() != ""
    if not has_status and not has_top:
        return unavailable("没有指出要改什么（状态 / 置顶），未改动")
    want_status = _tags.normalize_status(status) if has_status else None
    if has_status and want_status is None:
        return unavailable(f"认不出状态「{status}」（只支持 public 公开 / private 私密 /"
                           f" draft 草稿），未改动")
    want_top = _tags.normalize_top(is_top) if has_top else None
    if has_top and want_top is None:
        return unavailable(f"认不出置顶值「{is_top}」（只支持 1 置顶 / 0 取消置顶），未改动")

    before = _base._read_note(aid, config)
    if isinstance(before, ToolResult):
        return before
    if before is None:
        return unavailable(f"后台文章列表里没有 id={aid} 这一篇"
                           f"（它可能是某篇文章的「编辑修改稿」影子行，不能这样直接改），本次未改动")
    title = str(before.get("noteTitle") or "").strip()

    payload: dict = {}
    if want_status is not None:
        payload["status"] = want_status
    if want_top is not None:
        payload["isTop"] = want_top
    # ★ 刻意**不发** title/content（会触发 from_editor 分支：重定向 + 级联删修改稿
    #   + **署名回填**——`update_note` 里 `should_backfill_author(NULL, from_editor)`）、
    #   不发 isPublic（由 status 联动）、不发 updateTime（Rust 自己写 updated_at）。
    #   ⚠️ 最后那半句是 20261007 补的：署名回填原先**不看 from_editor**，于是这条元数据
    #   路（以及 `set_article_tags` 那条）只要碰上 `user_id` 为 NULL 的老文章，就会把
    #   **操作者**记成文章作者。生产实证 = 文章 23 被评测身份 uid 721 这样记上，而且
    #   「只在为空时补写」让它再也改不回来。所以这条"不发 title/content"现在同时是
    #   署名判据的前提——别把它当成单纯的"少触发一个分支"。

    # 已经就是目标值 → **不发这个请求**。`update_note` 无条件刷新 updated_at，
    # 一次空改动会把文章顶到列表最前（纯副作用、无收益）。
    same = ((want_status is None or _tags.normalize_status(before.get("status")) == want_status)
            and (want_top is None or _tags.normalize_top(before.get("isTop")) == want_top))
    if same:
        now_cn = "、".join(filter(None, [
            _tags.status_cn(before.get("status")) if want_status is not None else "",
            _tags.top_cn(before.get("isTop")) if want_top is not None else ""]))
        return ok(f"文章 {aid}《{title}》本来就是{now_cn}，无需改动（没有发出写请求）。",
                  meta=fact("set_status", changed=False, target=tgt("note", aid, title),
                            before=now_cn, after=now_cn, article_id=aid))

    data = _base._admin_post(f"/api/protected/notes/{aid}", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _base._read_note(aid, config)
    if isinstance(after, ToolResult):
        return after
    if after is None:
        return unavailable(f"改动请求已发出，但读回时文章 {aid} 已不在后台列表里，"
                           f"本次改动未确认生效")

    pairs = []
    if want_status is not None:
        pairs.append((_tags.status_cn(before.get("status")), _tags.status_cn(after.get("status"))))
    if want_top is not None:
        pairs.append((_tags.top_cn(before.get("isTop")), _tags.top_cn(after.get("isTop"))))
    if all(b == a for b, a in pairs):
        # 请求发出去了、读回来却原封不动 —— 这不是"改好了"，是"没确认生效"。
        return unavailable(f"改动请求已发出，但读回文章 {aid} 仍是原值"
                           f"（{(' / '.join(b for b, _ in pairs))}），本次改动未确认生效")
    before_s, after_s = _tags.render_change(pairs)
    return ok(_tags.render_status_ok(aid, title, before_s, after_s),
              meta=fact("set_status", changed=True, target=tgt("note", aid, title),
                        before=before_s, after=after_s, evidence=after_s, article_id=aid))




@tool
def set_article_tags(
    article_id: Annotated[int, "文章 id：用户本轮点名了（如「文章 12」）就**直接用点名的那个**，"
                               "不必先读；没说 id 只说特征（「那篇讲 OTA 的」）时必须先用 "
                               "list_admin_notes 读回确切 id，不许凭记忆猜"],
    config: RunnableConfig,
    add: Annotated[list[str] | None, "要**加上**的标签名（如 [\"Python\"]；"
                                      "必须是站内已存在的标签）"] = None,
    remove: Annotated[list[str] | None, "要**去掉**的标签名"] = None,
    replace: Annotated[list[str] | None, "整体**替换**成这些标签名；"
                                          "**只有明确要求「清空标签」时才传 []**"] = None,
) -> str:
    """给一篇文章加标签 / 去掉标签。**只动你点名的标签，没点名的原样保留**（绝不顺手清空）。
    标签按**名字**精确匹配站内已有的标签——找不到就如实说，**不会自动新建**
    （要新建标签用 create_tag）。传 replace 才是整体替换。需要管理员身份。"""
    from agent import adminops_tags as _tags
    aid = _base._as_article_id(article_id)
    if aid is None:
        return unavailable(f"文章 id「{article_id}」不合法，未改动")

    def _names(v) -> list:
        if v is None:
            return []
        if isinstance(v, list):
            return [x for x in v if str(x).strip() != ""]
        s = str(v).strip()
        return [s] if s else []

    add_l, rm_l, rep_l = _names(add), _names(remove), _names(replace)
    if replace is not None and (add_l or rm_l):
        return unavailable("replace 不能与 add/remove 同时使用"
                           "（一个说\"整体替换\"、一个说\"增减\"），未改动")
    if not add_l and not rm_l and replace is None:
        return unavailable("没有指出要加/去/替换哪些标签，未改动")

    index = _base._tag_index(config)
    if index is None:
        return unavailable("读不到现有的标签字典，无法把标签名对应到 id，本次未改动")

    def _resolve(items) -> tuple[list[int], list[str]]:
        """名字/数字混合列表 → (id 列表, 认不出来的原样列表)。"""
        ids, bad = [], []
        for it in items:
            tid = _base._as_article_id(it)
            if tid is not None:
                ids.append(tid)
                continue
            hit, cands = _tags.find_tag(index, str(it))
            if hit is not None:
                ids.append(hit.id)
            else:
                bad.append(str(it))
        return ids, bad

    before_note = _base._read_note(aid, config)
    if isinstance(before_note, ToolResult):
        return before_note
    if before_note is None:
        return unavailable(f"后台文章列表里没有 id={aid} 这一篇"
                           f"（它可能是某篇文章的「编辑修改稿」影子行，不能这样直接改），本次未改动")
    title = str(before_note.get("noteTitle") or "").strip()
    cur = _tags.parse_tag_ids(before_note.get("noteTags"))

    if replace is not None:
        add_ids, bad = _resolve(rep_l)
        if bad:
            # 返回文本里**不许出现工具名**（20260921）：narrator 被要求"按工具返回作答"，
            # 照抄到回复里就会变成"我调用了 X"（而 X 若不在本轮执行集里，5c 具名声称闸
            # 直接判编造 → 整条回复被换成兜底道歉）。同族事故见 adminops.render_tag_created。
            return unavailable(f"站内没有这些标签：{'、'.join(bad)}——"
                               f"先确认名字（或先把标签建出来），本次未改动")
        new = add_ids
    else:
        add_ids, bad_add = _resolve(add_l)
        rm_ids, bad_rm = _resolve(rm_l)
        if bad_add:
            return unavailable(f"站内没有这些标签：{'、'.join(bad_add)}——"
                               f"标签按名字精确匹配，不会自动新建，本次未改动")
        if bad_rm:
            return unavailable(f"站内没有这些标签：{'、'.join(bad_rm)}"
                               f"——无法确定要去掉的是哪一个，本次未改动")
        new = [i for i in cur if i not in rm_ids]
        for i in add_ids:
            if i not in new:
                new.append(i)

    if new == cur:
        return ok(f"文章 {aid}《{title}》的标签本来就是"
                  f"{_tags.render_tag_list(cur, index)}，无需改动（没有发出写请求）。",
                  meta=fact("set_tags", changed=False, target=tgt("note", aid, title),
                            before=_tags.render_tag_list(cur, index),
                            after=_tags.render_tag_list(cur, index), article_id=aid))

    # `noteTags` 是"传了就写"，`""` = 清空 ⇒ 只有 replace（含 replace=[]）才可能产出空串；
    # add/remove 路径下 new 至少含一个元素或被上面的 new==cur 拦下。
    # ⚠️ 与 `set_article_status` 同一条纪律：**不发 title/content**（元数据写不是"发布"，
    #    不该触发 `update_note` 的 `from_editor` 分支——含**署名回填**；20261007 那起
    #    "文章 23 被记成 uid 721" 正是这一条路，说明见那边那段注释）。
    data = _base._admin_post(f"/api/protected/notes/{aid}",
                             {"noteTags": _tags.join_tag_ids(new)}, config)
    if isinstance(data, ToolResult):
        return data

    after_note = _base._read_note(aid, config)
    if isinstance(after_note, ToolResult):
        return after_note
    if after_note is None:
        return unavailable(f"改动请求已发出，但读回时文章 {aid} 已不在后台列表里，"
                           f"本次改动未确认生效")
    got = _tags.parse_tag_ids(after_note.get("noteTags"))
    if got != new:
        return unavailable(f"改动请求已发出，但读回文章 {aid} 的标签是"
                           f"{_tags.render_tag_list(got, index)}、与预期的"
                           f"{_tags.render_tag_list(new, index)}不一致，本次改动未确认生效")
    before_s = _tags.render_tag_list(cur, index)
    after_s = _tags.render_tag_list(got, index)
    return ok(_tags.render_tags_ok(aid, title, before_s, after_s),
              meta=fact("set_tags", changed=True, target=tgt("note", aid, title),
                        before=before_s, after=after_s, evidence=after_s, article_id=aid))
