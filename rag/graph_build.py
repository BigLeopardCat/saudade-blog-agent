"""向量图谱重建任务（20261003 用户第 2 条）。

## 为什么要有这个模块

建图脚本 `scripts/build_word_graph.py` 此前**只在开发机上手跑**：产物写进
`frontend/public/graph/`，要等一次 `vite build` 才进 dist。于是"别人迁移过去用不了
这张图"有两层原因——产物里的站点归属是**构建期**烧死的（迁移后重建成功也画不出来），
而且就算重建成功，服务端写的文件也到不了浏览器。

本模块把"重建"做成**服务端后台任务**：后台点一下 → 这里起一个子进程 → 页面轮询进度。
产物写进 agent 自己的 `data/word_graph/web/`（脚本的 `--out-web`），由 Rust 的
`/api/public/graph/*` 直接供——不碰父仓、不产生 git 变更、不等 CI。

## 四样落盘的东西（uvicorn 起 4 个 worker，各持各的内存）

============================  ==================================================
`logs/graph_build/lock`       任务锁（`O_CREAT|O_EXCL` + pid 存活探测回收僵尸锁）
`logs/graph_build/state.json` 任务状态（原子 `os.replace`）
`logs/graph_build/<ts>.log`   该次任务的完整日志（不受环形缓冲的 400 行上限影响）
`data/word_graph/web/`        **上一次成功**的展示产物（失败的运行不会覆盖它）
============================  ==================================================

**为什么锁是文件而不是 `threading.Lock`**：4 个 worker 是 4 个**进程**，内存里的锁
互相看不见；轮询请求也不保证落回同一个 worker（nginx 不做会话粘性）。所以状态只能
从盘上读，而且读的时候要**当场核一遍 pid 是否还活着**——worker 被重启（部署/OOM）会
留下一个再也没人写终态的 `state.json`，只看文件就会永远显示"运行中"。

**僵尸锁怎么回收**：锁里记的是 `owner_pid`（起任务的 worker）与 `child_pid`（建图
进程）。属主没了而 `child_pid` 还在时，**先把子进程组杀掉再放行新任务**——子进程用
`start_new_session=True` 起的，属主死了它不会死，但它的 stdout 管道读端已随属主消失
（日志再也进不了 state、进程通常卡在 EPIPE 上），继续挂着只会白占内存，而这台机器
只有 3.7GB。这条取舍写在这里：**宁可杀掉一个快跑完的图，也不留一个没人看着的建图进程**。
（图本身可重算，代价是几分钟与一次 embedding 费用。）

## 诚实报告

失败要给 `exit_code` 与日志尾部；`--dry-run` 要报"没调 embedding"，不能把 0/0 说成
"没有新增向量"（那是**没调**和**调了但全命中缓存**两件事，见下面 `_embed_stats`）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
# 查询侧产物（`rag/wordgraph.py` 读它）——建图脚本的 `--out-agent`
GRAPH_DIR = ROOT / "data" / "word_graph"
# 展示侧产物（Rust 的 `GRAPH_ARTIFACT_DIR` 默认指向它）——建图脚本的 `--out-web`
WEB_DIR = GRAPH_DIR / "web"
STATE_DIR = ROOT / "logs" / "graph_build"
LOCK_PATH = STATE_DIR / "lock"
STATE_PATH = STATE_DIR / "state.json"
BUILD_SCRIPT = ROOT / "scripts" / "build_word_graph.py"
REQUIREMENTS = ROOT / "scripts" / "requirements-graph.txt"

# 环形缓冲只留最后这么多行给页面看（完整日志在 <ts>.log 里，页面给的是路径）
TAIL_LINES = 400
# 起建图前的内存闸：低于它**拒绝启动**并把当前值报出来，不做"试试看"。
#
# **这个数不是拍的，是量出来的**（20261003 纠正：原来写 1200MB 纯属估高，本机可用内存
# 常在 900MB 上下 ⇒ 门永远关着，"腾一腾再来"根本腾不到）。实测口径与结果：
#   `systemd-run --user --scope -p MemoryMax=1000M -p MemorySwapMax=1536M` 里跑一整次
#   真实重建（400 节点 / 12 篇文章 / UMAP），**峰值 RSS 552MB**（含 uv 那一层），
#   30 秒跑完，换页 0 次 ⇒ 1000MB 的笼子都没碰到。640 = 552 × ~1.16 的余量。
#
# **余量为什么故意留得小**（20261004 二次修正：先写成 700，当场就撞上了）：这台机器
# 的 MemAvailable 随开发工具起伏，实测 699–1070MB 之间晃。余量一大，门就常年关着
# ——那正是被修掉的那个毛病（主人连着点两次都只拿到"内存不够"）。640 的判据是
# "实测跑得完"，不是"看着放心"；真跑起来超了，OOM 会先找上这个 552MB 的进程，
# 而不是旁边的 MySQL/Rust（它们的 RSS 是几十 MB 量级）。
#
# ⚠️ 这个数与**节点数**正相关（UMAP/numba 的中间量按点数长）。默认上限是 400 节点；
# 把 `--max-nodes` 提到 2000 的站点要自己重新量一遍再抬这个数——量法同上（笼子设小，
# OOM 只死那个 scope，不会拖垮整机）。可用内存读的是 `/proc/meminfo` 的 MemAvailable，
# 换页空间**不计入**（真跑起来全靠 swap 会把这台机器拖到没反应，那正是 2026-08 那次
# OOM 的形状，宁可拒绝）。
# 紧急放行（主人自己承担风险，例如明确知道机器上还有可回收的页缓存）：
#   `GRAPH_BUILD_MEM_MIN_MB=500` 写进 agent 的 .env —— 非正数/非整数一律回落默认值。
MEM_MIN_MB = 640
MEM_ENV = "GRAPH_BUILD_MEM_MIN_MB"


def mem_min_mb() -> int:
    """内存闸的阈值。环境变量只在能解析成正整数时才认——写坏了回落默认值，
    不让一个错别字把重建功能整条锁死（同 `CHAT_QUOTA_LIMIT` 那条口径）。"""
    raw = os.environ.get(MEM_ENV, "").strip()
    if raw:
        try:
            v = int(float(raw))
        except ValueError:
            v = 0
        if v > 0:
            return v
    return MEM_MIN_MB


# `uv` 只装在用户目录时（`~/.local/bin/uv`）**systemd 服务里 `shutil.which` 找不到它**：
# 单元文件没有 `Environment=PATH=`，system 服务的默认 PATH 只有
# /usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin —— 不含 ~/.local/bin。
# 症状是"uv 明明装着，页面却报找不到 uv 命令"（20261003 实踩）。所以除了 PATH，
# 再按这张表找一遍；找不到时把**找过的地方**一起报给用户，别只说"没有"。
UV_FALLBACKS = ("~/.local/bin/uv", "~/.cargo/bin/uv", "/usr/local/bin/uv", "/snap/bin/uv")
_UV_PATH: str | None = None


def find_uv() -> str | None:
    """解析 uv 的**绝对路径**（找到一次就记住）。找不到返回 None。"""
    global _UV_PATH
    if _UV_PATH:
        return _UV_PATH
    found = shutil.which("uv")
    if not found:
        for cand in UV_FALLBACKS:
            p = Path(cand).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                found = str(p)
                break
    if found:
        _UV_PATH = found
        logger.info("[graph_build] uv = %s（PATH 里%s）", found,
                    "有" if shutil.which("uv") else "没有，走回落表")
    return found
# 取消：先 SIGTERM 整个进程组，宽限期内没退再 SIGKILL
CANCEL_GRACE = 5.0
# 语料来源缺省值：走回环直连 Rust（不绕 nginx）。**读的是 settings 那一项**——
# 在这里另写一个常量就等于第二份真相源（改了一处、另一处不跟着变，而且不会报错）。
# 产物归属站点**不从这里推**：页面会把浏览器自己的 origin 传上来（见 `server.py`
# 的 `/graph/rebuild`），因为 `http://127.0.0.1:3000` 与 `https://<域名>` 是两个
# origin，推错了首页就不画图。
_API_BASE_FALLBACK = "http://127.0.0.1:3000/api/public"


def default_api_base() -> str:
    from config.settings import settings
    return (getattr(settings, "graph_api_base", "") or "").strip() or _API_BASE_FALLBACK

# `embedding 新增 N 条（缓存命中 M 条）`（scripts/build_word_graph.py 的 embed_words）
_EMBED_RE = re.compile(r"embedding 新增 (\d+) 条（缓存命中 (\d+) 条）")

# 本进程内的一次任务（别的 worker 的任务在这里恒为 None —— 状态一律以盘为准）
_lock = threading.Lock()
_tail: list[str] = []
_proc: "subprocess.Popen | None" = None
_own_run: str | None = None


# ─────────────────────────────────────────────────────── 状态文件（原子写）

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_state() -> dict:
    return _read_json(STATE_PATH) or {"status": "idle"}


def _write_state(st: dict) -> None:
    """原子写：轮询端永远读到完整 JSON（半个 JSON 会让页面显示"解析失败"而不是进度）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _merge(patch: dict) -> dict:
    """读-改-写。**不能直接拿内存里那份整份覆盖**：取消请求可能来自另一个 worker，
    它在盘上留的 `cancel_requested` 会被属主进程的下一笔盖掉。"""
    st = _read_state()
    st.update(patch)
    try:
        _write_state(st)
    except OSError:
        logger.warning("[graph_build] state.json 写不进去", exc_info=True)
    return st


# ─────────────────────────────────────────────────────────── 进程与锁

def _pid_alive(pid: int) -> bool:
    """pid 存活探测。**僵尸进程（已退出、父进程还没 wait）也会回 True**——这里够用：
    属主是 uvicorn worker，子进程退出后它很快 wait；退一步说，把一个僵尸判成"还在跑"
    只是让新任务多等一轮，而把活着的判成死了会同时跑起两份建图。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock() -> dict | None:
    return _read_json(LOCK_PATH)


def _kill_pg(pid: int, sig: int) -> bool:
    """给**子进程组**发信号。`start_new_session=True` ⇒ 子进程自成会话、pgid == pid，
    所以 killpg 不会误伤 uvicorn 自己那一组；而用 `os.kill` 只杀得掉 `uv` 这一层，
    底下的 python/numba 会继续跑（那正是"取消了但内存还在涨"的形状）。"""
    if pid <= 0:
        return False
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _acquire_lock(run_id: str) -> dict | None:
    """抢锁。拿到返回锁内容，别人在做返回 None。僵尸锁**先收尸再抢**（见模块头注）。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"run_id": run_id, "owner_pid": os.getpid(),
                          "child_pid": 0, "at": _now()}).encode("utf-8")
    for attempt in (1, 2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            held = _read_lock() or {}
            owner = int(held.get("owner_pid") or 0)
            if _pid_alive(owner):
                return None
            if attempt == 2:                       # 刚被另一路抢走 ⇒ 让给他
                return None
            logger.warning("[graph_build] 回收僵尸锁（属主 pid=%s 已不在，run=%s）",
                           owner, held.get("run_id"))
            _reap_orphan(held)
            try:
                LOCK_PATH.unlink()
            except OSError:
                pass
            continue
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            return json.loads(payload.decode("utf-8"))
    return None


def _reap_orphan(held: dict) -> None:
    """属主已经没了：把还活着的建图子进程组收掉（理由见模块头注）。"""
    pid = int(held.get("child_pid") or 0)
    if pid > 0 and _pid_alive(pid):
        logger.warning("[graph_build] 收掉游离子进程 pid=%s（属主已不在）", pid)
        _kill_pg(pid, signal.SIGTERM)
        for _ in range(int(CANCEL_GRACE / 0.2)):
            time.sleep(0.2)
            if not _pid_alive(pid):
                return
        _kill_pg(pid, signal.SIGKILL)


def _owns_lock(run_id: str) -> bool:
    """锁还是不是我这次任务的。**收尾写状态前必查**：僵尸锁被别的 worker 回收并起了
    新任务之后，我们这笔迟到的收尾会把新任务的状态改成自己的终态。"""
    held = _read_lock() or {}
    return held.get("run_id") == run_id


def _release_lock(run_id: str) -> None:
    if not _owns_lock(run_id):
        return
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# ─────────────────────────────────────────────────────────── 前置条件

def mem_available_mb() -> int | None:
    """`/proc/meminfo` 的 MemAvailable（MB）。取不到返回 None（不拿"0"顶替——那会
    把"读不出来"变成"内存不够"，见热度那族的同一条纪律）。"""
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def preflight(mode: str) -> dict | None:
    """起任务前的确定性检查。返回 None = 可以起；否则返回给调用方的拒因。"""
    if find_uv() is None:
        tried = "、".join(("PATH", *UV_FALLBACKS))
        return {"ok": False, "reason": "uv_missing",
                "error": f"找不到 uv（找过：{tried}）——建图依赖（umap/numba/scipy）刻意不进"
                         f"生产 venv，重建要靠 `uv run --no-project` 现装。"
                         f"装好 uv（或把它的路径加进上面那张回落表）再试。"}
    if not BUILD_SCRIPT.exists() or not REQUIREMENTS.exists():
        return {"ok": False, "reason": "script_missing",
                "error": f"建图脚本或依赖清单不在：{BUILD_SCRIPT} / {REQUIREMENTS}"}
    avail = mem_available_mb()
    need = mem_min_mb()
    if avail is not None and avail < need:
        extra = "" if need == MEM_MIN_MB else f"（当前阈值来自 {MEM_ENV}={need}）"
        return {"ok": False, "reason": "low_memory",
                "error": f"可用内存 {avail}MB，低于本次任务的最低要求 {need}MB{extra}。"
                         f"这个阈值是实测线（400 节点的一次真实重建峰值 552MB，取 ~1.27 倍余量），"
                         f"不是拍的；换页空间不计入。腾出内存后再试——**不建议硬上**："
                         f"这台机器 OOM 会拖垮整站。"}
    return None


# ─────────────────────────────────────────────────────────── 命令行

# 页面表单能改的那几项。键 = 前端传的名字，值 = (argparse 名, 类型, 下限, 上限)。
# **白名单**：只认这几项，其余一律不收（表单是外部输入，别把任意 argv 拼进子进程）。
_INT_PARAMS = {
    "max_nodes": ("--max-nodes", 20, 2000),
    "min_chars": ("--min-chars", 0, 100000),
}
_CHOICE_PARAMS = {"layout": ("--layout", ("umap", "semantic", "pca"))}
_STR_PARAMS = {"api_base": ("--api-base", 200), "site": ("--site", 200),
               "exclude_ids": ("--exclude-ids", 500)}
_FLAG_PARAMS = {"refresh": "--refresh", "force": "--force", "dry_run": "--dry-run"}
# 开关的真值集。**不能只写 `if raw.get(key)`**：这个值来自网页表单，字符串
# "false" 在 Python 里是真的 —— 页面上没勾的框反而会把 `--refresh` 打开，而
# `--refresh` 的语义是**强制重嵌全部节点**（真花钱）。所以只认"确实是真"的那几种写法。
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


def _as_flag(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in _TRUE_WORDS


def resolve_params(raw: dict | None) -> dict:
    """表单 → 归一化后的参数字典。不合法**抛 ValueError**（由端点转成 400），
    不静默丢弃——静默丢弃会让"我明明填了 800 个节点"变成"出来还是 400"。"""
    raw = raw or {}
    out: dict = {"api_base": default_api_base(), "site": "", "exclude_ids": None,
                 "max_nodes": 400, "min_chars": 400, "layout": "umap",
                 "refresh": False, "force": False, "dry_run": False}
    for key, (_, lo, hi) in _INT_PARAMS.items():
        if key not in raw or raw[key] in ("", None):
            continue
        try:
            v = int(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} 要是整数，收到 {raw[key]!r}")
        if not (lo <= v <= hi):
            raise ValueError(f"{key} 要在 {lo}..{hi} 之间，收到 {v}")
        out[key] = v
    for key, (_, choices) in _CHOICE_PARAMS.items():
        if key not in raw or raw[key] in ("", None):
            continue
        v = str(raw[key])
        if v not in choices:
            raise ValueError(f"{key} 只能是 {'/'.join(choices)}，收到 {v!r}")
        out[key] = v
    for key, (_, limit) in _STR_PARAMS.items():
        if key not in raw or raw[key] is None:
            continue
        v = str(raw[key]).strip()
        if len(v) > limit:
            raise ValueError(f"{key} 太长（上限 {limit} 字符）")
        # exclude_ids 的空串**是有意义的值**（＝一个都不排除，见 20261003 修的静默 bug），
        # 所以这里保留空串、只在 None 时回落到默认
        out[key] = v
    for key in _FLAG_PARAMS:
        out[key] = _as_flag(raw.get(key))
    if out["exclude_ids"] is None:
        out["exclude_ids"] = None      # None ⇒ 交给脚本用它自己的默认值
    return out


def _build_argv(params: dict, mode: str) -> list[str]:
    # uv 用**绝对路径**（`find_uv`）：服务里的 PATH 没有 ~/.local/bin，写裸 `uv` 会在
    # Popen 那一刻才炸 FileNotFoundError，而那时锁已经拿了、状态已经置成 running。
    # 顺带一个好处：页面 `<details>` 里展示的 argv 会带上真实路径，排障一眼看得出用的哪个 uv。
    base = [find_uv() or "uv", "run", "--no-project", "--python", "3.12",
            "--with-requirements", str(REQUIREMENTS)]
    if mode == "precheck":
        # 只验"依赖装不装得起来"（首次会下 470MB，页面要提示这一点）
        return base + ["python3", "-c", "import numpy, jieba, umap"]
    argv = base + ["python3", str(BUILD_SCRIPT),
                   "--api-base", params["api_base"],
                   "--max-nodes", str(params["max_nodes"]),
                   "--min-chars", str(params["min_chars"]),
                   "--layout", params["layout"],
                   # 展示产物写 agent 自己的目录（Rust 经 GRAPH_ARTIFACT_DIR 供出去）：
                   # 给了 --out-web 脚本就不碰 frontend/public，也就不产生任何 git 变更
                   "--out-web", str(WEB_DIR),
                   "--out-agent", str(GRAPH_DIR)]
    if params["site"]:
        argv += ["--site", params["site"]]
    if params["exclude_ids"] is not None:
        argv += ["--exclude-ids", params["exclude_ids"]]
    for key, flag in _FLAG_PARAMS.items():
        if params.get(key):
            argv.append(flag)
    return argv


# ─────────────────────────────────────────────────────────── 起任务

def start(raw_params: dict | None = None, mode: str = "rebuild") -> dict:
    """起一次任务（`mode` = rebuild / precheck）。返回 `{ok, ...}`。

    **precheck 与 rebuild 共用同一把锁、同一个 state、同一条轮询路径**——它们是
    "同一个时刻只能有一件在建图机上跑的事"的两种形态，多开一套状态只会多一处
    会漂的真相源。
    """
    global _proc, _own_run
    if mode not in ("rebuild", "precheck"):
        return {"ok": False, "reason": "bad_mode", "error": mode}
    try:
        params = resolve_params(raw_params)
    except ValueError as e:
        return {"ok": False, "reason": "bad_params", "error": str(e)}

    blocked = preflight(mode)
    if blocked:
        return blocked

    run_id = time.strftime("%Y%m%dT%H%M%S")
    held = _acquire_lock(run_id)
    if held is None:
        cur = status()
        return {"ok": False, "reason": "busy",
                "error": f"已经有一件任务在跑（run={cur.get('run_id')} "
                         f"status={cur.get('status')}）——同一时刻只允许一件，"
                         f"要么等它结束，要么先取消。",
                "state": cur}

    argv = _build_argv(params, mode)
    log_path = STATE_DIR / f"{run_id}.log"
    with _lock:
        _tail.clear()
        _proc = None
        _own_run = run_id
    _merge({"status": "running", "mode": mode, "run_id": run_id,
            "owner_pid": os.getpid(), "child_pid": 0,
            "started_at": _now(), "ended_at": None, "exit_code": None,
            "params": params, "argv": argv, "log": str(log_path),
            "tail": [], "result": None, "error": None,
            "cancel_requested": False, "mem_available_mb": mem_available_mb()})
    threading.Thread(target=_run, name=f"graph-build-{run_id}",
                     args=(run_id, argv, mode, log_path), daemon=True).start()
    logger.info("[graph_build] 启动 %s run=%s mem=%sMB argv=%s",
                mode, run_id, mem_available_mb(), " ".join(argv[3:]))
    return {"ok": True, "started": True, "run_id": run_id, "state": status()}


def _run(run_id: str, argv: list[str], mode: str, log_path: Path) -> None:
    global _proc
    started = time.time()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # uv 要靠 HOME 找缓存（`~/.cache/uv`，那 470MB 依赖就在里面）与解释器目录；PATH 里
    # 补上 uv 所在目录，uv 自己再 shell out 找东西时不会二次踩同一个坑。systemd 给
    # User= 的服务设 HOME/LOGNAME（SetLoginEnvironment 默认开），但这里是**显式兜底**：
    # 少了 HOME，uv 会当成长得像 root 的环境去 /root/.cache 重建一份缓存——症状是
    # "头一次跑完，第二次又从头下 470MB"。
    env.setdefault("HOME", str(Path.home()))
    uv_bin = find_uv()
    if uv_bin:                              # 相对名（"uv"）不进 PATH，`.` 当目录只会添乱
        env["PATH"] = str(Path(uv_bin).parent) + os.pathsep + env.get("PATH", "").lstrip(os.pathsep)
    try:
        proc = subprocess.Popen(                       # noqa: S603 —— argv 由白名单拼出
            argv, cwd=str(ROOT), env=env, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # 自成进程组：取消/收尸时一次 killpg 收干净（见 _kill_pg）
            start_new_session=True,
        )
    except OSError as e:
        logger.exception("[graph_build] 起子进程失败 run=%s", run_id)
        _finish(run_id, mode, None, started, error=f"起子进程失败：{e}")
        return
    with _lock:
        _proc = proc
    _merge({"child_pid": proc.pid})

    last_flush = time.time()
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                with _lock:
                    _tail.append(line)
                    del _tail[:-TAIL_LINES]
                f.write(line + "\n")
                f.flush()
                now = time.time()
                if now - last_flush > 1.0:
                    last_flush = now
                    _flush_tail()
    except Exception:                                  # 读管道出意外也要收尾，不能留锁
        logger.exception("[graph_build] 读子进程输出失败 run=%s", run_id)
    rc = proc.wait()
    with _lock:
        _proc = None
    _finish(run_id, mode, rc, started)


def _flush_tail() -> None:
    with _lock:
        snapshot = list(_tail)
    held = _read_lock() or {}
    if held.get("run_id"):
        _merge({"tail": snapshot})


def _finish(run_id: str, mode: str, rc: int | None, started: float,
            error: str | None = None) -> None:
    """写终态。**先查锁还是不是自己的**（见 `_owns_lock` 的理由）。"""
    global _own_run
    elapsed = round(time.time() - started, 1)
    with _lock:
        if _own_run == run_id:
            _own_run = None
    if not _owns_lock(run_id):
        logger.warning("[graph_build] run=%s 的锁已被回收/易主，跳过收尾写状态", run_id)
        return
    st = _read_state()
    tail = st.get("tail") or []
    with _lock:
        tail = list(_tail) or tail
    cancelled = bool(st.get("cancel_requested"))
    ok = rc == 0
    status_word = ("ok" if ok else "cancelled" if cancelled else "failed")
    result = None
    if ok and mode == "rebuild":
        result = _collect_result(started, tail)
    elif ok and mode == "precheck":
        result = {"deps_ready": True, "elapsed": elapsed}
    err = error
    if err is None and not ok:
        err = ("已被取消（收到取消请求）" if cancelled
               else f"建图脚本退出码 {rc}——日志尾部见下（完整日志：{st.get('log')}）")
    _merge({"status": status_word, "exit_code": rc, "ended_at": _now(),
            "elapsed": elapsed, "result": result, "error": err,
            "tail": tail[-TAIL_LINES:]})
    if ok and mode == "rebuild":
        logger.info("[graph_build] 完成 run=%s %s", run_id, result)
    else:
        logger.warning("[graph_build] 结束 run=%s status=%s rc=%s err=%s",
                       run_id, status_word, rc, err)
    _release_lock(run_id)


def _embed_stats(tail: list[str]) -> tuple[int | None, int | None]:
    """从日志尾巴里取"这次真花了多少 embedding"。

    **取不到就返回 (None, None)**，不返回 0/0：`--dry-run` 根本不调 embedding，
    而"调了但全部命中缓存"也是 0/0 —— 把前者写成后者就是在报告里说谎。
    """
    for line in reversed(tail):
        m = _EMBED_RE.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def _collect_result(started: float, tail: list[str]) -> dict:
    """成功后的摘要。读的是**产物自己写的账**（脚本落的 index.json / manifest.json），
    不是我们猜的——"产物到底有没有落地"只能由产物回答。"""
    out: dict = {"elapsed": round(time.time() - started, 1)}
    idx = _read_json(GRAPH_DIR / "index.json") or {}
    man = _read_json(WEB_DIR / "manifest.json") or {}
    out.update(build_id=idx.get("build_id"), nodes=idx.get("count"), dim=idx.get("dim"),
               file=man.get("file"), bytes=man.get("bytes"),
               site=man.get("site"), built=man.get("built"))
    new, hit = _embed_stats(tail)
    out["embed_new"], out["embed_hit"] = new, hit
    # 让**本 worker** 立刻换新产物；其余 worker 下次查询时按 build_id 热重载
    # （rag/wordgraph.py 的 _load）。这一步失败不影响建图结果，如实记下来即可。
    try:
        from rag import wordgraph
        out["reload_local"] = bool(wordgraph._load())
    except Exception as e:                             # pragma: no cover - 兜底
        out["reload_local"] = False
        out["reload_error"] = str(e)
    missing = [k for k, v in out.items() if v is None and k in ("build_id", "file", "bytes")]
    if missing:
        # 退出码 0 但产物读不回来：这是**真问题**（脚本的成功分支没写产物），
        # 不能靠"退出码 0"就说成功
        out["warn"] = f"退出码 0 但产物摘要缺字段：{missing}"
        logger.warning("[graph_build] %s", out["warn"])
    return out


# ─────────────────────────────────────────────────────────── 查询 / 取消

def status() -> dict:
    """给页面轮询的状态。**锁 + pid 是当场核的**：state.json 是属主进程写的，
    属主被重启（部署/OOM）时它停在"running"永远不动。"""
    st = _read_state()
    held = _read_lock()
    if held is not None:
        owner = int(held.get("owner_pid") or 0)
        child = int(held.get("child_pid") or st.get("child_pid") or 0)
        st["run_id"] = held.get("run_id") or st.get("run_id")
        st["owner_pid"] = owner
        st["owner_alive"] = _pid_alive(owner)
        st["child_pid"] = child
        st["child_alive"] = _pid_alive(child)
        if st["owner_alive"]:
            st["status"] = "running"
            st.pop("stale_reason", None)
        else:
            # 属主没了 ⇒ 这个任务已经没人能给它写终态了。锁会在下一次 start 时被回收，
            # 这里只如实报告（页面据此允许"重新开始"，不需要用户去删文件）
            st["status"] = "interrupted"
            st["stale_reason"] = "owner_gone"
    else:
        st.setdefault("status", "idle")
        if st["status"] == "running":
            # 锁没了却写着 running：属主是正常退出（收尾写了终态就不该是 running），
            # 只可能是收尾写到一半被打断 —— 不猜，报成 interrupted
            st["status"] = "interrupted"
            st["stale_reason"] = "lock_gone"
        st["owner_alive"] = False
        st["child_alive"] = False
    # 本 worker 的环形缓冲比 state.json 新（state 最多滞后 1 秒）
    if st.get("run_id") and st.get("run_id") == _own_run_id():
        with _lock:
            if _tail:
                st["tail"] = list(_tail)
    st["mem_available_mb"] = mem_available_mb()
    st["mem_min_mb"] = mem_min_mb()
    st["running"] = st["status"] == "running"
    return st


def _own_run_id() -> str | None:
    """本进程正在跑的那次任务的 run_id（没有则 None）。只用来判断"内存里这份环形
    缓冲能不能给它看"——**不用来判断任务是否存在**（那以盘为准）。"""
    return _own_run


def cancel() -> dict:
    """取消当前任务。**不看内存里那份**（取消很可能落在另一个 worker 上）——
    一切从盘的 state + lock 里取，然后 SIGTERM 整个进程组。"""
    st = _read_state()
    held = _read_lock() or {}
    pid = int(st.get("child_pid") or held.get("child_pid") or 0)
    if not pid or not _pid_alive(pid):
        return {"ok": False, "reason": "not_running",
                "error": "当前没有正在运行的建图进程（可能刚结束或还没起来）"}
    _merge({"cancel_requested": True})
    _kill_pg(pid, signal.SIGTERM)
    for _ in range(int(CANCEL_GRACE / 0.2)):
        time.sleep(0.2)
        if not _pid_alive(pid):
            logger.info("[graph_build] 已取消 pid=%s（SIGTERM 生效）", pid)
            return {"ok": True, "pid": pid, "signal": "TERM"}
    _kill_pg(pid, signal.SIGKILL)
    logger.warning("[graph_build] pid=%s 宽限 %.0fs 未退，已 SIGKILL", pid, CANCEL_GRACE)
    return {"ok": True, "pid": pid, "signal": "KILL"}
