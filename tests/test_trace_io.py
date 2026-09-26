# -*- coding: utf-8 -*-
"""`eval/trace_io.py`（trace 读取的唯一实现）的离线自测。

**为什么需要这条锁**：`.gz` 那一支漏掉任何一个读取端，那个脚本就**静默少看一半语料**——
不是报错，是数字变小。20260926 实测过一次：一次即席扫描用裸 `json.load` 读"全量 948 份
trace"，862 份 `.gz` 全部抛异常被跳过，于是"948 份里只有 1 条"实际是"86 份里 1 条"
（真值 3 条，另加 78 条命令轮）。这类 bug 的形状是"报表照样出、只是小了一号"，
所以三条锁缺一不可：
  ① 同一份内容压成 `.gz` 后 `load_trace` 必须给出**逐键相同**的结果；
  ② 坏文件返回 None **而不是抛**（扫描不能因为一个坏文件中断，也不能因此改口径）；
  ③ **全仓只有一个 `load_trace` 定义**——四个读取端都从 `trace_io` 取，谁也别再抄一份。

用法：.venv/bin/python tests/test_trace_io.py
"""
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import trace_files as tf  # noqa: E402
import trace_io  # noqa: E402

FAILS = []


def check(name: str, ok: bool, extra: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  [{extra}]" if extra else ""))
    if not ok:
        FAILS.append(name)


def src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trace_io_test_")
    try:
        payload = {"input": {"message": "带 .gz 的那半语料"},
                   "events": [{"node": "planner", "event": "decision", "skill": "navigate", "round": 0}],
                   "reply": "喵"}

        # ── ① .json 与 .gz 同内容 ⇒ 结果逐键相同 ──────────────────────────
        plain = os.path.join(tmp, "20260925T143012_17_ab12cd34.json")
        packed = plain + ".gz"
        with open(plain, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        with open(packed, "wb") as fh:
            fh.write(gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8")))

        a, b = trace_io.load_trace(plain), trace_io.load_trace(packed)
        check("① .json 读得出来", isinstance(a, dict) and a.get("reply") == "喵")
        check("① .gz 读出来与 .json **逐键相同**（不是「读到了但少了几段」）",
              a == b and b is not None, f"json={a is not None} gz={b is not None}")

        # 用裸 json.load 读 .gz 会抛 ⇒ 这一条正是历史事故的形状，锁住它不能"悄悄跳过"
        try:
            json.load(open(packed))
            raised = False
        except Exception:
            raised = True
        check("① 反证：裸 json.load 读 .gz 确实抛（说明这条锁有内容，不是空转）", raised)

        # ── ② 坏文件返回 None，不抛 ──────────────────────────────────────
        bad = os.path.join(tmp, "20260925T143013_1_deadbeef.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{这不是 JSON")
        bad_gz = bad.replace(".json", ".json.gz")
        with open(bad_gz, "wb") as fh:
            fh.write(b"not gzip at all")
        check("② 坏 .json → None（不抛）", trace_io.load_trace(bad) is None)
        check("② 坏 .gz → None（不抛）", trace_io.load_trace(bad_gz) is None)
        check("② 不存在的文件 → None（不抛）",
              trace_io.load_trace(os.path.join(tmp, "没有这个文件.json")) is None)

        # ── ③ 枚举器把 .gz 也算进语料（少看一半的入口就在这里）─────────────
        got = tf.iter_trace_files(tmp)
        check("③ iter_trace_files 同时收 .json 与 .gz",
              plain in got and packed in got and len(got) == 4, f"{len(got)} 份")

        # ── ④ 全仓只有一个 load_trace 定义，四个读取端都从它取 ─────────────
        defines = []
        for p in sorted((ROOT / "eval").glob("*.py")):
            if re.search(r"^def load_trace\b", p.read_text(encoding="utf-8"), re.M):
                defines.append(p.name)
        check("④ 全仓只有 trace_io.py 定义 load_trace（不许再抄第二份）",
              defines == ["trace_io.py"], f"定义处={defines}")
        for rel in ("eval/trace_alert.py", "eval/trace_metrics.py",
                    "eval/trace_reconcile.py", "eval/golden_draft.py"):
            check(f"④ {rel} 从 trace_io 取读取器", "from trace_io import load_trace" in src(rel))
        # 三个曾经"整份读进来"的读取端不得再碰 gzip——留着 `import gzip` 就是留着抄第二份
        # 的口子。trace_reconcile 例外：它的 `_iter_lines` 是**逐行流式**解压（对账要按行扫
        # 大文件），用的不是 load_trace 那条路，这个 import 是它自己的。
        for rel in ("eval/trace_alert.py", "eval/trace_metrics.py", "eval/golden_draft.py"):
            check(f"④ {rel} 不再自带 gzip 解压", "import gzip" not in src(rel))

        # ── ⑤ parse_trace_name：一次取 stamp+uid（窗口过滤与 uid==0 排除共用）──
        check("⑤ parse_trace_name 取到 (stamp, uid)",
              tf.parse_trace_name(packed) == ("20260925T143012", "17"))
        check("⑤ 名字不规范 → 空串对（不抛）", tf.parse_trace_name("/tmp/乱七八糟.txt") == ("", ""))
        check("⑤ parse_stamp 与 parse_trace_name 同源", tf.parse_stamp(packed) == "20260925T143012")

        # ── ⑥ trace_files 仍然只依赖 stdlib（枚举器不该有 IO 语义）─────────
        tfsrc = src("eval/trace_files.py")
        check("⑥ trace_files.py 不 import json/gzip（IO 归 trace_io）",
              "import json" not in tfsrc and "import gzip" not in tfsrc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
