"""Offline coverage for advisory pin drift, unavailable inputs and model-only changes."""


import json
import os
import pathlib
import subprocess
import sys
import tempfile

import pytest
from _judge import collector

ROOT = pathlib.Path(__file__).resolve().parent.parent


SCRIPT = ROOT / "scripts" / "widget_pin_drift.sh"


WIDGET = "frontend/public/live2d-widgets"


FAILED = []
check = collector(FAILED)


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


def test_widget_pin_drift():
    SAME = "1111111111111111111111111111111111111111"
    OTHER = "2222222222222222222222222222222222222222"
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
        r = run_script(repo, "ok", OTHER, event="pull_request", before=first, sha=touched)
        check(r.returncode == 0 and "::warning::" in r.stdout,
              "PR 事件：漂移照样警告（不是只在 push 上判）", r.stdout.strip()[:60])

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
        check(r.returncode == 0 and "::warning::" in r.stdout,
              "没有私人凭据仍读取公开 pin 并发现漂移")
        check("PARENT_REPO_TOKEN" not in r.stdout, "公开读取不要求配置私人凭据")

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
        invalid = run_script(repo, "ok", SAME, before="f" * 40, sha=touched)
        check(invalid.returncode == 0 and "::notice::" in invalid.stdout,
              "未知基线：显式未评估且不阻断")
        invalid = run_script(repo, "ok", SAME, before=first, sha="f" * 40)
        check(invalid.returncode == 0 and "::notice::" in invalid.stdout,
              "未知目标：显式未评估且不阻断")
        ev = (ROOT / ".github" / "workflows" / "eval.yml").read_text(encoding="utf-8")
        check("widget-pin-drift:" in ev and "scripts/widget_pin_drift.sh" in ev,
              "eval.yml 里有这个 job 且在跑这个脚本")
        check("secrets." not in ev, "公开读取不使用仓库私人凭据")
    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：" + "；".join(FAILED))
        sys.exit(1)
    print("全部通过")

    assert not FAILED, (
        f"{len(FAILED)} 条未通过：" + "；".join(FAILED))


PATHS = ('frontend/public/live2d-widgets', 'frontend/public/live2d_model')


def git(root, *args):
    return subprocess.check_output(
        ['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
         '-c', 'commit.gpgsign=false', *args], cwd=root, text=True,
    ).strip()


@pytest.mark.parametrize('changed_path', PATHS)
@pytest.mark.parametrize('synced', [False, True])
def test_changed_subtree_is_checked_against_its_own_pin(tmp_path, changed_path, synced):
    git(tmp_path, 'init', '-q')
    for index, path in enumerate(PATHS):
        directory = tmp_path / path
        directory.mkdir(parents=True)
        (directory / f'fixture-{index}.txt').write_text('before')
    git(tmp_path, 'add', 'frontend')
    git(tmp_path, 'commit', '-qm', 'before')
    before = git(tmp_path, 'rev-parse', 'HEAD')
    target = next((tmp_path / changed_path).glob('*.txt'))
    target.write_text('after')
    git(tmp_path, 'add', 'frontend')
    git(tmp_path, 'commit', '-qm', 'after')
    after = git(tmp_path, 'rev-parse', 'HEAD')
    pin_revision = after if synced else before
    pin = tmp_path / 'pin.json'
    pin.write_text(json.dumps({'trees': {
        path: git(tmp_path, 'rev-parse', pin_revision + ':' + path) for path in PATHS
    }}))
    commands = tmp_path / 'bin'
    commands.mkdir()
    curl = commands / 'curl'
    curl.write_text('#!/bin/sh\ncat "$PIN_FIXTURE"\n')
    curl.chmod(0o755)
    result = subprocess.run(
        ['bash', str(ROOT / 'scripts/widget_pin_drift.sh')], cwd=tmp_path,
        env={**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
             'PIN_FIXTURE': str(pin), 'BEFORE': before, 'SHA': after,
             'EVENT_NAME': 'pull_request'}, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert changed_path in result.stdout
    assert ('::warning::' in result.stdout) is not synced
    assert (PATHS[1] if changed_path == PATHS[0] else PATHS[0]) not in result.stdout


if __name__ == "__main__":
    test_widget_pin_drift()
