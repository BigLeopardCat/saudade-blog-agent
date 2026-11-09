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
        why='Stable log filenames use daily rotation with 14 compressed generations; copytruncate '
            'preserves open descriptors.',
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
        why='Historical SQLite ledger files may require checkpoint evidence before deletion. '
            'Current production writes use the PostgreSQL ledger; this rule does not enable a '
            'SQLite fallback.',
        rule=None,
    ),
    dict(
        key="agent-scheduling-store", cleanup_api="DBOSClient.delete_workflows",
        label="DBOS plans, chat runs and execution streams", store=STORE_PG,
        status="managed", owner="agent/scheduling/retention.py",
        owner_ref="agent/scheduling/retention.py", retention="Terminal runs older than 30 days",
        why="Public DBOS APIs delete terminal execution trees. Live and paused plans remain; "
            "child executions must be terminal. Chat streams expire without deleting task checkpoints. "
            "Nightly cleanup is disabled with scheduling.",
        rule=None, dsn_env="SCHEDULING_DSN",
        tables=["saudade_automations.workflow_status", "saudade_automations.operation_outputs",
                "saudade_automations.schedules", "saudade_automations.streams",
                "saudade_automations.workflow_events"],
    ),
    dict(
        key="agent-checkpoint-store",
        label="PostgreSQL 会话、任务记录与执行回执",
        store=STORE_PG,
        status="managed",
        owner="eval/pg_retention.py",
        owner_ref="eval/pg_retention.py",
        retention=f"{KEEP_DAYS_CHECKPOINT} 天无活动且不可恢复的会话；已完成回执同窗清理",
        why='Recheck activity and framework state under the runtime conversation lock. '
            'Delete eligible checkpoints through the public saver API and task records in the '
            'same transaction. Preserve busy, resumable and undated conversations, and unresolved '
            'write intents. Expired completed receipts and deletion fences are cleaned separately.',
        rule=None,
        dsn_env="CHECKPOINT_DSN",
        tables=["checkpoints", "checkpoint_blobs", "checkpoint_writes", "agent_task_inputs",
                "agent_task_usage", "agent_write_idem", "agent_deleted_threads",
                "agent_deleted_turns"],
    ),
    dict(
        key="golden-traces",
        label="golden 每轮跑落的 trace",
        root=DEFAULT_LOGS_ROOT,
        patterns=["agent/golden_traces/**"],
        status="managed",
        owner="eval/golden_trace.py::prune",
        owner_ref="eval/golden_trace.py",
        retention="最近 30 个 run + **所有有失败记录的 run 永久保留**",
        why='Keep recent runs and all failed, flaky or unverified runs. Missing report links and '
            'unreadable reports never authorize trace deletion.',
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
        why='Preserve pre-migration data snapshots that cannot be reconstructed from another '
            'source; age alone does not authorize deletion.',
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
        why='Remove expired ordinary archives while preserving SQL snapshots and deletion audit '
            'records. Exclusions must match the separately frozen ownership entries.',
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
        why='Deployment locks, prior release identity and the current asset manifest are live '
            'operational state. They are not disposable log output.',
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
        why='Retain the latest reports per filename family, with a shorter count for candidate '
            'drafts. Count-based retention preserves recent runs during dense debugging.',
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
        why="独立的槽稳定性诊断材料；与任务报告分目录保留，避免混入任务质量样本。",
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
