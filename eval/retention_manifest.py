# -*- coding: utf-8 -*-
"""Artifact ownership and retention policies for files and PostgreSQL stores.

Filesystem ownership uses the first matching class. Managed classes require a
cleanup implementation; open classes retain evidence pending cleanup design;
frozen classes are preserved. PostgreSQL stores use DSN/table metadata rather
than filesystem patterns. Offline tests use isolated roots.

Run ``python eval/retention_manifest.py --json`` for a read-only operational
inventory. ``--logs-root`` and ``--report-root`` select explicit storage roots.
"""

import argparse
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Operational defaults; offline tests always override storage roots.
DEFAULT_LOGS_ROOT = "/home/ubuntu/Saudade-Blog/logs"
DEFAULT_REPORT_ROOT = os.path.join(REPO, "eval", "report")

# Retention constants are shared with cleanup executors.
KEEP_DAYS_ARCHIVE = 90
KEEP_COUNT_REPORTS = 20
KEEP_COUNT_DRAFTS = 10
KEEP_COUNT_WORDGRAPH = 10
KEEP_COUNT_SLOT_STABILITY = 10
# Must match eval.pg_retention.KEEP_DAYS_DEFAULT.
KEEP_DAYS_CHECKPOINT = 30

# Filesystem policies use root/patterns; PostgreSQL policies use dsn_env/tables.
STORE_FS = "fs"
STORE_PG = "pg"


def store_of(c: dict) -> str:
    """Return the storage type; filesystem storage is the default."""
    return c.get("store", STORE_FS)

# Report family names use YYYYMMDD-HHMMSS or YYYYMMDD_HHMMSS timestamps.
FAMILY_RE = re.compile(r"^([a-z][a-z0-9_]*?)_(\d{8})[-_](\d{6})\.(md|jsonl)$")

WORDGRAPH_RE = re.compile(r"^(\d{8})-(\d{6})_")
SLOT_STABILITY_RE = re.compile(r"^(\d{8})_(\d{6})_\d{3}\.json$")


# Ordered ownership policies. Managed entries require an implementation reference.
CLASSES = [
    dict(
        key="service-logs",
        label="各服务的 .log 与它的压缩代际",
        root=DEFAULT_LOGS_ROOT,
        patterns=["*.log*", "agent/*.log*", "frontend/*.log*"],
        status="managed",
        owner="logrotate（/etc/logrotate.d/saudade 的 `*.log` 块）",
        owner_ref=None,
        owner_external=True,
        retention="daily + rotate 14 + compress",
        why="固定文件名 + 进程持有 fd ⇒ `rotate N` 按同名后缀计数**有效**、copytruncate 保 fd。"
            "这类才是 rotate 真正管用的形态（对比已摘除的 traces 块：文件名唯一 ⇒ 装饰性）。"
            "20260925 实测三组各有完整 14 代际",
        rule=None,
    ),
    dict(
        key="traces",
        label="对话 trace",
        root=DEFAULT_LOGS_ROOT,
        patterns=["agent/traces/**"],
        status="managed",
        owner="eval/trace_retention.py",
        owner_ref="eval/trace_retention.py",
        retention=">24h 压缩、>30 天删",
        why="排障与 L3 对账的语料（`trace_alert --days 7` / `golden_draft --days 1` / "
            "`trace_reconcile` 的窗口都在 30 天内），14 天会把「上月同类问题」的对照面砍掉",
        rule=None,
    ),
    dict(
        key="agent-idem-ledger",
        label="写操作幂等账（**降级档**：agent 自持的 SQLite）",
        root=DEFAULT_LOGS_ROOT,
        patterns=["agent/idem.sqlite3*"],
        status="open",
        owner=None,
        owner_ref=None,
        retention="未接管（与将来的 checkpoint store 必须一起设计）",
        why="20261016 批 2（`agent/idem.py`，ADR-0012）新增；20261017 起账有**两档**，"
            "这条登记的是**降级档**——挂的是 PG saver 时账落 PG（`agent/idem_pg.py`，"
            "表 `agent_write_idem`，**与 checkpoint 同一个库**），不落这个文件。"
            "**今天这个文件根本不存在**：它的闸是「这一轮挂没挂 checkpointer」，"
            "而生产没有 saver ⇒ 惰性、一行都不写。登记它是因为**它一上线就会长在 `logs/` "
            "树下**（`settings.idem_db`），而没登记的表现就是审计把一条正常的运行产物报成异类。"
            "**为什么是「未接管」而不是给个保留期**：账本行的意义只到「同一个 superstep 的"
            "崩溃恢复」为止（几秒），按那个窗口裁当然安全——但账本与 checkpoint **必须一起裁**："
            "留下 checkpoint 却删掉账本行，恰恰等于把本批要堵的那个洞重新打开，"
            "而两者今天都还没有执行者。等 ADR-0011 真接上 saver 时，这件事与"
            "checkpoint store 的保留策略同批设计（与 `golden-traces` / `runs/` 同一条理由："
            "分开清会正好帮倒忙）。⚠️ **PG 那张表不在本清单的射程内**（这里只审计 `logs/` 树）"
            "⇒ 它的保留口径写在 ADR-0012 的《迁移到 PG》，并且是一条**欠账**",
        rule=None,
    ),
    dict(
        key="agent-scheduling-store", cleanup_api="DBOSClient.delete_workflows",
        label="DBOS plans and execution results", store=STORE_PG,
        status="managed", owner="agent/scheduling/retention.py",
        owner_ref="agent/scheduling/retention.py", retention="Terminal runs older than 30 days",
        why="Public DBOS APIs delete terminal execution trees. Live and paused plans remain; "
            "child executions must be terminal. Nightly cleanup is disabled with scheduling.",
        rule=None, dsn_env="SCHEDULING_DSN",
        tables=["saudade_automations.workflow_status", "saudade_automations.operation_outputs",
                "saudade_automations.schedules"],
    ),
    dict(
        key="agent-checkpoint-store",
        label="对话上下文与幂等账（**PG**：langgraph checkpoint 三张表 + 幂等账表）",
        store=STORE_PG,
        status="managed",
        owner="eval/pg_retention.py",
        owner_ref="eval/pg_retention.py",
        retention=f"线程最近活动 >{KEEP_DAYS_CHECKPOINT} 天裁（与幂等账**同窗、同事务**）",
        why="20261019 批 C 新增。它此前**整个不在本表的射程内**——本表只审计 `logs/` 与 "
            "`eval/report/` 两棵树，而 20261018 采纳之后 agent 的上下文落进了 PG："
            "`checkpoints` 那一族**每个 checkpoint 存整份快照**（实测 messages 通道的 blob "
            "1417 → 2917 → 2441 → 3713 → 5135 → 6646 字节随会话长度单调长），checkpoint 数又"
            "随轮数线性长 ⇒ **按会话长度平方增长**，而没有任何东西在裁它。"
            "**为什么与 `agent-idem-ledger`（上面那条）不是同一条**：那条登记的是"
            "**降级档**（agent 自持的 SQLite，住在 `logs/` 树下、今天根本不存在）；"
            "这条是挂 PG saver 时的**正档**，两个库、两个执行者、两套坐标。"
            "**执行者**：`eval/pg_retention.py`（默认只列不删、`--apply` 才动手，接在 "
            "`scripts/nightly_regression.sh` 末位）。**两条不变量写在那个文件的头注里**："
            "账与 checkpoint 必须同窗同事务一起裁（半裁比不裁更坏）；取不到时间的线程永不裁。"
            "⚠️ 库坐标**不写在这张表里**（本仓是公开仓）：DSN 由 `dsn_env` 指的那个环境变量给。",
        rule=None,
        dsn_env="CHECKPOINT_DSN",
        tables=["checkpoints", "checkpoint_blobs", "checkpoint_writes", "agent_write_idem",
                "agent_deleted_threads"],
    ),
    dict(
        key="golden-traces",
        label="golden 每轮跑落的 trace",
        root=DEFAULT_LOGS_ROOT,
        patterns=["agent/golden_traces/**"],
        status="managed",
        owner="eval/golden_trace.py::prune（由 run_golden / golden_full_run 调用）",
        owner_ref="eval/golden_trace.py",
        retention="最近 30 个 run + **所有有失败记录的 run 永久保留**",
        why="判红的 trace 是复核红的唯一证据；「证据不足就不删」是刻意的保守规则。"
            "20260925 只读审计（**数字以这次实测为准，早先写的归因是错的**）：59 个目录里 "
            "33 个能被某份留档的 `trace_run` 认领，另 26 个**没有任何留档提到过**"
            "（0924 清晨 05:02–05:06 一连 20 个 + `adhoc` 等）——那些跑落了 trace 却没把报告"
            "写进 `runs/`，按保守规则永久保留。连接键 `trace_run` 是 20260923 上线的："
            "0922 及以前 491 份留档**结构性没有这个字段**（一次性存量），上线后仍有 6 份 null"
            "（5 份有真实用例、1 份 0 用例空跑；成因未定，不猜）。"
            "prune 每轮都在跑（窗口内留 30 + 有失败记录的全留）⇒ 这不是无界泄漏",
        rule=None,
    ),
    dict(
        key="archive-audit-log",
        label="清理留档清单（DELETED-*.txt）",
        root=DEFAULT_LOGS_ROOT,
        patterns=["archive/DELETED-*.txt"],
        status="frozen",
        owner=None,
        owner_ref=None,
        retention="永久",
        why="删了什么、依据哪条授权、文件清单与总大小——**删完再删掉这份记录，就再也回答不了"
            "「那批文件去哪了」**。它只有几千字节，是审计轨迹不是产物",
        rule=None,
    ),
    dict(
        key="archive-sql-backup",
        label="迁移前的数据快照（*.sql）",
        root=DEFAULT_LOGS_ROOT,
        patterns=["archive/*.sql"],
        status="frozen",
        owner=None,
        owner_ref=None,
        retention="永久",
        why="例如 `chat_conv_20260903_pre.sql`（会话化迁移前的历史正文）——**它是唯一一份**，"
            "没有别处可重建。这类文件按内容重要度保留，不按年龄",
        rule=None,
    ),
    dict(
        key="archive",
        label="手工归档的旧日志/旧产物",
        root=DEFAULT_LOGS_ROOT,
        patterns=["archive/**"],
        status="managed",
        owner="eval/artifact_retention.py",
        owner_ref="eval/artifact_retention.py",
        retention=f"{KEEP_DAYS_ARCHIVE} 天（上面两条 frozen 除外）",
        why="归档目录此前**全仓零引用**（没人认领，最老到 2026-06-10）。给它一个保留期，"
            "比留着「谁也不敢删」更有用；两个 exclude 各有 frozen 登记项（见上面两条），"
            "**少了 exclude 这条规则就会去删审计轨迹**——check_classes 双向校验这层对应关系",
        rule=dict(kind="keep-days", days=KEEP_DAYS_ARCHIVE,
                  exclude=["DELETED-*.txt", "*.sql"]),
    ),
    dict(
        key="deploy-state",
        label="部署管线的锁、上一版 sha 与本代前端清单",
        root=DEFAULT_LOGS_ROOT,
        patterns=[".deploy.lock", ".last_deploy_sha", ".deploy_manifest.txt"],
        status="frozen",
        owner=None,
        owner_ref=None,
        retention="不轮转、不删",
        why="住在 logs/ 但不是日志（读写者 = 父仓 `scripts/deploy/deploy_from_r2.sh`）："
            "`.deploy.lock` 是 flock 的目标文件、`.last_deploy_sha` 是「上一版 sha」"
            "（后端按源码是否变化决定要不要重启）、`.deploy_manifest.txt` 是本代前端的"
            "`js`/`vendor` 成员清单（20260925 起解压那一步顺手写，落地后按**集合差**删上一代"
            "死块；每次部署整文件覆盖写 ⇒ 零增长）。**登记它们正是因为盘点时"
            "它们以「非 log 形态的异类」冒出来**——没有登记的表现就是「没人知道这是什么、"
            "能不能删」（`.deploy_manifest.txt` 就是这么被 `audit()` 在 20260925 当天抓出来的："
            "父仓那次改动落地后 4 分钟，L0 的「真树零未登记」就红了）",
        rule=None,
    ),
    dict(
        key="eval-reports",
        label="评测报告（复审单/对账/漂移/草稿/评审员/POC）",
        root=DEFAULT_REPORT_ROOT,
        patterns=["*.md", "*.jsonl"],
        status="managed",
        owner="eval/artifact_retention.py",
        owner_ref="eval/artifact_retention.py",
        retention=f"每族保留最近 {KEEP_COUNT_REPORTS} 份（族 = 文件名里日期戳之前的前缀；"
                  f"草稿族 {KEEP_COUNT_DRAFTS} 份，见 why）",
        why="按**份数**而不是天数：这些产物是「跑一次落一份」（review_ 一天能落 24 份），"
            "按天数会在密集调试的那天把整族清掉、按份数才对应「最近几次跑」这个真实语义。"
            f"`golden_drafts` 例外留 {KEEP_COUNT_DRAFTS} 份（20260925）：它是夜间自动产的**候选草稿**、"
            "等人手抄进 basic.jsonl，人审窗口比「复审单」短得多，留 20 天没有读者；"
            "其余族仍是复审/对账/漂移那类要回头看现场的，保持 20。",
        rule=dict(kind="keep-count", keep=KEEP_COUNT_REPORTS, family_re=FAMILY_RE.pattern,
                  keep_by_family={"golden_drafts": KEEP_COUNT_DRAFTS}),
    ),
    dict(
        key="eval-baselines",
        label="单例基线与历史基线",
        root=DEFAULT_REPORT_ROOT,
        patterns=["*.json"],
        status="frozen",
        owner=None,
        owner_ref=None,
        retention="永久（单例会原地覆盖写）",
        why="`last_run.json`（最近一次全量基线，只被全量跑覆盖）/ `last_reconcile.json` / "
            "`baseline_*.json`（历史对照）。合计 7 份、260K——**按份数清它没有意义，"
            "按天数清它会毁掉对照面**",
        rule=None,
    ),
    dict(
        key="task-evaluation-evidence",
        label="任务回归、语义校准与成本审计证据",
        root=DEFAULT_REPORT_ROOT,
        patterns=["online-*/**", "runtime-*/**", "semantic-*/**", "task-cost-audit-*/**"],
        status="frozen", owner=None, owner_ref=None,
        retention="整组保留，未经归档决策不自动删除",
        why="冻结的请求、判据快照、trace和人工复核共同组成任务验收依据；"
            "失败与中止记录不能按源码清理或单独丢弃。",
        rule=None,
    ),
    dict(
        key="golden-run-archive",
        label="每轮 golden 跑的留档（含 recall_eval 的输出）",
        root=DEFAULT_REPORT_ROOT,
        patterns=["runs/**"],
        status="open",
        owner=None,
        owner_ref=None,
        retention="未接管（与 golden-traces 必须一起设计）",
        why="576 份 / 9.8M，且**它是 `golden_trace.prune` 的判据源**（反查「那一晚红没红」）⇒ "
            "单独清它会让所有窗口外的 trace 目录变成「证据不足」永久保留（正好是反过来帮倒忙）。"
            "里面 554 份是 golden 跑的留档（其中 493 份没有 `trace_run` 连接键：487 份在连接键"
            "上线前、6 份是上线后的零星 null）、22 份是 recall_eval 的输出"
            "——**一个目录两个产出者、留档还跨了连接键的两代**。"
            "明确登记为未接管：它是盘点时最容易「看着像垃圾」的一个目录，写下来才不会有人顺手清掉",
        rule=None,
    ),
    dict(
        key="golden-experiment-archive",
        label="非默认 agent 路径的 golden 对照留档",
        root=DEFAULT_REPORT_ROOT,
        patterns=["runs_*/**"],
        status="open", owner=None, owner_ref=None,
        retention="与关联 trace 一起处理，暂不自动清理",
        why="不同路径按目录隔离；报告是 trace 保留判断的证据，不能单独删除。",
        rule=None,
    ),
    dict(
        key="ci-test-report",
        label="CI pytest JUnit 报告",
        root=DEFAULT_REPORT_ROOT,
        patterns=["ci/**"],
        status="managed",
        owner="GitHub Actions artifact retention",
        owner_ref=".github/workflows/eval.yml",
        retention="GitHub artifact 保留 14 天；runner 销毁时清理工作目录，本机同路径覆盖",
        why="保留失败用例与耗时供复核，不进入 golden 基线或 trace 保留判断。",
        rule=None,
    ),
    dict(
        key="manual-eval-evidence",
        label="手工故障取证与临时冒烟报告",
        root=DEFAULT_REPORT_ROOT,
        patterns=["_outage_*/**", "_smoke_*/**"],
        status="open", owner=None, owner_ref=None,
        retention="暂不自动清理，复核后由维护者处理",
        why="人工保留的诊断证据，不参与默认基线统计。",
        rule=None,
    ),
    dict(
        key="slot-stability",
        label="槽稳定性读数（`eval/slot_stability.py` 的留档）",
        root=DEFAULT_REPORT_ROOT,
        patterns=["slot_stability/**"],
        status="managed",
        owner="eval/artifact_retention.py",
        owner_ref="eval/artifact_retention.py",
        retention=f"保留最近 {KEEP_COUNT_SLOT_STABILITY} 份",
        why="`<YYYYMMDD>_<HHMMSS>_<mmm>.json`，跑一次落一份（含逐遍的原始槽值）。"
            "**为什么与 `runs/` 分开一个目录**：那一个是 `golden_trace.prune` 的判据源、"
            "`landing_gate` 又按目录整扫，本读数混进去会被当成一次 golden 跑"
            "（`param_matrix` 当初混进 `runs/` 就是这个形状，见那条登记）。"
            "**为什么这一族敢单独清**：它自足——不像 `runs/` 那样是别的产物的证据源，"
            "一份读数删掉只影响它自己。",
        rule=dict(kind="keep-count", keep=KEEP_COUNT_SLOT_STABILITY,
                  name_re=SLOT_STABILITY_RE.pattern),
    ),
    dict(
        key="wordgraph-builds",
        label="词图构建中间产物",
        root=DEFAULT_REPORT_ROOT,
        patterns=["wordgraph/**"],
        status="managed",
        owner="eval/artifact_retention.py",
        owner_ref="eval/artifact_retention.py",
        retention=f"保留最近 {KEEP_COUNT_WORDGRAPH} 份",
        why="`<YYYYMMDD>-<HHMMSS>_vocab.txt|build.json`，重建一次落一套。"
            "产物本身由 `rag/wordgraph.py` 从语料重算 ⇒ 旧的是可再生的中间态，不是证据",
        rule=dict(kind="keep-count", keep=KEEP_COUNT_WORDGRAPH, name_re=WORDGRAPH_RE.pattern),
    ),
]


def _glob_to_re(pat: str) -> re.Pattern:
    """Compile policy globs: ** crosses directories, **/ allows zero levels, * and ? do not."""
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                i += 2
                if pat[i:i + 1] == "/":
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def iter_entries(root: str) -> list[str]:
    """List files and empty directories relative to root; directory entries end with a slash."""
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel != ".":
            try:
                empty = not os.listdir(dirpath)
            except OSError:
                empty = False
            if empty:
                out.append(rel.replace(os.sep, "/") + "/")
        for fn in filenames:
            p = os.path.join(rel, fn) if rel != "." else fn
            out.append(p.replace(os.sep, "/"))
    return sorted(out)


def _matcher(classes: list[dict], root: str) -> list[tuple[str, re.Pattern]]:
    """Compile filesystem patterns for the selected root; PostgreSQL policies are excluded."""
    out = []
    for c in classes:
        if store_of(c) != STORE_FS:
            continue
        if os.path.abspath(c["root"]) != os.path.abspath(root):
            continue
        for pat in c["patterns"]:
            out.append((c["key"], _glob_to_re(pat)))
    return out


def owner_of(rel: str, root: str, classes: list[dict] | None = None) -> str | None:
    """Return the first matching policy key, or None when the path is unregistered."""
    classes = CLASSES if classes is None else classes
    for key, rx in _matcher(classes, root):
        if rx.match(rel):
            return key
    return None


def matches(rel: str, c: dict) -> bool:
    """Match a path relative to a policy root without comparing physical root locations."""
    return any(_glob_to_re(p).match(rel) for p in c["patterns"])


def unregistered(root: str, classes: list[dict] | None = None) -> list[str]:
    """List unowned entries; a missing storage root has no entries."""
    if not os.path.isdir(root):
        return []
    return [rel for rel in iter_entries(root) if owner_of(rel, root, classes) is None]


def audit(roots: list[str] | None = None, classes: list[dict] | None = None) -> dict:
    """Report unowned entries and scanned counts for each selected storage root."""
    classes = CLASSES if classes is None else classes
    roots = [DEFAULT_LOGS_ROOT, DEFAULT_REPORT_ROOT] if roots is None else roots
    per = {r: unregistered(r, classes) for r in roots}
    return {"roots": roots, "unregistered": per,
            "scanned": {r: len(iter_entries(r)) for r in roots if os.path.isdir(r)},
            "total_unregistered": sum(len(v) for v in per.values())}


def _sample_path(pattern: str) -> str:
    """Construct a representative path for frozen-policy overlap checks."""
    return pattern.replace("**/", "").replace("**", "x").replace("*", "x").replace("?", "x")


def _frozen_covered(classes: list[dict]) -> list[str]:
    """Require keep-days exclusions to protect overlapping frozen policies."""
    bad = []
    for fc in classes:
        if fc["status"] != "frozen" or store_of(fc) != STORE_FS:
            continue
        for pat in fc["patterns"]:
            sp = _sample_path(pat)
            for mc in classes:
                rule = mc.get("rule") or {}
                if mc["status"] != "managed" or rule.get("kind") != "keep-days":
                    continue
                if not any(_glob_to_re(mp).match(sp) for mp in mc["patterns"]):
                    continue
                ex = rule.get("exclude") or []
                if not any(basename_match(os.path.basename(sp), e) for e in ex):
                    bad.append(f"{mc['key']}：frozen 的 `{pat}` 落在它的范围内，"
                               f"但它的 exclude 盖不住 ⇒ 那条规则会去删永久保留的东西")
    return bad


def basename_match(name: str, pattern: str) -> bool:
    """Match an exclusion basename using the same glob semantics as ownership patterns."""
    return bool(_glob_to_re(pattern).match(name))


def check_classes(classes: list[dict] | None = None) -> list[str]:
    """Validate policy structure, implementation references and frozen-file protection."""
    classes = CLASSES if classes is None else classes
    bad: list[str] = []
    seen: set[str] = set()
    frozen = {p for c in classes if c["status"] == "frozen" and store_of(c) == STORE_FS
              for p in c["patterns"]}
    for c in classes:
        k = c.get("key", "?")
        if k in seen:
            bad.append(f"{k}：key 重复")
        seen.add(k)
        if not c.get("why"):
            bad.append(f"{k}：没写 why（登记的意义就在于说清为什么是这个处置）")
        for f in ("label", "status", "retention"):
            if not c.get(f):
                bad.append(f"{k}：缺字段 {f}")
        need = ("root", "patterns") if store_of(c) == STORE_FS else ("dsn_env", "tables")
        for f in need:
            if not c.get(f):
                bad.append(f"{k}：store={store_of(c)} 却缺字段 {f}")
        if c["status"] not in ("managed", "frozen", "open"):
            bad.append(f"{k}：status 不认识（{c['status']}）")
        if c.get("owner_ref") and not os.path.exists(os.path.join(REPO, c["owner_ref"])):
            bad.append(f"{k}：owner_ref 指向的文件不存在（{c['owner_ref']}）")
        if c["status"] == "managed":
            if not c.get("owner"):
                bad.append(f"{k}：managed 但没有执行者（owner）")
            if not (c.get("rule") or c.get("owner_ref") or c.get("owner_external")):
                bad.append(f"{k}：managed 既没有 rule、也没有 owner_ref/owner_external"
                           "——那它到底谁在清？")
        if c["status"] in ("frozen", "open") and c.get("owner"):
            bad.append(f"{k}：{c['status']} 却写着执行者（自相矛盾）")
        for ex in (c.get("rule") or {}).get("exclude", []) or []:
            if not any(fp.endswith(ex) or ex.endswith(fp.split("/")[-1]) for fp in frozen):
                bad.append(f"{k}：exclude `{ex}` 没有对应的 frozen 登记项（排除必须写明为什么）")
    bad += _frozen_covered(classes)
    return bad


def main(argv=None):
    """Explicit, read-only operational inventory; never deletes artifacts."""
    parser = argparse.ArgumentParser(description="Audit artifact ownership and retention policies")
    parser.add_argument("--logs-root", default=DEFAULT_LOGS_ROOT)
    parser.add_argument("--report-root", default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--json", action="store_true",
                        help="Print the complete machine-readable audit")
    args = parser.parse_args(argv)
    roots = {DEFAULT_LOGS_ROOT: args.logs_root, DEFAULT_REPORT_ROOT: args.report_root}
    classes = [dict(c, root=roots.get(c.get("root"), c.get("root"))) for c in CLASSES]
    probs = check_classes(classes)
    a = audit(list(roots.values()), classes)
    if args.json:
        print(json.dumps({**a, "policy_errors": probs}, ensure_ascii=False, indent=2))
        return 1 if (probs or a["total_unregistered"]) else 0
    for p in probs:
        print(f"✗ 登记表自检：{p}")
    for r, u in a["unregistered"].items():
        if not os.path.isdir(r):
            print(f"— {r}：不存在（跳过）")
            continue
        print(f"== {r}（扫描 {a['scanned'][r]} 条）==")
        for rel in u:
            print(f"    ✗ 未登记：{rel}")
    print(f"未登记合计 {a['total_unregistered']} 条；登记表自检问题 {len(probs)} 条")
    return 1 if (probs or a["total_unregistered"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
