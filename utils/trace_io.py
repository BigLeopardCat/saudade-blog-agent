# -*- coding: utf-8 -*-
"""trace 文件**读取**的单一实现（20260926）。

**为什么单列一个模块、而不塞进 `trace_files.py`**：那个模块立了一条刻意的断言
（`tests/test_trace_retention.py` ⑧「只 import os/glob/re、不含 json」）——枚举器不该有
IO 语义，谁都能零成本引它。所以**枚举归 `trace_files.py`、读取归这里**：两个模块都只吃
路径参数、不依赖应用配置（离线测试 / 夜间巡检 / 对账哨兵 / golden 草稿共用）。

**20261019 从 `eval/` 搬到 `utils/`**：生产代码（`agent/hostinfo.py::trace_stats`）也要引
这两个模块，而生产代码只许 import 本仓的 wheel 包（`tests/test_packaging.py` ①）。搬到
`utils/` 的代价是包门面会被连带 import——所以同一天把 `utils/__init__.py` 收成了空门面
（理由写在那边与 `trace_files.py` 头注里）。**"只吃路径参数"这条性质一个字没变。**

**为什么 loader 必须只有一份**：此前这三份逐字相同的实现分别长在 `trace_alert.py` /
`trace_metrics.py` / `trace_reconcile.py` 里，`golden_draft.py` 再借道 `trace_alert`
取用——**`.gz` 那一支漏掉任何一个，那个脚本就静默少看一半语料**。这不是假想：
20260926 一次即席扫描用裸 `json.load` 读"全量 948 份 trace"，862 份 `.gz` 全部抛异常
被跳过，于是"948 份里只有 1 条"实际是"86 份里 1 条"、真值 3 条（外加 78 条命令轮）。
**任何按语料统计的判据，都只能经由这里读文件。**
"""
import gzip
import json

# ── 改名遗留（20261017）：事件键 `skill` → `workflow` ───────────────────────────
# trace 是**保留 30 天**的长期语料（`eval/trace_retention.py` 按 mtime 删），改名当天
# 盘上全是旧键。读取端若只认新键，那 30 天里所有按 `workflow` 统计/巡检的判据都会
# **静默读到 0**——"看着在跑、其实没跑"那一族，而且它会自己恢复（30 天后旧文件没了），
# 于是没人查得动。所以旧键在**读取的唯一点**上映射成新键，而不是在十个读取端各写一遍。
#
# **日落 20261116**：最后一份写 `skill` 的 trace 是 20261017，加 30 天保留期。到那天
# 盘上不可能再有旧键，`tests/test_trace_legacy_key.py` 会变红，删掉这一个函数即可。
LEGACY_EVENT_KEY = "skill"
LEGACY_SUNSET = "20261116"


def _alias_legacy_event_key(doc: dict) -> dict:
    """旧 trace 的事件键 `skill` 补一份 `workflow`（新键已在则不动，不覆盖新值）。"""
    for ev in doc.get("events") or []:
        if isinstance(ev, dict) and LEGACY_EVENT_KEY in ev and "workflow" not in ev:
            ev["workflow"] = ev[LEGACY_EVENT_KEY]
    return doc


def load_trace(path: str) -> dict | None:
    """读一份 trace（`.json` 与 `.json.gz` 都认）；坏文件返回 None，不抛。

    「读不到」与「里面没有那件事」是两件事：调用端一律按前者处理，不许把 None 当成
    "这份 trace 里没有我要找的东西"（扫描不能因为一个坏文件中断，也不能因此改口径）。
    """
    try:
        if path.endswith(".gz"):
            doc = json.loads(gzip.decompress(open(path, "rb").read()))
        else:
            doc = json.load(open(path))
    except Exception:
        return None
    return _alias_legacy_event_key(doc) if isinstance(doc, dict) else doc
