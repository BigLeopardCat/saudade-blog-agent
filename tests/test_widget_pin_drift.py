#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看板娘 pin 漂移提示：判据的判据（20261002）。

`scripts/widget_pin_drift.sh` 是一道**只警告、恒退 0** 的守卫。恒退 0 的守卫有个天然
风险：它自己也可能是**恒不出声**的——那样它就成了一块装饰，而且没有任何东西会告诉你。
所以这里把它的每一个分支都真跑一遍（离线、无网络：`curl` 用桩顶掉），逐条判
"该响的时候响、不该响的时候不响、任何情况下都不退非 0"。

跑法：`.venv/bin/python tests/test_widget_pin_drift.py`
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "widget_pin_drift.sh"
WIDGET = "frontend/public/live2d-widgets"
SAME = "1111111111111111111111111111111111111111"
OTHER = "2222222222222222222222222222222222222222"

FAILED = []


def check(cond, name, extra=""):
    if cond:
        print(f"  ✅ {name}" + (f"  [{extra}]" if extra else ""))
        return
    FAILED.append(name)
    print(f"  ❌ {name}" + (f"  [{extra}]" if extra else ""))


# ── 一个一次性的小仓：两次提交，一次动了看板娘前端、一次没动 ────────────────────
def build_repo(tmp: pathlib.Path) -> tuple[str, str, str]:
    """返回 (动了前端的提交, 没动前端的提交, 没动前端那个提交的父提交)。"""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    run = lambda *a: subprocess.run(a, cwd=tmp, env=env, check=True,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("git", "init", "-q", "-b", "main")
    (tmp / WIDGET).mkdir(parents=True)
    (tmp / WIDGET / "renderer.js").write_text("// v1\n", encoding="utf-8")
    (tmp / "src").mkdir()
    (tmp / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "feat: 起手")
    first = run_out(tmp, "git", "rev-parse", "HEAD")

    (tmp / WIDGET / "renderer.js").write_text("// v2 —— 动了看板娘\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "fix: 看板娘改动")
    touched = run_out(tmp, "git", "rev-parse", "HEAD")

    (tmp / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "chore: 只动后端")
    untouched = run_out(tmp, "git", "rev-parse", "HEAD")
    return touched, untouched, first


def run_out(cwd: pathlib.Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


# ── curl 桩：按 CURL_FAKE 决定"拿到什么 / 失败" ────────────────────────────────
CURL_STUB = """#!/usr/bin/env bash
case "${CURL_FAKE:-ok}" in
  fail) exit 22 ;;
  garbage) printf '%s' 'not json at all'; exit 0 ;;
  *) printf '{"trees":{"frontend/public/live2d-widgets":"%s"}}\\n' "${FAKE_TREE}"; exit 0 ;;
esac
"""


def run_script(repo: pathlib.Path, fake: str, fake_tree: str, *,
               event: str = "push", before: str = "", sha: str = "HEAD",
               token: str = "x") -> subprocess.CompletedProcess:
    binp = repo / "_stub"
    binp.mkdir(exist_ok=True)
    stub = binp / "curl"
    stub.write_text(CURL_STUB, encoding="utf-8")
    stub.chmod(0o755)
    env = {**os.environ,
           "PATH": f"{binp}:{os.environ['PATH']}",
           "CURL_FAKE": fake, "FAKE_TREE": fake_tree,
           "EVENT_NAME": event, "BEFORE": before, "SHA": sha}
    if token is None:
        env.pop("PARENT_REPO_TOKEN", None)
    else:
        env["PARENT_REPO_TOKEN"] = token
    return subprocess.run(["bash", str(SCRIPT)], cwd=repo, env=env,
                          capture_output=True, text=True)


print("① 不该出声的时候不出声")
with tempfile.TemporaryDirectory() as td:
    repo = pathlib.Path(td)
    touched, untouched, first = build_repo(repo)

    r = run_script(repo, "ok", SAME, event="workflow_dispatch", before=first, sha=touched)
    check(r.returncode == 0, "非 push 事件：退 0", f"code={r.returncode}")
    check("::warning::" not in r.stdout, "非 push 事件：不警告", r.stdout.strip()[:60])

    r = run_script(repo, "ok", SAME, before="", sha=touched)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "before 为空（新分支/强推）：退 0 且不警告")

    r = run_script(repo, "ok", SAME, before="0" * 40, sha=touched)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "before 是全 0：退 0 且不警告")

    r = run_script(repo, "ok", SAME, before=touched, sha=untouched)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "本次 push 没动看板娘前端：退 0 且不警告", r.stdout.strip()[:40])

    print("\n② 该出声：漂移要警告，一致不警告")
    r = run_script(repo, "ok", OTHER, before=first, sha=touched)
    check(r.returncode == 0, "漂移：仍退 0（只警告不判红）", f"code={r.returncode}")
    check("::warning::" in r.stdout, "漂移：出了 ::warning::")
    local_tree = run_out(repo, "git", "rev-parse", f"{touched}:{WIDGET}")
    check(OTHER[:8] in r.stdout and local_tree[:8] in r.stdout,
          "漂移：警告里同时写了「pin 指着谁」和「本地是哪棵」——否则人不知道往哪改")
    check("fetch:widget" in r.stdout, "漂移：警告里给了下一步动作（回父仓 bump）")

    local_tree = run_out(repo, "git", "rev-parse", f"{touched}:{WIDGET}")
    r = run_script(repo, "ok", local_tree, before=first, sha=touched)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "一致：不警告（树 sha 相同，即使提交号不同）", r.stdout.strip()[:50])

    print("\n③ 读不到父仓：明说一句再跳过，不许静默")
    r = run_script(repo, "ok", SAME, before=first, sha=touched, token=None)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "缺凭据：退 0 且不警告")
    check("PARENT_REPO_TOKEN" in r.stdout, "缺凭据：说得出是缺了哪样（不是静默）")

    r = run_script(repo, "fail", SAME, before=first, sha=touched)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "父仓读不到（HTTP 失败）：退 0 且不警告")
    check("::notice::" in r.stdout, "父仓读不到：留一条 notice，不装没事")

    r = run_script(repo, "garbage", SAME, before=first, sha=touched)
    check(r.returncode == 0 and "::warning::" not in r.stdout,
          "父仓返回的不是 JSON：退 0 且不警告（解析失败不许当成漂移）")
    check("::notice::" in r.stdout, "JSON 解析失败：留一条 notice")

    print("\n④ 脚本自己的形状")
    src = SCRIPT.read_text(encoding="utf-8")
    check("exit 1" not in src, "脚本里没有 exit 1（它是提示，不是门禁）")
    check(src.count("exit 0") >= 6, "每条退出路径都显式 exit 0",
          f"{src.count('exit 0')} 处")
    ev = (ROOT / ".github" / "workflows" / "eval.yml").read_text(encoding="utf-8")
    check("widget-pin-drift:" in ev and "scripts/widget_pin_drift.sh" in ev,
          "eval.yml 里有这个 job 且在跑这个脚本")
    check(ev.count("PARENT_REPO_TOKEN") >= 2, "这个 job 也拿到了那枚只读凭据")

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
    sys.exit(1)
print("全部通过")
