# -*- coding: utf-8 -*-
"""trace 工具返回的截断策略（`utils/trace.tool_result_text`）单测：离线、秒级、零网络。

为什么要单独一套：这个函数决定**事后核查能拿到什么材料**——判官判"回复有没有编材料"
靠的就是它，跨轮执行记忆、trace_reconcile、效率基线读的也是它落下的东西。它有两个
曾经的毛病，这套测试就是钉住这两条：

  ① **一律 200 字符**：正文只留 200 字符，判官拿摘要当完整材料，把文章里真有的
     「第 3.3 节」判成编造（20260925 实测 `rag_git_branch`）。现在按工具分档。
  ② **截断看不出来**：`text[:limit]` 不留痕迹，读的人和脚本只能靠"长度恰好等于上限"
     猜——分档之后"上限是哪个数"甚至不是一个常量了。现在任何截断都带标记。

契约（改动要同步改这里，这里红=某条档位或标记语义被改掉了）：
  · 三层限值：env（全局）> 单工具档 > 默认档；`NO_LIMIT_TOOLS` 任何情况下不截断；
  · 截断文本 = 原文前 limit 个字符 + 标记，**标记里带原文总长度**（判官据此知道缺多少）；
  · `is_truncated()` 是唯一的机器判据，别处不许硬编码标记文本；
  · 档位表里的工具名必须真实存在（写错名字不会报错，只会让那一档静默失效）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根（20260924：测试统一搬进 tests/）
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from utils import trace as trace_mod  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _with_env(value):
    """临时设全局上限（None = 不设），返回还原函数。"""
    import os
    old = os.environ.pop(trace_mod.TOOL_RESULT_LIMIT_ENV, None)

    def restore():
        os.environ.pop(trace_mod.TOOL_RESULT_LIMIT_ENV, None)
        if old is not None:
            os.environ[trace_mod.TOOL_RESULT_LIMIT_ENV] = old
    if value is not None:
        os.environ[trace_mod.TOOL_RESULT_LIMIT_ENV] = value
    return restore


def test_tiers():
    print("[分档] 不设 env 时按工具取档，没列的走默认档")
    restore = _with_env(None)
    try:
        t = trace_mod.tool_result_text
        long_text = "x" * 20000
        got = t(long_text, "get_article_detail")
        check("正文档：截到 8000（这档是给'回复引的那段在不在'用的）",
              got.startswith("x" * 8000) and len(got) == 8000 + len(trace_mod.truncation_mark(20000)),
              str(len(got)))
        check("正文档截断带原文长度（判官知道缺了多少）",
              trace_mod.truncation_mark(20000) in got)
        check("默认档：没单列的工具截到 4000（覆盖其余工具的实测最大）",
              t(long_text, "list_guestbook").startswith("x" * 4000)
              and trace_mod.truncation_mark(20000) in t(long_text, "list_guestbook"))
        check("工具名缺失（空串）也走默认档，不是不截断",
              t(long_text) .startswith("x" * 4000))
        check("短于上限 ⇒ 逐字返回、不带标记",
              t("编程(8)", "list_tags") == "编程(8)")
        check("恰好等于上限 ⇒ 不截断（边界：`<=` 那一侧）",
              not trace_mod.is_truncated(t("y" * 4000, "list_tags")))
        check("上限 +1 ⇒ 截断",
              trace_mod.is_truncated(t("y" * 4001, "list_tags")))
        check("rag_search 任何情况下全文（20260831 定的：候选要能事后完整分析）",
              t(long_text, "rag_search") == long_text)
        check("rag_search 真的很大也不动（不是'上限够大所以没截'）",
              len(t("z" * 500000, "rag_search")) == 500000)
    finally:
        restore()


def test_env_override():
    print("[env] 全局覆盖优先于分档；值写坏退回分档")
    t = trace_mod.tool_result_text
    restore = _with_env("8000")
    try:
        check("env 一设，所有工具都按它（评测轮放开就靠这个）",
              t("x" * 20000, "list_guestbook").startswith("x" * 8000))
        check("env 也压过正文档（不是'取两者较大'）",
              t("x" * 20000, "get_article_detail").startswith("x" * 8000))
    finally:
        restore()
    restore = _with_env("0")
    try:
        check("env=0 ⇒ 不截断（语义明写 ≤0 = 全文）",
              t("x" * 20000, "get_article_detail") == "x" * 20000)
    finally:
        restore()
    restore = _with_env("八千")
    try:
        check("env 写坏 ⇒ 退回分档（既不静默放开也不静默砍到 200）",
              t("x" * 20000, "list_guestbook").startswith("x" * 4000))
        check("env 写坏 ⇒ 正文档也还在",
              t("x" * 20000, "get_article_detail").startswith("x" * 8000))
    finally:
        restore()
    restore = _with_env(None)
    try:
        check("还原后回到分档（测试之间不互相污染）",
              t("x" * 20000, "list_guestbook").startswith("x" * 4000))
    finally:
        restore()


def test_marker():
    print("[标记] 唯一的机器判据")
    check("标记里带原文长度",
          trace_mod.truncation_mark(12345) == f"{trace_mod.TRUNCATION_MARK_PREFIX}12345 字符]")
    check("is_truncated 认标记", trace_mod.is_truncated(trace_mod.truncation_mark(1)))
    check("没标记的普通返回不算截断（不靠长度猜）",
          not trace_mod.is_truncated("x" * 99999))
    check("is_truncated 对 None/非字符串不抛",
          trace_mod.is_truncated(None) is False and trace_mod.is_truncated(123) is False)


def test_tier_table_is_sound():
    print("[档位表] 名字必须真实存在——写错就是静默失效")
    from agent.graph import _TOOL_MAP
    names = set(_TOOL_MAP)
    unknown = sorted((set(trace_mod.TOOL_RESULT_LIMITS) | set(trace_mod.NO_LIMIT_TOOLS)) - names)
    check("档位表/豁免表里的工具名都在注册表里（错别字会被这条抓住）",
          not unknown, str(unknown))
    check("两张表不重叠（同一工具既单列又不截断是自相矛盾）",
          not (set(trace_mod.TOOL_RESULT_LIMITS) & set(trace_mod.NO_LIMIT_TOOLS)))
    check("默认档 > 0（0 在分档里是'不截断'，会把默认变成全文）",
          trace_mod.TOOL_RESULT_LIMIT_DEFAULT > 0)
    check("单工具档要么 >0（留这么多）要么 ≤0（不截断），没有歧义值",
          all(isinstance(v, int) for v in trace_mod.TOOL_RESULT_LIMITS.values()))


def test_judge_reads_the_marker():
    print("[下游] 判官认标记，不自己抄一份标记文本")
    import llm_judge
    marked = "x" * 300 + trace_mod.truncation_mark(99999)
    tr = {"input": {"message": "q"},
          "events": [{"t": 1.0, "node": "execute", "event": "call", "name": "list_guestbook",
                      "args": {}, "result": marked}],
          "reply": "r"}
    check("判官认得出被截断的轮（新 trace 靠标记，不靠长度）",
          llm_judge.truncated_calls(tr) == ["list_guestbook"],
          str(llm_judge.truncated_calls(tr)))
    m = llm_judge.material(tr, result_limit=99999)
    check("材料里明写这份被截断过（判官据此不判截断处之后的说法）",
          "在 trace 里被截断过" in m)
    src = (ROOT / "eval" / "llm_judge.py").read_text(encoding="utf-8")
    # 提示词里**要**写出标记的形态（判官是读材料文本的人，得知道它长什么样）；
    # 但代码里不许拿它当判据——判据只有 `utils.trace.is_truncated` 一处。
    # 判据用 AST：注释/docstring 里的引用是无害的说明文字，**字符串字面量**才是能被拿去
    # 比较/切分的东西——那才是"抄了一份标记文本"的形态。
    import ast
    tree = ast.parse(src)
    docs = {ast.get_docstring(n, clean=False) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    hits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and trace_mod.TRUNCATION_MARK_PREFIX in n.value and n.value not in docs]
    check("提示词里写了标记的形态（判官得认得出它）",
          trace_mod.TRUNCATION_MARK_PREFIX in llm_judge._JUDGE_SYS)
    check("除提示词外，判官代码里没有第二份标记文本（解析只走 utils.trace）",
          hits == [llm_judge._JUDGE_SYS], f"{len(hits)} 处")
    check("判官用的是 utils.trace 那一处",
          "from utils import trace as trace_mod" in src and "trace_mod.is_truncated(" in src)


def test_wiring():
    print("[接线] 落盘那一处必须传工具名，否则分档不生效")
    src_graph = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    check("graph 的 call 事件调用 tool_result_text 时**传了工具名**",
          "trace_mod.tool_result_text(str(out), name)" in src_graph)
    check("graph 里没有别处再截一次工具返回（两处截断会各自为政）",
          src_graph.count("tool_result_text(") == 1)
    src_run = (ROOT / "eval" / "run_golden.py").read_text(encoding="utf-8")
    check("golden 轮把全局上限放开（判官要有完整材料）",
          "os.environ.setdefault(trace_mod.TOOL_RESULT_LIMIT_ENV" in src_run)
    src_gt = (ROOT / "eval" / "golden_trace.py").read_text(encoding="utf-8")
    check("golden trace 记下本轮上限（读的人知道自己手里是不是全文）",
          '"tool_result_limit": _lim' in src_gt)


def main():
    for fn in (test_tiers, test_env_override, test_marker,
               test_tier_table_is_sound, test_judge_reads_the_marker, test_wiring):
        fn()
    if FAILS:
        print(f"\n=== {len(FAILS)} 项失败 ===")
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
