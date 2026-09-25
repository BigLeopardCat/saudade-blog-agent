# -*- coding: utf-8 -*-
"""产物保留登记表：**落到盘上的每一类产物，必须在这里登记**。

## 为什么要有这张表（20260925 盘点的结论）

同一族坑这个仓踩了四次，每次症状都一样——**保留/清理策略「写了」，但没有任何东西在执行它**：

  1. R2 的保留数（`prune_r2.py` 之前只有注释里写着「只留最近 3 个」）；
  2. logrotate 的 traces 块（`rotate 14` 对「文件名唯一」的一次性产物结构上无效，
     实测最老文件 26 天、`.2.gz`/`.3.gz` 各 0 个）；
  3. `logs/archive/`（手工归档后 12M 里最老的到 2026-06-10，全仓零引用、没有任何机制认领）；
  4. `eval/report/runs/`（576 份 / 9.8M，零策略；而且它是 golden_traces 删除判据的证据源）。

前两次是「配置是装饰性的」，后两次是**连装饰都没有**——目录就那么长着，没人知道归谁。
所以这里换一个判据：**不问「有没有写保留策略」，问「这一类产物有没有登记」**。
登记只要求两件事：说清楚谁负责、以及为什么是这个处置；`managed` 必须有执行者，
`frozen`/`open` 必须写明为什么不动它。**没登记的产物 = 缺口**，由 `audit()` 报出来。

## 这张表管什么、不管什么

- 管：**盘的形状**——哪些类产物该存在、由谁清、留多久、谁碰不得。
- 不管：**内容是否正确**（那是测试的事）。审计器只回答「这个路径登记过吗」。
- 判据是**路径**，不是文件内容，也不看大小 ⇒ 对空目录同样有效
  （空 ≠ 不存在，空目录也得有主人；这条判据当初就是被一个 0 文件的空目录逼出来的）。
- 匹配**按 CLASSES 顺序取第一个命中**（`owner_of`）⇒ 更具体的类必须排在更宽的前面
  （`archive-sql-backup` 是 `archive/**` 的子集，所以必须排在 `archive` 之上）。
  排在前面的那个是**归属的答案**——`archive/` 下的
  `*.sql` 被判给 `archive-sql-backup`（永久保留），而不是判给 `archive`（90 天）——
  后者会让人读成「这个文件会被 retain 掉」，而事实正好相反。

## 与执行者的关系

`managed` 且带 `rule` 的类由 `eval/artifact_retention.py` 按这份表执行（表是唯一事实源，
脚本不再自己写一遍路径与天数）。`rule=None` 表示「有执行者，但在别的脚本里」
（trace 走 `eval/trace_retention.py`、golden trace 走 `eval/golden_trace.py::prune`）。
**改保留期只改这里的常量**，不去改夜间的命令行。

只 import os/re（与 `eval/trace_files.py` 同一条理由：读取端与离线测试要能引它，
不能连带拉起 pydantic-settings 与 `.env`）。
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 两个产物根。日志根是生产落点（与 settings.trace_dir 同址），报告根在仓库里
# （`eval/report/` 被 gitignore，是 run artifacts 不是源码）。
DEFAULT_LOGS_ROOT = "/home/ubuntu/memory_blog_rust/logs"
DEFAULT_REPORT_ROOT = os.path.join(REPO, "eval", "report")

# ── 保留期常量（**唯一的数在这里**，`artifact_retention.py` 与夜间脚本都不复述） ──
KEEP_DAYS_ARCHIVE = 90          # logs/archive/ 下手工归档的东西，三个月足够回头找
KEEP_COUNT_REPORTS = 20         # eval/report/ 每族报告保留最近 N 份
KEEP_COUNT_DRAFTS = 10          # 例外：golden 草稿族只留 10 份（人审用品的窗口比复审单短）
KEEP_COUNT_WORDGRAPH = 10       # 词图构建中间产物

# 报告文件名：`<族>_<YYYYMMDD>[-_]<HHMMSS>.<ext>`。族名非贪婪取到第一个 8 位日期前
# （`golden_drafts_20260921_014611.jsonl` ⇒ 族 = `golden_drafts`）。日期与时刻的分隔符
# **两种都出现过**（`20260824-002825` 与 `20260924_014855`）⇒ 都要认。
FAMILY_RE = re.compile(r"^([a-z][a-z0-9_]*?)_(\d{8})[-_](\d{6})\.(md|jsonl)$")

# 词图中间产物：`<YYYYMMDD>-<HHMMSS>_<vocab.txt|build.json>`
WORDGRAPH_RE = re.compile(r"^(\d{8})-(\d{6})_")


# ── 登记表 ────────────────────────────────────────────────────────────────────
# 字段：
#   key/label      标识与中文名
#   root/patterns  归属判据（相对 root 的 glob；`**` 跨层，`*` 不跨层）
#   status         managed（有执行者）/ frozen（刻意永不删）/ open（登记了，暂时没执行者）
#   owner          谁负责清（人读描述；**只有 managed 能填**，frozen/open 写了就是自相矛盾）
#   owner_ref      相关文件（**相对本仓**，存在性由 check_classes 校验；父仓文件写
#                  `../scripts/...` 这种形态）。managed 指执行者；frozen 指「谁在用它」
#                  ——没有清理者，但有被谁读写的事实
#   owner_external managed 专用：执行者不在本仓（如 logrotate 的配置在 /etc），没有 owner_ref 可指
#   retention      人读的保留期
#   why            为什么是这个处置（**必填**，包括 frozen/open 也要写）
#   rule           交给 `eval/artifact_retention.py` 的机器可读规则；None = 执行者在别处
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
    # 曾经这里登记过 `trace-audio`（`utils/tts.py` 写的 output_audio）。**20260925 删除**：
    # 那条登记项的三处坐标全是错的——tts.py 的 OUTPUT_DIR 已锚在**仓根**（不再随调用方
    # CWD 漂移），目录因此不在 `logs/` 之下（本表只管 `logs/` 这一棵树），而且那个目录
    # 已随修复一起消失（原先散落的 6 个都是 0 文件）。留一条"管不到的路径"等于给下一个人
    # 一个假坐标；真要再出现在 traces/ 下，`audit()` 会照常报"未登记"。
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
    # 两条 frozen 必须排在 archive 之上：否则 `archive/**` 先把它们认走，
    # owner_of 的答案就成了「90 天后删」——与事实正好相反
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
        # 没人负责清（frozen）⇒ 不填 owner（填了 check_classes 判自相矛盾）
        owner=None,
        # 不给 owner_ref：读写者在**父仓**（`scripts/deploy/deploy_from_r2.sh`），而 owner_ref
        # 是存在性校验的仓相对路径——父仓文件在独立克隆里不存在，填了会让 check_classes 变成
        # **环境依赖**（本机过、CI 红）。它只写进 why，不做机器校验：这里要的是"别删它"，
        # 不是"脚本在哪"。同族的诚实：机器能证的才写成断言，证不了的写成人读的话。
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


# ── 判据 ─────────────────────────────────────────────────────────────────────
def _glob_to_re(pat: str) -> re.Pattern:
    """把登记表里的 glob 翻成正则。`**` 跨层（`**/` 也匹配零层）、`*`/`?` 不跨层。

    不引 `pathlib.PurePath.match`：它不把 `**` 当跨层通配，
    `archive/**` 会匹配不到 `archive/20260829/x.log`——那正是这张表要判的东西。
    """
    out, i, n = [], 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 2] == "**":
                i += 2
                if pat[i:i + 1] == "/":       # `**/`：零层或多层
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
    """枚举 root 下**需要登记的条目**，相对路径、目录带尾斜杠。

    只收「文件」与「空目录」：中间层目录（`agent/`、`frontend/`）是结构不是产物，
    收了就得给每个父目录也编一条登记项。空目录要收——这条判据就是被一个 0 文件的空目录
    逼出来的（`agent/traces/output_audio/`，20260925 已随 `utils/tts.py` 的锚定修复消失），
    **空不等于不存在**（它照样得有主人）。
    """
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
    """[(class_key, 编译后的正则)]——只取 root 与当前根相同的类。"""
    out = []
    for c in classes:
        if os.path.abspath(c["root"]) != os.path.abspath(root):
            continue
        for pat in c["patterns"]:
            out.append((c["key"], _glob_to_re(pat)))
    return out


def owner_of(rel: str, root: str, classes: list[dict] | None = None) -> str | None:
    """这条相对路径归哪个类；没登记返回 None。**按 CLASSES 顺序取第一个命中**。"""
    classes = CLASSES if classes is None else classes
    for key, rx in _matcher(classes, root):
        if rx.match(rel):
            return key
    return None


def matches(rel: str, c: dict) -> bool:
    """rel（相对**这个类自己的 root**）是否属于类 `c`——只看 patterns，不看 root 落在哪。

    与 `owner_of` 的分工：`owner_of` 回答「这条路径归哪一类」（要比较 root，
    故按 CLASSES 顺序取第一个命中）；`matches` 回答「这个类要不要管这条路径」
    （不看 root，所以执行者可以用它配合 `--root` 覆盖到临时目录上）。
    glob 语义仍只有 `_glob_to_re` 一处实现。
    """
    return any(_glob_to_re(p).match(rel) for p in c["patterns"])


def unregistered(root: str, classes: list[dict] | None = None) -> list[str]:
    """root 下**没登记**的条目（这就是「缺口」的定义）。目录不存在 ⇒ 空表（不是异常）。"""
    if not os.path.isdir(root):
        return []
    return [rel for rel in iter_entries(root) if owner_of(rel, root, classes) is None]


def audit(roots: list[str] | None = None, classes: list[dict] | None = None) -> dict:
    """全量盘点：每根下没登记的条目 + 各自扫了多少条。"""
    classes = CLASSES if classes is None else classes
    roots = [DEFAULT_LOGS_ROOT, DEFAULT_REPORT_ROOT] if roots is None else roots
    per = {r: unregistered(r, classes) for r in roots}
    return {"roots": roots, "unregistered": per,
            "scanned": {r: len(iter_entries(r)) for r in roots if os.path.isdir(r)},
            "total_unregistered": sum(len(v) for v in per.values())}


def _sample_path(pattern: str) -> str:
    """把一个 glob 变成一条具体的样本路径（`archive/DELETED-*.txt` → `archive/DELETED-x.txt`）。

    用于"这条 frozen 规则是否落在某个 managed 规则的范围里"——才够判**子集关系**：
    glob 的子集判定一般做不到，但拿样本路径去过一遍宽规则，足以抓住实际会出现的那种疏漏
    （有人加了一条永久保留的例外、却忘了从 keep-days 的 exclude 里排掉）。
    """
    return pattern.replace("**/", "").replace("**", "x").replace("*", "x").replace("?", "x")


def _frozen_covered(classes: list[dict]) -> list[str]:
    """**反向校验**：frozen 的规则落在某个 managed keep-days 类的范围里时，
    那个类的 exclude 必须盖住它。少了这层，保护就只活在"我记得加过 exclude"里。"""
    bad = []
    for fc in classes:
        if fc["status"] != "frozen":
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
    """基名 glob（`rule.exclude` 用的形态，如 `DELETED-*.txt`）。

    **不引 fnmatch**：glob 语义在这份文件里只有 `_glob_to_re` 一处实现
    （`*` = `[^/]*`，基名里没有 `/` 故等价）。两个实现就会有两种行为，
    而 exclude 的失效方式是"以为护住了、其实没有"——正是这条链最不该有的歧义。
    """
    return bool(_glob_to_re(pattern).match(name))


def check_classes(classes: list[dict] | None = None) -> list[str]:
    """登记表自身的结构自检，返回问题清单（空表 = 表是自洽的）。"""
    classes = CLASSES if classes is None else classes
    bad: list[str] = []
    seen: set[str] = set()
    frozen = {p for c in classes if c["status"] == "frozen" for p in c["patterns"]}
    for c in classes:
        k = c.get("key", "?")
        if k in seen:
            bad.append(f"{k}：key 重复")
        seen.add(k)
        if not c.get("why"):
            bad.append(f"{k}：没写 why（登记的意义就在于说清为什么是这个处置）")
        for f in ("label", "root", "patterns", "status", "retention"):
            if not c.get(f):
                bad.append(f"{k}：缺字段 {f}")
        if c["status"] not in ("managed", "frozen", "open"):
            bad.append(f"{k}：status 不认识（{c['status']}）")
        # owner_ref 的存在性对所有类都查（frozen 的 owner_ref 指「谁在用它」，一样不许指向空气）
        if c.get("owner_ref") and not os.path.exists(os.path.join(REPO, c["owner_ref"])):
            bad.append(f"{k}：owner_ref 指向的文件不存在（{c['owner_ref']}）")
        if c["status"] == "managed":
            if not c.get("owner"):
                bad.append(f"{k}：managed 但没有执行者（owner）")
            # 「谁在清」必须落在盘上可指的地方：本仓脚本、别的执行者脚本、或仓外的执行者
            # （logrotate 的配置在 /etc）——三者都没有，这条登记就只是句口头承诺
            if not (c.get("rule") or c.get("owner_ref") or c.get("owner_external")):
                bad.append(f"{k}：managed 既没有 rule、也没有 owner_ref/owner_external"
                           "——那它到底谁在清？")
        if c["status"] in ("frozen", "open") and c.get("owner"):
            bad.append(f"{k}：{c['status']} 却写着执行者（自相矛盾）")
        # exclude 必须与某个 frozen 类的 patterns 对得上：否则「排除」就成了一句没登记的口头承诺
        for ex in (c.get("rule") or {}).get("exclude", []) or []:
            if not any(fp.endswith(ex) or ex.endswith(fp.split("/")[-1]) for fp in frozen):
                bad.append(f"{k}：exclude `{ex}` 没有对应的 frozen 登记项（排除必须写明为什么）")
    # 双向：上面正向管「exclude 不许凭空冒出来」，反向管「frozen 不许被宽规则吞掉」
    bad += _frozen_covered(classes)
    return bad


if __name__ == "__main__":
    import sys
    probs = check_classes()
    for p in probs:
        print(f"✗ 登记表自检：{p}")
    a = audit()
    for r, u in a["unregistered"].items():
        if not os.path.isdir(r):
            print(f"— {r}：不存在（跳过）")
            continue
        print(f"== {r}（扫描 {a['scanned'][r]} 条）==")
        for rel in u:
            print(f"    ✗ 未登记：{rel}")
    print(f"未登记合计 {a['total_unregistered']} 条；登记表自检问题 {len(probs)} 条")
    sys.exit(1 if (probs or a["total_unregistered"]) else 0)
