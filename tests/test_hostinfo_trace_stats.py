# -*- coding: utf-8 -*-
"""`hostinfo.trace_stats` 的**布局判据**（20261018）：零 LLM、零网络、只碰临时目录。

**这一件为什么单列**（原先住 `tests/test_reports.py` ③，20261018 搬出来）：那份文件
已经 961 行、在预算台账里是**超预算的存量**（预算 800）。本件要加的三样（按天目录夹具、
`.gz` 归档、只有按天目录的正控）会让它更长，而闸自己的话是"别急着 `--update`：先看能不能
按主题拆"。这一族确实**自成一个主题**：它判的不是"报表渲染得对不对"，而是
**"trace 语料被读到了没有"**。

**要治的病**（20261018 实测）：`trace_stats` 原来自己 `os.listdir(TRACES_DIR)` +
`fn.endswith(".json")` —— 那是 **20260925 之前**的平铺布局。布局改成"按天一层"
（`<root>/<YYYYMMDD>/`）之后，`os.listdir` 回的全是目录名 ⇒ **一份都进不了循环**
⇒ 管理助手报表上**天天**是"今日 0 轮 / 异常 0 / 质检拦截 0 / shadow 权限拒绝 0"，
而 `readable` 还是 `True`。**"缺键绝不编 0" 那一族里最坏的一种：它编的是一份全绿。**

**为什么原先那条判据没抓住它**：夹具只铺平铺的 `<名字>.json` ⇒ 判据在一个**已经不存在
的形状**上保持绿。同族教训见 `tests/test_ready.py` 头注（判据的前提要自己摆出来）与
`saudade-offline-suite-dial-blindspot`：**摆的前提必须是今天的那个前提。**
所以本件的夹具**同时铺两种布局**，并单列一条"只有按天目录"的正控——那才是生产的样子。

用法：.venv/bin/python tests/test_hostinfo_trace_stats.py
"""
import ast
import gzip
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import hostinfo as H  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILED.append(name)


def _write(root: str, rel: str, reason: str, events: list) -> None:
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump({"end_reason": reason, "events": events}, f)


DAY = "20261018"
TODAY = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
_real = H.TRACES_DIR


def _stats(root: str) -> dict:
    H.TRACES_DIR = root
    try:
        return H.trace_stats(TODAY)
    finally:
        H.TRACES_DIR = _real


print("① 两种布局同时在场：平铺（20260925 前）+ 按天一层（今天）")
with tempfile.TemporaryDirectory() as td:
    tdir = os.path.join(td, "traces")
    os.makedirs(tdir)
    # 平铺四份（**存量布局**，盘上今天还有 437 份这样的归档；仍要数得到）
    for name, reason, events in [
            ("a", "producer_done", []),
            ("b", "client_disconnect", [{"event": "fallback"}, {"event": "authz_shadow"}]),
            ("c", "producer_done", [{"event": "authz_shadow"}]),
            ("d", "idle_timeout", [])]:
        _write(tdir, name + ".json", reason, events)
    # 按天目录（**今天真实的布局**）：两份 `.json` + 一份 `.gz` 归档。
    # `.gz` 也是 trace 语料（`eval/trace_retention.py` 压的就是它），旧实现只认
    # `.json` 后缀 ⇒ 压缩过的那些它一份都看不见。
    _write(tdir, f"{DAY}/e.json", "producer_done", [{"event": "authz_shadow"}])
    _write(tdir, f"{DAY}/f.json", "total_timeout", [])
    with gzip.open(os.path.join(tdir, DAY, "g.json.gz"), "wt") as f:
        json.dump({"end_reason": "producer_done", "events": []}, f)
    # 坏文件与旧文件：都不该被算成一轮
    with open(os.path.join(tdir, "broken.json"), "w") as f:
        f.write("{not json")
    _write(tdir, "old.json", "producer_done", [])
    os.utime(os.path.join(tdir, "old.json"), (0, 0))

    t = _stats(tdir)
    # 7 = 平铺 4 + 按天 3。**这个数只有在两种布局都被遍历时才对**——
    # 20261018 之前实现在这里得 4（按天那三份一份都看不见）。
    check("今日轮数 = 平铺 + 按天目录里的可解析 trace（坏 JSON 与旧文件不计）",
          t["rounds"] == 7, str(t["rounds"]))
    check("  ⭐ 按天目录**被遍历到了**（否则上面那个 7 会退成 4）",
          t["rounds"] > 4, "只数到平铺那 4 份 ⇒ 按天目录没被遍历")
    check("异常收尾 = end_reason ≠ producer_done（按天目录里的也算）",
          t["abnormal"] == 3, str(t["abnormal"]))
    check("质检拦截与 shadow 拒绝分别计数（含按天目录里那份）",
          t["fallback"] == 1 and t["denied"] == 3, str(t))

print("\n② ⭐ 正控（**洞本体**）：**只有**按天目录 —— 这就是生产的形状")
# 这一条是 20261018 那个洞的最小复现：旧实现 `os.listdir` 回 `["20261018"]`，
# `endswith(".json")` 判假 ⇒ rounds 恒 0，而 `readable` 还是 True。
# **"今日 0 轮"就是这么被编出来的**，而且它长得跟"今天真的没人聊"一模一样。
with tempfile.TemporaryDirectory() as td:
    only = os.path.join(td, "traces")
    for name in ("x", "y"):
        _write(only, f"{DAY}/{name}.json", "producer_done", [])
    t = _stats(only)
    check("只有按天目录 ⇒ 数得到 2 轮（不是 0）", t["rounds"] == 2, str(t["rounds"]))
    check("  且 readable 仍是 True（这一格说的是'目录读得到'，不是'有数据'）",
          t["readable"] is True, str(t["readable"]))
    check("  反控：把按天目录整个挪走 ⇒ 0 轮（证明上一条不是恒 2）",
          _stats(os.path.join(td, "nothing"))["rounds"] == 0)

print("\n③ 读不到 ≠ 零条（两态：目录不在 / 目录在但没数据）")
with tempfile.TemporaryDirectory() as td:
    check("目录不存在 → readable=False（如实说读不到，不报 0 轮）",
          _stats(os.path.join(td, "nowhere"))["readable"] is False)
    empty = os.path.join(td, "empty")
    os.makedirs(empty)
    t = _stats(empty)
    check("目录在、里面没东西 → readable=True 且 rounds=0（**这是事实，不是读不到**）",
          t["readable"] is True and t["rounds"] == 0, str(t))

# **第三态（枚举器取不到）20261019 已随代码一起删掉，不是被放弃的判据**：那一态当年是
# `H._trace_readers()` 的 try/except 造出来的（`eval/` 那两个模块引不到 ⇒ 报"读不到"、
# **不许退回平铺自己扫**）。20261019 枚举与读取搬进 `utils/`（生产代码只许 import 本仓的
# wheel 包）⇒ 它们与 hostinfo 同住一只 wheel，**"取不到"在结构上不存在了**：真丢了就是
# `import agent.hostinfo` 当场炸（响亮），不会静默退化成一份全绿。留一个 monkeypatch 去
# 测一条不存在的分支，只是把判据挂在空气上。
# ⚠️ **本套件不管**（写下来免得起假安心）：目录**存在但读不动**（权限）时 `iter_trace_files`
# 吞掉 OSError 返回空表 ⇒ `readable=True / rounds=0`，长得跟"今天真的没人聊"一样。
# 那是 `utils/trace_files.py` 的既有取向（"没数据不是异常"），这一套件没有覆盖它。

print("\n④ 接线：枚举与读取都走 `utils/` 那两份单一实现")
# **读程序上 AST，不拿源码文本做子串匹配。** 这一节第一版用 `inspect.getsource` + 子串，
# 结果**被自己的注释骗红**了：上面解释病灶的那段话里写着 `os.listdir(TRACES_DIR)`，
# 于是"不许出现 listdir"当场命中注释。判据自己犯这个错很讽刺，但正说明那条纪律
# （见 `docs/`：读程序上 AST，读文件才用正则）不是洁癖 —— 文本里有注释、有字符串、有文档。
# 射程 = **模块顶层的 import + `trace_stats` 函数体**：20261019 起那两件 import 住在模块
# 顶部（原来在 `_trace_readers()` 里），只扫函数体会把它们漏掉 ⇒ 下面两条判据**空过**。
_tree = ast.parse((ROOT / "agent" / "hostinfo.py").read_text(encoding="utf-8"))
_used: set = set()
for _node in _tree.body:
    if isinstance(_node, (ast.Import, ast.ImportFrom)):
        _used.update(a.asname or a.name.split(".")[-1] for a in _node.names)
for _node in ast.walk(_tree):
    if isinstance(_node, ast.FunctionDef) and _node.name == "trace_stats":
        for _n in ast.walk(_node):
            if isinstance(_n, ast.Name):
                _used.add(_n.id)
            elif isinstance(_n, ast.Attribute):
                _used.add(_n.attr)          # `os.listdir` / `json.load` 这种
check("枚举走 `iter_trace_files`（不是自己 listdir/glob）",
      "iter_trace_files" in _used and not ({"listdir", "glob"} & _used),
      str(sorted(_used & {"iter_trace_files", "listdir", "glob"})))
check("读取走 `load_trace`（`.gz` 与坏文件都由它一处处理）",
      "load_trace" in _used and "load" not in _used,
      str(sorted(_used & {"load_trace", "load"})))
check("  （反控）AST 收集器确实收得到 Attribute —— 否则上面那句"
      "'没有 listdir' 是**空过**（收集器瞎了，什么都'没有'）",
      "getmtime" in _used, str(sorted(_used)[:10]))

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
