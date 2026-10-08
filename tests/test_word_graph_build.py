# -*- coding: utf-8 -*-
"""建图脚本的纯函数（`scripts/build_word_graph.py`）单测（20261003）。

离线、秒级、零网络、零 LLM：`http_json` 被换成一张**打好的表**，所以这里量的是
"读数怎么算成热度"，不是"接口通不通"（那是真机复核的事）。

## 为什么给 numpy 打桩

建图侧那 470MB 的重依赖（umap/numba/scipy）**刻意不进生产 venv**（见
`scripts/requirements-graph.txt` 头注），而本套件要在 `.venv/bin/python tests/run_all.py`
里跑 —— 直接 `import build_word_graph` 会 ModuleNotFoundError。脚本里 `import numpy as np`
只在模块顶层出现这一次、且这两个被测函数一行都不碰它，所以先塞一个空壳模块进
`sys.modules` 再 import。**这就是"能不能在离线套件里跑"的全部代价**；
哪天模块顶层真的开始用 numpy 算常量（而不是 import），这里会立刻报错——那正是我们想要的信号。

## 量的是三件事

① **`--exclude-ids` 的三种取值语义**（20261003 修的静默 bug：以前 `--exclude-ids ""`
   也照样排除默认那三个 id ⇒ 别人 clone 过去建图，同号文章被无声丢掉，日志只说"排除 3 篇"）；
② **热度公式与归一化**（节点大小从此由它决定，算错了图还是能画出来，只是大小没意义）；
③ **"取不到读数"与"读数真的是 0"不许混**（`liked` 那一族出过的问题：缺键被当成 0）。
"""
import math
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# 生产 venv 没有 numpy，而本套件要能在它里面跑（见文件头注）
sys.modules.setdefault("numpy", types.ModuleType("numpy"))

import build_word_graph as bw  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def raises(fn) -> bool:
    """跑一下、只关心"有没有抛 ValueError"。"""
    try:
        fn()
    except ValueError:
        return True
    return False


DEFAULT = set(bw.EXCLUDE_IDS_DEFAULT)


def art(nid: int, title: str = "一篇正常文章", content: str = "正" * 500) -> dict:
    return {"id": nid, "title": title, "content": content, "desc": "", "tags": [], "cat": ""}


print("\n① --exclude-ids：三种取值是三种意思")
# 默认值**只在没传参数时**生效。写成 `in exclude or in DEFAULT`（20261003 之前那样）
# 会让空串也排除默认那几个 id —— 别人的站点里同号文章就这么没了。
check("没传参数 ⇒ 用本站默认的三个 id", bw.resolve_exclude_ids(None) == DEFAULT,
      bw.resolve_exclude_ids(None))
check("空串 ⇒ **一个都不排除**（这一条就是那个 bug 的回归锁）",
      bw.resolve_exclude_ids("") == set(), bw.resolve_exclude_ids(""))
check("只写逗号/空格 ⇒ 同上，不是一个奇怪的 id", bw.resolve_exclude_ids(" , , ") == set())
check("点名 id ⇒ 以点名的为准（默认值不叠加）",
      bw.resolve_exclude_ids("42") == {42}, bw.resolve_exclude_ids("42"))
check("写成别的（'abc'）⇒ 报错，不静默跳过（跳过 = 让人以为排除了）",
      raises(lambda: bw.resolve_exclude_ids("abc")))

kept, dropped = bw.select_articles([art(9), art(42), art(7), art(8, content="短")],
                                   bw.resolve_exclude_ids(""), 400)
check("传空串后，默认那三篇里的 9 号**留在语料里**（正文够长就该进图）",
      [a["id"] for a in kept] == [9, 42, 7], [a["id"] for a in kept])
check("  被丢的只有真的不合格的那篇（太短），且理由写在报告行里",
      len(dropped) == 1 and "id=8 too_short" in dropped[0], dropped)


print("\n② 热度：四个读数 log1p 加权、按最大值归一化")
CALLS: list[str] = []
TABLE = {
    1: {"views": 0, "likes": 0, "favorites": 0, "comments": 0},
    2: {"views": 9, "likes": 0, "favorites": 0, "comments": 0},
    3: {"views": 99, "likes": 9, "favorites": 9, "comments": 9},
    4: {"views": 10, "likes": 10},                      # 缺一个键 ⇒ 整篇算取不到
}
def fake_http(url, payload=None, headers=None, timeout=30):
    nid = int(url.rstrip("/").split("/")[-2])
    CALLS.append(url)
    if nid == 5:
        raise OSError("连接被拒绝")
    return {"data": TABLE.get(nid)}

bw.http_json = fake_http
raw, norm, missing = bw.fetch_heat("http://x/api/public", [art(i) for i in (1, 2, 3, 4, 5)])

check("五篇都读了一遍 + 那篇炸了的也没中断整次建图（异常被吞成『取不到』）",
      len(CALLS) == 5, len(CALLS))
w = bw.HEAT_W
want3 = (w["views"] * math.log1p(99) + w["likes"] * math.log1p(9)
         + w["favorites"] * math.log1p(9) + w["comments"] * math.log1p(9))
check("公式就是 HEAT_W 那一块：log1p 加权求和（浏览权 1、点赞收藏 3、讨论 4）",
      abs(raw[3] - want3) < 1e-9, raw[3])
check("★最大的一篇归一成 1.0，其余按比例", norm[3] == 1.0 and 0 < norm[2] < 1.0,
      {k: round(v, 4) for k, v in norm.items()})
check("  零热度那篇是 0.0（不是 None、不是 NaN）", norm[1] == 0.0, norm[1])
check("★缺一个键 ⇒ 整篇算**取不到**，不拿 0 顶替（否则『接口没给』被读成『没人看』）",
      raw[4] is None and 4 in missing, raw[4])
check("  异常那篇同样进 missing、且不在归一表里当真值",
      raw[5] is None and 5 in missing, missing)

raw0, norm0, _ = bw.fetch_heat("http://x/api/public", [art(1)])   # 全站零热度
check("★全站零热度不做除法（除零会得到 NaN，图上所有节点会一起消失）",
      norm0[1] == 0.0 and norm0[1] == norm0[1], norm0)


print("\n③ --out-web 的默认：不给就还是写 frontend/public/graph")
# 判据只能建在"这条分支写在源码里"上：真跑一次建图要 470MB 依赖 + 真 embedding。
src = (ROOT / "scripts" / "build_word_graph.py").read_text(encoding="utf-8")
check("有 --out-web 这个参数、且缺省是空（空 = 沿用 frontend/public）",
      'ap.add_argument("--out-web", default=""' in src)
check("给了 --out-web 就不再拼 frontend/public 那一半（两条路互斥，不是叠加）",
      'Path(args.out_web).expanduser() if args.out_web.strip()' in src)
check("write_artifacts 收的是**目录**、不再自己拼 `graph`（否则 --out-web 会被拼成 web/graph）",
      "gdir: Path, out_agent: Path" in src and 'gdir = out_frontend / "graph"' not in src)

print(f"\ntest_word_graph_build: {'全绿' if not FAILS else str(len(FAILS)) + ' 条红'}")
sys.exit(1 if FAILS else 0)
