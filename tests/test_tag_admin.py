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

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))

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

with patch(_tag_index=lambda c: IDX):
    # —— 近失（20260925）：名字差一截时**不能说"站内没有"** ——
    # 那个字面可能只是被节选截短/被模型抄漏了一个字母，而站内**有**这一个。
    # 说不存在就是一句假话，主人还得自己重新描述一遍；候选摆出来他才点得动。
    got, err = base._find_named_tag("编程 / Asynci", None)
    check("名字差一截 → 零写 + 把最接近的候选摆出来请主人点名",
          got is None and "名字最接近的是" in err
          and "编程 / Asyncio（id=10001）" in err, str(err))
    got, err = base._find_named_tag("编程 / Asyncio 异步", None)
    check("近似候选**只用来提问**：包住真名字的长名字照样零写（不按模糊匹配动手）",
          got is None and "名字最接近的是" in err
          and "编程 / Asyncio（id=10001）" in err, str(err))
    got, err = base._find_named_tag("分布", None)
    check("过短的名字不生成近似候选（2 字包含一切，只会把追问变成噪声）",
          got is None and "名字最接近的是" not in err
          and "站内没有叫「分布」的标签" in err, str(err))

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
check("白名单里已有 announcement_title / announcement_id（20260922 第五轮公告三件）",
      {"announcement_title", "announcement_id"} <= set(_RCPT_META_KEYS),
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
    "announcement_create": {"title": "维护通知", "content": "今晚 23 点维护"},
    "announcement_update": {"title": "维护通知", "new_title": "维护改期"},
    "announcement_delete": {"title": "维护通知"},
    "board_audit": {"quote": "今天天气真好呀", "verdict": "通过"},
    "board_delete": {"quote": "今天天气真好呀"},
    "article_status": {"article_id": 12, "status": "private"},
    "article_tags": {"article_id": 12, "add": ["摄影"]},
    # 用户**自己**的数据（20260923 批 7）：scope=write.own，与上面几件的差别只在
    # "动谁的"——展开器的判据一样要锁（漏一个就少锁一条通道）。
    "favorite_add": {"article_id": 12},
    "favorite_remove": {"article_id": 12},
    "notice_read": {"all": True},
    # 站内信标记已读（20260923 批 8）：与 notice_read 同一形状、另一个物件。
    "message_read": {"all": True},
    # 后台首页待办 / 日程（20260926）：写面里**唯一目标是自由文本**的一件——
    # 目标不是站内既有名字、也不是 id，所以它既不在名字通道也不在 own 通道，
    # 由 `_expand_todo_skill` 展开（见 agent/skills.py 的同名函数头注）。
    "dashboard_todo_add": {"text": "给猫买罐头", "date": "明天"},
    # 后台账号冻结 / 解冻（20260926 第九轮）：名字通道的又一件——名字 → 账号的
    # 解析在工具侧对着**后台账号名录**做（`tools.base._find_named_user`）。
    # ⚠️ 目标名字必须**明显是假的**：这一族跑展开器只碰纯函数、不发请求，但名字
    # 一旦写成一个真账号，将来谁把这条用例改成真跑就成了生产写。
    "account_freeze": {"name": "probe_target_1"},
    "account_unfreeze": {"name": "probe_target_1"},
}
_EXPECT_TOOL = {
    "tag_create": "create_tag", "tag_update": "update_tag", "tag_delete": "delete_tag",
    "category_create": "create_category", "category_update": "update_category",
    "category_delete": "delete_category",
    "announcement_create": "create_announcement",
    "announcement_update": "update_announcement",
    "announcement_delete": "delete_announcement",
    "board_audit": "audit_board_comment",
    "board_delete": "delete_board_comment",
    "article_status": "set_article_status", "article_tags": "set_article_tags",
    "favorite_add": "add_favorite", "favorite_remove": "remove_favorite",
    "notice_read": "read_notifications",
    "message_read": "read_messages",
    "dashboard_todo_add": "create_dashboard_todo",
    "account_freeze": "freeze_account", "account_unfreeze": "unfreeze_account",
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


# ══════════════════════════════════════════════════════════════════
# ⑨ 规划轮的"先看能不能做"（20260922，探针腿⑮）：
#    _write_target_refusal 是**弹窗之前**那道判断——字典读得到、而名字落不到唯一
#    一行时，不弹窗也不执行，直接确定性如实收尾。锁的重点是它**不许越界**：
#    读不到字典（None）与"名字不存在"必须分开，挂 $ref 的 spec 不许被它拦，
#    不是按名字的写工具（多 spec / 文章写）一律不碰。
print("\n⑨ 写目标预检 _write_target_refusal：只拦「名字落不到唯一一行」，其余一律不碰")
from agent.graph import _write_target_refusal  # noqa: E402


def _plan(skill, params):
    """真实的技能展开（字段名与计划文本同源，避免测试自己拼一个假计划）。"""
    return instantiate_plan(skill, params)


with patch(_tag_index=lambda c: IDX, _category_index=lambda c: CIDX):
    check("唯一命中的标签 → 不拦（放行给弹窗/执行）",
          _write_target_refusal(_plan("tag_update", {"name": "Python", "new_title": "蟒"}), cfg())
          is None)
    got = _write_target_refusal(_plan("tag_update", {"name": "没有这个标签", "new_title": "x"}), cfg())
    check("查无此名 → 拦下，理由与工具同一套措辞（含名字）",
          got is not None and got[0] == "update_tag" and "站内没有叫「没有这个标签」的标签" in got[1],
          f"{got}")
    got = _write_target_refusal(_plan("tag_delete", {"name": "Asyncio"}), cfg())
    check("同名二级挂在不同父下（歧义）→ 拦下并把候选交出来",
          got is not None and "无法确定要动的是哪一个" in got[1] and "编程" in got[1],
          f"{got}")
    got = _write_target_refusal(_plan("tag_create", {"title": "新标签", "parent_tag": "没这个爸爸"}), cfg())
    check("父标签查无此名 → 拦下（建不出来，别弹窗问'要不要建'）",
          got is not None and got[0] == "create_tag" and "一级标签" in got[1], f"{got}")
    check("父标签唯一命中 → 不拦",
          _write_target_refusal(_plan("tag_create", {"title": "新标签", "parent_tag": "编程"}), cfg())
          is None)
    check("新建标签自己的名字不在字典里 → **不拦**（新建的名字当然查不到）",
          _write_target_refusal(_plan("tag_create", {"title": "从没有过的名字"}), cfg()) is None)
    got = _write_target_refusal(_plan("category_update", {"name": "没这个分类", "new_title": "x"}), cfg())
    check("分类查无此名 → 拦下（分类走另一张字典）",
          got is not None and got[0] == "update_category" and "站内没有叫「没这个分类」的分类" in got[1],
          f"{got}")
    check("分类唯一命中 → 不拦",
          _write_target_refusal(_plan("category_delete", {"name": "随笔"}), cfg()) is None)

# 字典读不到 ≠ 没有：一律不拦（保持既有行为——弹窗与工具侧各自的"读不到"说法都还在）
with patch(_tag_index=lambda c: None):
    check("读不到标签字典 → **不拦**（把网络故障说成'站内没有'是最坏的错法）",
          _write_target_refusal(_plan("tag_delete", {"name": "随便什么"}), cfg()) is None)
with patch(_category_index=lambda c: None):
    check("读不到分类字典 → **不拦**",
          _write_target_refusal(_plan("category_delete", {"name": "随便什么"}), cfg()) is None)

# `$ref` 是"取值没解析出来"，不是"名字不存在"——交给 execute 的 resolve_args 报原因码
with patch(_tag_index=lambda c: IDX):
    _refplan = {"tools": ['update_tag({"name": "$list_tags[0].tagKey", "new_title": "x"})']}
    check("参数挂着 $ref → **不拦**（那是 execute 的错误码链路，planner 还能改参数）",
          _write_target_refusal(_refplan, cfg()) is None)
    check("不是按名字的写工具（文章写）→ 不碰",
          _write_target_refusal(
              {"tools": ['set_article_status({"article_id": 12, "status": "private"})']}, cfg())
          is None)
    check("多 spec 混排 → 不在这一层判（交工具自己如实拒绝）",
          _write_target_refusal(
              {"tools": ['delete_tag({"name": "没有这个标签"})',
                         'delete_tag({"name": "编程"})']}, cfg()) is None)
    check("零工具计划 → 不碰",
          _write_target_refusal({"tools": []}, cfg()) is None)


# ══════════════════════════════════════════════════════════════════
# ⑩ 站内公告三件（20260922 第五轮）：代发 / 改 / 删
#    公告与标签/分类的差别不在写法，在**三件公告独有的事实**——
#      ① 没有唯一约束、没有草稿态 ⇒ 目标只能按标题认，重名一律拒绝；
#      ② 新建端点**不回 id**（返回字符串 "Created"）⇒ 复核只能靠"id 差集 + 标题正文对上"；
#      ③ 改端点 title/content **都必填** ⇒ 只改正文时必须把现有标题原样带上。
#    这三条各自都有一条"看起来成功、其实没做成"的错法，故逐条锁住。
print("\n⑩ 公告：标题即身份、创建按 id 差集复核、PUT 两字段必填（三件独有的坑）")

ANN = [{"id": 4, "title": "维护通知", "content": "今晚 23 点维护",
        "createdAt": "2026-09-20 10:00:00"},
       {"id": 5, "title": "欢迎", "content": "欢迎来到本站",
        "createdAt": "2026-09-01 09:00:00"},
       # 同名公告：站内**真会**出现（公告标题没有唯一约束）⇒ 必须追问，不许挑一条
       {"id": 6, "title": "维护通知", "content": "上一次的维护通知",
        "createdAt": "2026-08-01 09:00:00"}]


def aidx(*extra):
    return {r["id"]: dict(r) for r in ANN + list(extra)}


with patch(_announcement_index=lambda c: aidx(), _admin_request=_Req("Created")):
    r = base.create_announcement.invoke({"title": "新公告", "content": "正文"},
                                        config=None)
    check("创建后读回清单里没有这条新公告 → unavailable（端点不回 id，认不出就不能说发成功）",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

post = _Req("Created")
with patch(_announcement_index=_Seq(aidx(), aidx({"id": 7, "title": "新公告",
                                                  "content": "正文"})),
           _admin_request=post):
    r = base.create_announcement.invoke({"title": "新公告", "content": "正文"}, config=None)
    check("创建成功：POST /api/protected/announcements，载荷只有 title+content",
          post.calls == [("POST", "/api/protected/announcements",
                          {"title": "新公告", "content": "正文"})], str(post.calls))
    check("  复核认出新增那条（id 差集 + 标题正文都对上）→ ok，且带回执键",
          r.kind == "ok" and r.meta.get("op") == "announcement_create"
          and r.meta.get("announcement_id") == 7
          and r.meta.get("announcement_title") == "新公告", f"{r.kind}: {r.meta}")

# 差集里认出的那条**标题对不上**（别人刚发的同名公告）⇒ 不能拿它充当自己的成果
post = _Req("Created")
with patch(_announcement_index=_Seq(aidx(), aidx({"id": 8, "title": "别人的公告",
                                                  "content": "正文"})),
           _admin_request=post):
    r = base.create_announcement.invoke({"title": "新公告", "content": "正文"}, config=None)
    check("差集里只有标题对不上的一条 → unavailable（不许把别人的公告认成自己发的）",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

with patch(_announcement_index=lambda c: aidx(), _admin_request=_Req("Created")):
    for kw, why in (({"title": "  ", "content": "正文"}, "标题空"),
                    ({"title": "新公告", "content": "   "}, "正文空"),
                    ({"title": "新公告", "content": "正" * 2001}, "正文超长")):
        r = base.create_announcement.invoke(kw, config=None)
        check(f"创建前拦下（{why}）→ unavailable 且零请求",
              r.kind == "unavailable", f"{r.kind}: {r}")

print("  · 改：目标按标题解析（四态与标签同源），两字段必填那条走「原样带上」")
put = _Req("Updated")
# 只改正文：现有标题必须原样发过去（Rust 的 UpsertAnnouncement 两个字段都必填，
# 发空标题 = 把公告改成没名字）。用标题唯一的那条（维护通知在样本里是重名形态，
# 它专门留给下面那条歧义判据）。
with patch(_announcement_index=_Seq(aidx(), aidx({"id": 5, "title": "欢迎",
                                                  "content": "改过的欢迎词"})),
           _admin_request=put):
    r = base.update_announcement.invoke({"title": "欢迎", "content": "改过的欢迎词"},
                                        config=None)
    check("只改正文 → PUT /api/protected/announcements/5，**标题原样带上**",
          put.calls == [("PUT", "/api/protected/announcements/5",
                         {"title": "欢迎", "content": "改过的欢迎词"})], str(put.calls))
    check("  复核读到新值 → ok，回执只说标题怎么变、不复述正文",
          r.kind == "ok" and "正文已更新" in r and "改过的欢迎词" not in r, f"{r.kind}: {r}")
    check("  meta 带 announcement_id/title/change（Rust render_exec_row 读的键）",
          r.meta.get("op") == "announcement_update"
          and r.meta.get("announcement_id") == 5 and r.meta.get("change") == "正文已更新",
          str(r.meta))

put = _Req("Updated")
with patch(_announcement_index=_Seq(aidx(), aidx({"id": 5, "title": "欢迎词",
                                                  "content": "欢迎来到本站"})),
           _admin_request=put):
    r = base.update_announcement.invoke({"title": "欢迎", "new_title": "欢迎词"}, config=None)
    check("改名：change 写「原名」而不是重复新名（回执行的主语已经是新名了）",
          r.kind == "ok" and r.meta.get("change") == "改名（原「欢迎」）"
          and r.meta.get("announcement_title") == "欢迎词"
          and "标题「欢迎」→「欢迎词」" in r, f"{r.kind}: {r.meta} / {r}")

put = _Req("Updated")
with patch(_announcement_index=lambda c: aidx(), _admin_request=put):
    r = base.update_announcement.invoke({"title": "欢迎"}, config=None)
    check("没说要改什么（既无 new_title 也无 content）→ unavailable 且零请求",
          r.kind == "unavailable" and put.calls == [], f"{r.kind}: {r}")
    r = base.update_announcement.invoke({"title": "不存在的公告", "content": "x"}, config=None)
    check("标题查无此公告 → unavailable、零请求，且措辞与标签/分类同一套",
          r.kind == "unavailable" and put.calls == []
          and "站内没有标题是「不存在的公告」的公告" in r, f"{r.kind}: {r}")
    r = base.update_announcement.invoke({"title": "欢迎", "content": "欢迎来到本站"},
                                        config=None)
    check("要改的内容与现状一字不差 → ok + 无需改动（这不是失败，是事实）",
          r.kind == "ok" and "无需改动" in r and put.calls == [], f"{r.kind}: {r}")
    r = base.update_announcement.invoke({"title": "维护通知", "content": "x"}, config=None)
    check("同名两条 → 追问、零请求（改错一条就是改了别人的公告）",
          r.kind == "unavailable" and put.calls == []
          and "站内有 2 条标题都叫「维护通知」" in r, f"{r.kind}: {r}")

# —— 近失（20260925 生产现场，trace 20260925T232645）——
# 目标标题被节选截短成"管理员助手公告发布测试"（真名「泠月喵管理员助手公告发布测试」，
# 少了开引号和前三个字）⇒ 台账按**完全相等**查不到 ⇒ 旧行为回一句"站内没有标题是「…」的
# 公告"，而站内明明有——**一次假否定**，随后 narrator 与 planner 各说各话。
# 判据：没有完全同名的，就把**最接近的真标题**连同 id 摆出来，仍然零写。
put = _Req("Updated")
near = aidx({"id": 14, "title": "泠月喵管理员助手公告发布测试", "content": "测试公告",
             "createdAt": "2026-09-25 15:56:00"})
with patch(_announcement_index=lambda c: near, _admin_request=put):
    r = base.update_announcement.invoke({"title": "管理员助手公告发布测试", "content": "x"},
                                        config=None)
    check("被截短的标题 → 零请求 + 把最接近的真标题摆出来（不是一句「站内没有」）",
          r.kind == "unavailable" and put.calls == []
          and "名字最接近的是" in r and "id=14" in r
          and "泠月喵管理员助手公告发布测试" in r, f"{r.kind}: {r}")
    r = base.update_announcement.invoke({"title": "维护通知（补充）", "content": "x"},
                                        config=None)
    check("  近似只用来提问：包住真标题的长标题照样零请求、照样只回候选",
          r.kind == "unavailable" and put.calls == []
          and "名字最接近的是" in r and "id=4" in r, f"{r.kind}: {r}")
    check("  近失分支把结论**限定**在「没有完全同名的」（不笼统说站内没有这条公告）",
          "站内没有标题**完全等于**" in r, f"{r.kind}: {r}")
    r = base.update_announcement.invoke({"title": "维护", "content": "x"}, config=None)
    check("  过短的标题不生成近似候选（2 字包含一切）",
          r.kind == "unavailable" and "名字最接近的是" not in r
          and "站内没有标题是「维护」的公告" in r, f"{r.kind}: {r}")

with patch(_announcement_index=lambda c: None, _admin_request=_Req("Updated")):
    r = base.update_announcement.invoke({"title": "欢迎", "content": "x"}, config=None)
    check("读不到公告清单 → unavailable、**零请求**（读不到 ≠ 没有，绝不动手）",
          r.kind == "unavailable" and "读不到现有" in r, f"{r.kind}: {r}")

print("  · 删：真删、删不回，复核必须确认那条真的没了")
dl = _Req("1")
with patch(_announcement_index=_Seq(aidx(), {k: v for k, v in aidx().items() if k != 5}),
           _admin_request=dl):
    r = base.delete_announcement.invoke({"title": "欢迎"}, config=None)
    check("删成功：DELETE 载荷是**裸 id 列表**（与删分类同形态）",
          dl.calls == [("DELETE", "/api/protected/announcements", [5])], str(dl.calls))
    check("  复核确认 id 已不在清单里 → ok，回执写清「取不回来」",
          r.kind == "ok" and "取不回来" in r
          and r.meta.get("op") == "announcement_delete"
          and r.meta.get("announcement_id") == 5, f"{r.kind}: {r}")

dl = _Req("1")
with patch(_announcement_index=_Seq(aidx(), aidx()), _admin_request=dl):
    r = base.delete_announcement.invoke({"title": "欢迎"}, config=None)
    check("删完读回还在 → unavailable（DELETE 对不存在的 id 会**静默 no-op**，不能只看 HTTP）",
          r.kind == "unavailable" and "还在" in r, f"{r.kind}: {r}")

dl = _Req("1")
with patch(_announcement_index=lambda c: aidx(), _admin_request=dl):
    r = base.delete_announcement.invoke({"title": "维护通知"}, config=None)
    check("同名公告两条 → 追问、零请求（挑错一条就是删了别人的公告）",
          r.kind == "unavailable" and dl.calls == []
          and "站内有 2 条标题都叫「维护通知」" in r, f"{r.kind}: {r}")

print("  · 弹窗问句：正文预览必须在里面（主人是扫一眼就点确定的）")
_q = A.render_confirm_question([{"tool": "create_announcement",
                                "args": {"title": "维护通知",
                                         "content": "今晚 23 点开始维护，预计一小时"}}])
check("新建公告的问句含标题**和正文预览**（只写标题 = 让主人盲签一份没看过的公告）",
      "发布公告「维护通知」" in _q and "今晚 23 点开始维护" in _q, _q)
_q = A.render_confirm_question([{"tool": "update_announcement",
                                "args": {"title": "维护通知", "new_title": "维护改期"}}])
check("改公告的问句写清改的是哪条、改成什么",
      "修改公告「维护通知」" in _q and "标题改为「维护改期」" in _q, _q)
_q = A.render_confirm_question([{"tool": "delete_announcement",
                                "args": {"title": "维护通知"}}])
check("删公告的问句写清「取不回来」", "删除公告「维护通知」" in _q and "取不回来" in _q, _q)

print("  · 过程行（server._tool_action_text）有中文动作词，且**不打印正文**")
import server as _srv  # noqa: E402
_p = _srv._tool_action_text("create_announcement",
                            {"title": "维护通知", "content": "今晚 23 点维护"})
check("新建公告的过程行 = 发布公告「维护通知」，正文不进过程行",
      _p == "发布公告「维护通知」", _p)
check("改/删的过程行也只报标题",
      _srv._tool_action_text("update_announcement",
                             {"title": "维护通知", "new_title": "维护改期"})
      == "修改公告「维护通知」：改名为「维护改期」"
      and _srv._tool_action_text("delete_announcement", {"title": "维护通知"})
      == "删除公告「维护通知」")


# ══════════════════════════════════════════════════════════════════
# ⑪ 河灯留言人工复核（20260922 第六轮）：留言没有名字，**正文片段就是它的身份**
#    与标签/分类/公告的差别不在写法，在三件留言独有的事实：
#      ① 目标通道 = **正文片段**（唯一子串匹配）。片段撞车比标签重名常见得多
#         （「谢谢」一撞就是好几条）⇒ "命中多条一律不追问、绝不挑最新那条"必须单独锁；
#      ② 审核端点的请求体是 `{approved: i8}` 且 **0 = 驳回**（Rust 收到非 0 写
#         DB 的 1；收到 0 写 DB 的 2）⇒ **请求体值与 DB 值是两张不同的表**
#         （1/0 与 1/2）。合并它们就会把 DB 语义的 2 当请求体发出去，端点读成"通过"，
#         于是"该驳回的给放行了"——这是本组最贵的一条错法，故方向两侧都锁；
#      ③ DELETE 对不存在的 id **静默 no-op**（照样返回 "Deleted"）⇒ 读不回就等于
#         没删掉（与公告三件同一取向）。
print("\n⑪ 留言复核：正文片段指认、请求体 0/1 与 DB 1/2 是两张表、删不回")

BOARD = [
    {"talkKey": 12, "content": "今天天气真好呀", "author": "路人甲", "nickname": "路人甲",
     "userId": 3, "createTime": "2026-09-21 20:10:00", "approved": 0, "ai_result": "flag"},
    {"talkKey": 13, "content": "谢谢站长的分享！", "author": "小舟", "nickname": "小舟",
     "userId": 4, "createTime": "2026-09-21 21:00:00", "approved": 1, "ai_result": "pass"},
    # 片段撞车：「谢谢站长的分享！」正是下面这条的**前缀**——真会发生的形态
    {"talkKey": 14, "content": "谢谢站长的分享！学到了", "author": "夜航", "nickname": "夜航",
     "userId": 5, "createTime": "2026-09-22 09:00:00", "approved": 0, "ai_result": None},
    {"talkKey": 15, "content": "这条早就被驳回了", "author": "旧客", "nickname": "旧客",
     "userId": 6, "createTime": "2026-09-01 08:00:00", "approved": 2, "ai_result": "flag"},
]


def bidx(*extra):
    return {r["talkKey"]: dict(r) for r in BOARD + list(extra)}


def bstate(**kv):
    """写后复核的清单：把若干条的 approved 改成指定值（键是 id 的字符串形态）。"""
    out = bidx()
    for k, v in kv.items():
        out[int(k)]["approved"] = v
    return out


print("  · 目标通道四态：唯一命中 / 片段撞车 / 查无此句 / 清单读不到")
with patch(_board_index=lambda c: bidx()):
    hit, err = base._find_board_comment("今天天气真好呀", None)
    check("唯一命中 → 给那一行（id/作者/正文都在），不报错",
          err is None and hit is not None and hit.get("talkKey") == 12, f"{hit} / {err}")

    hit, err = base._find_board_comment("谢谢站长的分享！", None)
    check("片段撞车（2 条都含）→ 不替主人挑，列出候选（带 id/作者/原文）",
          hit is None and "站内有 2 条留言都含" in err and "#13" in err and "#14" in err,
          str(err))
    check("  · 撞车时**零请求**（挑错一条就是把别人的留言驳回了）",
          "本次未改动" in err and "更完整的原话" in err, str(err))

    hit, err = base._find_board_comment("根本没有这句话", None)
    check("查无此句 → 如实说没有，**绝不模糊匹配**（也不猜'最新的那条'）",
          hit is None and "站内没有含「根本没有这句话」的河灯留言" in err
          and "本次未改动" in err, str(err))

    hit, err = base._find_board_comment("", None)
    check("片段是空的 → 单独一种说法（不是「没有这条留言」）",
          hit is None and "没有给出能指认" in err, str(err))

    # 模型转写用户原话时常把换行/空格抹平 ⇒ 去空白兜底（只为救回这种转写差）
    hit, err = base._find_board_comment("今天 天气  真好呀", None)
    check("原文子串不中、去空白后命中 → 照样唯一命中（转写差兜底，不引入模糊匹配）",
          err is None and hit is not None and hit.get("talkKey") == 12, f"{hit} / {err}")

with patch(_board_index=lambda c: None):
    hit, err = base._find_board_comment("今天天气真好呀", None)
    check("清单读不到 → 单独一种说法（「读不到」 ≠ 「没有这条留言」）",
          hit is None and "读不到后台的留言列表" in err and "本次未改动" in err, str(err))

print("  · 审核：请求体发 1/0，读回复核 DB 的 1/2（两张表不许合并）")
put = _Req("Audited")
with patch(_board_index=_Seq(bidx(), bstate(**{"12": 1})), _admin_request=put):
    r = base.audit_board_comment.invoke({"quote": "今天天气真好呀", "verdict": "通过"},
                                        config=None)
    check("通过：PUT /api/protect/board/12/audit，载荷恰好 {approved: 1}",
          put.calls == [("PUT", "/api/protect/board/12/audit", {"approved": 1})],
          str(put.calls))
    check("  读回 approved=1 → ok，回执带 Rust render_exec_row 要读的键",
          r.kind == "ok" and r.meta.get("op") == "board_audit"
          and r.meta.get("board_id") == 12 and r.meta.get("board_author") == "路人甲"
          and r.meta.get("change") == "待审 → 通过", f"{r.kind}: {r.meta}")
    check("  回执说清这一动作**可改判**（否则主人会以为留言没了）",
          "对所有访客可见" in r and "#12" in r, str(r))

put = _Req("Audited")
with patch(_board_index=_Seq(bidx(), bstate(**{"14": 2})), _admin_request=put):
    r = base.audit_board_comment.invoke({"quote": "谢谢站长的分享！学到了",
                                        "verdict": "驳回"}, config=None)
    check("驳回：载荷发 **0**（端点内部落 DB 的 2——发 2 会被读成「通过」）",
          put.calls == [("PUT", "/api/protect/board/14/audit", {"approved": 0})],
          str(put.calls))
    check("  读回 approved=2（不是 1、也不是 0）才算成功",
          r.kind == "ok" and r.meta.get("change") == "待审 → 驳回", f"{r.kind}: {r}")
    check("  回执说清「隐藏」而不是「删了」（作者在「我的河灯」仍看到未通过）",
          "已隐藏" in r and "我的河灯" in r, str(r))

put = _Req("Audited")
with patch(_board_index=_Seq(bidx(), bstate(**{"14": 1})), _admin_request=put):
    r = base.audit_board_comment.invoke({"quote": "谢谢站长的分享！学到了",
                                        "verdict": "驳回"}, config=None)
    check("驳回后读回「已通过」→ unavailable（方向传反必须响亮，绝不能算成功）",
          r.kind == "unavailable" and "未确认生效" in r, f"{r.kind}: {r}")

put = _Req("Audited")
with patch(_board_index=_Seq(bidx(), None), _admin_request=put):
    r = base.audit_board_comment.invoke({"quote": "今天天气真好呀", "verdict": "通过"},
                                        config=None)
    check("复核后读不回清单 → unavailable（「发出去了」 ≠ 「改上了」）",
          r.kind == "unavailable" and "读不回" in r, f"{r.kind}: {r}")

put = _Req("Audited")
with patch(_board_index=_Seq(bidx(), bidx()), _admin_request=put):
    r = base.audit_board_comment.invoke({"quote": "这条早就被驳回了", "verdict": "驳回"},
                                        config=None)
    check("现状即目标（已是驳回）→ 零请求 + ok「无需改动」（这不是失败，是事实）",
          r.kind == "ok" and put.calls == [] and "现在就是「驳回」状态" in r,
          f"{r.kind}: {r} / {put.calls}")

put = _Req("Audited")
with patch(_board_index=lambda c: bidx(), _admin_request=put):
    r = base.audit_board_comment.invoke({"quote": "今天天气真好呀", "verdict": "删掉吧"},
                                        config=None)
    check("认不出的复核结论 → unavailable 且**零请求**（1/0 由工具内部产生，模型不许碰）",
          r.kind == "unavailable" and put.calls == [] and "认不出复核结论" in r,
          f"{r.kind}: {r}")

print("  · 删：真删、删不回，复核必须确认那条真的没了")
dl = _Req("Deleted")
with patch(_board_index=_Seq(bidx(), {k: v for k, v in bidx().items() if k != 12}),
           _admin_request=dl):
    r = base.delete_board_comment.invoke({"quote": "今天天气真好呀"}, config=None)
    check("删成功：DELETE /api/protect/board/12（路径带 id，与公告的裸数组不同）",
          dl.calls == [("DELETE", "/api/protect/board/12", None)], str(dl.calls))
    check("  复核确认 id 已不在清单里 → ok，回执写清「取不回来」",
          r.kind == "ok" and "取不回来" in r and r.meta.get("op") == "board_delete"
          and r.meta.get("board_id") == 12, f"{r.kind}: {r}")

dl = _Req("Deleted")
with patch(_board_index=_Seq(bidx(), bidx()), _admin_request=dl):
    r = base.delete_board_comment.invoke({"quote": "今天天气真好呀"}, config=None)
    check("删完读回还在 → unavailable（DELETE 对不存在的 id 静默 no-op，不能只看 HTTP）",
          r.kind == "unavailable" and "还在" in r, f"{r.kind}: {r}")

dl = _Req("Deleted")
with patch(_board_index=_Seq(bidx(), None), _admin_request=dl):
    r = base.delete_board_comment.invoke({"quote": "今天天气真好呀"}, config=None)
    check("删完读不回清单 → unavailable（删留言没有回收站，更不能含糊）",
          r.kind == "unavailable" and "读不回" in r, f"{r.kind}: {r}")

dl = _Req("Deleted")
with patch(_board_index=lambda c: bidx(), _admin_request=dl):
    r = base.delete_board_comment.invoke({"quote": "谢谢站长的分享！"}, config=None)
    check("片段撞车时删除 → 追问 + **零请求**（删错一条是永久损失）",
          r.kind == "unavailable" and dl.calls == [] and "2 条留言都含" in r,
          f"{r.kind}: {r} / {dl.calls}")

print("  · 弹窗问句：必须写清**要动的是哪一条**（片段是主人唯一的核对依据）")
_q = A.render_confirm_question([{"tool": "audit_board_comment",
                                 "args": {"quote": "今天天气真好呀", "verdict": "reject"}}],
                               None, None, bidx())
check("问句里有 #id + 原文 + 作者 + 当前状态（缺一项，主人就无从核对）",
      "#12「今天天气真好呀」" in _q and "路人甲 的留言" in _q and "现在：待审" in _q, _q)
check("  驳回写全后果（隐藏 / 作者自己仍看得见），不写成「删除」",
      "驳回（隐藏，只有作者自己在「我的河灯」看到未通过）" in _q, _q)
_q = A.render_confirm_question([{"tool": "delete_board_comment",
                                 "args": {"quote": "今天天气真好呀"}}], None, None, bidx())
check("删除问句写清「删掉取不回来」",
      "删除留言 #12「今天天气真好呀」" in _q and "取不回来" in _q, _q)
_q = A.render_confirm_question([{"tool": "delete_board_comment",
                                 "args": {"quote": "谢谢站长的分享！"}}], None, None, bidx())
check("片段对不上唯一一条（撞车）→ 问句**如实标注没核对上**，不装作核对过",
      "没能核对上站内具体是哪一条" in _q and "「谢谢站长的分享！」" in _q, _q)
_q = A.render_confirm_question([{"tool": "audit_board_comment",
                                 "args": {"quote": "今天天气真好呀", "verdict": "pass"}}],
                               None, None, None)
check("读不到留言清单（boards=None）→ 同样如实标注，且**照样弹窗**（不因此不弹）",
      "没能核对上" in _q and "通过（放行" in _q, _q)

print("  · 过程行（server._tool_action_text）报片段、报结论，**不打印整条留言**")
check("复核的过程行 = 人工复核留言（含「…」的那条）：通过",
      _srv._tool_action_text("audit_board_comment",
                             {"quote": "今天天气真好呀", "verdict": "reject"})
      == "人工复核留言（含「今天天气真好呀」的那条）：驳回"
      and _srv._tool_action_text("delete_board_comment", {"quote": "今天天气真好呀"})
      == "删除留言（含「今天天气真好呀」的那条）",
      _srv._tool_action_text("audit_board_comment",
                             {"quote": "今天天气真好呀", "verdict": "reject"}))

print("  · 规划轮的「先看能不能做」：留言走**同一套**预检（⑨ 的留言版）")
with patch(_board_index=lambda c: bidx()):
    check("唯一命中 → 不拦",
          _write_target_refusal(
              _plan("board_delete", {"quote": "今天天气真好呀"}), cfg()) is None)
    got = _write_target_refusal(_plan("board_delete", {"quote": "谢谢站长的分享！"}), cfg())
    check("片段撞车 → 拦下（连弹窗都不弹：弹了也是一句「哪一条？」）",
          got is not None and got[0] == "delete_board_comment"
          and "2 条留言都含" in got[1], f"{got}")
    got = _write_target_refusal(_plan("board_audit", {"quote": "根本没有这句话",
                                                     "verdict": "通过"}), cfg())
    check("查无此句 → 拦下，理由与工具同一套措辞",
          got is not None and got[0] == "audit_board_comment"
          and "站内没有含" in got[1], f"{got}")
    check("认不出的结论在**技能展开**那一层就被拒（零工具 + 具体原因）",
          _plan("board_audit", {"quote": "今天天气真好呀", "verdict": "随便看看"})["tools"] == []
          and "认不出来" in _plan("board_audit", {"quote": "今天天气真好呀",
                                                "verdict": "随便看看"})["note"])
with patch(_board_index=lambda c: None):
    check("清单读不到 → **不拦**（读不到 ≠ 没有这条留言）",
          _write_target_refusal(
              _plan("board_delete", {"quote": "随便什么"}), cfg()) is None)

check("白名单里已有 board_id / board_author（新回执键不许被静默过滤）",
      {"board_id", "board_author"} <= set(_RCPT_META_KEYS), str(_RCPT_META_KEYS))


# ══════════════════════════════════════════════════════════════════
# ⑫ 近失的**截断形态**：候选当系统数据带走，载体是弹卡（20260926，D3）
#    现场：主人上一轮听系统报过候选（「名字最接近的是「大笨狗汪汪」」），这一轮他说
#    「那把「大笨狗」删了吧」——他给的是**短的那一截**。旧行为是又一次"站内没有叫
#    「大笨狗」的标签"（候选名单只活在 narrator 的回复里，下一轮就被历史节选截断，
#    主人的「就那个」再也对不上任何东西）。新行为：台账里**只有一条**以他说的那一截
#    开头 ⇒ 就地改回全名 ⇒ 走弹卡（卡片上印着全名与 id，他点一下），**不直接执行**。
#    本节锁四件事：判据只认"抄短"这一种形态；改完必须重新验一遍；纯指代句里模型自己
#    猜的名字不算来源；改出来的计划**注记同步重生成**（陈旧注记会让 narrator 说错名字）。
print("\n⑫ 台账近失的截断形态：唯一候选 → 就地校正，交弹卡（不是又一次「站内没有」）")
from agent.graph import _truncation_candidate, _confirm_popup, plan_encode  # noqa: E402
from agent.graph import _tool_args  # noqa: E402


def _args_of(plan):
    """计划里唯一那条规格的参数（用执行器同一套解析，不自己 split/loads）。"""
    return _tool_args(plan["tools"][0])[0]


# 台账样本：多一条一级标签「大笨狗汪汪」（id=3），其余同 ONE/TWO
IDX_DOG = A.build_tag_index(
    ONE + [{"tagKey": 3, "title": "大笨狗汪汪", "level": 1, "color": "#1677ff", "noteCount": 2}],
    TWO)
# 同名二级两条挂在不同父下（校正出来的名字**歧义** ⇒ 不许替主人挑）
IDX_DUP = A.build_tag_index(ONE, TWO + [
    {"tagKey": 10010, "title": "大笨狗汪汪", "level": 2, "fatherTag": "编程",
     "fatherKey": 1, "color": "#52c41a"},
    {"tagKey": 10011, "title": "大笨狗汪汪", "level": 2, "fatherTag": "架构",
     "fatherKey": 2, "color": "#52c41a"}])

print("  · 判据本身（纯函数）：只认「以它开头」，且只认唯一一条")
check("唯一一条以它开头 → 返回那条的全名",
      _truncation_candidate("大笨狗", [(1, "编程"), (3, "大笨狗汪汪")]) == "大笨狗汪汪")
check("两条都以它开头 → None（歧义不替主人挑）",
      _truncation_candidate("大笨狗", [(3, "大笨狗汪汪"), (4, "大笨狗流浪记")]) is None)
check("中段包含（不在开头）→ None（那不是「抄短了」的形态）",
      _truncation_candidate("大笨狗", [(3, "汪汪大笨狗")]) is None)
check("反方向（主人说的比台账那条还长）→ None",
      _truncation_candidate("大笨狗汪汪", [(3, "大笨狗")]) is None)
check("短于 3 字 → None（两个字的前缀能命中一大片，只会把判据变成噪声）",
      _truncation_candidate("笨狗", [(3, "笨狗汪汪")]) is None)
check("台账里没有以它开头的 → None",
      _truncation_candidate("大笨狗", [(1, "编程"), (2, "架构")]) is None)

print("  · 走进预检：校正后交弹卡；形态不对就照旧如实拒绝")
MSG_DOG = "那把「大笨狗」删了吧"
with patch(_tag_index=lambda c: IDX_DOG):
    plan = _plan("tag_delete", {"name": "大笨狗"})
    check("前置：这一步之前计划里写的还是主人说的那一截",
          _args_of(plan)["name"] == "大笨狗", f"{plan['tools']}")
    got = _write_target_refusal(plan, cfg(), MSG_DOG)
    check("唯一截断候选 → **不拦**（放行去弹卡）", got is None, f"{got}")
    check("  计划里的目标已改成台账全名",
          _args_of(plan)["name"] == "大笨狗汪汪", f"{plan['tools']}")
    check("  params 同步（不是只改 spec 字符串）",
          plan["params"].get("name") == "大笨狗汪汪", str(plan.get("params")))
    check("  **注记同步重生成**（陈旧注记会让 narrator 照旧说错名字）",
          "大笨狗汪汪" in plan["note"], plan["note"])
    check("  同一条计划、只把主人原话换成纯指代 → 拒绝（模型自己猜的名字不算来源）",
          _write_target_refusal(_plan("tag_delete", {"name": "大笨狗"}), cfg(),
                                "把那个标签删了吧") is not None)
    check("  不传原话（默认 None）→ 同样不动手（存量调用点零影响）",
          _write_target_refusal(_plan("tag_delete", {"name": "大笨狗"}), cfg()) is not None)
    got = _write_target_refusal(_plan("tag_delete", {"name": "大笨狗", "level": "two"}),
                                cfg(), MSG_DOG)
    check("校正出来的名字在 planner 指定的层级里查不到 → **不校正**，退回如实拒绝",
          got is not None and got[0] == "delete_tag", f"{got}")
with patch(_tag_index=lambda c: IDX_DUP):
    # 「大笨狗汪汪」在台账里是**同名两条**（挂 编程 / 架构 两个父下）。判据这一步算得出
    # 候选（两条同名去重后只剩一个名字），所以挡住它的是**解析器复验**那一道：
    # `_lookup("大笨狗汪汪")` 不唯一 ⇒ 不校正，退回主人那句话本身的如实拒绝（顺带把
    # 两条候选连展示名一起摆出来，请他照「父 / 子」指认）。
    plan = _plan("tag_delete", {"name": "大笨狗"})
    got = _write_target_refusal(plan, cfg(), MSG_DOG)
    check("校正出来的名字**同名两条**（不同父下）→ 复验不唯一，退回如实拒绝",
          got is not None and got[0] == "delete_tag"
          and "名字最接近的是" in got[1]
          and "（id=10010）" in got[1] and "（id=10011）" in got[1], f"{got}")
    check("  且计划一个字节都没改（没拿两条里的任何一条当目标）",
          _args_of(plan)["name"] == "大笨狗", f"{plan['tools']}")
with patch(_tag_index=lambda c: None):
    check("台账读不到 → 照旧不拦（读了才能校正，读不到这一层什么都不做）",
          _write_target_refusal(_plan("tag_delete", {"name": "大笨狗"}), cfg(),
                                MSG_DOG) is None)

print("  · 分类与公告同族：同一条判据、同一个解析器")
CAT_DOG = A.build_category_index(CATS + [
    {"categoryKey": 21, "categoryTitle": "随笔集锦", "pathName": "essay2",
     "introduce": "", "icon": "", "color": "", "noteCount": 0}])
ANN_DOG = [{"id": 7, "title": "图库上线公告", "content": "图库上线了",
            "createdAt": "2026-09-26 09:00:00"}]
with patch(_category_index=lambda c: CAT_DOG):
    # 主人说的是「随笔集」——台账里「随笔集锦」以它开头（同族的「随笔」不以它开头，
    # 所以候选唯一）。注意判据要求那一截 **≥3 字**：两个字的前缀能命中一大片。
    plan = _plan("category_delete", {"name": "随笔集"})
    check("分类：唯一截断候选「随笔集锦」→ 校正",
          _write_target_refusal(plan, cfg(), "把「随笔集」那个分类清理掉吧") is None
          and _args_of(plan)["name"] == "随笔集锦", f"{plan['tools']}")
with patch(_tag_index=lambda c: IDX_DOG):
    # 主人只说了 2 个字：判据自己就把这一形态挡了（`_truncation_candidate` 要求那一截
    # **≥3 字**——两个字的前缀能命中一大片，那不是"抄短了"而是"说得太笼统"）。
    plan = _plan("tag_delete", {"name": "大笨"})
    check("主人只说得出一半（2 字）→ 不校正，照旧如实拒绝（宁少认不多认）",
          _write_target_refusal(plan, cfg(), "把「大笨」那个标签删掉吧") is not None
          and _args_of(plan)["name"] == "大笨", f"{plan['tools']}")
with patch(_announcement_index=lambda c: {r["id"]: dict(r) for r in ANN_DOG}):
    plan = _plan("announcement_delete", {"title": "图库上线"})
    check("公告：唯一截断候选「图库上线公告」→ 校正",
          _write_target_refusal(plan, cfg(), "把标题是「图库上线」的公告删掉吧") is None
          and _args_of(plan)["title"] == "图库上线公告", f"{plan['tools']}")

print("  · 留言族**不校正**（片段是正文；「抄短了」在片段匹配下根本不需要，也会挑错人）")
with patch(_board_index=lambda c: bidx()):
    # ① 主人给的那一截**正好是**某条正文的前缀：片段匹配本身就落对了（子串级），
    #    计划一个字节都不该动——这一步不是"校正生效"，是"根本用不着校正"。
    plan = _plan("board_delete", {"quote": "今天天气真好"})
    check("片段是唯一命中那条的**前缀** → 片段匹配自己就落对了，计划原样",
          _write_target_refusal(plan, cfg(),
                                "把写着「今天天气真好」的那条留言删掉吧") is None
          and _args_of(plan)["quote"] == "今天天气真好", f"{plan['tools']}")
    # ② 这才是守卫真正挡下的形态：片段**撞车**（13/14 两条都含「谢谢站长的分享！」），
    #    而 14 的正文恰好**以那一截开头** ⇒ 判据自己会算出唯一候选「…学到了」。
    #    放行就等于系统替主人从两条命中的留言里挑定了 14 条——`D3` 在留言族不成立
    #    （留言没有"名字"，片段本来就允许多条候选由人指认）。
    plan = _plan("board_delete", {"quote": "谢谢站长的分享！"})
    check("前置：这个形状判据自己算得出候选（挡住它的是留言族守卫，不是判据算不出）",
          _truncation_candidate("谢谢站长的分享！",
                                [(13, "谢谢站长的分享！"),
                                 (14, "谢谢站长的分享！学到了")]) == "谢谢站长的分享！学到了")
    got = _write_target_refusal(plan, cfg(), "把写着「谢谢站长的分享！」的那条留言删掉吧")
    check("片段撞车 → 照旧如实拒绝，不替主人从命中的两条里挑一条",
          got is not None and got[0] == "delete_board_comment"
          and "2 条留言都含" in got[1], f"{got}")
    check("  计划一个字节都没改",
          _args_of(plan)["quote"] == "谢谢站长的分享！", f"{plan['tools']}")

print("  · 卡片上印的是**台账全名**（主人点确定之前看得出系统要动哪一个）")
from config.settings import settings as _settings  # noqa: E402
_saved_secret = _settings.jwt_secret
_settings.jwt_secret = "test-secret-for-confirm-tokens"   # CI 里没有 .env（同 test_confirm 的桩）
try:
    with patch(_tag_index=lambda c: IDX_DOG):
        plan = _plan("tag_delete", {"name": "大笨狗"})
        _write_target_refusal(plan, cfg(), MSG_DOG)
        st = {"messages": [], "plan": plan_encode(plan), "plan_rounds": 0, "done": False}
        pop = _confirm_popup(st, plan["tools"], cfg()["configurable"]["principal"],
                             MSG_DOG, cfg())
        check("校正后**照旧弹卡**（全名不在主人原话里 → 免弹窗前提不成立）",
              pop is not None, f"{pop}")
        if pop:
            q = pop["pending_confirm"]["q"]
            check("  问句印的是台账全名（不是主人说的那一截）",
                  "大笨狗汪汪" in q and "大笨狗」" not in q, q)
            check("  签发的 spec 也是全名（执行轮照它走）",
                  _args_of(plan)["name"] == "大笨狗汪汪")
finally:
    _settings.jwt_secret = _saved_secret


print("\n" + ("=== 全部通过 ===" if not FAILS else f"=== {len(FAILS)} 项失败 ==="))
for f in FAILS:
    print("  · " + f)
sys.exit(1 if FAILS else 0)
