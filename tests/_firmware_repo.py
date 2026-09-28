# -*- coding: utf-8 -*-
"""固件仓（ESP32-S3-OBC）源码在哪，以及"跑不到的时候怎么办"（20260929）。

**为什么单起一个模块**（与 `tests/_parent_repo.py` 逐字同源的理由）：画板这条路把
**跨仓契约**摆到了明面上——`agent/oled_draw.py` 的 op 名单/参数个数/6-12 像素宽度规则/
上限，在固件 `main/main.c` 里各有一份对应物。两仓是两个 git 仓、两次发布、**谁也没法
import 对方**，所以判据只能是"读对方源码、逐项比对"。

这份守卫比的是**源码**，不是运行中的设备。三种状态显式，没有隐形的第四种：

  · **找得到** → 正常断言（op 名单 ⊆、逐 op 参数个数、6/12 规则、上限关系、固件版本）；
  · **找不到 + `SAUDADE_REQUIRE_FIRMWARE=1`** → **红**（`SystemExit(1)`）。夜间门禁这样设
    ——跨仓守卫"跑不到"不能算通过；
  · **找不到 + 没设要求** → 打一行**响亮**的说明并返回 None（本机只有 agent 仓时的情形）。
    **那行字必须显眼**：静默跳过正是这套守卫失效的方式本身。

**CI 本批不接线**（有意识的延后，不是漏了）：父仓那条是走 ADR-0004 单独一枚只读 PAT +
第二个 sparse checkout 步骤才进 CI 的，固件仓同办要**再一枚凭据**（同一条 ADR 的取舍）。
所以本批在 CI 上这一条是"未评估"、在**本机/夜间是硬判**。

⚠️ 两条边界（同 `_parent_repo.py`）：
  · 路径必须**真的含有** `main/main.c` 与 `docs/device-integration.md` 才认
    （指向空目录会让守卫**假绿**）；
  · env `SAUDADE_FIRMWARE_REPO` 只用来**指位置**，不是"绕过检查"的开关。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent      # agent 仓根
# 认固件仓的锚：两个文件同时在场才算（只有 main.c 的仓库不止一个——
# mqtt-demo 那个 Arduino 模板目录同族，但它是 stub，见 device-integration.md 开头）
_SENTINELS = ("main/main.c", "docs/device-integration.md")


def firmware_root() -> pathlib.Path | None:
    """固件仓根目录；找不到（或那个目录里没有两处锚）返回 None。

    查找顺序：env `SAUDADE_FIRMWARE_REPO` → 常见布局的兄弟目录
    （`~/ESP32-S3-OBC`、`~/memory_blog_rust/ESP32-S3-OBC`）。**env 是排他的**：
    设了它只认它——"父仓在这里"的显式声明指错了地方，等于守卫在断言另一个仓库
    （假绿比跳过更坏）。
    """
    env = (os.environ.get("SAUDADE_FIRMWARE_REPO") or "").strip()
    cands = [pathlib.Path(env)] if env else [
        pathlib.Path("/home/ubuntu/ESP32-S3-OBC"),          # 本机常规位置
        ROOT.parent / "ESP32-S3-OBC",
        ROOT.parent.parent / "ESP32-S3-OBC",
    ]
    for c in cands:
        try:
            if all((c / s).is_file() for s in _SENTINELS):
                return c
        except OSError:          # 路径不可读 ＝ 等同"找不到"，不猜
            continue
    return None


def read(rel: str, why: str = "") -> str | None:
    """读固件仓里的某个文件（相对固件仓根）；读不到时按上面的三分支处理。

    `why` 是这一处守卫守的是什么契约（"固件不认的 op，屏幕不会变"这类）——没有它，
    红的时候只能看到"文件不在"，看不出影响。
    """
    root = firmware_root()
    if root is not None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except OSError as e:                                   # 存在但读不动：不静默
            _bail(f"固件仓 {rel} 读不动（{type(e).__name__}: {e}）", why)
    _bail(f"找不到固件仓源码 {rel}", why)
    return None


def _bail(msg: str, why: str) -> None:
    note = f"  ← 这一处守的是：{why}" if why else ""
    if os.environ.get("SAUDADE_REQUIRE_FIRMWARE"):
        print(f"  ❌ {msg}（SAUDADE_REQUIRE_FIRMWARE=1 ⇒ 跨仓守卫跑不到就不算通过）{note}")
        sys.exit(1)
    print(f"  ⏭ 跳过固件仓断言：{msg}。{note}")
    print("     （固件仓不在本机常见位置时用 env SAUDADE_FIRMWARE_REPO 指过去；夜间门禁设"
          " SAUDADE_REQUIRE_FIRMWARE=1 把这一处**跑不到变成红**。**看到这行 ⏭ 说明这一条"
          "结论是空的**：画板与固件之间的 op 契约本批没有被校验。）")
