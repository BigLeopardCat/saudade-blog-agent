# -*- coding: utf-8 -*-
"""pytest 的测试现场（20261020 批 1：测试体系转 pytest 的双轨起步）。

**为什么是双轨**：tests/ 下 179 个套件此前是「自研 runner（tests/run_all.py）+
裸脚本判据」的体系——判据长在**模块级**（import 即执行）、报红靠 `sys.exit(1)`、
输出靠 print 的 ✅/❌ 判据行。转 pytest 不是换条命令跑：**收集即 import** 会把裸脚本
的判据当场执行掉（sys.exit 直接杀收集器）。所以迁移按批走（每批一个 PR），期间
**两个 runner 并行跑同一批文件、红绿必须一致**（对拍判据 `test_pytest_parity.py`），
末批删 run_all。对拍不是形式：转写时把判据改弱（比如 FAILED 少 append 一条）、
conftest 的环境钉子与 run_all 的 _PINNED 漂移、同进程收集带来的属性污染——
这三种「看着迁移了、其实判据变了」只有对拍抓得住。

三件事（顺序敏感，逐条说明）：

① **出厂档环境钉子**（必须在任何 `config.settings` 的 import 之前设置——conftest
   在收集前加载，早于一切测试模块，这个顺序由 pytest 保证）。与
   `tests/run_all.py` 的 `_PINNED` 同源同值：离线判据必须钉在**代码的形状**上，
   不能跟着本机 `.env`（产线那份）或 shell 里恰好 export 过的档位走——那正是
   20260928 实测到的「本机恒绿、CI 恒红」形状。两处 _PINNED 的**一致性本身有判据**
   （`test_pytest_parity.py` ②），改一处不改另一处当场红。

② **tests/ 进 sys.path**：`_ctx_src` / `_planner_stub` / `_rust_src` 这批 `_*` 助手
   按脚本态的 import 方式（`import _ctx_src`）工作，pytest 的 rootdir 导入模式不会
   自动把 tests/ 放进 path——不补这一行，试点里三个拼接视图套件当场
   ModuleNotFoundError。

③ **迁移台账 `_MIGRATED`（本文件是真值源）**：pytest 只收集台账内文件，其余
   `test_*.py` 全部 `collect_ignore`。台账是**逐批增长**的（每个迁移 PR 往里加），
   末批清空、collect_ignore 退化为空。判据 `test_pytest_parity.py` ① 钉着
   「台账里的文件都真实存在且含 `def test_`（真的可被收集），台账外一个不多收」
   ——防止「名单忘了同步」造成的静默漏跑（那正是 run_all 头注里「按磁盘枚举、
   不写名单」要防的同一种坏法；双轨期名单不可避免，所以给它配了判据）。
"""
from __future__ import annotations

import os
import pathlib
import sys

# ① 出厂档钉子（值与 tests/run_all.py::_PINNED 逐字同源；parity ② 对拍）
_PINNED = {"AGENT_EVIDENCE": "0",
           "AGENT_FASTPATH": "1",
           "RAG_HYBRID_ENABLED": "0",
           "CHECKPOINT_DSN": "",
           # `AGENT_REQUIRE_ASSERTION`（20261019 安全审计 SAU-005，随并进 main 同步）：
           # 出厂档是 1（默认强制服务间身份断言）——不钉的话谁在 shell 里 export 过 0，
           # 凡是经由 `server._resolve_principal` 的用例都会按另一档判、红得与代码无关。
           # 要验回退档的用例在同一进程里临时拨 `settings.agent_require_assertion`。
           "AGENT_REQUIRE_ASSERTION": "1",
           "SAUDADE_IGNORE_ENV_FILE": "1",
           "IOT_ENABLED": "1"}
for _k, _v in _PINNED.items():
    os.environ[_k] = _v

# ② 助手模块的 import 方式与脚本态一致（run_all 是靠子进程 cwd=tests/ 的隐式 path；
# pytest 的收集不经过那一条，这里显式补上）
sys.path.insert(0, str(pathlib.Path(__file__).parent))

# ③ 迁移台账：批 1 = 试点 10 个 + 对拍判据自己。
#    每条后面标它代表的「迁移手法/风险面」，批 2 起按主题批量扩。
_MIGRATED = frozenset({
    # —— 批 1 试点（20261020）：五种手法的代理人 ——
    "test_cancel",               # A：已有 test 函数 + main 循环（多函数共享 FAILED）
    "test_write_facts",          # B：纯脚本 FAILED+check（最普遍形态）
    "test_ready",                # B + import server（FastAPI app 副作用面）
    "test_error_frame",          # C：改模块属性 save/restore（同进程污染风险面）
    "test_settings_surface",     # B + 环境敏感（判据⑥ setenv/还原；conftest 钉子的直接消费者）
    "test_authz",                # B + _ctx_src 拼接视图
    "test_capability_denial",    # B + 拼接视图（取值点跟着代码走的先例用户）
    "test_board_dto_keys",       # 跨语言守卫：外部环境预期红 → skipif 标准语义
    "test_commit_msg_hook",      # subprocess 壳模式（判据 = 跑子进程看退出码）
    "test_changelog_fragments",  # 元测试（判据对象是仓库的形状而不是 agent 行为）
    "test_pytest_parity",        # 本体：双轨对拍（这个文件天然是 pytest 形态）
})

# 台账外的 test_*.py 一律不收集（理由见模块头注 ③；`test_*` 通配与 pytest 默认
# 收集规则一致，所以这里只需要「排除」不需要「点名」）。
_here = pathlib.Path(__file__).parent
collect_ignore = sorted(
    p for p in _here.glob("test_*.py") if p.stem not in _MIGRATED
)
