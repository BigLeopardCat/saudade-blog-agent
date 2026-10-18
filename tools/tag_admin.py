# -*- coding: utf-8 -*-
"""管理助手写工具：**标签**（建 / 改 / 删）—— 20261018 拆 `tools/base.py`。

**为什么单独成模块**：这一族是 `tools/base.py` 里**离散的三段**（`_tag_index` /
`create_tag` / `_find_named_tag` + `update_tag` + `delete_tag`），scope 全是
`write.console`，目标通道全是**标签名字**。它们共享同一个解析入口
（`_find_named_tag`）与同一份字典快照（`_tag_index`），与同节的分类族只共用
"名字 → id"这个形状、不共用任何数据——按域切开之后，改标签的人不必再在
五千行的 `base.py` 里找那三个工具。

## 这一族独有的事实（实现为什么长这样）
  ① **两级标签**：`level` 是 `one` / `two`，二级标签挂在父标签下。`_tag_index`
     把两级拼成一个 `{id: TagInfo}`，所以"同名标签不止一个"是**常态**而不是异常
     ⇒ 按名字解析时必须能按 `level` 收窄（`update_tag` / `delete_tag` 的 `level`
     形参就是为此存在），收不窄就拒绝并列出候选（`_near_miss_names`）。
  ② **改名会撞名**：`update_tag` 的 `new_title` 与已有标签重名时后端会拒，工具
     不预判、按后端政策出口（写前读的是旧值，写后复核读的是新值）。
  ③ **删标签不删文章**：后端外键是 `ON DELETE SET NULL`，文章变成"没有标签"。
     这条必须写进工具 docstring——planner 读的就是它。

## 搬家时守的一条
⚠️ **所有共享帮手一律写成 `_base.<名>(...)`**：`_admin_get`（套件打桩 50 处）、
`_admin_post`（28 处）、`_admin_request`（49 处）都是**被套件直接打桩**的模块全局
（`patch()` 就是 `setattr(tools.base, 名, 桩)`）；`_near_miss_names` 虽然今天没人
打桩，但它按**在 `tools/base.py` 里的原位置**留给标签/分类/公告/账号四族共用
（`tools/announcements.py` 与 `tools/account_admin.py` 已经经 `_base.` 读它）⇒
本模块一个名字都不按名从 `tools.base` 取：模块级写 `from tools.base import <名>`
会让 mypy 在这个环上判不出被 re-export 的那些名字的类型（20261018 在
`tools/admin_read.py` 那一刀实测 7 条）⇒ 属性查找同时解决了时序与类型两件事。

⚠️⚠️ **连本模块自己的 `_tag_index` 也走 `_base._tag_index(...)`**（8 处调用点）：
它是这一族里**唯一被套件直接打桩的名字**（全仓 47 处 `patch(_tag_index=…)`），
而桩是打在 `tools.base` 上的。函数对象按**自己所在模块**的 `__dict__` 解析全局名
⇒ 在这里写裸 `_tag_index(...)` 会解析到**本模块**这个真函数，`tools.base` 上那份桩
**静默失效**、测试真去发网络请求（20261018 实测：`tests/test_tag_admin.py` 当场
`AttributeError: 'NoneType' object has no attribute 'get'`，因为桩传的 `config=None`）。
这不是"多余的一层间接"：**打桩点在哪，取值点就得在哪**。同族的 `_find_named_tag`
没人打桩，按裸名调用（同模块内），与搬家前逐字一致。
"""

from typing import Annotated

from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import tool

from tools import base as _base
from tools.result import ToolResult, fact, ok, tgt, unavailable

# `__all__` = 本模块**所有不以 `_` 开头**的模块级名字（社区惯例，不留例外）：
# 三件 `@tool`。`_tag_index` / `_find_named_tag` 是下划线名，由 `tools/base.py`
# 按原名显式 re-export 供给（`agent/graph.py` 按名取 `_tag_index`、`_find_named_tag`；
# 套件更是直接 `setattr(tools.base, "_tag_index", 桩)`）。
__all__ = ["create_tag", "delete_tag", "update_tag"]


def _tag_index(config: RunnableConfig):
    """读两级标签字典 → `{id: TagInfo}`；任一读不到 → **None**。

    None 不是"没有标签"，是"这次读不到字典"——调用方必须分开处理（见
    adminops.render_tag_list 的同名区分）。返回 None 而不是抛，是因为"字典读不到"
    在只读场景（渲染清单）不该让整次查询失败，而在写场景由调用方直接拒绝。
    """
    from agent import adminops_tags as _tags
    one = _base._admin_get("/api/tagone", config)
    two = _base._admin_get("/api/tagtwo", config)
    if isinstance(one, ToolResult) or isinstance(two, ToolResult):
        return None
    return _tags.build_tag_index(one, two)



@tool
def create_tag(
    title: Annotated[str, "新标签的名字（如「Python」「分布式」）"],
    config: RunnableConfig,
    parent_tag: Annotated[str | None,
                          "父标签的**名字**（建二级标签时给，如「编程」；建一级标签不填）；"
                          "**不是 id**"] = None,
    color: Annotated[str | None,
                     "用户点了名的颜色（中文色名如「粉色」，或站内色板色值如 #eb2f96）；"
                     "用户没说就不填——不填按标签名哈希取色，同名永远同色"] = None,
) -> str:
    """新建一个文章标签：不填 parent_tag 建**一级**标签，填了则在该一级标签下建**二级**标签。
    同名标签已存在时**不重复创建**，直接复用并告知它的 id。
    **本工具只往标签字典里加一项，不会把它挂到任何文章上**（挂标签是另一件事）。
    需要管理员身份。"""
    from agent import adminops_tags as _tags
    name = str(title or "").strip()
    if not name:
        return unavailable("标签名为空，未创建")
    if len(name) > 40:
        return unavailable(f"标签名过长（{len(name)} 字，上限 40），未创建")

    index = _base._tag_index(config)
    if index is None:
        return unavailable("读不到现有的标签字典，无法确认是否重名，本次未创建")

    # 父标签：**按名字**解析成 id（20260921 第四轮）。为什么不收 id：用户嘴里说的
    # 就是名字，跨轮执行记忆里也只有名字没有 id（`list_tags` 的摘要不带编号），
    # 而"父标签 id 从哪来"曾是这条链上唯一解不开的环——planner 只可能猜一个数字。
    # 名字对不上就**拒绝**（零写），不新建、不做模糊匹配。
    pid = None
    pname = str(parent_tag or "").strip()
    if pname:
        parent, cands = _tags.find_tag(index, pname, level=1)
        if parent is None:
            if cands:
                return unavailable(
                    f"站内有两个同名的一级标签「{pname}」，无法确定是哪一个，本次未创建")
            return unavailable(f"站内没有叫「{pname}」的一级标签，本次未创建"
                               f"（要建的话先把它建出来，或者换个爸爸）")
        pid = parent.id

    # 先查后建（幂等）：同名同层 → 直接复用，不写库。
    hit, cands = _tags.find_tag(index, name, pid)
    # 复用只认**同一层**的同名标签：建一级时命中的若是二级同名标签，那不是"已存在"
    # （管理员要的是一个一级标签），而是"你可能想要那个子标签"——走下面的拒绝路径。
    # 给 pid 时 find_tag 已按 (level==2 且 father_id==pid) 过滤，故命中即同层。
    if hit is not None and (pid is not None or hit.level == 1):
        # changed=False：同名同层标签已经在了，**这一下没写库**（是"复用"，不是"新建"）。
        # 这一支是本工具最常见的结果，在事实信封之前它是一句"没声明"——上层只能按"改了"
        # 处理（见 fact() 头注）。
        return ok(_tags.render_tag_reuse(hit), meta=fact(
            "tag_reuse", changed=False, target=tgt("tag", hit.id, hit.name),
            after="已存在", tag_id=hit.id, tag_name=hit.name, level=hit.level))

    if pid is not None:
        parent = index.get(pid)
        # 参数对调守卫（20260921）：父标签的**名字与新标签名相同**时拒绝。
        # 生产实证：planner 把「在编程标签下新建 Rust」填成 title=编程、父也填编程
        # （父名当成了新标签名），若放行就会在「编程」下建出一个也叫「编程」的二级
        # 标签——库里多一条永远不该有的同名父子，而回复还会说"建好了"。这一判据不需要
        # 理解用户意图，只认"父子同名"这个自身矛盾的结构，误伤面 0（正常的二级标签
        # 不会与父同名：同名父子在做任何按名字找标签的操作时都是歧义）。
        # 名字通道下这条更容易撞上（两边都是名字，写串了就直接同名），所以留着。
        if parent is not None and parent.name == name:
            return unavailable(
                f"要新建的标签名「{name}」与父标签「{parent.name}」同名，这多半是把父标签名"
                f"当成了新标签名（「在{name}下面新建 X」里的 X 才是新标签的名字），本次未创建")
    elif cands or hit is not None:
        cands = cands or [hit]
        # 有同名标签、但都在二级（同一个或不同父下）——这不是"已存在"，是"你可能是
        # 想要那个"。直接建一个同名一级标签会让以后所有按名字找标签的操作都变歧义。
        return unavailable(
            f"站内已有同名**二级**标签「{name}」（"
            f"{'、'.join(c.label + ' id=' + str(c.id) for c in cands)}）；"
            f"如果你要的是它，直接用它；如果确实要新建一个同名一级标签，请说明后再来。本次未创建")

    # 颜色（20260921）：点名了就用它，没点名按名字哈希（同名同色，前端手建同源）。
    # **点名的色认不出 → 拒绝**，不回落哈希——那会让用户拿到一个他没要的颜色，
    # 且屏幕上看着"确实建成了"。这是写操作 fail-closed 纪律的一部分。
    color_spec = str(color or "").strip()
    picked = _tags.match_tag_color(color_spec) if color_spec else None
    if color_spec and picked is None:
        return unavailable(f"颜色「{color_spec}」不在站内色板里"
                           f"（可选：{_tags.TAG_COLOR_SPEC}），未创建")
    color = picked or _tags.color_for_name(name)
    if pid is not None:
        path, payload = "/api/protected/tagtwo", {"title": name, "color": color, "fatherTag": pid}
    else:
        path, payload = "/api/protected/tagone", {"title": name, "color": color}
    data = _base._admin_post(path, payload, config)
    if isinstance(data, ToolResult):
        return data
    try:
        new_id = int(str(data).strip())
    except Exception:
        new_id = 0
    if new_id <= 0:
        return unavailable(f"后台没有返回新标签 id（返回 {data!r}），无法确认创建结果")

    # 建后复核：**读回来证明它真的在**，而不是相信返回值。理由：id 由库分配、
    # 创建与读取之间还可能撞上并发/约束问题；只有"字典里真有一行 id=新id 且名字一致"
    # 才算数（tag 列表接口 `unwrap_or(vec![])` 会把 DB 故障伪装成空表，读回是唯一的证）。
    after = _base._tag_index(config)
    if after is None:
        return unavailable(f"标签可能已创建（后台返回 id={new_id}），但读不回标签字典、无法确认——"
                           f"请到后台标签页核对后再决定是否重试")
    got = after.get(new_id)
    if got is None or got.name != name:
        return unavailable(f"创建后复核失败：标签字典里找不到 id={new_id} 且名字为「{name}」的行，"
                           f"本次改动未确认生效")
    return ok(_tags.render_tag_created(got), meta=fact(
        "tag_create", changed=True, target=tgt("tag", got.id, got.name),
        before="不存在", after="已创建", evidence=got.label,
        tag_id=got.id, tag_name=got.name, level=got.level))



def _find_named_tag(name, config, level=None, role="标签", index=None):
    """按名字在标签字典里找一个标签 → `(TagInfo, None)` 或 `(None, 拒绝文本)`。

    这是**所有按名字改标签的工具共用的解析入口**（改 / 删 / 移动），也是"名字通道"
    落地的地方：planner 写名字，这里确定性换成 id 与层级。四种结局都响亮：
      · 唯一命中 → 交给调用方；
      · 命中多个（不同父下的同名二级）→ 请调用方追问，**不替用户选一个**
        （选错就是改错/删错数据）；
      · 一个都没有 → 如实说站内没这个标签，**并附上名字最接近的候选**请主人点名
        （`_near_miss_names`）；**绝不新建、绝不按模糊匹配动手**；
      · 字典读不到 → 单独一种说法（"读不到" ≠ "没有"）。
    `role` 只影响措辞（找父标签时说「一级标签」）；**给了 level 就自动说清是
    "一级里没有"还是"二级里没有"**——否则 planner 会以为这个名字站内根本不存在，
    去新建一个，而不是回来把 level 去掉。
    `index` 是**可选的外部快照**：一个工具要连着查两个名字（目标 + 父）时，
    用同一份快照查才有意义（两次读之间标签可能被挪走），也省一次网络往返。
    """
    from agent import adminops_tags as _tags
    if index is None:
        index = _base._tag_index(config)
    if index is None:
        return None, "读不到现有的标签字典，无法把名字对应到标签，本次未改动"
    want = str(name or "").strip()
    if not want:
        return None, "标签名为空，本次未改动"
    lv = None
    if str(level or "").strip():
        lv = _tags.normalize_level(level)
        if lv is None:
            return None, f"层级「{level}」认不出来（只支持一级 / 二级），本次未改动"
        if role == "标签":
            role = "一级标签" if lv == "one" else "二级标签"
    hit, cands = _tags.find_tag(index, want, level=(1 if lv == "one" else 2) if lv else None)
    if hit is None:
        # 同名都在同一层（两个二级挂在不同父下）时，"说明是一级还是二级"帮不上忙
        # ——所以再给一条**能落下的**指认方式：用展示名（`父 / 子`）。这正是下面
        # 候选名单写出来的形态，用户照着念、planner 照着填都能对上。
        want_lv = (1 if lv == "one" else 2) if lv else None
        by_label = [t for t in index.values()
                    if t.label == want and (want_lv is None or t.level == want_lv)]
        if len(by_label) == 1:
            return by_label[0], None
    if hit is not None:
        return hit, None
    if cands:
        return None, (f"站内有 {len(cands)} 个叫「{want}」的{role}（"
                      + "、".join(c.label + f"（id={c.id}）" for c in cands)
                      + "）：无法确定要动的是哪一个，本次未改动——"
                        "请用「父 / 子」这样的全名指认它（或说明是一级还是二级）")
    near = _base._near_miss_names(want, [(t.id, t.label) for t in index.values()])
    if near:
        # 近失（20260925）：没有**同名**的，但名字接近——把候选摆出来请主人点名，
        # 而不是丢一句"站内没有"（那句话在主人的视角里是假的：他记得站内有）。
        return None, (f"站内没有叫「{want}」的{role}（完全同名的一个都没有）；"
                      + "名字最接近的是 "
                      + "、".join(f"{nm}（id={cid}）" for cid, nm in near)
                      + "：本次未改动——若就是其中一个，请照它的**完整名字**再说一遍，"
                        "我按那个名字动手")
    return None, f"站内没有叫「{want}」的{role}，本次未改动"



@tool
def update_tag(
    name: Annotated[str, "要改的那个标签的**名字**（站内已有的标签，如「Asyncio」）"],
    config: RunnableConfig,
    new_title: Annotated[str | None, "改成什么名字；不改名就不填"] = None,
    color: Annotated[str | None,
                     "改成什么颜色（中文色名如「粉色」，或站内色板色值）；不改颜色就不填"] = None,
    parent_tag: Annotated[str | None,
                          "把这个标签挪到哪个**一级标签**下面（写它的名字，如「编程」）；"
                          "不动位置就不填"] = None,
    to_level: Annotated[str | None,
                        "要把这个标签改成一级还是二级（one/two）；不改层级就不填。"
                        "改成二级（two）时必须同时给出 parent_tag"] = None,
    level: Annotated[str | None,
                     "这个标签现在是几级（one/two）；站内同名标签不止一个时用它指认"] = None,
) -> str:
    """修改一个已有标签：改名 / 改颜色 / 换父级 / 一级↔二级互转（可同时改几样）。
    **只动你点名的那几样**，没点名的保持不动。需要管理员身份。

    换父级与换层级走一条原子接口：**换父级时标签 id 与文章引用都不变**（可逆）；
    只有跨表（一级↔二级）且旧 id 在新表里已被占用时才会换 id，那种情况下所有引用
    过它的文章会被同步改写（返回里会说清楚）。"""
    from agent import adminops_tags as _tags
    # 目标与父标签用**同一份**字典快照解析：两次读之间标签可能被挪走/改名，
    # 拿两个时刻的数据拼一个请求是自找的错；顺带少一次网络往返。
    index = _base._tag_index(config)
    hit, err = _find_named_tag(name, config, level, index=index)
    if err:
        return unavailable(err)

    new_title = str(new_title or "").strip()
    if new_title and len(new_title) > 40:
        return unavailable(f"新标签名过长（{len(new_title)} 字，上限 40），本次未改动")
    color_spec = str(color or "").strip()
    picked = _tags.match_tag_color(color_spec) if color_spec else None
    if color_spec and picked is None:
        return unavailable(f"颜色「{color_spec}」不在站内色板里（可选：{_tags.TAG_COLOR_SPEC}），"
                           f"本次未改动")

    cur = _tags.level_of(hit)
    tl = None
    if str(to_level or "").strip():
        tl = _tags.normalize_level(to_level)
        if tl is None:
            return unavailable(f"层级「{to_level}」认不出来（只支持一级 / 二级），本次未改动")
    pname = str(parent_tag or "").strip()
    father = hit.father_id
    target = cur
    if pname:
        if tl == "one":
            return unavailable(f"既说了挪到「{pname}」下面、又说了改成一级标签——"
                               f"一级标签没有父标签，这两个说法互相矛盾，本次未改动")
        parent, perr = _find_named_tag(pname, config, "one", role="一级标签", index=index)
        if perr:
            return unavailable(perr)
        if parent.id == hit.id:
            # 自环：后端会硬拒（CASCADE 会把刚插入的行一起删掉，标签彻底消失），
            # 这里先拦一道，报错更直白。
            return unavailable("不能把标签挂到它自己下面，本次未改动")
        father, target = parent.id, "two"
    elif tl:
        target = tl
        if tl == "two" and cur == "one":
            return unavailable("要改成二级标签就必须给出它挂在哪个一级标签下"
                               "（parent_tag 填那个一级标签的名字），本次未改动")
        if tl == "one":
            father = None
    moves = (target != cur) or (target == "two" and father != hit.father_id)
    if not new_title and not picked and not moves:
        return unavailable("没有指出要改什么（名字 / 颜色 / 父标签 / 层级），本次未改动")

    if moves:
        payload = {"level": cur, "id": hit.id}
        if target != cur:
            payload["toLevel"] = target
        if target == "two" and father is not None:
            payload["fatherTag"] = father
        if new_title:
            payload["title"] = new_title
        if picked:
            payload["color"] = picked
        data = _base._admin_request("POST", "/api/protected/tag/move", payload, config)
        if isinstance(data, ToolResult):
            return data
        res = data if isinstance(data, dict) else {}
        new_id = res.get("toId")
        if not isinstance(new_id, int) or new_id <= 0:
            return unavailable(f"后台没有返回标签移动后的 id（返回 {data!r}），"
                               f"本次改动未确认生效——请到后台标签页核对后再决定是否重试")
        after = _base._tag_index(config)
        if after is None:
            return unavailable(f"标签可能已经改好（后台返回 id={new_id}），但读不回标签字典、"
                               f"无法确认——请到后台标签页核对后再决定是否重试")
        got = after.get(new_id)
        if got is None:
            return unavailable(f"改动请求已发出，但读回标签字典里找不到 id={new_id} 的行，"
                               f"本次改动未确认生效")
        mismatch = []
        if target == "two" and got.father_id != father:
            mismatch.append("父标签")
        if got.level != (2 if target == "two" else 1):
            mismatch.append("层级")
        if new_title and got.name != new_title:
            mismatch.append("名字")
        before_label = hit.label
        if mismatch:
            return unavailable(f"改动请求已发出，但读回标签「{got.label}」的"
                               f"{'、'.join(mismatch)}与预期不一致，本次改动未确认生效")
        return ok(_tags.render_tag_moved(before_label, got.label, _tags.move_impact(res)),
                  meta=fact("tag_update", changed=True, target=tgt("tag", got.id, got.name),
                            before=before_label, after=got.label, evidence=got.label,
                            tag_id=got.id, tag_name=got.name, level=got.level))

    # 同层改名 / 改色：PUT /tagone|tagtwo/:id。**两个字段都是必填**，所以必须把
    # 当前颜色原样回传（不猜、也不许传空——传空等于把这个标签的颜色抹掉）。
    if not picked and not hit.color:
        return unavailable("读不到这个标签现在的颜色，而改名接口要求同时提交颜色；"
                           "为避免把它的颜色抹掉，本次未改动")
    payload = {"title": new_title or hit.name, "color": picked or hit.color}
    path = ("/api/protected/tagone/" if cur == "one" else "/api/protected/tagtwo/")
    data = _base._admin_request("PUT", f"{path}{hit.id}", payload, config)
    if isinstance(data, ToolResult):
        return data
    after = _base._tag_index(config)
    if after is None:
        return unavailable("改动请求已发出，但读不回标签字典、无法确认，请到后台标签页核对")
    got = after.get(hit.id)
    if got is None:
        return unavailable(f"改动请求已发出，但读回标签字典里找不到 id={hit.id} 的行，"
                           f"本次改动未确认生效")
    want_color = picked or hit.color
    if got.name != payload["title"] or got.color != want_color:
        return unavailable(f"改动请求已发出，但读回标签「{got.label}」的值与预期不一致"
                           f"（名字或颜色没落库），本次改动未确认生效")
    pairs = []
    if new_title:
        pairs.append((hit.name, got.name))
    if picked:
        pairs.append((_tags.describe_color(hit.color) if hit.color else "（未知）",
                      _tags.describe_color(got.color) if got.color else "（未知）"))
    before_s, after_s = _tags.render_change(pairs) if pairs else (hit.label, got.label)
    return ok(_tags.render_tag_updated(got.label, before_s, after_s),
              meta=fact("tag_update", changed=True, target=tgt("tag", got.id, got.name),
                        before=before_s, after=after_s, evidence=got.label,
                        tag_id=got.id, tag_name=got.name, level=got.level))



@tool
def delete_tag(
    name: Annotated[str, "要删掉的那个标签的**名字**（站内已有的标签）"],
    config: RunnableConfig,
    level: Annotated[str | None,
                     "这个标签是几级（one/two）；站内同名标签不止一个时用它指认"] = None,
) -> str:
    """删除一个文章标签（一级或二级都可以）。需要管理员身份。

    ⚠️ **删除不可撤销**：删一级标签会连带删掉它下面的所有二级标签，并把这些标签
    从**所有文章**上摘掉（文章本身不会被删）。要动的标签名对不上就什么都不做。"""
    from agent import adminops_tags as _tags
    # 子标签名单取自**解析目标的那一份快照**（同一次读）：删一级时会连坐子标签，
    # 名单要是从第二次读里取，两次读之间新建的子标签就会在回执里凭空消失
    # ——而它已经被 CASCADE 删掉了。
    index = _base._tag_index(config)
    hit, err = _find_named_tag(name, config, level, index=index)
    if err:
        return unavailable(err)
    hits_before = {hit.id}
    kids = _tags.children_of(index or {}, hit.id) if hit.level == 1 else []
    hits_before |= {k.id for k in kids}

    payload = {"level": _tags.level_of(hit), "ids": [hit.id]}
    data = _base._admin_request("DELETE", "/api/protected/tag", payload, config)
    if isinstance(data, ToolResult):
        return data

    after = _base._tag_index(config)
    if after is None:
        return unavailable("删除请求已发出，但读不回标签字典、无法确认它是否真的删掉了"
                           "——请到后台标签页核对")
    left = [i for i in sorted(hits_before) if i in after]
    if left:
        return unavailable(f"删除请求已发出，但读回标签字典里 id={left[0]} 还在"
                           f"（共 {len(left)} 个没删掉），本次改动未确认生效")
    if hit.level == 1 and kids:
        extra = f"它下面的 {len(kids)} 个二级标签（{'、'.join(k.name for k in kids)}）已一并删除"
    elif hit.note_count is not None:
        extra = f"它原本挂在 {hit.note_count} 篇文章上，这些引用已一并摘掉（文章本身没删）"
    else:
        extra = ""
    return ok(_tags.render_tag_deleted(hit.label, extra),
              meta=fact("tag_delete", changed=True, target=tgt("tag", hit.id, hit.name),
                        before="已存在", after="已删除", change=extra or "已删除",
                        tag_id=hit.id, tag_name=hit.name, level=hit.level))
