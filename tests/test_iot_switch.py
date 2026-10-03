# -*- coding: utf-8 -*-
"""物联网平台开关（`IOT_ENABLED`，20261002）：**两档都要验**。

物联网平台（EMQX + device-service + 静态控制台）是可选件，源码收在博客仓 `iot/`，
装不装由部署者决定。agent 这一侧的收口有四条链路（见 `config/settings.py` 的
`iot_enabled`）——它们**全都读同一个值**，所以这里不必逐条发明判据，只需要在
两个档位下各跑一遍，看四条链路是否一致地跟着走。

**为什么必须开子进程**：`IOT_ENABLED` 是模块级常量（`tools/base.py` 顶部读 settings），
NAV_MAP / FUZZY_NAV_RULES / 技能可见性 / 提示词文本全是 import 时定型的。同一个进程里
改不了档——只能各起一个子进程，把 env 喂进去再读结果。这也正是 `run_all.py` 的做法。

**两档各自的失法**（都不报错、只是说假话）：
  · 关了没收干净 ⇒ agent 指路到一个 404，还说「页面已跳转」（系统替不存在的页面背书）；
  · 开了被误收 ⇒ 装了平台的站上，agent 突然说"本站没有这个页面"。

⚠️ 本套件**自己**跑在 run_all 钉的那一档（`IOT_ENABLED=1`）——那是"装了"的那档；
"没装"那档由子进程验。最后一段有断言把这件事挑明（否则将来有人改了钉子，
这个套件会在错误的档位上"通过"）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 子进程探针 ─────────────────────────────────────────────────────────
# 只回报**判据对象**，不回报任何配置值（本仓纪律：凭据一次都不许打印；这里虽然
# 只有开关，但保持同一个形状，免得将来有人往探针里加字段时顺手带上密钥）。
_PROBE = r"""
import json, sys
sys.path.insert(0, %r)
import tools.base as B
import agent.skills as S
import agent.context as C
import agent.hostinfo as H
from agent.decisions import _display_fast_path

def nav(target):
    p = S.instantiate_plan("navigate", {"target": target})
    return {"status": p["status"], "tools": p["tools"], "note": p["note"]}

out = {
    "iot": B.IOT_ENABLED,
    "exact_has": B.IOT_NAV_PATH in B._NAV_EXACT_PATHS,
    "whole_page": sorted(B._NAV_WHOLE_PAGE_PATHS),
    "map_lines_mentions": "物联网" in S._NAV_MAP_LINES,
    "ref_hint_mentions": "物联网" in S._NAV_REF_HINT,
    "real_pages_mentions": "物联网" in S._NAV_REAL_PAGES,
    "nav_iot": nav("物联网平台"),
    "nav_console": nav("设备控制台"),
    "nav_literal": nav("/device-console/"),
    "nav_fuzzy": nav("设备面板"),
    "nav_friend": nav("友链"),
    "nav_unknown": nav("量子对撞机车间"),
    "nav_ok": nav("留言板"),
    "skills": sorted(s.name for s in S.visible_skills(None)),
    "skill_caps": {s.name: s.capability for s in S.SKILLS if s.name == "navigate"},
    "site_guide": C.site_guide(None),
    "site_map": B.get_site_map.invoke({}),
    "services": list(H.SERVICES),
    "display_fast": bool(_display_fast_path("在屏幕上显示晚安")),
    "aliases_all_mapped": sorted(a for a in S._IOT_NAV_ALIASES if a not in S.NAV_MAP),
}
print(json.dumps(out, ensure_ascii=False))
"""

# （20261004 去掉了 `PLANNER_ENGINE` 那一钉：接口层只剩 native、拨盘已删。
# 本套件只读 skills/tools 的声明，与接口层无关。）
_PINS = {"AGENT_TASK_STATE": "0", "SAUDADE_IGNORE_ENV_FILE": "1"}


def probe(iot_on: bool) -> dict:
    """起一个子进程，按指定档位读回全部判据对象。"""
    env = dict(os.environ)
    env.update(_PINS)
    env["IOT_ENABLED"] = "1" if iot_on else "0"
    r = subprocess.run([sys.executable, "-c", _PROBE % str(ROOT)],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("子进程探针跑不起来（上面是它的输出）")
    return json.loads(r.stdout.strip().splitlines()[-1])


# ══════════════════════════════════════════════════════════════════
print("\n① 关（出厂档）：页面不存在，agent 也必须一致地收口")

off = probe(False)
check("开关读进来是关的（探针自己先自证，别拿一个跑错档的结果去判下面的）",
      off["iot"] is False, str(off["iot"]))

check("工具层白名单不收 /device-console/（放行一个 404 = 系统替不存在的页面背书）",
      off["exact_has"] is False)
check("整页目标集合为空（那个「回复说完再跳」的整页目标只有 IoT 控制台一个）",
      off["whole_page"] == [], str(off["whole_page"]))
check("提示词的导航映射表不提物联网（出现即 planner 的合法候选）",
      off["map_lines_mentions"] is False)
check("注记的「可参照真实页面」清单不提物联网", off["ref_hint_mentions"] is False)
check("gate 兜底文案的页面清单不提物联网（兜底是最后一处会推荐入口的地方）",
      off["real_pages_mentions"] is False)

# 别名 / 字面路径 / 口语模糊归一 —— 三条入口都要落到同一档
for key, label in (("nav_iot", "别名「物联网平台」"),
                   ("nav_console", "别名「设备控制台」"),
                   ("nav_literal", "字面路径 /device-console/"),
                   ("nav_fuzzy", "口语模糊归一「设备面板」")):
    n = off[key]
    check(f"{label} → nav_iot_off、零工具", n["status"] == "nav_iot_off", n["status"])
    check(f"  · 注记说的是「未部署」而不是「已下线」（后者暗示曾经有过）",
          "未部署" in n["note"], n["note"][:40])
    check(f"  · 零工具（不许带着一个注定 404 的命令出门）", n["tools"] == [], str(n["tools"]))

check("★ 与「已下线」不混：友链仍是 nav_offline（两个值分开的意义就在这一条）",
      off["nav_friend"]["status"] == "nav_offline", off["nav_friend"]["status"])
check("★ 与「认不出」不混：量子对撞机车间仍是 nav_unresolved",
      off["nav_unknown"]["status"] == "nav_unresolved", off["nav_unknown"]["status"])
check("别的页面照常跳（收口不是把 navigate 整条关掉）",
      off["nav_ok"]["status"] == "executed" and off["nav_ok"]["tools"],
      off["nav_ok"]["status"])

check("device_display / device_query 两个技能不可见（没平台就没设备可列、没屏幕可写）",
      "device_display" not in off["skills"] and "device_query" not in off["skills"],
      str(off["skills"]))
check("navigate 及别的技能照常在", "navigate" in off["skills"] and "chat" in off["skills"])
check("显示意图快道不命中（快道不许自己发明一句「未部署」——那是第二处判据）",
      off["display_fast"] is False)
check("navigate 的能力文案不提物联网控制台",
      "物联网" not in off["skill_caps"]["navigate"], off["skill_caps"]["navigate"][:50])

check("站内板块清单写明「未部署」（留白会被 narrator 的先验填上）",
      "未部署" in off["site_guide"] and "/device-console/" not in off["site_guide"])
check("get_site_map 的功能结构图没有物联网那一行",
      "物联网" not in off["site_map"], off["site_map"][-80:])
check("服务健康不列 saudade-device（没装那个 unit，列出来就是一行假故障）",
      "saudade-device" not in off["services"], str(off["services"]))

check("IoT 别名全部仍在 NAV_MAP 里（少了键 ⇒ 那句「未部署」根本走不到）",
      off["aliases_all_mapped"] == [], str(off["aliases_all_mapped"]))

# ══════════════════════════════════════════════════════════════════
print("\n② 开（本机/生产档）：与关闭前逐条一致")

on = probe(True)
check("开关读进来是开的", on["iot"] is True, str(on["iot"]))

check("白名单收 /device-console/", on["exact_has"] is True)
check("整页目标集合 = {/device-console/}（它还没跳，话术是「即将跳转」）",
      on["whole_page"] == ["/device-console/"], str(on["whole_page"]))
check("提示词的导航映射表含物联网别名", on["map_lines_mentions"] is True)
check("注记的参照清单含物联网平台", on["ref_hint_mentions"] is True)
check("gate 兜底的页面清单含物联网平台", on["real_pages_mentions"] is True)

check("别名「物联网平台」→ 真跳转（executed，且 TOOLS 行带 /device-console/）",
      on["nav_iot"]["status"] == "executed"
      and "/device-console/" in "".join(on["nav_iot"]["tools"]),
      on["nav_iot"]["status"] + " " + str(on["nav_iot"]["tools"]))
check("口语模糊归一「设备面板」也指向 /device-console/",
      "设备面板" in "".join(on["nav_fuzzy"]["tools"]) or
      "/device-console/" in "".join(on["nav_fuzzy"]["tools"]),
      str(on["nav_fuzzy"]["tools"]))
check("友链仍是 nav_offline（开关不该动到别的别名）",
      on["nav_friend"]["status"] == "nav_offline", on["nav_friend"]["status"])

check("device_display / device_query 可见",
      "device_display" in on["skills"] and "device_query" in on["skills"])
check("显示意图快道命中（屏幕类名词 + 写动词）", on["display_fast"] is True)
check("navigate 的能力文案提物联网控制台",
      "物联网" in on["skill_caps"]["navigate"], on["skill_caps"]["navigate"][:50])

check("站内板块清单列出控制台且不提「未部署」",
      "/device-console/" in on["site_guide"] and "未部署" not in on["site_guide"])
check("get_site_map 有物联网那一行", "物联网控制台" in on["site_map"])
check("服务健康列三个服务（含 saudade-device）",
      list(on["services"]) == ["saudade-rust", "saudade-agent", "saudade-device"],
      str(on["services"]))

# ══════════════════════════════════════════════════════════════════
print("\n③ 档位自证：本套件自己跑在哪一档")

# `run_all._PINNED` 钉的是"装了"那一档（既有几十条判据写的是装了的样子）。
# 这里把它挑明：钉子被摘掉时，这个套件不该悄无声息地在另一档上"通过"。
sys.path.insert(0, str(ROOT / "tests"))
import run_all  # noqa: E402

check("run_all 把 IOT_ENABLED 钉成 1（不钉 ⇒ 几十条既有判据会按「没装」跑，集体红）",
      run_all._PINNED.get("IOT_ENABLED") == "1", str(run_all._PINNED))

import tools.base as B  # noqa: E402

check("本进程实际生效档 = run_all 钉的那一档（两处不一致 ⇒ 本套件在自欺）",
      B.IOT_ENABLED is True, f"进程内 IOT_ENABLED={B.IOT_ENABLED}")

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"❌ {len(FAILS)} 项未过：")
    for f in FAILS:
        print("   - " + f)
    raise SystemExit(1)
print("✅ 物联网平台开关（两档）：全部通过")
