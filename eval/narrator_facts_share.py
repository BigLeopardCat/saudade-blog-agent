"""Estimate factual repetition in archived tool-using replies; read-only.

The strict bound counts sentence/tool-result 4-gram overlap. The broad bound
also counts completion phrases with 2-gram overlap. Both are heuristics,
not semantic correctness judgments. Only turns with recorded execute.call
events enter the denominator; current native traces need separate coverage."""

from __future__ import annotations

import os
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓根

from utils import trace_io  # noqa: E402
from utils.trace_files import iter_trace_files  # noqa: E402  （枚举的唯一实现）

# 分族判据**只有一份实现**（agent/factblock.py）：D3 的射程与这里的量化口径必须同源，
# 否则"能砍多少"这句话就不可核——两处各写一份 _WRITE_PREFIX_RE 正是会漂移的形状。
from agent.factblock import (  # noqa: E402
    FAMILY_CMD, FAMILY_DATA, FAMILY_WRITE, family_of,
)

TRACE_DIR = "/home/ubuntu/Saudade-Blog/logs/agent/traces"

# 完成式/动作族词：出现它 + 与工具返回有 2-gram 重合 ⇒ 计入上界（改写型复述）
_COMPLETION_RE = re.compile(
    r"已经|已|啦|好[了啦]|成功|完成|跳转|打开|开启|关掉|关闭|显示|设置|建好|办好"
)
_CJK_ALNUM = re.compile(r"[一-龥A-Za-z0-9]+")
_SENT_SPLIT = re.compile(r"[。！？!?\n]+")


def _norm(text: str) -> str:
    """只留中日韩汉字与字母数字——标点/空白不参与 n-gram（它们会让"重合"变成噪声）。"""
    return "".join(_CJK_ALNUM.findall(text or ""))


def _ngrams(text: str, n: int) -> set[str]:
    s = _norm(text)
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _facts(trace: dict) -> tuple[list[str], list[str]]:
    """Return archived execution results and their presentation families.

    Command/write receipts and data-query results are reported separately;
    turning structured query data into prose is not redundant receipt narration."""
    facts, families = [], []
    for e in trace.get("events") or []:
        if e.get("node") != "execute" or e.get("event") != "call" or not e.get("result"):
            continue
        facts.append(str(e["result"]))
        families.append(family_of(str(e.get("name") or ""), bool(e.get("cmd"))))
    return facts, families


def _engine(trace: dict) -> str:
    for e in trace.get("events") or []:
        if e.get("event") == "native_decision":
            return "native"
    return "text"


def _round_metric(reply: str, facts: list[str]) -> dict | None:
    sents = _sentences(reply)
    if not sents:
        return None
    strict_pool = _ngrams(" ".join(facts), 4)
    wide_pool = _ngrams(" ".join(facts), 2)
    lengths, strict_chars, wide_chars = [], 0, 0
    strict_n = wide_n = 0
    for s in sents:
        n = len(_norm(s))
        if n == 0:
            continue
        lengths.append(n)
        g4 = _ngrams(s, 4)
        hit_strict = bool(g4 & strict_pool)
        hit_wide = hit_strict or (
            bool(_COMPLETION_RE.search(s)) and bool(_ngrams(s, 2) & wide_pool)
        )
        strict_chars += n if hit_strict else 0
        wide_chars += n if hit_wide else 0
        strict_n += int(hit_strict)
        wide_n += int(hit_wide)
    total = sum(lengths)
    if total == 0:
        return None
    return {
        "chars": total, "sents": len(lengths),
        "strict_chars": strict_chars, "wide_chars": wide_chars,
        "strict_n": strict_n, "wide_n": wide_n,
        "strict_ratio": strict_chars / total, "wide_ratio": wide_chars / total,
    }


def _report(name: str, rows: list[dict]) -> None:
    if not rows:
        print(f"\n[{name}] 无样本")
        return
    sr = [r["strict_ratio"] for r in rows]
    wr = [r["wide_ratio"] for r in rows]
    chars = [r["chars"] for r in rows]
    keep = [r["chars"] - r["wide_chars"] for r in rows]
    print(f"\n[{name}] 动作轮 {len(rows)} 个")
    print(f"  回复字数      中位 {st.median(chars):.0f}  均值 {st.mean(chars):.0f}")
    print(f"  事实句占比·下界  中位 {st.median(sr):.0%}  均值 {st.mean(sr):.0%}"
          f"  P25-P75 {sorted(sr)[len(sr)//4]:.0%}-{sorted(sr)[3*len(sr)//4]:.0%}")
    print(f"  事实句占比·上界  中位 {st.median(wr):.0%}  均值 {st.mean(wr):.0%}"
          f"  P25-P75 {sorted(wr)[len(wr)//4]:.0%}-{sorted(wr)[3*len(wr)//4]:.0%}")
    print(f"  ⇒ 若正文只留「事实块 + 一句包装」：每轮平均剩 {st.mean(keep):.0f} 字"
          f"（现在均值 {st.mean(chars):.0f} 字，砍掉约 {1 - st.mean(keep) / st.mean(chars):.0%}）")
    sents = st.mean([r["sents"] for r in rows])
    ns = st.mean([r["strict_n"] for r in rows])
    nw = st.mean([r["wide_n"] for r in rows])
    print(f"  句子层面：平均每轮 {sents:.1f} 句，其中事实句 下界 {ns:.1f} / 上界 {nw:.1f} 句")


def main() -> int:
    files = sorted(iter_trace_files(TRACE_DIR))
    all_rows, native_rows, buckets = [], [], {}
    samples = []
    for p in files:
        t = trace_io.load_trace(p)
        if not t:
            continue
        facts, families = _facts(t)
        if not facts:
            continue                      # 纯闲聊/零执行轮：D3 不动它们，不进分母
        m = _round_metric(t.get("reply") or "", facts)
        if not m:
            continue
        m["family"] = families[0] if len(set(families)) == 1 else "mixed"
        m["facts"] = facts
        m["reply"] = t.get("reply") or ""
        m["path"] = os.path.basename(p)
        m["engine"] = _engine(t)
        all_rows.append(m)
        buckets.setdefault(m["family"], []).append(m)
        if m["engine"] == "native":
            native_rows.append(m)
        samples.append(m)

    print(f"trace 总数 {len(files)}；其中有工具执行的轮次 {len(all_rows)}")
    _report("全部引擎", all_rows)
    _report("接口层", native_rows)
    safe = buckets.get(FAMILY_CMD, []) + buckets.get(FAMILY_WRITE, [])
    _report("可安全砍族（命令类 + 写类：事实块系统现成）", safe)
    _report("数据类（返回是 JSON，讲人话本身就是模型的活）", buckets.get(FAMILY_DATA, []))

    print("\n== 样例（看判据判得对不对：『=事实』的句子是不是真的在复述工具原话）==")
    for m in samples[-3:]:
        print(f"\n- {m['path']}  engine={m['engine']}  family={m['family']}")
        print(f"  事实源：{str(m['facts'])[:160]}")
        print(f"  回复：{m['reply'][:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
