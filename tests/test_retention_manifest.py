"""Retention policies and inventory behavior use isolated artifact trees.

The operational command ``python eval/retention_manifest.py --json`` audits real
storage. Offline tests validate the same ownership and unknown-artifact checks
against temporary roots so a developer's report history cannot change CI results.
"""


import os
import sys
import tempfile
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent


sys.path.insert(0, str(ROOT))


sys.path.insert(0, str(ROOT / "eval"))


import retention_manifest as rm  # noqa: E402

FAILS: list[str] = []
check = collector(FAILS)


def _bad_class(**over) -> list[dict]:
    """一个"看起来对、实际有洞"的类 + 一个合法的冻结类（好让 exclude 有对应项）。"""
    good = dict(key="frozen-thing", label="冻结样本", root="/tmp",
                patterns=["keep/*.sql"], status="frozen", owner=None, owner_ref=None,
                retention="永久", why="样本", rule=None)
    bad = dict(key="bad-thing", label="坏样本", root="/tmp",
               patterns=["**"], status="managed", owner="someone",
               owner_ref=None, retention="1 天", why="样本",
               rule=dict(kind="keep-days", days=1, exclude=["nope/*"]))
    bad.update(over)
    return [good, bad]


def _pg_checks() -> None:
    """验证 PostgreSQL 保留策略登记了实际执行者，不能依赖磁盘扫描代替数据库清理。"""
    pg = [c for c in rm.CLASSES if rm.store_of(c) == rm.STORE_PG]
    check("⑥ 至少有一条 store=pg 的登记项（PG 那一路有人在管）",
          len(pg) >= 1, str([c["key"] for c in pg]))
    check("⑥ PG 类的判据是 dsn_env + tables，不是 root/patterns（它不在盘上，"
          "硬塞进路径判据只会得到一条恒不命中的规则）",
          all(c.get("dsn_env") and c.get("tables") for c in pg)
          and all("patterns" not in c and "root" not in c for c in pg))
    for c in pg:
        p = os.path.join(rm.REPO, c["owner_ref"])
        src = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
        check(f"⑥ {c['key']} 的执行者真的在盘上", bool(src), str(c["owner_ref"]))
        check(f"⑥ {c['key']} 的执行者**默认只列不删**（源码里有 `--apply`）", "--apply" in src)
        if c.get("cleanup_api") == "DBOSClient.delete_workflows":
            # Library-owned tables are cleaned through the public client, not handwritten SQL.
            import ast
            tree_cleanup = next(node for node in ast.parse(src).body
                                if isinstance(node, ast.FunctionDef) and node.name == "collect")
            calls = [node for node in ast.walk(tree_cleanup)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "delete_workflows"]
            check(f"⑥ {c['key']} 使用框架公开接口删除完整执行树",
                  len(calls) == 1 and any(kw.arg == "delete_children"
                  and isinstance(kw.value, ast.Constant) and kw.value.value is True
                  for kw in calls[0].keywords))
            check(f"⑥ {c['key']} 的默认预览与保留边界有独立行为测试",
                  os.path.isfile(os.path.join(rm.REPO, "tests/scheduling/test_retention.py")))
        else:
            check(f"⑥ {c['key']} 的执行者真的会删（源码里有 `DELETE FROM`）", "DELETE FROM" in src)
            check(f"⑥ {c['key']} 声明的表与执行者**同源**（改名会当场红）",
                  all(t in src for t in c["tables"]), str(c["tables"]))
    check("⑥ store 的缺省是 fs —— 老登记项一个都不用改",
          all(rm.store_of(c) == rm.STORE_FS for c in rm.CLASSES if "root" in c))
    check("⑥ PG 类**不**出现在任何一根树的归属表里（否则它会抢走盘上那条路径的答案）",
          all(rm.owner_of("checkpoints", rm.DEFAULT_LOGS_ROOT) != c["key"] for c in pg))


def _table_checks() -> None:
    """① 真表自洽：登记表自己讲不讲得通（`check_classes()` 回答的那几条）。"""
    probs = rm.check_classes()
    check("① 真表 check_classes 零问题", probs == [], "；".join(probs[:3]))
    check("① 登记表里每个 key 都是唯一的",
          len({c["key"] for c in rm.CLASSES}) == len(rm.CLASSES))
    check("① 每个类都有 why（登记的意义就是说清为什么是这个处置）",
          all(c.get("why") for c in rm.CLASSES))
    check("① managed 的执行者都能在盘上指认（rule / owner_ref / owner_external 三选一）",
          all(c.get("rule") or c.get("owner_ref") or c.get("owner_external")
              for c in rm.CLASSES if c["status"] == "managed"))
    check("① owner_ref 全部指向真实存在的文件（相对本仓）",
          all(os.path.exists(os.path.join(rm.REPO, c["owner_ref"]))
              for c in rm.CLASSES if c.get("owner_ref")))
    check("① 三种 status 都出现过（表不是只有一种处置）",
          {c["status"] for c in rm.CLASSES} == {"managed", "frozen", "open"},
          str(sorted({c['status'] for c in rm.CLASSES})))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="retention_manifest_")
    try:
        # ── ① 真表自洽 ────────────────────────────────────────────────────
        _table_checks()
        # 执行者在别的脚本里的那几个必须**真的**在那些脚本里被调用（不然 rule=None 只是脱身话术）
        for key, ref, needle in (("traces", "eval/trace_retention.py", "--apply"),
                                 ("golden-traces", "eval/golden_trace.py", "def prune")):
            text = (ROOT / ref).read_text(encoding="utf-8")
            check(f"① {key} 的 owner_ref 里确实有那种机制（{ref} 含 {needle!r}）",
                  needle in text)

        # ── ② 反向：坏表会被抓住（证明校验不是装饰） ──────────────────────
        check("② managed 没有执行者 ⇒ 报出来",
              any("owner" in p for p in rm.check_classes(
                  _bad_class(owner=None, owner_external=None))))
        check("② managed 没有任何可指的清理者 ⇒ 报出来",
              any("谁在清" in p for p in rm.check_classes(
                  _bad_class(owner="x", rule=None, owner_ref=None))))
        check("② frozen 却写着执行者 ⇒ 报出来（自相矛盾）",
              any("自相矛盾" in p for p in rm.check_classes(
                  _bad_class(status="frozen"))))
        check("② status 写错 ⇒ 报出来",
              any("status" in p for p in rm.check_classes(
                  _bad_class(key="k2", status="cleaned"))))
        check("② owner_ref 指向空气 ⇒ 报出来",
              any("不存在" in p for p in rm.check_classes(
                  _bad_class(key="k3", owner_ref="eval/没有这个文件.py"))))
        check("② 没写 why ⇒ 报出来",
              any("why" in p for p in rm.check_classes(_bad_class(key="k4", why=""))))
        check("② exclude 没有对应 frozen 登记项 ⇒ 报出来（排除得说明依据）",
              any("exclude" in p for p in rm.check_classes(_bad_class(key="k5"))))
        # 反向（`_frozen_covered`）：frozen 落在宽规则范围内、而 exclude 没盖住它
        # ⇒ 那条 keep-days 会去删「永久保留」的东西。这是**保护真正失效的那种形态**
        # （正向那条只证明"exclude 不凭空冒"，盖不住逆向的漏）。
        wide = dict(key="walso", label="宽规则", root="/tmp", patterns=["archive/**"],
                    status="managed", owner="x", owner_ref=None, retention="1 天",
                    why="样本", rule=dict(kind="keep-days", days=1, exclude=[]))
        keep = dict(key="keeper", label="永久保留", root="/tmp", patterns=["archive/*.sql"],
                    status="frozen", owner=None, owner_ref=None, retention="永久",
                    why="样本", rule=None)
        check("② frozen 落在宽规则范围内而 exclude 没盖住 ⇒ 报出来",
              any("盖不住" in p for p in rm.check_classes([wide, keep])),
              str(rm.check_classes([wide, keep])[:2]))
        covered = dict(wide, key="wcovered",
                       rule=dict(kind="keep-days", days=1, exclude=["*.sql"]))
        check("② 对照组：exclude 盖住 ⇒ 不再报",
              not any("盖不住" in p for p in rm.check_classes([covered, keep])))
        # `keep-days` 之外的规则不适用这条（只有按时间删的才会误伤折叠进来的例外）
        count_rule = dict(wide, key="wcount",
                          rule=dict(kind="keep-count", keep=1, family_re=rm.FAMILY_RE.pattern))
        check("② 对照组：keep-count 规则不适用这条（不按年龄删 ⇒ 不构成误伤）",
              not any("盖不住" in p for p in rm.check_classes([count_rule, keep])))
        # 对照组：把 exclude 换成冻结类里有的那个词，同一张表就干净了
        clean = _bad_class(key="k6",
                           rule=dict(kind="keep-days", days=1, exclude=["*.sql"]))
        check("② 对照组：exclude 有对应 frozen 项 ⇒ 不再报",
              not any("exclude" in p for p in rm.check_classes(clean)))

        # ── ③ glob 语义（`**` 跨层、`*` 不跨层、`**/` 零层） ───────────────
        by_key = {c["key"]: c for c in rm.CLASSES}
        cases = [
            ("agent/traces/20260925/x.json", "traces", True),
            ("agent/traces/x.json", "traces", True),                # ** 也匹配零层
            ("archive/20260829/a.log", "archive", True),
            ("archive/x/y/z.log", "archive", True),                 # ** 跨任意层
            ("agent/agent.log", "service-logs", True),              # 固定名 + 代际
            ("agent/agent.log.3.gz", "service-logs", True),
            ("rust.log", "service-logs", True),
            ("a/b/c.log", "service-logs", False),                   # * 不跨层
            ("agent/sub/deep.log", "service-logs", False),
            ("agent/somefile.txt", "service-logs", False),
            (".deploy.lock", "deploy-state", True),
            (".last_deploy_sha", "deploy-state", True),
            ("review_20260925_043825.md", "eval-reports", True),
            ("runs/20260925-043825_golden.json", "golden-run-archive", True),
            ("wordgraph/20260915-203224_vocab.txt", "wordgraph-builds", True),
            ("last_run.json", "eval-baselines", True),
            ("archive/DELETED-20260925.txt", "archive-audit-log", True),
            ("archive/chat_conv_20260903_pre.sql", "archive-sql-backup", True),
        ]
        wrong = [f"{rel}→{key}" for rel, key, want in cases
                 if (rm.owner_of(rel, by_key[key]["root"]) == key) is not want]
        check(f"③ {len(cases)} 条归属判断全部符合预期", not wrong, "；".join(wrong[:4]))
        # 更具体的类必须排在更宽的前面（否则 archive/ 下的 *.sql 会被 archive 吞掉）
        keys = [c["key"] for c in rm.CLASSES]
        check("③ 两条 archive 的 frozen 排在 archive 之前（归属答案不能反）",
              keys.index("archive-audit-log") < keys.index("archive")
              and keys.index("archive-sql-backup") < keys.index("archive"))
        # `nginx-error.log` 属于 service-logs（`*.log*` 命中；logrotate 的 glob 也认它），
        # 不是"未登记"的例子——真正没有主人长这样：
        check("③ 没登记的路径 owner_of 返回 None（不含混）",
              rm.owner_of("somefile.txt", rm.DEFAULT_LOGS_ROOT) is None
              and rm.owner_of("random_dir/x.bin", rm.DEFAULT_LOGS_ROOT) is None
              and rm.owner_of("nginx-error.log", rm.DEFAULT_LOGS_ROOT) == "service-logs")

        # ── ④ 枚举器：只收文件与空目录，相对路径、目录带尾斜杠 ─────────────
        d = os.path.join(tmp, "root")
        os.makedirs(os.path.join(d, "a", "empty"))
        os.makedirs(os.path.join(d, "a", "b"))
        for rel in ("top.txt", "a/mid.txt", "a/b/deep.txt"):
            open(os.path.join(d, rel), "w").write("x")
        got = rm.iter_entries(d)
        check("④ 文件 + 空目录（带尾斜杠）都在，父目录不算条目",
              got == ["a/b/deep.txt", "a/empty/", "a/mid.txt", "top.txt"], str(got))
        check("④ 有内容的中间目录不进表（结构不是产物）", "a/" not in got and "a/b/" not in got)
        check("④ 目录不存在 ⇒ unregistered 空表不抛",
              rm.unregistered(os.path.join(tmp, "nope")) == [])

        # Inventory both root types using the same audit API as operations.
        fixture_logs = os.path.join(tmp, "logs")
        fixture_reports = os.path.join(tmp, "reports")
        os.makedirs(os.path.join(fixture_logs, "agent", "traces", "20260925"))
        os.makedirs(fixture_reports)
        Path(fixture_logs, "rust.log").write_text("x")
        Path(fixture_logs, "agent", "traces", "20260925", "t.json").write_text("{}")
        Path(fixture_logs, "stray.bin").write_text("x")
        Path(fixture_reports, "unknown.bin").write_text("x")
        roots = {rm.DEFAULT_LOGS_ROOT: fixture_logs,
                 rm.DEFAULT_REPORT_ROOT: fixture_reports}
        classes = [dict(c, root=roots.get(c.get("root"), c.get("root"))) for c in rm.CLASSES]
        logs = [c for c in classes if c.get("root") == fixture_logs]

        def claims(rel: str) -> list[str]:
            return [c["key"] for c in logs if rm.matches(rel, c)]

        check("⑤ 日志夹具由 service-logs 认领", claims("rust.log") == ["service-logs"])
        check("⑤ trace夹具由 traces 认领",
              claims("agent/traces/20260925/t.json") == ["traces"])
        check("⑤ 未知产物没有被宽泛规则认领", claims("stray.bin") == [])
        inventory = rm.audit([fixture_logs, fixture_reports], classes)
        check("⑤ 两种根的未登记产物均被完整报告",
              inventory["unregistered"] == {fixture_logs: ["stray.bin"],
                                             fixture_reports: ["unknown.bin"]},
              str(inventory["unregistered"]))
        check("⑤ 审计汇总与实际扫描条目一致",
              inventory["total_unregistered"] == 2
              and inventory["scanned"] == {fixture_logs: 3, fixture_reports: 1},
              str(inventory))

        _pg_checks()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


def test_retention_manifest():
    main()

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


def test_explicit_inventory_cli_reports_unknown_files(tmp_path, capsys):
    import json

    logs, reports = tmp_path / "logs", tmp_path / "reports"
    logs.mkdir()
    reports.mkdir()
    (logs / "unowned.bin").write_bytes(b"test")
    status = rm.main(["--logs-root", str(logs), "--report-root", str(reports), "--json"])
    result = json.loads(capsys.readouterr().out)
    assert status == 1
    assert result["unregistered"] == {str(logs): ["unowned.bin"], str(reports): []}
    assert result["total_unregistered"] == 1 and result["policy_errors"] == []


if __name__ == "__main__":
    test_retention_manifest()
