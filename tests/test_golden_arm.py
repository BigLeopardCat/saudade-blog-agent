# -*- coding: utf-8 -*-
"""第二臂（arm）接线锁：同一套语料/判据，跑的是哪一套循环（20261004）。

**被锁的缺陷**：「ReAct 能不能到 95%」这个问题此前**没有读数**——`eval/run_golden.py` 把
`server._agent = create_agent()` 写死在一处，报告里的 `engine` 是常量，第二条臂没有任何
入口。这一层（`eval/golden_arm.py` + 三处 eval 壳的接线）就是那个入口。开工前先把可判定的
那几条钉住，因为这一层的失效方式全都**不报错**：

 ① **静默退回 graph**：臂名拼错 / 试验模块不在本树时若退回第一条臂，跑出来的是一份
    "看起来是第二条臂"的假读数——比报错难查得多；
 ② **子进程 import 到主仓**：`golden_case_runner.py` 原先把主仓绝对路径插在
    `PYTHONPATH` **前面** ⇒ 在 worktree 里跑评测，import 到的恒是主仓的 `run_golden`
    /`server`，试验臂的模块**永远看不见**（同样是假读数，且一句提示都没有）；
 ③ **两臂的报告混栏 / 基线被覆盖**：`engine` 若给 graph 臂加后缀，`baseline_group.py`
    按 engine 归档会把历史基线劈成两档；试验臂若覆盖 `last_run.json`，被当成"最近一次
    基线"读的那份就悄悄换了主人（报告字段一模一样，从数字上分辨不出来）。

判据分四段：① 选臂（含**响亮失败**）② 分栏（engine/目录/基线三件）③ 建臂路由
（graph 走 `agent.create_agent`，别的臂**不回退**）④ 三处 eval 壳的源码锁（能力有测试
≠ **接线**有测试——本仓反复吃过这个）。

秒级、零网络、零 LLM、零生产写入：不 import `agent`/`server`（用一个假 `agent` 模块顶替）。
"""
import os
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓根（tests/ 的上一层）
EVAL = ROOT / "eval"
sys.path.insert(0, str(EVAL))

import golden_arm as ga  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


def _src(name: str) -> str:
    return (EVAL / name).read_text(encoding="utf-8")


def _code(name: str) -> str:
    """源码**剥掉注释**再判「没有」（判据写的是"代码里不许再这么写"，不是"文档里不许提
    它"）：注释里往往正是旧写法的样子（"这里原来写死 `.venv/bin/python`"），拿全文判
    「没有」会把**说明这条纪律的那句话**判成违规——本仓在 bodyOf 那条判据上吃过同型的
    亏（判"只一处"要把 `#[cfg(test)]` 剥掉）。"""
    out = []
    for ln in _src(name).splitlines():
        s = ln.lstrip()
        if s.startswith("#"):
            continue
        i = ln.find(" #")
        out.append(ln[:i] if i >= 0 else ln)
    return "\n".join(out)


# 假 `agent` 模块：本套件不许把 langgraph/图的构建拖进来（离线套件要秒级），而
# `build_agent` 只做 `from agent import create_agent` / `importlib.import_module` 两件事
# ——这两件都能用假的顶。
_FAKE = types.ModuleType("agent")
CALLS: list = []
_SENTINEL = object()
_FAKE.create_agent = lambda: (CALLS.append("create_agent"), _SENTINEL)[1]
sys.modules.setdefault("agent", _FAKE)

print("① 选臂：`GOLDEN_ARM`（缺省 graph），**不认识的取值响亮失败**")
_env_bak = os.environ.get(ga.ENV_ARM)
try:
    os.environ.pop(ga.ENV_ARM, None)
    check("缺省 = graph", ga.arm_name() == ga.ARM_GRAPH)
    os.environ[ga.ENV_ARM] = ""
    check("空串 = graph（不是未知值）", ga.arm_name() == ga.ARM_GRAPH)
    os.environ[ga.ENV_ARM] = "react"
    check("取值照读", ga.arm_name() == ga.ARM_REACT)
    os.environ[ga.ENV_ARM] = "  REACT  "
    check("大小写/空白归一（日志里粘过来的值也能用）", ga.arm_name() == ga.ARM_REACT)
    os.environ[ga.ENV_ARM] = "reakt"          # 拼错一个字母
    try:
        ga.arm_name()
        check("拼错的臂名抛错（不许静默退回 graph）", False, "它没抛")
    except RuntimeError as e:
        check("拼错的臂名抛错（不许静默退回 graph）", "reakt" in str(e))
    # 子进程继承：选臂走的是**环境变量**而不是 argv（`golden_full_run.py` 每条用例起一个
    # 子进程，argv 管道要穿透三层壳）。这条是那个设计选择的判据。
    check("选臂的载体是环境变量（子进程天然继承）", ga.ENV_ARM == "GOLDEN_ARM")
finally:
    os.environ.pop(ga.ENV_ARM, None)
    if _env_bak is not None:
        os.environ[ga.ENV_ARM] = _env_bak

print("\n② 分栏：engine / 目录 / 基线归属")
check("graph 臂的 engine **逐字** `native`（历史基线的归档键，加后缀会把基线劈成两档）",
      ga.engine_for(ga.ARM_GRAPH) == "native", repr(ga.engine_for(ga.ARM_GRAPH)))
check("试验臂的 engine 带臂名、且仍以 native 开头（看得出它改良自哪一层）",
      ga.engine_for(ga.ARM_REACT) == "native+react", repr(ga.engine_for(ga.ARM_REACT)))
check("graph 臂的留档目录 = 历史那个（一个旧档都不挪）",
      ga.reports_dir(ga.ARM_GRAPH) == "eval/report/runs")
check("试验臂另开目录", ga.reports_dir(ga.ARM_REACT) == "eval/report/runs_react")
check("**臂名只进目录、不进文件名**（文件名序 = 时间序是全仓不变量，见 report_archive）",
      ga.ARM_REACT not in os.path.basename(ga.reports_dir(ga.ARM_REACT))
      or ga.reports_dir(ga.ARM_REACT) != ga.reports_dir(ga.ARM_GRAPH))
check("能当基线的只有 graph 臂（`last_run.json` 的写入资格）",
      ga.is_baseline_arm(ga.ARM_GRAPH) and not ga.is_baseline_arm(ga.ARM_REACT))

print("\n③ 建臂路由：graph 走 create_agent；别的臂**要么建起来、要么响亮失败**（不回退）")
_agent_mod = sys.modules["agent"]
check("graph 臂确实走 `agent.create_agent`（不是顺手走了别的什么）",
      ga.build_agent(ga.ARM_GRAPH) is _SENTINEL and CALLS == ["create_agent"])
try:
    ga.build_agent("nosucharm")               # 本树里没有 `agent.nosucharm_arm`
    check("试验臂模块缺席时抛错", False, "它没抛")
except RuntimeError as e:
    check("试验臂模块缺席时抛错", "nosucharm" in str(e))
    check("错误信息里写明**不回退 graph**（读的人要立刻知道这不是「用不了就算了」）",
          "回退" in str(e))
check("上面那次失败**没有**去建 graph（静默退回 = 一份看着像第二条臂的假读数）",
      CALLS == ["create_agent"])
# 试验臂的**模块名约定**（`agent.<臂>_arm`）本身是接线的一部分：改名会让主线那份壳
# 认不出试验分支的实现，而症状是上面那条 RuntimeError（响亮，可查）。这里锁住约定文字，
# 免得日后"顺手"把模块挪个位置、判定却仍写着旧名。
check("试验臂按约定名 `agent.<臂>_arm` 去找（改名要连着改这里）",
      'f"agent.{arm}_arm"' in (EVAL / "golden_arm.py").read_text(encoding="utf-8"))

print("\n④ 接线锁（能力有测试 ≠ 接线有测试）：三处 eval 壳")
_rg, _fr, _cr = _src("run_golden.py"), _src("golden_full_run.py"), _src("golden_case_runner.py")

check("run_golden: ensure_agent 经 golden_arm 建臂", "golden_arm.build_agent(_arm)" in _rg)
check("run_golden: 不再直接 `from agent import create_agent`（建哪条臂必须是可拨的）",
      "from agent import create_agent" not in _rg)
check("run_golden: engine 按臂派生", '"engine": golden_arm.engine_for(_ARM)' in _rg)
check("run_golden: 留档目录按臂分", "os.makedirs(_REPORTS_DIR" in _rg
      and "open_archive(_REPORTS_DIR)" in _rg)
check("run_golden: 没有残留的硬编码 graph 留档目录",
      'open_archive("eval/report/runs")' not in _rg)
check("run_golden: `last_run.json` 多一道臂的闸", "is_baseline_arm(_ARM)" in _rg)
check("run_golden: 臂名在开跑前解析（拼错不许跑完 18 分钟才发现）",
      re.search(r"_ARM = golden_arm\.arm_name\(\)", _rg) is not None)
check("golden_full_run: engine 与目录同源", '"engine": golden_arm.engine_for(_ARM)' in _fr
      and "open_archive(_REPORTS_DIR)" in _fr)
check("golden_full_run: 子进程拿到的也是分臂目录",
      "_REPORTS_DIR]" in _fr and '"eval/report/runs"' not in _fr)
check("golden_full_run: 基线闸同源", "is_baseline_arm(_ARM)" in _fr)
check("golden_full_run: 解释器用 `sys.executable`（worktree 里没有 `.venv`）",
      "sys.executable" in _code("golden_full_run.py")
      and '".venv/bin/python"' not in _code("golden_full_run.py"))
check("golden_case_runner: 不再硬编码主仓绝对路径（否则 worktree 里 import 到主仓）",
      "/home/ubuntu/Saudade-Blog" not in _code("golden_case_runner.py"))
check("golden_case_runner: 仓根按**本文件位置**算",
      "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))" in _cr
      and "sys.path.insert(0, ROOT)" in _cr)

print("\n⑤ `engine` 的字面量只有一份（改了实现而漏改某个产出者，这里会红）")
check("字面量在 golden_arm", ga.INTERFACE_LAYER == "native")
check("run_golden 只做再导出、不再各写一遍",
      "from golden_arm import INTERFACE_LAYER" in _rg
      and 'INTERFACE_LAYER = "native"' not in _rg)

sys.modules.pop("agent", None)

print("\n" + ("全部通过" if not FAILS else f"失败 {len(FAILS)} 项：" + "; ".join(FAILS)))
raise SystemExit(1 if FAILS else 0)
