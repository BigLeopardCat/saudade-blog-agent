# -*- coding: utf-8 -*-
"""标签/分类写能力（20260921 第三轮）单测：纯函数 + 假边界，秒级、零网络、零 LLM。

被测的是这一轮新增的五条写通道（`update_tag` / `delete_tag` / `create_category` /
`update_category` / `delete_category`）与它们的共同地基——**名字通道**：

  planner 只会写名字（用户嘴里说的就是名字，跨轮执行记忆里也只有名字没有 id），
  工具在 execute 阶段用活字典把名字确定性换成 id + 层级。于是本文件的主要断言对象是
  名字解析的**四种结局**与"说不清就不动手"这条纪律：

    唯一命中 → 动手；  命中多个（同名二级挂在不同父下）→ 追问，不替用户挑；
    一个都没有 → 如实说没有，绝不新建也不模糊匹配；  字典读不到 → 单独一种说法
    （"读不到" ≠ "没有"，把前者说成后者会让管理员以为标签真被删了）。

另外三组锁：

  1. **写通道只有 `_admin_request` 一个出口**（POST/PUT/DELETE 都走它），且形态正确
     ——`PUT /tagone|tagtwo/:id` 的两个字段都是必填（改名必须把当前色原样回传）、
     `DELETE /tag` 的 body 是 `{level, ids}`、`DELETE /category` 的 body 是**裸数组**；
  2. **发出去了 ≠ 做成了**：每个写都回读复核，复核不上就是 unavailable（checker 判
     BLOCK、不进跨轮执行记忆），措辞里带「本次改动未确认生效」；
  3. **空操作不许算成功**（"没说要改什么"要拒绝），以及删除/移动的**影响面播报**
     必须出现在弹窗与回执里（删一级标签连坐子标签、删分类让文章变成没有分类——
     这两句是用户点"确定"之前唯一能看到的后果）。
"""
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import adminops as A  # noqa: E402
import tools.base as base  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


@contextlib.contextmanager
def patch(**kw):
    """临时替换 tools/base 的模块级函数（工具调用时按模块全局名解析，故替换生效）。"""
    saved = {k: getattr(base, k) for k in kw}
    for k, v in kw.items():
        setattr(base, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(base, k, v)


class _Seq:
    """按序返回的桩（写前读 / 写后复核各一次）。"""

    def __init__(self, *vals):
        self.vals = list(vals)
        self.n = 0

    def __call__(self, *a, **k):
        v = self.vals[min(self.n, len(self.vals) - 1)]
        self.n += 1
        return v


class _Req:
    """记录 `_admin_request` 调用（方法 + 路径 + 载荷）并返回预设值。"""

    def __init__(self, ret):
        self.ret = ret
        self.calls: list = []

    def __call__(self, method, path, payload, config):
        self.calls.append((method, path, payload))
        return self.ret


class _Post:
    """记录 `_admin_post` 调用（路径 + 载荷）——create/update_category 走的是它。"""

    def __init__(self, ret):
        self.ret = ret
        self.calls: list = []

    def __call__(self, path, payload, config):
        self.calls.append((path, payload))
        return self.ret


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class _Client:
    """桩 httpx 客户端：写通道泛化成 `request` 后，桩也要记 method。"""

    def __init__(self, resp=None):
        self.calls = []
        self.resp = resp or _Resp(200, {"code": 200, "data": "ok"})

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.calls.append((method, url, payload_key(json)))
        return self.resp


def payload_key(payload):
    """只留"载荷是什么形状"这个关键差异（dict → 键集合；list → 类型）。"""
    if isinstance(payload, dict):
        return tuple(sorted(payload))
    return type(payload).__name__


# ── 标签字典样本（形态抄自 /api/tagone + /api/tagtwo）──────────────────
ONE = [{"tagKey": 1, "title": "编程", "level": 1, "color": "#1677ff", "noteCount": 5},
       {"tagKey": 2, "title": "架构", "level": 1, "color": "#fa8c16", "noteCount": 4}]
TWO = [{"tagKey": 10000, "title": "Python", "level": 2, "fatherTag": "编程",
        "fatherKey": 1, "color": "#52c41a", "noteCount": 3},
       {"tagKey": 10001, "title": "Asyncio", "level": 2, "fatherTag": "编程",
        "fatherKey": 1, "color": "#eb2f96"},
       # 同名二级挂在**另一个**父下：制造"命中多个"（歧义）这个真实形态
       # 这一条**没有 color 字段**（接口不给色是真会发生的形态）——改名要回传色值时，
       # 它就必须被拒绝而不是拿空值把颜色抹掉
       {"tagKey": 10002, "title": "Asyncio", "level": 2, "fatherTag": "架构",
        "fatherKey": 2, "noteCount": 1},
       {"tagKey": 10003, "title": "分布式", "level": 2, "fatherTag": "架构",
        "fatherKey": 2, "color": "#13c2c2"}]
IDX = A.build_tag_index(ONE, TWO)

CATS = [{"categoryKey": 3, "categoryTitle": "随笔", "pathName": "essay",
         "introduce": "随手写的", "icon": "book", "color": "#1677ff", "noteCount": 4},
        {"categoryKey": 7, "categoryTitle": "技术", "pathName": "tech",
         "introduce": "", "icon": "", "color": "", "noteCount": 9}]
CIDX = A.build_category_index(CATS)


def cfg(uid=7, role="admin"):
    """工具配置（写通道的 uid 与角色都从它取）。"""
    from agent.principal import Principal
    return {"configurable": {"user_id": uid, "principal": Principal(uid=uid, role=role)}}


def cidx(*extra):
    """分类字典 + 追加几行（写后复核用）。"""
    return A.build_category_index(CATS + list(extra))


# ══════════════════════════════════════════════════════════════════
print("① 名字通道：四种结局各说各的话（这是本轮所有写工具的共同地基）")

hit, err = base._find_named_tag("编程", "unused") if False else (None, None)  # 占位，见下
with patch(_tag_index=lambda c: IDX):
    got, err = base._find_named_tag("Python", None)
    check("唯一命中 → 给 TagInfo（id/层级/父都解析出来了）",
          err is None and got is not None and got.id == 10000 and got.level == 2
          and got.father_id == 1, f"{got} / {err}")

    got, err = base._find_named_tag("没有这个标签", None)
    check("一个都没有 → 如实说站内没有，**绝不新建/模糊匹配**",
          got is None and "站内没有叫「没有这个标签」的标签" in err
          and "本次未改动" in err, str(err))

    got, err = base._find_named_tag("Asyncio", None)
    check("命中多个（不同父下同名二级）→ 追问并列候选，**不替用户挑一个**",
          got is None and "2 个叫「Asyncio」" in err and "编程 / Asyncio" in err
          and "架构 / Asyncio" in err, str(err))
    check("  追问里给了消歧的路（说明是一级还是二级）", "一级还是二级" in err, str(err))

    got, err = base._find_named_tag("编程 / Asyncio", None)
    check("同名都在二级时给出**能落下的**指认方式：用「父 / 子」全名 → 唯一命中",
          err is None and got is not None and got.id == 10001, f"{got} / {err}")

    got, err = base._find_named_tag("Asyncio", None, level="one")
    check("planner 说清层级 → 该层无此名 ⇒ 同一句「没有」但限定在一级里找",
          got is None and "一级标签" in err, str(err))
    got, err = base._find_named_tag("Python", None, level="one")
    check("  层级说错（Python 是二级）→ 也如实说一级里没有，不去二级凑一个",
          got is None and "一级标签" in err, str(err))

    got, err = base._find_named_tag("Python", None, level="二级")
    check("层级词表收中文（planner 写「二级」也认）", err is None and got.id == 10000,
          str(err))

    got, err = base._find_named_tag("Python", None, level="三级")
    check("认不出的层级 → 拒绝（不当作「没给」悄悄放宽）",
          got is None and "认不出来" in err, str(err))

    got, err = base._find_named_tag("   ", None)
    check("空名字 → 拒绝", got is None and "标签名为空" in err, str(err))

    got, err = base._find_named_tag("编程", None, role="一级标签")
    check("role 只改措辞（找父标签时说「一级标签」，用户才知道该给什么）",
          err is None and got.id == 1, str(err))

with patch(_tag_index=lambda c: None):
    got, err = base._find_named_tag("编程", None)
    check("字典读不到 → **单独一种说法**（「读不到」≠「没有」：说成没有会让管理员以为标签真没了）",
          got is None and "读不到现有的标签字典" in err, str(err))


print("\n② update_tag：改名走 PUT（当前色必须原样回传）、换位置走移动端点")

post = _Req("done")
seq = _Seq(IDX, A.build_tag_index(ONE, TWO[:1] + [
    {"tagKey": 10001, "title": "AsyncIO", "level": 2, "fatherTag": "编程",
     "fatherKey": 1, "color": "#eb2f96"}] + TWO[2:]))
with patch(_tag_index=seq, _admin_request=post):
    r = base.update_tag.invoke({"name": "编程 / Asyncio", "new_title": "AsyncIO"},
                               config=None)
    check("改名（同层）→ PUT /tagtwo/10001，且**把当前色原样回传**（改名接口两个字段都必填）",
          post.calls == [("PUT", "/api/protected/tagtwo/10001",
                          {"title": "AsyncIO", "color": "#eb2f96"})], str(post.calls))
    check("  ok + 回执写清「旧 → 新」（narrator 照抄这句，不能只说「改好了」）",
          r.kind == "ok" and "已修改标签「编程 / AsyncIO」：Asyncio → AsyncIO" in r,
          f"{r.kind}: {r}")
    check("  meta 带 op/before/after/id/name/level（跨语言回执契约的键）",
          r.meta.get("op") == "tag_update" and r.meta.get("tag_id") == 10001
          and r.meta.get("name") is None and r.meta.get("tag_name") == "AsyncIO"
          and r.meta.get("before") == "Asyncio", str(r.meta))

post = _Req("done")
with patch(_tag_index=lambda c: IDX, _admin_request=post):
    # 10001 这件没给 color（接口没给色）→ 改名要回传色值，读不到就只能拒绝
    post.calls.clear()
    r = base.update_tag.invoke({"name": "架构 / Asyncio", "new_title": "AsyncIO"},
                               config=None)
    check("当前色读不到 + 要改名 → 拒绝，零请求（传空 = 把这个标签的颜色抹掉）",
          r.kind == "unavailable" and post.calls == [] and "颜色" in r,
          f"{r.kind}: {r}")

    post.calls.clear()
    r = base.update_tag.invoke({"name": "编程"}, config=None)
    check("什么都没说要改 → 拒绝（空操作不算成功）",
          r.kind == "unavailable" and "没有指出要改什么" in r and post.calls == [], str(r))

    post.calls.clear()
    r = base.update_tag.invoke({"name": "编程", "to_level": "two"}, config=None)
    check("改成二级却没给父 → 拒绝（二级必须有爸爸）",
          r.kind == "unavailable" and "必须给出它挂在哪个一级标签下" in r, str(r))

    post.calls.clear()
    r = base.update_tag.invoke({"name": "编程", "parent_tag": "架构", "to_level": "one"},
                               config=None)
    check("「挪到架构下面」+「改成一级」自相矛盾 → 拒绝（一级没有父）",
          r.kind == "unavailable" and "互相矛盾" in r, str(r))

    post.calls.clear()
    r = base.update_tag.invoke({"name": "编程", "parent_tag": "编程"}, config=None)
    check("自环（挂到自己下面）→ 拒绝（后端 CASCADE 会把刚插入的行一起删掉）",
          r.kind == "unavailable" and "挂到它自己下面" in r, str(r))

    post.calls.clear()
    r = base.update_tag.invoke({"name": "编程", "parent_tag": "没有这个"}, config=None)
    check("父标签名对不上 → 拒绝且措辞点明是「一级标签」（否则 planner 会去改一个本来就对的名字）",
          r.kind == "unavailable" and "一级标签" in r and post.calls == [], str(r))

    post.calls.clear()
    r = base.update_tag.invoke({"name": "Python", "color": "天蓝"}, config=None)
    check("认不出的颜色 → 拒绝（不静默回落成别的色）",
          r.kind == "unavailable" and "色板" in r, str(r))

post = _Req({"fromLevel": "two", "fromId": 10000, "toLevel": "two", "toId": 10000,
             "idChanged": False, "fatherKey": 2, "fatherTitle": "架构",
             "rewrittenNotes": 0, "warnings": []})
moved = A.build_tag_index(ONE, [{"tagKey": 10000, "title": "Python", "level": 2,
                                 "fatherTag": "架构", "fatherKey": 2,
                                 "color": "#52c41a", "noteCount": 3}] + TWO[1:])
with patch(_tag_index=_Seq(IDX, moved), _admin_request=post):
    r = base.update_tag.invoke({"name": "Python", "parent_tag": "架构"}, config=None)
    check("换父级 → POST /tag/move，载荷 {level, id, fatherTag}（**不给 toLevel**：同层）",
          post.calls == [("POST", "/api/protected/tag/move",
                          {"level": "two", "id": 10000, "fatherTag": 2})],
          str(post.calls))
    check("  ok 且前后展示名都写出来（「已移动」不说移去哪，等于让用户自己回去核对）",
          r.kind == "ok" and "编程 / Python" in r and "架构 / Python" in r,
          f"{r.kind}: {r}")
    check("  meta before/after 是两端的展示名", r.meta.get("before") == "编程 / Python"
          and r.meta.get("after") == "架构 / Python", str(r.meta))

post = _Req({"fromLevel": "one", "fromId": 1, "toLevel": "two", "toId": 1,
             "idChanged": True, "fatherKey": 2, "fatherTitle": "架构",
             "rewrittenNotes": 7, "warnings": ["目标下已有同名标签"]})
bad = A.build_tag_index(ONE, [{"tagKey": 1, "title": "编程", "level": 2,
                               "fatherTag": "架构", "fatherKey": 2, "color": "#1677ff"}])
with patch(_tag_index=_Seq(IDX, bad), _admin_request=post):
    r = base.update_tag.invoke({"name": "编程", "to_level": "two", "parent_tag": "架构"},
                               config=None)
    check("跨表移动（id 变了）→ 影响面必须报出来：id 由几变几、几篇文章的引用被改写",
          r.kind == "ok" and "id 由 1 变成 1" in r and "7 篇文章上的引用已同步改写" in r,
          f"{r.kind}: {r}")
    check("  warnings 原样透出（「做成了，但有话要说」不能吞）",
          "目标下已有同名标签" in r, str(r))

with patch(_tag_index=_Seq(IDX, IDX), _admin_request=_Req({"toId": None})):
    r = base.update_tag.invoke({"name": "Python", "parent_tag": "架构"}, config=None)
    check("端点没回 toId → unavailable，措辞带「本次改动未确认生效」+ 让人去后台核对",
          r.kind == "unavailable" and "本次改动未确认生效" in r and "核对" in r,
          f"{r.kind}: {r}")

with patch(_tag_index=_Seq(IDX, IDX),
           _admin_request=_Req({"fromLevel": "two", "fromId": 10000, "toLevel": "two",
                                "toId": 10000, "idChanged": False, "fatherKey": 1,
                                "warnings": []})):
    r = base.update_tag.invoke({"name": "Python", "parent_tag": "架构"}, config=None)
    check("回读到父标签**还是旧的** → unavailable（发出去了不算做成了）",
          r.kind == "unavailable" and "与预期不一致" in r, f"{r.kind}: {r}")

with patch(_tag_index=_Seq(IDX, None), _admin_request=_Req({"toId": 10000})):
    r = base.update_tag.invoke({"name": "Python", "parent_tag": "架构"}, config=None)
    check("移动后台返回了新 id、但字典读不回 → 如实说「可能已经改好但无法确认」（不谎报成功）",
          r.kind == "unavailable" and "可能已经改好" in r
          and "本次改动未确认生效" not in r.replace("可能已经改好", ""), f"{r.kind}: {r}")


print("\n③ delete_tag：删除不可撤销，影响面必须说全（子标签连坐 / 摘掉多少篇文章）")

post = _Req("1")
with patch(_tag_index=_Seq(IDX, A.build_tag_index(ONE, [TWO[0]] + TWO[2:])),
           _admin_request=post):
    r = base.delete_tag.invoke({"name": "编程 / Asyncio"}, config=None)
    check("删二级 → DELETE /api/protected/tag，body {level,ids}（层级用接口词表 one/two）",
          post.calls == [("DELETE", "/api/protected/tag",
                          {"level": "two", "ids": [10001]})], str(post.calls))
    check("  ok 且报出「从 N 篇文章上摘掉」（引用被摘是不可逆的事，必须让用户知道）",
          r.kind == "ok" and "已删除标签「编程 / Asyncio」" in r and "0 篇" not in r,
          f"{r.kind}: {r}")

post = _Req("1")
after_all_gone = A.build_tag_index([ONE[1]], [TWO[2], TWO[3]])
with patch(_tag_index=_Seq(IDX, after_all_gone), _admin_request=post):
    r = base.delete_tag.invoke({"name": "编程", "level": "one"}, config=None)
    check("删一级（下面有 2 个二级）→ 回执写明连坐删除的**名单**",
          r.kind == "ok" and "2 个二级标签（Python、Asyncio）已一并删除" in r,
          f"{r.kind}: {r}")
    check("  body 只送点名的那个 id（子标签由后端 CASCADE，不是我们逐条删）",
          post.calls[0][2] == {"level": "one", "ids": [1]}, str(post.calls))

with patch(_tag_index=_Seq(IDX, IDX), _admin_request=_Req("1")):
    r = base.delete_tag.invoke({"name": "Python", "level": "two"}, config=None)
    check("回读发现它**还在** → unavailable（删了但没删掉，绝不能报成功）",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

with patch(_tag_index=_Seq(IDX, None), _admin_request=_Req("1")):
    r = base.delete_tag.invoke({"name": "Python", "level": "two"}, config=None)
    check("删完读不回字典 → unavailable + 让人去后台核对（不可逆的操作宁可让人看一眼）",
          r.kind == "unavailable" and "核对" in r, f"{r.kind}: {r}")

with patch(_tag_index=lambda c: IDX, _admin_request=_Req("1")):
    r = base.delete_tag.invoke({"name": "Asyncio"}, config=None)
    check("同名二级两个、又没说层级 → 拒绝（删错一个就是删错数据）",
          r.kind == "unavailable" and "无法确定要动的是哪一个" in r, f"{r.kind}: {r}")

    r = base.delete_tag.invoke({"name": "编程"}, config=None)
    check("不给层级但唯一命中 → 照常放行（唯一命中不需要追问）",
          r.kind in ("ok", "unavailable") and "无法确定" not in r, f"{r.kind}: {r}")


print("\n④ 分类：一张平表、名字即身份（重名不唯一约束 ⇒ 重名要追问）")

cpost = _Post("Category created")
with patch(_category_index=_Seq(CIDX, cidx({"categoryKey": 11, "categoryTitle": "读书",
                                            "pathName": "read", "noteCount": 0})),
           _admin_post=cpost):
    r = base.create_category.invoke({"title": "读书", "path_name": "read"}, config=None)
    check("新建分类 → POST /api/protected/category，字段是 categoryTitle/pathName",
          cpost.calls == [("/api/protected/category",
                           {"categoryTitle": "读书", "pathName": "read"})],
          str(cpost.calls))
    check("  ok 且**不报篇数**（刚建的分类必然是 0 篇，报出来是把 0 当影响面）+ 不带工具名",
          r.kind == "ok" and "已新建分类「读书」" in r and "篇文章" not in r
          and "0 篇" not in r and "create_category" not in r, f"{r.kind}: {r}")

with patch(_category_index=_Seq(CIDX, cidx({"categoryKey": 11, "categoryTitle": "读书"},
                                           {"categoryKey": 12, "categoryTitle": "别的"})),
           _admin_post=cpost):
    r = base.create_category.invoke({"title": "读书"}, config=None)
    check("读回多出**两**行 → 说不清哪行是我的 ⇒ unavailable（不蒙一个当成功）",
          r.kind == "unavailable" and "找不到名字为「读书」的新行" in r, f"{r.kind}: {r}")

cpost2 = _Post("Category created")
with patch(_category_index=_Seq(CIDX, CIDX), _admin_post=cpost2):
    r = base.create_category.invoke({"title": "随笔"}, config=None)
    check("同名分类已存在 → 拒绝，零请求（分类无唯一约束，重名会让以后按名字找全变歧义）",
          r.kind == "unavailable" and "已经有叫「随笔」的分类" in r and cpost2.calls == [],
          f"{r.kind}: {r}")

    r = base.create_category.invoke({"title": "读书", "color": "天蓝"}, config=None)
    check("认不出的颜色 → 拒绝（分类色走宽口径 6 位 hex，但认不出就是不认）",
          r.kind == "unavailable" and "认不出来" in r, f"{r.kind}: {r}")

with patch(_category_index=lambda c: None, _admin_post=_Post("x")):
    r = base.create_category.invoke({"title": "读书"}, config=None)
    check("读不到分类列表 → 拒绝（不冒着重名风险建）",
          r.kind == "unavailable" and "读不到现有的分类列表" in r, f"{r.kind}: {r}")

upost = _Post("Updated")
with patch(_category_index=_Seq(CIDX, cidx({"categoryKey": 3, "categoryTitle": "随笔集",
                                            "pathName": "essay"})),
           _admin_post=upost):
    r = base.update_category.invoke({"name": "随笔", "new_title": "随笔集"}, config=None)
    check("改分类 → POST /api/protected/category/3，**只发点名的字段**（其余字段后端当空=不改）",
          upost.calls == [("/api/protected/category/3", {"categoryTitle": "随笔集"})],
          str(upost.calls))
    check("  ok 且写清旧 → 新", r.kind == "ok" and "随笔 → 随笔集" in r, f"{r.kind}: {r}")

with patch(_category_index=_Seq(CIDX, CIDX), _admin_post=_Post("Updated")):
    r = base.update_category.invoke({"name": "随笔", "new_title": "随笔集"}, config=None)
    check("回读仍是旧值 → unavailable（那个「Updated」字符串不算数）",
          r.kind == "unavailable" and "仍是旧值" in r, f"{r.kind}: {r}")

    r = base.update_category.invoke({"name": "随笔"}, config=None)
    check("没说要改什么 → 拒绝（且说明「只能改不能清空」这条接口限制）",
          r.kind == "unavailable" and "没有指出要改什么" in r, f"{r.kind}: {r}")

dpost = _Req("1")
with patch(_category_index=_Seq(CIDX, A.build_category_index(CATS[1:])),
           _admin_request=dpost):
    r = base.delete_category.invoke({"name": "随笔"}, config=None)
    check("删分类 → DELETE /api/protected/category，body 是**裸数组**（与其他写端点不同形）",
          dpost.calls == [("DELETE", "/api/protected/category", [3])], str(dpost.calls))
    check("  ok 且报「4 篇文章已变成没有分类」（FK 是 SET NULL：文章不会删，但会失去分类）",
          r.kind == "ok" and "4 篇文章已变成没有分类" in r, f"{r.kind}: {r}")
    check("  meta change 也带上同一句（回执落库后 narrator 才有据可说）",
          "4 篇文章" in str(r.meta.get("change")), str(r.meta))

with patch(_category_index=_Seq(CIDX, CIDX), _admin_request=_Req("1")):
    r = base.delete_category.invoke({"name": "随笔"}, config=None)
    check("回读发现分类还在 → unavailable", r.kind == "unavailable" and "未确认生效" in r,
          f"{r.kind}: {r}")


print("\n⑤ 写通道只有一个出口：方法/路径/载荷形态（谁都不能绕开 _admin_request）")

real_client = base._client
try:
    for method, path, payload, want in [
            ("POST", "/api/protected/tag/move", {"level": "one", "id": 1}, "POST"),
            ("PUT", "/api/protected/tagone/1", {"title": "X", "color": "#eb2f96"}, "PUT"),
            ("DELETE", "/api/protected/tag", {"level": "one", "ids": [1]}, "DELETE")]:
        c = _Client(_Resp(200, {"code": 200, "data": "ok"}))
        base._client = c
        base._admin_request(method, path, payload, cfg())
        got_method, got_url, keys = c.calls[0]
        check(f"{want} {path} → 方法/路径原样（载荷键 {keys}）",
              got_method == want and got_url == base.ADMIN_BASE + path, str(c.calls))
    c = _Client(_Resp(200, {"code": 200, "data": "ok"}))
    base._client = c
    base._admin_request("DELETE", "/api/protected/category", [3], cfg())
    check("裸数组载荷原样透传（写通道不假设 body 一定是 dict）",
          c.calls[0][2] == "list", str(c.calls))
    base._client = _Client(_Resp(403, {"code": 403}))
    r = base._admin_request("PUT", "/api/protected/tagone/1", {}, cfg())
    check("403 → unavailable 且措辞是「无权」不是「故障」（与 _admin_post 同族）",
          r.kind == "unavailable" and "无权" in r, f"{r.kind}: {r}")
finally:
    base._client = real_client


print("\n⑥ 弹窗问句：用户点「确定」之前必须看得出后果（盲签是本轮最贵的错）")

q = A.render_confirm_question([{"tool": "update_tag",
                                "args": {"name": "Python", "new_title": "AsyncIO",
                                         "parent_tag": "架构"}}], IDX)
check("改标签：点名标签 + 改什么 + 移到哪（还能看到它现在挂在几篇文章上）",
      "修改标签「编程 / Python」" in q and "改名为「AsyncIO」" in q
      and "移到「架构」下面" in q and "3 篇文章" in q, q)

q = A.render_confirm_question([{"tool": "delete_tag", "args": {"name": "编程"}}], IDX)
check("删一级标签：**子标签连坐**写在问句里，且点名是谁",
      "删除一级标签「编程」" in q and "2 个二级标签" in q and "Python" in q
      and "会一起删除" in q, q)

q = A.render_confirm_question([{"tool": "delete_tag", "args": {"name": "Python"}}], IDX)
check("删二级标签：写清「从 N 篇文章上摘掉」", "删除二级标签「编程 / Python」" in q
      and "从 3 篇文章上摘掉" in q, q)

q = A.render_confirm_question([{"tool": "delete_tag", "args": {"name": "没有这个"}}], IDX)
check("标签名对不上 → 问句当场写出来（点确定之前就该知道这个名字不存在）",
      "标签字典里没有这个名字" in q, q)

q = A.render_confirm_question([{"tool": "delete_category", "args": {"name": "随笔"}}], None, CIDX)
check("删分类：「文章会变成没有分类」必须写出来（「删分类」听着像连文章一起删）",
      "删除分类「随笔」" in q and "4 篇文章" in q and "变成没有分类" in q, q)

q = A.render_confirm_question([{"tool": "create_category",
                                "args": {"title": "读书", "path_name": "read"}}], None, CIDX)
check("建分类：点名名字与路径", "新建分类「读书」" in q and "路径 read" in q, q)

q = A.render_confirm_question([{"tool": "update_category",
                                "args": {"name": "随笔", "color": "#eb2f96"}}], None, CIDX)
check("改分类：只写点名的字段", "修改分类「随笔」" in q and "颜色改为「#eb2f96」" in q, q)

t = A.render_confirm_text([{"tool": "delete_tag", "args": {"name": "编程"}}], IDX)
check("气泡正文与问句同源（同一段确定性文本，不存在两种说法）", "会一起删除" in t, t)
check("读不到字典也不炸（退化不改戏）",
      "删除标签「没有这个」" in A.render_confirm_question(
          [{"tool": "delete_tag", "args": {"name": "没有这个"}}]))

for spec in [{"tool": "update_tag", "args": {}}, {"tool": "delete_category", "args": {}},
             {"tool": "create_category", "args": {}}]:
    q = A.render_confirm_question([spec], IDX, CIDX)
    check(f"字段缺失不炸也不编名字：{spec['tool']}", "（未命名）" in q, q)


print("\n⑦ 后端回执行（Rust `render_exec_row`）读的键必须真的落进 meta（跨语言契约）")

with patch(_tag_index=_Seq(IDX, A.build_tag_index(ONE, [TWO[0], TWO[1], TWO[2]])),
           _admin_request=_Req("1")):
    r = base.delete_tag.invoke({"name": "分布式", "level": "two"}, config=None)
    check("delete_tag 的 meta 带 change（Rust 渲染删除行读它；缺了就只剩「删除标签 X」）",
          r.kind == "ok" and str(r.meta.get("change") or "") != "", str(r.meta))

with patch(_category_index=_Seq(CIDX, A.build_category_index(CATS[1:])),
           _admin_request=_Req("1")):
    r = base.delete_category.invoke({"name": "随笔"}, config=None)
    check("delete_category 的 meta 带 category_name + change",
          r.meta.get("category_name") == "随笔" and "没有分类" in str(r.meta.get("change")),
          str(r.meta))

from agent.graph import _RCPT_META_KEYS  # noqa: E402
check("白名单里已有 change / category_name / category_id（新写工具的键不许被静默过滤）",
      {"change", "category_name", "category_id"} <= set(_RCPT_META_KEYS),
      str(_RCPT_META_KEYS))


# ── ⑧ 技能名 → 工具名：**展开出来的名字必须真的存在** ────────────────────
# 为什么单开一节：技能名与工具名是两套命名，且**只有分类那两件不同名**
# （技能 `category_create` / 工具 `create_category`；标签那两件恰好同名）。
# 于是"顺手写 name"会让标签侧全绿、分类侧整条坏掉——20260922 全量 golden 实测：
# admin_category_create_popup 整轮 FAIL、零弹窗、用户收到一句"未知工具"的系统报错。
# 这类错误的形态是**参数、模板、注册表都各看一遍都看不出问题**（三处都自洽），
# 只有把展开结果与注册表对一遍才照得出来——所以锁在这一层。
from agent.skills import WRITE_SKILL_NAMES, instantiate_plan  # noqa: E402

_REGISTERED = {t.name for t in base.get_all_tools()}
# 每个写技能的最小合法参数（只为把模板填满，不涉及真调用）
_MIN_PARAMS = {
    "tag_create": {"title": "新标签"},
    "tag_update": {"name": "旧标签", "new_title": "新标签"},
    "tag_delete": {"name": "旧标签"},
    "category_create": {"title": "新分类"},
    "category_update": {"name": "旧分类", "new_title": "新分类"},
    "category_delete": {"name": "旧分类"},
    "article_status": {"article_id": 12, "status": "private"},
    "article_tags": {"article_id": 12, "add": ["摄影"]},
}
_EXPECT_TOOL = {
    "tag_create": "create_tag", "tag_update": "update_tag", "tag_delete": "delete_tag",
    "category_create": "create_category", "category_update": "update_category",
    "category_delete": "delete_category",
    "article_status": "set_article_status", "article_tags": "set_article_tags",
}
check("写技能名单与这张对照表同步（漏一个就少锁一条通道）",
      set(_EXPECT_TOOL) == set(WRITE_SKILL_NAMES) and set(_MIN_PARAMS) == set(WRITE_SKILL_NAMES),
      f"{sorted(WRITE_SKILL_NAMES)}")
for skill_name, params in _MIN_PARAMS.items():
    out = instantiate_plan(skill_name, params)
    specs = out["tools"]
    names = [s.split("(", 1)[0] for s in specs]
    check(f"{skill_name} 展开出的工具名 = {_EXPECT_TOOL[skill_name]}",
          names == [_EXPECT_TOOL[skill_name]], f"{names}（注记：{out['note'][:40]}）")
    check(f"{skill_name} 展开出的工具名都在 _TOOL_REGISTRY 里（否则 execute 只能回"
          f"「未知工具」错误帧）",
          all(n in _REGISTERED for n in names), f"{[n for n in names if n not in _REGISTERED]}")


print("\n" + ("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} 项失败 ==="))
for f in FAILS:
    print("  · " + f)
sys.exit(1 if FAILS else 0)
