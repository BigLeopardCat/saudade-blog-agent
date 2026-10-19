#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线套件统一入口（20260924）：把 tests/ 下的每个套件跑一遍，一处汇总。

**为什么是 glob 而不是名单**：套件清单此前散在三处（CI 的 eval.yml 逐步列、夜间脚本单列、
README 正文），加一个套件要改三处，漏一处就变成"看着在跑、其实没跑"。这里按**磁盘**枚举——
说准确一点：**枚举 `tests/` 这一个目录下的 `*.py`**（不递归子目录），文件躺在那儿就自动进队
（`_` 开头的辅助模块与 run_all.py 自己除外），增删套件不用改任何名单。
（这一条以前写成光秃秃的"按磁盘"，容易被读成"整个仓按磁盘扫"；实际射程就是 `tests/*.py`
这一层。**不递归**是刻意的——见 `tests/test_tests_dir_shape.py`：`tests/` 下不许有子目录，
套件想带数据就自己开 `tempfile`。）

跑法（在仓根，用仓的 venv）：
  .venv/bin/python tests/run_all.py                 # 全部
  .venv/bin/python tests/run_all.py -k authz        # 只跑文件名含 authz 的
  .venv/bin/python tests/run_all.py --timeout 600   # 单套件超时（默认 300s）
  .venv/bin/python tests/run_all.py -j 4            # 并行 4 路（默认 1＝串行；见 run_suites）
  .venv/bin/python tests/run_all.py --coverage      # 多出一份覆盖率报告（**非门禁**）

**串行是默认，不是"没做并行"**：`-j 1` 时输出**边跑边出**（现场能看到卡在哪个套件上），
`-j >1` 时改成"谁跑完整块打谁"——两者不是同一个东西，所以默认值不动，CI 显式写 `-j 4`。

**覆盖率只出报告、不设阈值，也不进 CI**（20261013）：只有形状判据配当门禁，百分比会把
"该测的没测"与"测了但那一支没走到"混成一个数，拿它当闸门只会把人推去测 getter；它回答的
是另一个问题——"这轮动得多不多、哪些文件一直没人碰"。CI 的墙钟因此不为它翻倍。
⚠️ **本机 `.venv` 是产线 venv（不许 `uv sync`，里面没有 coverage）** ⇒ 本机那份是
**临时挂上去的**（coverage 是纯 Python 包，挂上去就能用；C 版 tracer 装不上会自动退回
Python 版并打一行警告，不影响报告）。**要带 `[toml]` 这个 extra**——3.10 时代它是必需
（没有标准库 TOML 时 coverage 连配置都打不开、**退出码直接是 1**，现场看起来像"每个
套件都红"）；20261013 升到 3.12 后 `tomllib` 进了标准库，它成了**多余但无害**的声明
（理由见 `pyproject.toml` 的 dev 组那一节）。三行配方——**刻意不写死解释器版本**，
那样升一次版本就会烂一次（本文件升级到 3.12 时正是这么坏的）：
  uv venv /tmp/cov --python .venv/bin/python && uv pip install --python /tmp/cov/bin/python "coverage[toml]"
  SITE=$(/tmp/cov/bin/python -c 'import coverage,os;print(os.path.dirname(os.path.dirname(coverage.__file__)))')
  PYTHONPATH="$SITE" .venv/bin/python tests/run_all.py --coverage
（`--python .venv/bin/python` 是**故意的**：建临时环境的那一侧必须与跑套件的那一侧
是**同一个解释器**，否则 `[toml]` extra 那件事会按对方版本的规则来——3.12 上它不装
`tomli`，挂到还在跑 3.10 的产线 venv 上就是"配置读不进来"那个老毛病。写路径而不是写
版本号，两侧就由构造保证同版本，也顺带把版本号从这条配方里彻底去掉。
`SITE` 那一行问的是**装好的那个包自己在哪**，所以它跟着解释器走。
真写错时不用猜：下面 `_coverage_problem` 起不来会把这三行**按当前解释器算好**打出来。）
（为什么要挂而不用 `uvx --from coverage coverage run` 跑整套：`coverage run` 是在**它自己
那个解释器**里执行被测脚本的 ⇒ 子进程会换成一个没有 langgraph 的环境，套件集体红。）
CI 里则由 dev 组的 `uv sync --frozen` 装。覆盖率这一档起不来时本入口**直接拒绝运行**
（一个套件都不跑），不会静默降级成"跑了一遍但没有覆盖率"。
⚠️ 另一条只在这个档里才露头的事：`coverage run <脚本>` 往 `sys.path` 放的是**当前目录**、
不是脚本目录（`python <脚本>` 才放脚本目录），所以下面**显式把 `tests/` 挂进 `PYTHONPATH`**
——否则 `import _parent_repo` 那一族跨语言守卫会集体 `ModuleNotFoundError`（实测红过 17 个）。

退出码：全绿 0 / 有红 1（每个套件的输出原样透传，失败时不用重跑就能看到现场）。
纪律：这些都必须是**秒级、无网络、无 LLM**的套件——要真模型的那几条腿（golden/L1/L2）
不在这里，见 eval/ 与 README「测试与评测」。

**离线套件一律按"出厂档"跑**：`config/settings.py` 读 `.env`，而本机 `.env` 就是产线那份
——不钉的话，"离线判据"会跟着**产线今天选了哪一档**变，同一份代码在产线改配置那天红一片
（实测过一次：换接口层档位后 4 个套件红，把那一项钉回默认值立刻全绿）。
判据必须钉在代码的形状上，不能钉在运维的取值上。
（20261004：接口层只剩 native 一条、`PLANNER_ENGINE` 拨盘已删 ⇒ 那项钉子连同这个隐患
一起没了；假 LLM 桩现在自带 `bind_tools`，见 `tests/_planner_stub.py`。）

20260928 起这个钉子从"钉两项"扩到**整个 .env 都不读**（`SAUDADE_IGNORE_ENV_FILE=1`，见
`config/settings.py` 的 model_config）：只钉两项不够——凡是**值本身**会影响判据形状的取值
都一样能造成"本机绿、CI 红"。实测那一次是 `JWT_SECRET`：CI 没有 `.env` ⇒ 空密钥 ⇒
弹窗签不出令牌 ⇒ `test_confirm.py` 从 §⑪ 起整片红，而本机因为有产线密钥恒绿
（= 判据在测"这台机器装了哪份 .env"，不是在测代码）。**离线套件的环境从今天起由本文件
定义**：本机、CI、夜间三处同一套，差异不再来自 .env。
下面 `_PINNED` 两项仍显式留着——它们是"出厂档"的**声明**（谁说得出跑的是哪一档），
也防着有人临时单跑套件时漏了这个环境变量。
⚠️ **直接单跑某个套件**（`.venv/bin/python tests/test_x.py`）不带这个钉子：环境非默认时结论
可能与这里相反——所以红了的套件先按上面打印的那行环境重跑一遍，再判断是不是真缺陷。
"""
import argparse
import concurrent.futures
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# 覆盖率那一档的落点（`--coverage`）：各子进程的并行数据文件都写进这里，跑完合成一份。
# 与 pyproject 的 `[tool.coverage.run]` 的 `parallel = true` 是一对——那一条说的是
# "别互相覆盖"，这一条说的是"都写到这一处"。目录整个在 .gitignore 里。
COV_DIR = ROOT / ".coverage-data"

# 钉成"出厂默认值"的能力开关（= `config/settings.py` 的默认档）。只钉**会影响离线判据**
# 的那些：`AGENT_EVIDENCE` 决定 schema 里多不多 `evidence` 那一格。其余（模型名、超时）不
# 影响离线套件的形状判据，不钉。
# （20261004 删掉了 `PLANNER_ENGINE` 那一钉：接口层只剩 native 一条，没有第二个取值可钉。
# 20261015 删掉了 `AGENT_TASK_STATE` / `AGENT_TASK_PLAN_EXEC` 那两钉：档位整族撤掉，
# 任务状态常开、没有第二个取值可钉——但**钉子表没被清空**，理由见下面那段 ⚠️。）
#
# 另有一项不是"取值"而是"环境"：`SAUDADE_IGNORE_ENV_FILE=1` ⇒ 子进程**整份 .env 不读**
# （`config/settings.py` 的 model_config）。上面那几项漏了还能靠人眼从 ⚠️ 那行看出来，
# 而 .env 漏了是**静默**的：判据照样绿，只是绿的理由变成了"这台机器有产线密钥"。
#
# `IOT_ENABLED=1`（20261002）：物联网平台是可选件，出厂默认**关**。离线套件里几十条
# 判据写的是"装了的样子"——`物联网平台 → /device-console/`、`navigate_to("/device-console/")`
# 是整页目标、`device_display`/`device_query` 在能力清单里、`get_service_health` 数三个
# 服务。不钉住的话，它们全会按"没装"那一档跑，然后集体红——而红的原因不是代码坏了，
# 是**判据与档位错配**。钉成 1 = "既有判据跑在装了的那一档"，关掉那一档另有
# `tests/test_iot_switch.py` 专门验（它自己在子进程里把开关设成 0）。
#
# `RAG_HYBRID_ENABLED=0`（20261005）：混合检索同样是**可选件**，而出厂默认是关的
# （`config/settings.py` 那段注）。不钉的话有个很隐蔽的后果——产线 `.env` 一开这个
# 开关，本机跑离线套件就跟着走混合档：① 判据绿的理由变成"这台机器装了哪份 .env"
# （与 `SAUDADE_IGNORE_ENV_FILE` 那一条同族）；② 更要命的是**会去打真 embedding
# 端点**——离线套件的纪律是"秒级、无网络、无 LLM"，这一条破了不只是慢，是它在
# 本机花的钱/CI 上的必然超时。要验混合档的套件自己在子进程/构造 Settings 里开。
#
# `AGENT_TASK_STATE` / `AGENT_TASK_PLAN_EXEC`：**20261015 已从钉子表里删掉**——两个
# 档位按主人裁决整族撤掉（任务状态在业界是默认必备的，不该有开关把它变成摆设；撤档
# 在产线上是零行为差异，理由见 `config/settings.py` 那段注与 `docs/问题记录.md` §1.63）。
# 不许它们长回来的判据在 `tests/test_task_continuation.py` ③（AST 扫四个消费面）。
#
# ⚠️ 这一条是**替换**而不是删除：`_ambient_note` 靠 `_PINNED` 里**真设置项**那几格
# 报"本机 .env 与出厂档不同"。以前那两格兼职做这件事，撤档后改挂在
# `AGENT_EVIDENCE` 与 `AGENT_FASTPATH` 上（都还在）——**别把 `_PINNED` 清成只剩
# 环境开关**，那会让这个机制静默变成空跑（本仓纪律：撤一条判据要同时证明剩下的还咬）。
#
# `AGENT_EVIDENCE=0`（20261014 第一批，影子）：写参数出处改"引文核对"这条通道同样
# **出厂默认关**，且这一档**同时**决定两件事（schema 里有没有 `evidence` 格、影子核验
# 跑不跑，见 `config/settings.py` 那段注）。钉它的理由多一条：开档会往 planner 的
# **函数 schema** 里多加一格——`tests/test_planner_llm.py` 钉的是 props 键集，不钉住的
# 话谁在 shell 里 export 过它，那几条就会按另一档的键集判。要验开档的用例在同一进程里
# 临时拨 `settings.agent_evidence`（并同步重建 schema，见 test_provenance 的那一节）。
_PINNED = {"AGENT_EVIDENCE": "0",
           # `AGENT_FASTPATH`（20261015 快道定价档）：**出厂开**，但这里钉 `1` 是必须的
           # ——本仓教训"离线套件对档位是瞎的"：不钉的话，谁在 shell 里 export 过
           # `AGENT_FASTPATH=0`（比如为了跑两臂），整套里凡是走 planner 的用例都会
           # 按另一档判，而**红得毫无理由**（快道不跑 ⇒ 那一轮多一次 LLM、计划文本
           # 也不同）。要验关档手臂的用例在同一进程里临时拨 `settings.agent_fastpath`。
           "AGENT_FASTPATH": "1",
           "RAG_HYBRID_ENABLED": "0",
           # `CHECKPOINT_DSN`（20261017 外围准备）：`build_checkpointer()` 不给参数时读
           # 这一格，而 `test_checkpoint_factory.py` ① 断的正是"不给参数 ⇒ None"——谁在
           # shell 里 export 过一个非空 DSN，那一条就会**红得与代码无关**。钉空 = 出厂档
           # 就是"没配 saver"（生产今天也确实没挂）。
           "CHECKPOINT_DSN": "",
           "SAUDADE_IGNORE_ENV_FILE": "1", "IOT_ENABLED": "1"}


def suites(keyword: str = "") -> list[pathlib.Path]:
    """按磁盘枚举要跑的套件（不写名单）。"""
    out = []
    for p in sorted(TESTS.glob("*.py")):
        if p.name.startswith("_") or p.name == pathlib.Path(__file__).name:
            continue
        if keyword and keyword not in p.name:
            continue
        out.append(p)
    return out


def _ambient_note(env: dict) -> str:
    """本机**实际生效**的档位与出厂档不同时，返回一行给人看的说明（否则空串）。

    为什么在本进程里 import `config.settings`：产线取值通常不在 shell 环境里，而是躺在
    `.env`（产线那份）——只看 `os.environ` 会得出"没有差异"的假结论，而那恰恰是这套钉子
    要防的那一格。settings 读进来即是**本机实际生效值**。
    读不进来（依赖缺失/校验失败）就**不猜**，直接说"读不到"，不静默当作没差异。

    ⚠️ 只对**真设置项**做这个比较：`SAUDADE_IGNORE_ENV_FILE` 是"环境开关"不是设置项
    （它决定 .env 读不读），本进程读的是**读 .env 的那一档**（子进程才不读）——
    把它塞进 `live` 会 KeyError，也会把"本机 .env 与出厂档不同"这个本就该报的差异盖掉。
    """
    try:
        from config.settings import settings  # noqa: PLC0415  （只在需要时报这一行说明）
        # 只挂**真设置项**（撤档后从 `AGENT_TASK_STATE` 换成这两格，见 `_PINNED` 上面那段注）。
        live = {"AGENT_EVIDENCE": "1" if settings.agent_evidence else "0",
                "AGENT_FASTPATH": "1" if settings.agent_fastpath else "0"}
    except Exception as e:  # pragma: no cover - 依赖缺失时也把话说清楚
        return f"读不到本机档位（{type(e).__name__}: {e}）——本次一律按出厂档跑"
    diff = {k: live[k] for k in live if live[k].strip() != _PINNED[k]}
    if not diff:
        return ""
    want = "、".join(f"{k}={v}" for k, v in _PINNED.items() if k != "SAUDADE_IGNORE_ENV_FILE")
    return (f"本机生效档位 {'、'.join(f'{k}={v}' for k, v in diff.items())}"
            f"（产线取值）与出厂档不同 ⇒ 本次运行**整份 .env 都不读**、一律按出厂档跑"
            f"（{'、'.join(f'{k}={v}' for k, v in live.items())}），与产线行为不同；"
            f"单跑某个套件时请带上 `SAUDADE_IGNORE_ENV_FILE=1` 与 {want}")


def _ckpt_layer_note(env: dict) -> str:
    """每次运行都报一句：**真连库的那两层判据这次量了没有**。

    `tests/test_checkpoint_setup_concurrent.py`（4 进程并发建表）与
    `tests/test_pg_retention_live.py`（保留治理真删一次）缺 DSN 时都报「未评估」并**退 0**
    （不许因为量不了就报绿）——但那样一来，汇总表上它们就是两行平白的 `✓`，与"真量过"
    长得一模一样。20261018 那次真停服正是死在第一层、而当时所有套件全绿 ⇒ **"没量"必须
    每次都说出来**，不能只在套件自己的输出里说一遍。两层共用同一个前提（一个专门的
    测试库），所以合成一行报。

    它判的是**子进程实际会看到的那个取值**（`env`，不是 `os.environ`）：这两层的前提
    都是 DSN，而 DSN 不进 `_PINNED`（钉住它就得钉一个真库，本机没有那个前提）。
    """
    dsn = (env.get("SAUDADE_CKPT_TEST_DSN") or "").strip()
    what = "真连库 · 4 进程并发建表 / PG 保留治理真删"
    if not dsn:
        return (f"⏭  {what} 那两层**本次未评估**"
                "（没有 SAUDADE_CKPT_TEST_DSN）——上面那两个 ✓ 只代表「前提不可用」，"
                "不代表这两层是绿的")
    db = dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    if "saudade_blog" in db:
        return (f"⏭  {what} 那两层**本次未评估**"
                f"（SAUDADE_CKPT_TEST_DSN 指向产线库 {db!r}，套件当场拒跑）")
    return f"✅ {what} 那两层**本次量了**（库 = {db}）"


def _coverage_report(env: dict) -> None:
    """把各子进程留下的并行数据文件合起来，出一份**非门禁**报告（见模块头注）。

    `combine` 显式喂文件（不靠"扫描当前目录"那套隐含约定：数据文件的位置由
    `COVERAGE_FILE` 决定，两处约定一旦分岔，表现是"报出来永远是最后一个套件"）。
    报告**不设阈值**：命令的退出码不进本入口的结论（本入口的结论只有套件自己的 rc）。
    """
    print(f"\n{'=' * 8} 覆盖率（非门禁）{'=' * 8}", flush=True)
    parts = [str(p) for p in sorted(COV_DIR.glob(".coverage.*"))]
    if not parts:
        print("⚠️  没有采到任何覆盖率数据（子进程都没留下数据文件）——不是「没有覆盖」，"
              "是这一档没工作", file=sys.stderr)
        return
    for cmd, cwd in ((["-m", "coverage", "combine", *parts], COV_DIR),
                     (["-m", "coverage", "report"], ROOT)):
        rc = subprocess.run([sys.executable, *cmd], cwd=cwd, env=env).returncode
        if rc != 0:
            print(f"⚠️  `coverage {' '.join(cmd[2:])}` 退 {rc}——报告不完整（不影响上面的"
                  f"套件结论）", file=sys.stderr)


def _coverage_problem() -> str:
    """coverage 这一档起不来时返回一行**给人看的原因**（能用则空串）。**不静默降级**。

    只判 `import coverage` 是不够的——真踩过一次：装了 coverage 但**没有 `[toml]` extra**
    （当时是 3.10，标准库没有 TOML）时它读不了 `pyproject.toml`，而且不是"跳过配置接着跑"，是**构造
    Coverage 对象时就抛 ConfigError**（见 `coverage/tomlconfig.py` 那段：只在我们自己的
    配置文件里搜到 `[tool.coverage` 就会拒绝）。于是每个子套件的退出码都变成 1，现场看着
    像"这一批代码全坏了"——正是本入口最该拦住的那类假信号。
    ⇒ 探针把"配置读得进来"一起判了（`cwd=ROOT`：配置就在仓根的 pyproject.toml）。
    """
    # `get_option` 那一下是为了**把配置读进来**：只构造不给它机会用配置，某些版本会推迟读。
    probe = ("import coverage\n"
             "coverage.Coverage(config_file='pyproject.toml').get_option('run:parallel')")
    r = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode == 0:
        return ""
    lines = [ln for ln in (r.stderr or r.stdout).strip().splitlines() if ln.strip()]
    return lines[-1].strip() if lines else f"退出码 {r.returncode}"


def _suite_cmd(p: pathlib.Path, coverage: bool) -> list[str]:
    """跑一个套件的命令。子进程的形状只多一层薄壳：`coverage run <套件>` 的退出码就是
    套件的退出码（判据一个字不变，这也是覆盖率**可以**挂在路上的前提）。"""
    return ([sys.executable, "-m", "coverage", "run", str(p)] if coverage
            else [sys.executable, str(p)])


def _as_text(x) -> str:
    """`TimeoutExpired` 抓下来的半截输出：有 bytes 有 str，两种都收。"""
    if x is None:
        return ""
    return x.decode("utf-8", "replace") if isinstance(x, bytes) else x


def run_suites(todo, *, jobs: int = 1, timeout: float = 300.0, env: dict | None = None,
               coverage: bool = False, cwd: pathlib.Path = ROOT,
               root: pathlib.Path = ROOT, out=None) -> list[tuple]:
    """跑一批套件 → `[(相对名, rc, 秒数)]`，**按传入顺序**（汇总表要的就是文件序）。

    `jobs == 1` 走**串行**：子进程直接继承本进程的 stdout ⇒ 输出**边跑边出**，块头在
    跑之前打（这就是 20260924 以来的行为，一个字没改）。

    `jobs > 1` 走**并行**：每个套件仍然是一个**独立的子进程**、同一份 `env`、同一个
    `timeout`——这三件与串行**共用同一段代码**，不另抄一份（抄一份就等于两处约定，早晚
    分岔）。区别只有一处：stdout 被**捕获**下来、等它跑完再整块打出来。并行下没法同时
    做到"保序"与"直播"，只能选一个：这里选**保序 + 谁跑完打谁**。块头仍带套件名，所以
    看到的是"谁跑完了"，不是"谁在跑"——这一点写在这里，免得有人以为并行档卡住了。

    为什么是线程池而不是进程池：真正干活的是 `subprocess`，线程在这里只负责等它，GIL
    不参与。

    ⚠️ **并行安全的前提**（20261019 实测，不是推的）：`tests/` 下**没有**任何套件绑端口、
    起服务，或往**仓内固定路径**写。判据是"两份 `run_all.py` 同时跑一遍，两边都是
    170/170、零超时"——比任何静态审计可靠（审计脚本给过一堆假阳性）。写台账的那两条
    （`test_module_budget` / `wordlist_gate`）**只在 `--update` 档写**，常规路径只读。
    **要加"往仓内固定路径写"或"绑端口"的套件，得先回到这条注上**：并行度一开，
    这类套件就会自己撞自己，而症状是随机的假红。
    """
    root = pathlib.Path(root)
    out = out if out is not None else sys.stdout

    def _one(p: pathlib.Path):
        name = p.relative_to(root)
        cmd = _suite_cmd(p, coverage)
        t0 = time.monotonic()
        if jobs <= 1:
            print(f"\n{'─' * 8} {name} {'─' * 8}", file=out, flush=True)
            try:
                rc = subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env).returncode
            except subprocess.TimeoutExpired:
                rc = -1
                print(f"⏱ 超时（>{timeout:g}s）——套件内部多半有东西在等网络/挂起了",
                      file=out, flush=True)
            return name, rc, time.monotonic() - t0, None
        try:
            r = subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env,
                               capture_output=True, text=True)
            return name, r.returncode, time.monotonic() - t0, _as_text(r.stdout + r.stderr)
        except subprocess.TimeoutExpired as e:
            # 超时也要把那半截输出留着：套件挂在哪儿，往往就在这半截的最后一行。
            return (name, -1, time.monotonic() - t0,
                    _as_text(e.stdout) + _as_text(e.stderr)
                    + f"\n⏱ 超时（>{timeout:g}s）——套件内部多半有东西在等网络/挂起了\n")

    if jobs <= 1:
        return [(n, rc, dt) for n, rc, dt, _ in (_one(p) for p in todo)]

    done: dict[pathlib.Path, tuple[int, float]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = [ex.submit(_one, p) for p in todo]
        for f in concurrent.futures.as_completed(futs):
            name, rc, dt, text = f.result()
            done[name] = (rc, dt)
            print(f"\n{'─' * 8} {name} {'─' * 8}", file=out, flush=True)
            if text:
                print(text, end="" if text.endswith("\n") else "\n", file=out, flush=True)
    # 汇总表按**文件序**（`todo` 的顺序），不按完成序——完成序每次都不同，两次跑的
    # 汇总表没法逐行对读，那等于把这张表废了。
    return [(n, *done[n]) for n in (p.relative_to(root) for p in todo)]


def main() -> int:
    ap = argparse.ArgumentParser(description="跑 tests/ 下全部离线套件")
    ap.add_argument("-k", "--keyword", default="", help="只跑文件名含该串的套件")
    ap.add_argument("--timeout", type=float, default=300.0, help="单个套件超时秒数（默认 300）")
    ap.add_argument("-j", "--jobs", type=int, default=1,
                    help="并行跑几个套件（默认 1＝串行、输出边跑边出；>1 时输出按套件整块打）")
    ap.add_argument("--coverage", action="store_true",
                    help="逐套件收集覆盖率并在结尾出一份报告（非门禁；需要 coverage）")
    args = ap.parse_args()

    if args.coverage:
        why = _coverage_problem()
        if why:
            # 宁可一行都不跑：静默降级成"跑完没有覆盖率"与"跑了覆盖率"在输出上长得一样；
            # 而"覆盖率的毛病把套件染红"更糟——那是一整片假红。
            #
            # 配方里**只有 `sys.executable` 这一个变量**，没有写死的版本号：临时环境
            # 与跑套件必须是同一个解释器（否则 `[toml]` extra 那件事按对方版本的规则来），
            # 而 site-packages 的路径要问包自己装在哪——旧配方两处都写死了小版本号，
            # 20261013 从 3.10 升 3.12 时正是这么烂的。
            print(f"❌ 覆盖率这一档起不来（{sys.executable}）：{why}\n"
                  f"   本机产线 venv 不许 `uv sync`，按下面三行给它临时挂一份隔离装的"
                  f"（**要带 `[toml]` extra**，否则就是上面这个错）；CI 里由 dev 组的 "
                  f"`uv sync --frozen` 装。本次**一个套件都没跑**。\n"
                  f"     uv venv /tmp/cov --python {sys.executable} && uv pip install "
                  f"--python /tmp/cov/bin/python \"coverage[toml]\"\n"
                  f"     SITE=$(/tmp/cov/bin/python -c "
                  f"'import coverage,os;print(os.path.dirname(os.path.dirname("
                  f"coverage.__file__)))')\n"
                  f"     PYTHONPATH=\"$SITE\" {sys.executable} tests/run_all.py --coverage",
                  file=sys.stderr)
            return 1

    todo = suites(args.keyword)
    if not todo:
        print(f"没有匹配的套件（目录 {TESTS}）", file=sys.stderr)
        return 1

    env = dict(os.environ)
    env.update(_PINNED)
    # 套件要 import **同目录的助手**（`_parent_repo` / `_planner_stub` / `_rust_src`…）。
    # 直接 `python tests/test_x.py` 时这是白送的：`sys.path[0]` 就是脚本所在目录。但
    # `coverage run tests/test_x.py` **不是**——coverage 往 `sys.path` 里放的是**当前目录**
    # 而不是脚本目录（7.16.2 实测：`path0` = 仓根）。不补这一下，覆盖率档里那一族跨语言
    # 守卫会以 `ModuleNotFoundError: No module named '_parent_repo'` 集体变红，现场看着像
    # "这批守卫全坏了"（真发生过一次：17 个套件同时红）。显式挂上，两档的 import 视野一致。
    env["PYTHONPATH"] = str(TESTS) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    if args.coverage:
        # 上一次跑的残留数据必须清掉：不清的话"这次没采到"与"上次采到了"混成一份，
        # 报出来的数字既不是这一次的、也不是上一次的。目录本身在 .gitignore 里。
        shutil.rmtree(COV_DIR, ignore_errors=True)
        COV_DIR.mkdir(parents=True)
        env["COVERAGE_FILE"] = str(COV_DIR / ".coverage")
    note = _ambient_note(env)
    if note:
        # **差异必须打出来**：钉住是"判据不跟运维取值走"，不是"当产线没换过档"——
        # 有人盯着这行才知道今天的绿是在产线档位之外跑出来的。
        print("⚠️  " + note)

    rows = run_suites(todo, jobs=args.jobs, timeout=args.timeout, env=env,
                      coverage=args.coverage, cwd=ROOT)
    failed = [name for name, rc, _dt in rows if rc != 0]

    print(f"\n{'=' * 8} 汇总 {'=' * 8}")
    for name, rc, dt in rows:
        print(f"{'✓' if rc == 0 else '✗'}  {str(name):<38} rc={rc:<3} {dt:5.1f}s")
    print(f"\n{len(rows) - len(failed)}/{len(rows)} 通过"
          + (f"；红：{'、'.join(str(f) for f in failed)}" if failed else ""))
    # 紧跟在那句"多少/多少通过"后面：**"通过"这个数不含"哪一层没量"**。
    print(_ckpt_layer_note(env))
    if args.coverage:
        _coverage_report(env)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
