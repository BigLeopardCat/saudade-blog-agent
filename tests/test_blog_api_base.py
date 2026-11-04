"""工具出口地址：**两档都要验**。
"""


import json
import os
import subprocess
import sys
from pathlib import Path

from _judge import collector

ROOT = Path(__file__).resolve().parent.parent  # 仓根


sys.path.insert(0, str(ROOT))


FAILS: list[str] = []
check = collector(FAILS)


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


_PINS = {"SAUDADE_IGNORE_ENV_FILE": "1", "IOT_ENABLED": "1"}


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


sys.path.insert(0, str(ROOT / "tests"))


import tools.base as B  # noqa: E402
from config import settings as S  # noqa: E402  （`config/__init__` 导出的就是那个实例）


def test_blog_api_base():
    DEFAULT_API_BASE = "http://127.0.0.1:3000/api/public"
    print("\n① 默认配置：只连接本机宿主，不访问维护者站点")
    out = probe()
    check("默认 API_BASE 指向本机宿主",
          out["api_base"] == DEFAULT_API_BASE, out["api_base"])
    check("站点根 = 该地址的 origin（导航链接由它拼）",
          out["site_base"] == "http://127.0.0.1:3000", out["site_base"])
    check("同源关系成立：站点根 + /api/public == API_BASE",
          out["site_base"] + "/api/public" == out["api_base"])
    check("导航目标与宿主同源",
          out["nav_talk"] == "页面已跳转：http://127.0.0.1:3000/talk", out["nav_talk"])
    check("整页导航保留待执行状态",
          out["nav_whole"]
              == "页面即将跳转：http://127.0.0.1:3000/device-console/（本条回复说完再跳）",
          out["nav_whole"])
    check("连线命令里的 url 也是那个绝对地址",
          out["nav_talk_cmd"] == {"kind": "navigate", "url": "http://127.0.0.1:3000/talk",
                                  "mode": "direct"}, str(out["nav_talk_cmd"]))
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
    print("\n③ 接线：换档之后**真出去**的 URL 落在新站（只改常量、不改调用点是不行的）")
    _expected = [["GET", OTHER + "/notes"], ["POST", OTHER + "/notes/search"]]
    check("_get 与 search_notes 两个动词都打到新基址",
          alt["outbound"] == _expected, str(alt["outbound"]))
    check("对照：默认请求使用本机基址",
          out["outbound"] == [["GET", DEFAULT_API_BASE + "/notes"],
                              ["POST", DEFAULT_API_BASE + "/notes/search"]], str(out["outbound"]))
    print("\n④ 单一事实源：站点字面量只剩 settings 里那一处")
    _src = "".join((ROOT / "tools" / f).read_text(encoding="utf-8")
                   for f in ("base.py", "notes.py"))
    check("tools/base.py + tools/notes.py 里不再有站点字面量（写回去 = 又变回一个改不动的常量）",
          "saudade.site" not in _src,
          f"出现 {_src.count('saudade.site')} 次")
    check("默认值定义在 config/settings.py 的 blog_api_base 上",
          "blog_api_base" in (ROOT / "config" / "settings.py").read_text(encoding="utf-8"))
    check("README 的环境变量表点出这一项（改配置的人得知道它存在）",
          "BLOG_API_BASE" in (ROOT / "README.md").read_text(encoding="utf-8"))
    check("本进程：API_BASE 是 settings.blog_api_base 的引用（不是另抄的一份字面量）",
          B.API_BASE == S.blog_api_base, B.API_BASE)
    check("本进程：站点根与 API 基址同源",
          B.API_BASE.startswith(B.SITE_BASE + "/") if B.SITE_BASE else True,
          f"{B.SITE_BASE} + {B.API_BASE}")
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项未过：")
        for f in FAILS:
            print("   - " + f)
        raise SystemExit(1)
    print("✅ 工具出口地址（两档）：全部通过")

    assert not FAILS, (
        f"{len(FAILS)} 条未通过：" + "；".join(FAILS))


if __name__ == "__main__":
    test_blog_api_base()
