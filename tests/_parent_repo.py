# -*- coding: utf-8 -*-
"""父仓（Rust）源码在哪，以及"跑不到的时候怎么办"（20260928）。

**为什么单起一个模块**：跨语言契约的守卫（agent 侧断言 Rust 源码里真有那个臂、那个键）
此前各写各的 `ROOT.parent / "src" / ...` + `if exists(): … else: print("⏭ 跳过")`。
而 CI 只 checkout agent 仓（父仓私有，公开仓的 CI 读不到它）⇒ **那几处守卫在 CI 里恒跳过**：
本机跑得过，最需要它的地方从不校验。审计实测：磁盘上 50 个套件里 CI 只引用 26 个，
而这批守卫是"看着在跑、其实没跑"的最典型一档。

三种状态**显式**，不再有隐形的第四种：
  · **找得到** → 正常断言；
  · **找不到 + `SAUDADE_REQUIRE_PARENT=1`** → **红**（`SystemExit(1)`）。跨语言守卫是
    "必须跑"的那一类，跑不到不能算通过。**夜间门禁与 CI 都这么设**——CI 侧父仓以只读凭据
    稀疏 checkout 到 `_parent/`（`.github/workflows/eval.yml`，取舍见
    `docs/adr/adr-0004-cross-language-guard-in-ci.md`），所以这一档在 CI 里判的是真东西；
  · **找不到 + 没设要求** → 打一行**响亮**的说明并返回 `None`。这是**本机在 agent 仓单独
    checkout 且父仓不在兄弟目录**时的情形（调试用），不是 CI 的常态——CI 走上面那一档。
    **那行字必须显眼**：静默跳过正是这套守卫失效的方式本身。

⚠️ 两条边界：
  · 路径必须**真的含有** `src/routes/chat.rs` 才认（指向一个空目录/错误目录会让守卫**假绿**）；
  · env `SAUDADE_PARENT_REPO` 只用来**指位置**，不是"绕过检查"的开关——指错了就等于找不到。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent      # agent 仓根
_SENTINEL = "src/routes/chat.rs"                            # 认父仓的锚：Rust 侧对话主文件


def parent_root() -> pathlib.Path | None:
    """父仓根目录；找不到（或那个目录里没有 Rust 源码锚）返回 None。

    查找顺序：env `SAUDADE_PARENT_REPO` → agent 仓的**兄弟目录**（本机的常规布局：
    `memory_blog_rust/saudade-blog-agent`）。**env 是排他的**：设了它就只认它——它是
    "父仓在这里"的显式声明，指着 A 却在 B 里找到了，等于守卫在断言一个**不是你要的那个**
    仓库（那比"跳过"更坏：假绿）。
    """
    cands = []
    env = (os.environ.get("SAUDADE_PARENT_REPO") or "").strip()
    if env:
        cands.append(pathlib.Path(env))
    else:
        cands.append(ROOT.parent)
    for c in cands:
        try:
            if (c / _SENTINEL).is_file():
                return c
        except OSError:          # 路径不可读（父目录权限等）＝等同"找不到"，不猜
            continue
    return None


def read(rel: str, why: str = "") -> str | None:
    """读父仓里的某个文件（相对父仓根）；读不到时按上面的三分支处理。

    `why` 是给跳过/失败信息用的**一句人话**：这一处守卫守的是什么契约（"Rust 那半
    真读了 success/error"这类）。没有它，红的时候只能看到"文件不在"，看不出影响。
    """
    root = parent_root()
    if root is not None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except OSError as e:                                   # 存在但读不动：不静默
            _bail(f"父仓 {rel} 读不动（{type(e).__name__}: {e}）", why)
    _bail(f"找不到父仓源码 {rel}", why)
    return None


def _bail(msg: str, why: str) -> None:
    note = f"  ← 这一处守的是：{why}" if why else ""
    if os.environ.get("SAUDADE_REQUIRE_PARENT"):
        print(f"  ❌ {msg}（SAUDADE_REQUIRE_PARENT=1 ⇒ 跨语言守卫跑不到就不算通过）{note}")
        sys.exit(1)
    print(f"  ⏭ 跳过父仓断言：{msg}。{note}")
    print("     （本机常规布局下父仓应在兄弟目录；夜间门禁与 CI 都设 SAUDADE_REQUIRE_PARENT=1"
          "、把这一处**跑不到变成红**——CI 侧父仓由 workflow 稀疏 checkout 到 `_parent/`。"
          "**看到这行 ⏭ 说明你在用一个没配父仓的临时环境单跑**，结论里这一条是空的。）")
