"""Article catalog reads; writes use versioned domain operations."""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, empty, ok, unavailable


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

__all__ = ['list_admin_notes']
