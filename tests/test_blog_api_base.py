# -*- coding: utf-8 -*-
"""工具出口地址（`BLOG_API_BASE`，20261006）：**两档都要验**。

`tools/base.py` 的 `API_BASE` 决定 agent 读的是**谁的博客**——所有只读工具（文章、分类、
标签、留言板、站内搜索、知识库）与 **RAG 语料**（`rag/search.py::_fetch_corpus` 也走 `_get`）
都以它为前缀；导航工具给出的链接的站点根也由它反推。在 20261006 之前它是一个写死的字面量
（本站域名），于是 clone 这个仓的人**不改源码就没法把它指向自己的库**，而症状是**静默的**：
接口是通的、返回是真的，只是答案属于另一个站点。

**为什么必须开子进程**：`API_BASE` 是模块级常量（import 时从 settings 定型），
`SITE_BASE` 由它算出，导航事实文本的拼装也定型在函数里。同一个进程里改不了档——
只能各起一个子进程，把 env 喂进去再读结果。这也正是 `run_all.py` 与
`tests/test_iot_switch.py` 的做法。

**两档各自的失法**（都不报错）：
  · 出厂档被改 ⇒ 本站自己的行为变了（判据 ① 钉住"默认 = 历史上那个字面量"）；
  · 换档不起作用 ⇒ 别人的部署读的还是本站，或**只有一半跟着走**（读接口换了、导航链接
    没换 → 前端同源校验拦下跳转，模型照常说"已跳转"）⇒ 判据 ② ③ 分头盯这两半。

⚠️ 本套件**不断言本进程跑在哪一档**（产线 `.env` 若设了这一项，进程内就不是默认值——
那是配置事实、不是缺陷）。默认值在 ① 的子进程里验（那里显式不设该变量、且不读 `.env`）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓根
sys.path.insert(0, str(ROOT))

# 历史上写死在 `tools/base.py:41` 的那个值。① 拿它当"出厂档"的期望值——
# **这一条就是"默认没变"的判据**，不是随手抄一份（改了默认值它必须红）。
LEGACY_API_BASE = "https://saudade.site/api/public"

FAILS: list[str] = []


def check(desc: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + desc + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(desc)


# ── 子进程探针 ─────────────────────────────────────────────────────────
# 只回报**判据对象**，不回报任何配置值（本仓纪律：凭据一次都不许打印；这里虽然是公开
# 地址，但保持同一个形状，免得将来有人往探针里加字段时顺手带上密钥）。
_PROBE = r"""
import json, sys
sys.path.insert(0, %r)
import tools.base as B

# 出网地址探针：把 `_client` 的两个动词换掉，只记地址不发请求（离线套件的纪律：
# 秒级、无网络）。这条判据是**接线**——换了配置之后，真出去的 URL 跟不跟着走。
seen = []

class _Resp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return {"code": 200, "data": []}

def _fake_get(url, **kw):
    seen.append(["GET", url]); return _Resp()

def _fake_post(url, **kw):
    seen.append(["POST", url]); return _Resp()

B._client.get = _fake_get
B._client.post = _fake_post
B._get("/notes")
B.search_notes.invoke({"keyword": "x"})

nav = B.navigate_to.invoke({"path": "/talk", "confirm": False})
nav_whole = B.navigate_to.invoke({"path": "/device-console/", "confirm": False})
print(json.dumps({
    "api_base": B.API_BASE,
    "site_base": B.SITE_BASE,
    "nav_talk": str(nav),
    "nav_talk_cmd": (nav.meta or {}).get("cmd"),
    "nav_whole": str(nav_whole),
    "outbound": seen,
}, ensure_ascii=False))
"""

# 与 run_all 同一形状的钉子：出厂档 + .env 不读（本机 .env 是产线那份）。
_PINS = {"AGENT_TASK_STATE": "0", "SAUDADE_IGNORE_ENV_FILE": "1", "IOT_ENABLED": "1"}


def probe(api_base: str = "") -> dict:
    """起一个子进程读回全部判据对象；`api_base` 为空 = **不设该变量**（验出厂默认值）。"""
    env = dict(os.environ)
    env.update(_PINS)
    env.pop("BLOG_API_BASE", None)
    if api_base:
        env["BLOG_API_BASE"] = api_base
    r = subprocess.run([sys.executable, "-c", _PROBE % str(ROOT)],
                       cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit("子进程探针跑不起来（上面是它的输出）")
    return json.loads(r.stdout.strip().splitlines()[-1])


# ══════════════════════════════════════════════════════════════════
print("\n① 出厂档（不设变量、不读 .env）：与历史上那个写死的字面量逐字相同")

out = probe()

check("API_BASE 就是历史值（改了默认 = 改了本站线上行为）",
      out["api_base"] == LEGACY_API_BASE, out["api_base"])
check("站点根 = 该地址的 origin（导航链接由它拼）",
      out["site_base"] == "https://saudade.site", out["site_base"])
check("同源关系成立：站点根 + /api/public == API_BASE",
      out["site_base"] + "/api/public" == out["api_base"])
check("导航事实文本逐字未变",
      out["nav_talk"] == "页面已跳转：https://saudade.site/talk", out["nav_talk"])
check("整页目标的另一套措辞也逐字未变（它还没跳，字不一样）",
      out["nav_whole"] == "页面即将跳转：https://saudade.site/device-console/（本条回复说完再跳）",
      out["nav_whole"])
check("连线命令里的 url 也是那个绝对地址",
      out["nav_talk_cmd"] == {"kind": "navigate", "url": "https://saudade.site/talk",
                              "mode": "direct"}, str(out["nav_talk_cmd"]))

# ══════════════════════════════════════════════════════════════════
print("\n② 换档（BLOG_API_BASE=别的站）：读接口与导航链接**一起**跟着走")

OTHER = "https://blog.example.test/api/public"
alt = probe(OTHER)

check("API_BASE 读到了子进程里那个值",
      alt["api_base"] == OTHER, alt["api_base"])
check("导航链接换成新站根（只换一半 ⇒ 前端同源校验拦下跳转、模型照常说已跳转）",
      alt["nav_talk"] == "页面已跳转：https://blog.example.test/talk", alt["nav_talk"])
check("整页目标同样跟着换",
      alt["nav_whole"] == "页面即将跳转：https://blog.example.test/device-console/（本条回复说完再跳）",
      alt["nav_whole"])
check("连线命令里的 url 用的是新站根",
      alt["nav_talk_cmd"] == {"kind": "navigate", "url": "https://blog.example.test/talk",
                              "mode": "direct"}, str(alt["nav_talk_cmd"]))

# ══════════════════════════════════════════════════════════════════
print("\n③ 接线：换档之后**真出去**的 URL 落在新站（只改常量、不改调用点是不行的）")

_expected = [["GET", OTHER + "/notes"], ["POST", OTHER + "/notes/search"]]

check("_get 与 search_notes 两个动词都打到新基址",
      alt["outbound"] == _expected, str(alt["outbound"]))
check("对照：出厂档那一次打的是历史地址（证明 ③ 判的是环境、不是恒定值）",
      out["outbound"] == [["GET", LEGACY_API_BASE + "/notes"],
                          ["POST", LEGACY_API_BASE + "/notes/search"]], str(out["outbound"]))

# ══════════════════════════════════════════════════════════════════
print("\n④ 单一事实源：站点字面量只剩 settings 里那一处")

_src = (ROOT / "tools" / "base.py").read_text(encoding="utf-8")
check("tools/base.py 里不再有站点字面量（写回去 = 又变回一个改不动的常量）",
      "saudade.site" not in _src,
      f"出现 {_src.count('saudade.site')} 次")
check("默认值定义在 config/settings.py 的 blog_api_base 上",
      "blog_api_base" in (ROOT / "config" / "settings.py").read_text(encoding="utf-8"))
check("README 的环境变量表点出这一项（改配置的人得知道它存在）",
      "BLOG_API_BASE" in (ROOT / "README.md").read_text(encoding="utf-8"))

# ── 本进程：只验"常量跟着 settings 走"这件接线事实（任何档都成立），不验档位本身 ──
sys.path.insert(0, str(ROOT / "tests"))
import tools.base as B           # noqa: E402
from config import settings as S  # noqa: E402  （`config/__init__` 导出的就是那个实例）

check("本进程：API_BASE 是 settings.blog_api_base 的引用（不是另抄的一份字面量）",
      B.API_BASE == S.blog_api_base, B.API_BASE)
check("本进程：站点根与 API 基址同源",
      B.API_BASE.startswith(B.SITE_BASE + "/") if B.SITE_BASE else True,
      f"{B.SITE_BASE} + {B.API_BASE}")

# ══════════════════════════════════════════════════════════════════
print()
if FAILS:
    print(f"❌ {len(FAILS)} 项未过：")
    for f in FAILS:
        print("   - " + f)
    raise SystemExit(1)
print("✅ 工具出口地址（两档）：全部通过")
