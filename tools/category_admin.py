# -*- coding: utf-8 -*-
"""管理助手写工具：**文章分类**（建 / 改 / 删）—— 20261018 拆 `tools/base.py`。

**为什么单独成模块**：这一族是 `tools/base.py` 里**连续的 207 行、5 个模块级名字**
（分类字典 `_category_index` / 按名字解析 `_find_named_category` / 建 / 改 / 删），
scope 全是 `write.console`、目标通道全是**分类名字**。它与紧邻的标签族只共用
"名字 → id 解析"这个**形状**，不共用任何数据、任何通道（标签走 `/api/tag*`，
分类走 `/api/category`）⇒ 两族各自成模块，改分类的人不必再翻标签那 400 行。

## 这一族独有的事实（实现为什么长这样）
  ① **分类没有 `level`**：标签是两级的、同名是常态，分类是平铺的；所以这里
     `_find_named_category` 的 `level` 收窄参数不存在，重名就直接拒绝（后端对
     `categoryTitle` 没有唯一约束，重名是**真会出现的**）。
  ② **删分类不删文章**：后端外键 `ON DELETE SET NULL`，文章变成"没有分类"。
     `delete_category` 的 `change` 行把"几篇文章变成没有分类"写进事实信封。
  ③ **`_category_index` 的读点是公开面 `/api/category`**（不是 `/api/protected/…`）
     ——它和公开读族打的是同一个 handler，只是这里以管理员身份读全量。

## 搬家时守的一条
⚠️ **所有共享帮手一律写成 `_base.<名>(...)`**：`_admin_get`（套件打桩 50 处）、
`_admin_post`（28 处）、`_admin_request`（49 处）是**被套件直接打桩**的模块全局；
`_near_miss_names` 按它在 `tools/base.py` 里的原位置留给标签/分类/公告/账号四族共用。
⚠️⚠️ **连本模块自己的 `_category_index` 也走 `_base._category_index(...)`**（5 处
调用点）：全仓有 **15 处** `patch(_category_index=…)`，而 `patch()` 就是
`setattr(tools.base, 名, 桩)`——函数对象按**自己所在模块**的 `__dict__` 解析全局名
⇒ 裸名调用会解析回本模块这个真函数、桩**静默失效**、测试真去发网络请求。
同族的 `_find_named_category` 没人打桩，按裸名调用（同模块内），与搬家前逐字一致。
本模块一个名字都不按名从 `tools.base` 取（模块级 `from tools.base import <名>` 会让
mypy 在这个环上判不出被 re-export 的那些名字的类型，见 `tools/admin_read.py` 头注）。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, fact, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外）：
# 三件 `@tool`。`_category_index` / `_find_named_category` 是下划线名，由
# `tools/base.py` 按原名显式 re-export 供给（`agent/graph.py` 按名取
# `_category_index`；套件更是直接 `setattr(tools.base, "_category_index", 桩)`）。
__all__ = ["create_category", "delete_category", "update_category"]


def _category_index(config: RunnableConfig):
    """分类字典 → `{id: CategoryInfo}`；读不到返回 None（≠"没有分类"）。"""
    from agent import adminops_tags as _tags
    data = _base._admin_get("/api/category", config)
    if isinstance(data, ToolResult):
        return None
    return _tags.build_category_index(data)



def _find_named_category(name, config, index=None):
    """按名字找一个分类 → `(CategoryInfo, None)` 或 `(None, 拒绝文本)`（同上）。

    `index` 与 `_find_named_tag` 同义：可选的**外部快照**。调用方一次要查多个名字
    （或只是"先看看能不能做"，见 graph._write_target_refusal）时传进来，省一次往返、
    也让两次判断看到同一份字典。完全没有同名分类时附名字最接近的候选（同 `_near_miss_names`）。"""
    from agent import adminops_tags as _tags
    if index is None:
        index = _base._category_index(config)
    if index is None:
        return None, "读不到现有的分类列表，无法把名字对应到分类，本次未改动"
    want = str(name or "").strip()
    if not want:
        return None, "分类名为空，本次未改动"
    hit, cands = _tags.find_category(index, want)
    if hit is not None:
        return hit, None
    if cands:
        return None, (f"站内有 {len(cands)} 个叫「{want}」的分类（"
                      + "、".join(f"id={c.id}" for c in cands)
                      + "）：无法确定要动的是哪一个，本次未改动")
    near = _base._near_miss_names(want, [(c.id, c.name) for c in index.values()])
    if near:
        return None, (f"站内没有叫「{want}」的分类（完全同名的一个都没有）；"
                      + "名字最接近的是 "
                      + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                      + "：本次未改动——若就是其中一个，请照它的**完整名字**再说一遍，"
                        "我按那个名字动手")
    return None, f"站内没有叫「{want}」的分类，本次未改动"




@tool
def create_category(
    title: Annotated[str, "新分类的名字"],
    config: RunnableConfig,
    path_name: Annotated[str | None,
                         "分类的路径名（分类页 URL 里那一段，如 pythonfy）；用户没说就不填"] = None,
    introduce: Annotated[str | None, "分类简介；用户没说就不填"] = None,
    icon: Annotated[str | None, "分类图标；用户没说就不填"] = None,
    color: Annotated[str | None,
                     "用户点了名的颜色（中文色名，或任意 6 位色值如 #eb2f96）；没说就不填"] = None,
) -> str:
    """新建一个文章分类。分类是**平铺**的、没有层级。
    站内已有同名分类时**拒绝**（既不重复建、也不当成"已存在"复用——分类没有唯一
    约束，重名会让以后每一次按名字找分类都变成歧义）。需要管理员身份。"""
    from agent import adminops_tags as _tags
    name = str(title or "").strip()
    if not name:
        return unavailable("分类名为空，未创建")
    if len(name) > 40:
        return unavailable(f"分类名过长（{len(name)} 字，上限 40），未创建")

    index = _base._category_index(config)
    if index is None:
        return unavailable("读不到现有的分类列表，无法确认是否重名，本次未创建")
    hit, cands = _tags.find_category(index, name)
    if hit is not None or cands:
        ids = "、".join(f"id={c.id}" for c in (cands or [hit]))
        return unavailable(f"站内已经有叫「{name}」的分类（{ids}），本次未创建")

    payload: dict = {"categoryTitle": name}
    color_spec = str(color or "").strip()
    if color_spec:
        picked = _tags.match_any_color(color_spec)
        if picked is None:
            return unavailable(f"颜色「{color_spec}」认不出来（可用中文色名，"
                               f"或 6 位色值如 #eb2f96），本次未创建")
        payload["color"] = picked
    for key, val in (("pathName", path_name), ("introduce", introduce), ("icon", icon)):
        s = str(val or "").strip()
        if s:
            payload[key] = s

    data = _base._admin_post("/api/protected/category", payload, config)
    if isinstance(data, ToolResult):
        return data

    # 建后复核：这个端点**不回 id**（返回死字符串 "Category created"），所以要靠
    # "分类列表里多出来一行、且那行的名字就是我建的那个"来证明它真的在。多出两行
    # （并发下别人也建了一个）就说不清哪行是我的——如实报不确定，不当成成功。
    after = _base._category_index(config)
    if after is None:
        return unavailable("分类可能已经建好，但读不回分类列表、无法确认"
                           "——请到后台分类页核对后再决定是否重试")
    new_ids = [i for i in after if i not in index]
    got = after.get(new_ids[0]) if len(new_ids) == 1 else None
    if got is None or got.name != name:
        return unavailable(f"新建请求已发出，但读回分类列表里找不到名字为「{name}」的新行，"
                           f"本次改动未确认生效")
    return ok(_tags.render_category_created(got),
              meta=fact("category_create", changed=True, target=tgt("category", got.id, got.name),
                        before="不存在", after="已创建", evidence=got.name,
                        category_name=got.name))




@tool
def update_category(
    name: Annotated[str, "要改的那个分类的**名字**（站内已有的分类）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么名字；不改名就不填"] = None,
    path_name: Annotated[str | None, "改成什么路径名；不改就不填"] = None,
    introduce: Annotated[str | None, "改成什么简介；不改就不填"] = None,
    icon: Annotated[str | None, "换成什么图标；不改就不填"] = None,
    color: Annotated[str | None, "换成什么颜色；不改就不填"] = None,
) -> str:
    """修改一个已有分类（改名 / 路径名 / 简介 / 图标 / 颜色）。**只动你点名的字段**，
    没点名的保持不动。需要管理员身份。

    注：这些字段都**只能改不能清空**（清空请求会被后端当成"不改"忽略），
    所以本工具不接受"把简介清空"这类要求。"""
    from agent import adminops_tags as _tags
    hit, err = _find_named_category(name, config)
    if err:
        return unavailable(err)

    payload: dict = {}
    new_title = str(new_title or "").strip()
    if new_title:
        if len(new_title) > 40:
            return unavailable(f"新分类名过长（{len(new_title)} 字，上限 40），本次未改动")
        payload["categoryTitle"] = new_title
    color_spec = str(color or "").strip()
    if color_spec:
        picked = _tags.match_any_color(color_spec)
        if picked is None:
            return unavailable(f"颜色「{color_spec}」认不出来（可用中文色名，"
                               f"或 6 位色值如 #eb2f96），本次未改动")
        payload["color"] = picked
    for key, val in (("pathName", path_name), ("introduce", introduce), ("icon", icon)):
        s = str(val or "").strip()
        if s:
            payload[key] = s
    if not payload:
        return unavailable("没有指出要改什么（名字 / 路径名 / 简介 / 图标 / 颜色），本次未改动")

    data = _base._admin_post(f"/api/protected/category/{hit.id}", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _base._category_index(config)
    if after is None:
        return unavailable("改动请求已发出，但读不回分类列表、无法确认，请到后台分类页核对")
    got = after.get(hit.id)
    if got is None:
        return unavailable(f"改动请求已发出，但读回分类列表里已经没有 id={hit.id} 这一行，"
                           f"本次改动未确认生效")
    # 逐字段复核"我要的它变成了"——**不是**相信那个 "Updated" 字符串
    # （update_category 对不存在的 id 也返回 Not found 之外的正常路径，
    #  且 0 行更新不会报错）。
    fields = {"categoryTitle": "name", "pathName": "path_name",
              "introduce": "introduce", "icon": "icon", "color": "color"}
    pairs = []
    for key, attr in fields.items():
        if key not in payload:
            continue
        want = str(payload[key])
        if str(getattr(got, attr)) != want:
            return unavailable(f"改动请求已发出，但读回分类「{got.name}」的这一项仍是旧值，"
                               f"本次改动未确认生效")
        pairs.append((str(getattr(hit, attr)) or "（空）", want))
    before_s, after_s = _tags.render_change(pairs)
    return ok(_tags.render_category_updated(got.name, before_s, after_s),
              meta=fact("category_update", changed=True, target=tgt("category", got.id, got.name),
                        before=before_s, after=after_s, change=after_s,
                        category_name=got.name))




@tool
def delete_category(
    name: Annotated[str, "要删掉的那个分类的**名字**（站内已有的分类）"],
    config: RunnableConfig,
) -> str:
    """删除一个文章分类。需要管理员身份。

    ⚠️ **文章不会被删**：分类删掉后，原本属于它的文章会变成"没有分类"（后端外键是
    ON DELETE SET NULL）。分类名对不上就什么都不做。"""
    from agent import adminops_tags as _tags
    hit, err = _find_named_category(name, config)
    if err:
        return unavailable(err)

    data = _base._admin_request("DELETE", "/api/protected/category", [hit.id], config)
    if isinstance(data, ToolResult):
        return data

    after = _base._category_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回分类列表、无法确认它是否真的删掉了"
                           "——请到后台分类页核对")
    if hit.id in after:
        return unavailable(f"删除请求已发出，但读回分类列表里 id={hit.id}（{hit.name}）还在，"
                           f"本次改动未确认生效")
    change = (f"{hit.note_count} 篇文章变成没有分类"
              if hit.note_count is not None else "")
    return ok(_tags.render_category_deleted(hit.name, hit.note_count),
              meta=fact("category_delete", changed=True, target=tgt("category", hit.id, hit.name),
                        before="已存在", after="已删除", change=change,
                        category_name=hit.name))
