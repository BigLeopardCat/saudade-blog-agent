# -*- coding: utf-8 -*-
"""向量图谱重建任务（`rag/graph_build.py`）的离线套件（20261003）。

秒级、零网络、零 LLM、**不起真子进程**（起一次要 470MB 依赖 + 一次真建图）。
这里量的是"这个任务模块会不会骗人"的四件事：

① **参数白名单**——表单是外部输入，多传/乱传的东西绝不能进 argv；
   尤其 `exclude_ids` 的**三态**（没传 / 空串 / 点名）：20261003 修的那个静默
   bug 就是『空串』被当成『没传』（别人 clone 过去，同号文章被无声丢掉）。
② **起任务前的三道闸**——uv 在不在、内存够不够、脚本在不在。它们必须是
   **拒绝**（带原话），不是"试试看"；这台机器只有 3.7GB，试着试着的代价是整站 OOM。
③ **锁与状态**——4 个 worker 是 4 个进程，状态只能落盘。最要命的一态是
   "属主 worker 被重启（部署/OOM）后 state.json 永远停在 running"：判据必须
   当场核 pid，而不是读文件里写的那个词。
④ **诚实报告**——`--dry-run` 没调 embedding，不能报成『新增 0 条』（那是『调了但
   全命中缓存』）。见 `_embed_stats` 的 docstring。

⚠️ 本套件**不碰仓库里的 `logs/`**：所有状态文件都落在临时目录（`_fresh`）。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.authz import SCOPE_ADMIN_CONSOLE, SCOPE_WRITE_CONSOLE, holds  # noqa: E402
from agent.principal import (SOURCE_ASSERTION, ROLE_ADMIN, ROLE_SECRETARY,  # noqa: E402
                             ROLE_SUPERADMIN, ROLE_USER, Principal)
from rag import graph_build as gb  # noqa: E402

FAILS: list[str] = []


def check(desc: str, cond: bool, detail="") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# 三个落盘路径整体挪到临时目录：套件在**仓库里**跑，而锁/状态是运行时产物
_TMP = Path(tempfile.mkdtemp(prefix="graph-build-test-"))
gb.STATE_DIR = _TMP / "graph_build"
gb.STATE_PATH = gb.STATE_DIR / "state.json"
gb.LOCK_PATH = gb.STATE_DIR / "lock"
gb.WEB_DIR = _TMP / "web"
gb.GRAPH_DIR = _TMP / "graph"


def _fresh(*payload_files: tuple[Path, dict]):
    """把现场清干净再摆上给定的文件（每个用例都从"什么都没发生"开始）。"""
    shutil.rmtree(gb.STATE_DIR, ignore_errors=True)
    for path, data in payload_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def _lock(owner_pid: int, child_pid: int = 0, run_id: str = "R0") -> tuple[Path, dict]:
    return (gb.LOCK_PATH, {"run_id": run_id, "owner_pid": owner_pid, "child_pid": child_pid})


# ─────────────────────────────────────────────────────────── ① 参数白名单
print("\n① 参数白名单：表单 → argv 只走列表里的那几项")

p = gb.resolve_params({"max_nodes": "500", "min_chars": 300, "layout": "pca",
                       "refresh": True, "dry_run": "on", "exclude_ids": ""})
check("能转的都转了（数字串认成整数、开关认成真）",
      p["max_nodes"] == 500 and p["min_chars"] == 300 and p["layout"] == "pca"
      and p["refresh"] is True and p["dry_run"] is True, p)
check("★ exclude_ids 的空串**保留成空串**（＝一个都不排除），没有被当成『没传』",
      p["exclude_ids"] == "", repr(p["exclude_ids"]))
check("没传 exclude_ids 时是 None（⇒ 交给脚本用它自己的默认值）",
      gb.resolve_params({})["exclude_ids"] is None)
check("点名几个 id 原样带过去（不解析、不排序——语义在脚本那一侧）",
      gb.resolve_params({"exclude_ids": "9,10"})["exclude_ids"] == "9,10")

check("未知字段被丢掉（不进 argv，也不报错）",
      "whatever" not in gb.resolve_params({"whatever": "rm -rf /"}))
check("布尔字段只认真值（字符串 'false' 不当成真）",
      gb.resolve_params({"force": "false"})["force"] is False)

for bad, why in (({"max_nodes": "abc"}, "不是整数"),
                 ({"max_nodes": 10 ** 6}, "超出上限"),
                 ({"layout": "spring"}, "不在选项里")):
    try:
        gb.resolve_params(bad)
        check(f"非法参数（{why}）抛错，而不是静默改用默认值", False, bad)
    except ValueError:
        check(f"非法参数（{why}）抛错，而不是静默改用默认值", True)

argv = gb._build_argv(gb.resolve_params({"exclude_ids": "9,10"}), "rebuild")
check("命令行里带上了 --out-web（写 agent 自己的目录 ⇒ 不碰父仓、无 git 变更）",
      str(gb.WEB_DIR) in argv, argv[-6:])
check("命令行里有 --exclude-ids（空串也要带上——那正是『不排除』的意思）",
      "--exclude-ids" in gb._build_argv(gb.resolve_params({"exclude_ids": ""}), "rebuild"))
check("没传 exclude_ids 时**不带**这个参数（让脚本走它自己的默认）",
      "--exclude-ids" not in gb._build_argv(gb.resolve_params({}), "rebuild"))
check("依赖清单是那份离线建图专用的（生产 venv 里刻意没有 umap/numba）",
      str(gb.REQUIREMENTS).endswith("requirements-graph.txt")
      and "--with-requirements" in argv)
pre = gb._build_argv(gb.resolve_params({}), "precheck")
check("预检模式不跑建图脚本，只 import 三个依赖",
      pre[-2] == "-c" and "import" in pre[-1] and str(gb.BUILD_SCRIPT) not in pre,
      pre[-1])
check("语料地址来自 settings 那一项（不是模块里另写的第二份常量）",
      "def default_api_base" in (ROOT / "rag" / "graph_build.py").read_text(encoding="utf-8")
      and gb.default_api_base() == gb.resolve_params({})["api_base"])


# ─────────────────────────────────────────────────────────── ② 起任务前的闸
print("\n② 三道闸：uv / 脚本 / 内存——**拒绝并给原话**，不试")

_real_which, _real_mem, _real_fb = shutil.which, gb.mem_available_mb, gb.UV_FALLBACKS
try:
    # 20261003 起 uv 有**两处**查找（PATH + 回落表）：只堵 PATH 已经不等于"没有 uv"。
    # 这条判据自己踩过这个坑——本机 uv 就装在 `~/.local/bin`，而 systemd 服务默认 PATH
    # 不含用户目录，所以"服务里找不到 uv"的真实成因恰恰是回落表在兜（见 `find_uv`）。
    shutil.which = lambda _n: None
    gb.UV_FALLBACKS = ()
    gb._UV_PATH = None
    r = gb.preflight("rebuild")
    check("没有 uv ⇒ 拒绝，且说的是『装了 uv 再来』而不是空话",
          bool(r) and r["reason"] == "uv_missing" and "uv" in r["error"], r)
    check("★ 拒绝文案里写明**找过哪些地方**（不然用户只能猜该往哪装）",
          bool(r) and "PATH" in r["error"] and "uv_missing" == (r or {}).get("reason"),
          r and r["error"])
finally:
    shutil.which, gb.UV_FALLBACKS, gb._UV_PATH = _real_which, _real_fb, None

# 正向回归（这条就是 20261003 那个 bug 本体）：uv **不在 PATH**、只在回落目录里，
# 也必须找得到，而且返回的是**绝对路径**——写裸 `uv` 会在 Popen 那一刻才炸。
_real_fb, _real_which, _real_mem2 = gb.UV_FALLBACKS, shutil.which, gb.mem_available_mb
try:
    shutil.which = lambda _n: None
    gb._UV_PATH = None
    fake_dir = Path(tempfile.mkdtemp())
    fake = fake_dir / "uv"                                 # 绝对路径 ⇒ 不碰真的 ~（回落表用 ~ 展开）
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    gb.UV_FALLBACKS = (str(fake),)
    got = gb.find_uv()
    check("★ uv 只在回落目录（服务 PATH 看不到它）时照样找得到，且给绝对路径",
          got == str(fake), got)
    # 内存那条**必须一起钉死**：不钉的话这条会在"机器刚好紧张"时红成 uv 的问题，
    # 判据就变成了在量这台机器当时的负载。
    gb.mem_available_mb = lambda: 99999
    check("★ 找到了就不再拒绝（preflight 放行，拦路的是内存那条，不是 uv）",
          (gb.preflight("rebuild") or {}).get("reason") != "uv_missing")
finally:
    gb.UV_FALLBACKS, shutil.which, gb.mem_available_mb, gb._UV_PATH = (
        _real_fb, _real_which, _real_mem2, None)

try:
    gb.mem_available_mb = lambda: 300
    r = gb.preflight("rebuild")
    check("★ 内存不够 ⇒ 拒绝，且**当前值与门槛都写进文案**（不然用户不知道要腾多少）",
          bool(r) and r["reason"] == "low_memory"
          and "300" in r["error"] and str(gb.MEM_MIN_MB) in r["error"],
          r and r["error"])
finally:
    gb.mem_available_mb = _real_mem

check("内存读数取不到（None）时不拿『读不出来』冒充『内存不够』（不拒）",
      (gb.preflight("rebuild") or {}).get("reason") != "low_memory")
# 阈值必须**卡在实测峰值之上、又不高到把门焊死**。20261003 实测：一次真实重建（400 节点
# / 12 篇文章 / UMAP，笼子 MemoryMax=1000M）峰值 RSS 552MB、30 秒、换页 0 次。低于它大概率
# OOM；高出一倍多就等于门常年关着——原来那个 1200 正是这么来的（本机可用内存常在 900MB
# 上下，于是"腾一腾再来"根本腾不到，用户点了两次都只拿到一句"内存不够"）。
# 改动这个数要连着那次测量一起改（出处写在 `rag/graph_build.py` 的常量注释里）。
check("内存门槛落在『实测峰值 552MB ~ 它的一倍』之间",
      552 <= gb.MEM_MIN_MB <= 552 * 2, gb.MEM_MIN_MB)
_env_before = os.environ.get(gb.MEM_ENV)
try:
    os.environ[gb.MEM_ENV] = "900"
    check("阈值可以用环境变量放行（确有把握时主人自己抬/降）", gb.mem_min_mb() == 900,
          gb.mem_min_mb())
    for bad in ("abc", "", "0", "-1"):
        os.environ[gb.MEM_ENV] = bad
        check(f"★ 阈值写坏了（{bad!r}）回落默认值——一个错别字不能把重建功能锁死",
              gb.mem_min_mb() == gb.MEM_MIN_MB, gb.mem_min_mb())
finally:
    if _env_before is None:
        os.environ.pop(gb.MEM_ENV, None)
    else:
        os.environ[gb.MEM_ENV] = _env_before


# ─────────────────────────────────────────────────────────── ③ 锁与状态
print("\n③ 锁与状态：4 个 worker 是 4 个进程，状态只能落盘、判据必须当场核 pid")

_fresh()
check("没跑过任何任务时：idle（不是一个看起来像失败的东西）",
      gb.status()["status"] == "idle")

# 属主活着 ⇒ running（哪怕 state.json 里写着别的）
_fresh(_lock(os.getpid(), 4242, "R1"))
gb._write_state({"status": "ok", "run_id": "R1", "child_pid": 4242})
st = gb.status()
check("锁在 + 属主活着 ⇒ running（**以 pid 为准**，不读 state.json 里那个词）",
      st["status"] == "running" and st["owner_alive"] is True, st.get("status"))

# 属主死了（worker 被部署重启）⇒ interrupted
_real_alive = gb._pid_alive
try:
    gb._pid_alive = lambda pid: False
    _fresh(_lock(999999, 0, "R1"))
    gb._write_state({"status": "running", "run_id": "R1", "child_pid": 0})
    st = gb.status()
    check("★ 属主已不在 ⇒ interrupted（不是永远显示『运行中』——部署/OOM 重启后的真实形状）",
          st["status"] == "interrupted" and st["stale_reason"] == "owner_gone",
          f"{st['status']}/{st.get('stale_reason')}")

    # 僵尸锁回收时要把游离的子进程收掉（它没人看、白占内存）
    killed: list[tuple[int, int]] = []
    _real_killpg, _real_reap = gb._kill_pg, gb._reap_orphan
    gb._kill_pg = lambda pid, sig: (killed.append((pid, sig)), True)[1]
    try:
        # 先单量收尸那一步：子进程还活着 ⇒ 发 SIGTERM 整个进程组
        gb._reap_orphan = _real_reap
        seq = iter([True, False])
        gb._pid_alive = lambda pid: next(seq, False)
        gb._reap_orphan({"child_pid": 777})
        check("★ 游离子进程被收掉（start_new_session 起的进程不随属主死，"
              "而没人读它的 stdout ⇒ 只会白占内存）",
              killed == [(777, 15)], killed)

        # 再量整条路：僵尸锁能被回收，且锁归新任务
        gb._kill_pg = lambda pid, sig: True
        gb._reap_orphan = lambda held: None
        _fresh(_lock(999999, 777, "R0"))
        held = gb._acquire_lock("R2")
        check("★ 僵尸锁能被回收（否则一次 OOM 之后后台再也点不动重建）",
              bool(held) and held["run_id"] == "R2", held)
        check("  回收后锁归新任务（旧 run_id 被顶掉）",
              gb._read_lock().get("run_id") == "R2")
    finally:
        gb._kill_pg, gb._reap_orphan = _real_killpg, _real_reap

    # 真有人在做 ⇒ 不让抢
    _fresh(_lock(os.getpid(), 0, "R3"))
    gb._pid_alive = lambda pid: True
    check("属主还活着时抢不到锁（同一时刻只允许一件建图）",
          gb._acquire_lock("R4") is None)
finally:
    gb._pid_alive = _real_alive

_fresh()
gb._write_state({"status": "running", "run_id": "R5", "child_pid": 0})
check("锁没了却写着 running ⇒ 报 interrupted（不猜，也不假装还在跑）",
      gb.status()["stale_reason"] == "lock_gone")

check("收尾前核对锁还是不是自己的（否则迟到的收尾会改掉**新任务**的状态）",
      gb._owns_lock("不是我的") is False)

_fresh()
check("取消一个没在跑的任务：如实说 not_running（不假装成功）",
      gb.cancel()["reason"] == "not_running")


# ─────────────────────────────────────────────────────────── ④ 诚实报告
print("\n④ 报告：dry-run 与『全命中缓存』不许长成同一句话")

TAIL_DRY = ["② 抽词选词…", "--dry-run：不调 embedding、不写产物", "完成，用时 1.2s"]
check("★ dry-run 的日志里根本没有那一行 ⇒ (None, None)，不报成『新增 0 条』",
      gb._embed_stats(TAIL_DRY) == (None, None), gb._embed_stats(TAIL_DRY))
check("命中了缓存 ⇒ 如实给出 (0, 120)（『没调』与『调了但全命中』是两件事）",
      gb._embed_stats(["embedding 新增 0 条（缓存命中 120 条）"]) == (0, 120))
check("真有新增时取最后一行（多轮重跑只报最新那次）",
      gb._embed_stats(["embedding 新增 5 条（缓存命中 1 条）",
                       "embedding 新增 3 条（缓存命中 9 条）"]) == (3, 9))


# ─────────────────────────────────────────────────────────── ⑤ 端点与权限
print("\n⑤ 端点：三件都挂在 admin.console 那道门上")

src = (ROOT / "server.py").read_text(encoding="utf-8")
routes = [l.strip() for l in src.splitlines()
          if l.strip().startswith('@app.post("/graph/rebuild')
          or l.strip().startswith('@app.get("/graph/rebuild')]
check("起任务 / 查状态 / 取消三个端点都在（多一个少一个都会静默）",
      routes == ['@app.post("/graph/rebuild")', '@app.get("/graph/rebuild/status")',
                 '@app.post("/graph/rebuild/cancel")'], routes)
check("权限判据是 authz.holds + admin.console（不是自己写 role == 'admin'）",
      "authz.holds(principal, authz.SCOPE_ADMIN_CONSOLE)" in src)
check("身份走既有的身份断言（_resolve_principal），不新开一套鉴权",
      "principal = _resolve_principal(request, uid)" in src)
check("拒绝是 403，不是 200+ok=false（权限失败与『忙/内存不够』必须分得开）",
      'raise HTTPException(403, "需要管理员权限")' in src)
check("端点把活交给线程池（起任务/取消都是阻塞调用，不能占事件循环）",
      src.count("_submit_with_context(loop, graph_build.") == 3,
      src.count("_submit_with_context(loop, graph_build."))

gbsrc = (ROOT / "rag" / "graph_build.py").read_text(encoding="utf-8")
check("四个 worker 之间靠文件锁（O_CREAT|O_EXCL），不靠内存里那把锁",
      "os.O_CREAT | os.O_EXCL" in gbsrc)
check("state.json 是**原子**写（os.replace）：轮询端读不到半个 JSON",
      "os.replace(tmp, STATE_PATH)" in gbsrc)
check("子进程自成会话（取消一次 killpg 收干净，不杀到 uvicorn 自己那一组）",
      "start_new_session=True" in gbsrc and "os.killpg(pid, sig)" in gbsrc)
check("完整日志另有文件（环形缓冲只有 400 行，不能当唯一留档）",
      "TAIL_LINES = 400" in gbsrc and "{run_id}.log" in gbsrc)
check("§ 提示：precheck 与 rebuild 共用同一把锁（不给建图机开第二处真相源）",
      "def start(raw_params: dict | None = None, mode: str = \"rebuild\")" in gbsrc)
ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
check("建图任务的 logs/ 进了 .gitignore（运行时产物不入库）",
      "logs/" in [l.strip() for l in ignore], [l for l in ignore if "log" in l])


# ─────────────────────────────────────────────────────────── ⑥ holds 语义
print("\n⑥ authz.holds：端点级能力只查授予表，身份不明恒 False")

check("管理员与超管都有 admin.console",
      holds(Principal(1, ROLE_ADMIN, SOURCE_ASSERTION), SCOPE_ADMIN_CONSOLE)
      and holds(Principal(1, ROLE_SUPERADMIN, SOURCE_ASSERTION), SCOPE_ADMIN_CONSOLE))
check("★ 普通用户与秘书都没有（秘书读得了他人数据，但进不了后台）",
      not holds(Principal(2, ROLE_USER, SOURCE_ASSERTION), SCOPE_ADMIN_CONSOLE)
      and not holds(Principal(3, ROLE_SECRETARY, SOURCE_ASSERTION), SCOPE_ADMIN_CONSOLE))
check("★ 身份不明（role=None / 不认识的角色 / 根本没有 principal）⇒ False，从不默认放行",
      not holds(Principal(9, None, SOURCE_ASSERTION), SCOPE_ADMIN_CONSOLE)
      and not holds(Principal(9, "谁", SOURCE_ASSERTION), SCOPE_ADMIN_CONSOLE)
      and not holds(None, SCOPE_ADMIN_CONSOLE))
check("它查的是同一张授予表（管理员在写面上也为真）",
      holds(Principal(1, ROLE_ADMIN), SCOPE_WRITE_CONSOLE))

print(f"\ntest_graph_build: {'全绿' if not FAILS else str(len(FAILS)) + ' 条红'}")
sys.exit(1 if FAILS else 0)
