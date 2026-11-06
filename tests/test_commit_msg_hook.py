"""Exercise contribution validation with isolated Git objects and literal PR titles."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".githooks/commit-msg"
CHECK = ROOT / ".github/scripts/check-contribution.sh"
TYPES = (
    "feat",
    "fix",
    "refactor",
    "perf",
    "test",
    "docs",
    "style",
    "build",
    "ci",
    "chore",
    "revert",
)


class ContributionPolicyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.environment = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.test",
        }

    def command(self, args, *, text=None, **env):
        return subprocess.run(
            args,
            cwd=self.root,
            env={**self.environment, **env},
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )

    def hook(self, message, *, title=False):
        path = self.root / "message.txt"
        path.write_text(message)
        return self.command(["bash", str(HOOK), str(path), *(["--title"] if title else [])])

    def git(self, *args, text=None):
        result = self.command(["git", *args], text=text)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def repository(self):
        self.git("init", "--quiet")
        folder = self.root / ".githooks"
        folder.mkdir()
        shutil.copy2(HOOK, folder / "commit-msg")
        return self.git("mktree", text="")

    def commit(self, tree, message, parent=None):
        return self.git("commit-tree", tree, *(["-p", parent] if parent else []), text=message)

    def test_supported_types_and_documentation(self):
        documentation = (ROOT / "CONTRIBUTING.md").read_text()
        for kind in TYPES:
            with self.subTest(kind=kind):
                self.assertIn(f"`{kind}`", documentation)
                result = self.hook(f"{kind}(chat): 更新说明\n")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_valid_messages_and_real_contributor_trailers(self):
        for message in (
            "docs: 更新说明\n",
            "feat(api)!: 更新接口\n\n需要迁移配置。\n",
            "refactor!: 移除旧接口\n",
            "fix(tools/own): 核对回执\n",
            "fix: " + "中" * 55 + "\n",
            "fix: " + "x" * 55 + "\n",
            "fix(chat): clarify recovery\n\nA complete paragraph is valid.\n\n"
            "Co-authored-by: Pat Example <pat@example.test>\n",
            "fix(chat): 恢复会话\r\n\r\n修复状态读取。\r\n",
        ):
            with self.subTest(message=message):
                result = self.hook(message)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_messages(self):
        for message in (
            "update(chat): 修改\n",
            "修复会话\n",
            "fix(chat):修改\n",
            "fix:    \n",
            "fix(): 修改\n",
            "fix(Chat): 修改\n",
            "fix(chat: 修改\n",
            "fix(chat)extra: 修改\n",
            "fix(chat runtime): 修改\n",
            "fix(chat): 修改\n缺少空行\n",
            "fix: " + "中" * 56 + "\n",
            "fix: " + "x" * 56 + "\n",
        ):
            with self.subTest(message=message):
                self.assertNotEqual(self.hook(message).returncode, 0)

    def test_generated_commit_messages_are_not_valid_pr_titles(self):
        for message in ("Merge branch 'main'\n", 'Revert "fix: x"\n', "fixup! fix: x\n"):
            with self.subTest(message=message):
                self.assertEqual(self.hook(message).returncode, 0)
                self.assertNotEqual(self.hook(message, title=True).returncode, 0)
        self.assertEqual(self.hook("\n").returncode, 0)
        self.assertNotEqual(self.hook("\n", title=True).returncode, 0)
        self.assertNotEqual(self.hook("fix: title\n\nbody\n", title=True).returncode, 0)

    def test_pr_title_is_literal_and_does_not_validate_working_commits(self):
        self.repository()
        result = self.command(
            ["bash", str(CHECK)],
            EVENT_NAME="pull_request",
            PR_TITLE="fix: $(touch injected-file)",
            HEAD_SHA="not-a-commit",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "injected-file").exists())
        result = self.command(["bash", str(CHECK)], EVENT_NAME="pull_request", PR_TITLE="invalid")
        self.assertNotEqual(result.returncode, 0)

    def test_mainline_validates_the_whole_new_range_and_current_head(self):
        tree = self.repository()
        base = self.commit(tree, "docs: 初始化\n")
        bad = self.commit(tree, "临时修改\n", base)
        head = self.commit(tree, "fix(chat): 恢复状态\n", bad)
        result = self.command(["bash", str(CHECK)], EVENT_NAME="push", BASE_SHA=base, HEAD_SHA=head)
        self.assertNotEqual(result.returncode, 0)
        result = self.command(["bash", str(CHECK)], EVENT_NAME="push", BASE_SHA=bad, HEAD_SHA=head)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.command(
            ["bash", str(CHECK)], EVENT_NAME="workflow_dispatch", BASE_SHA="", HEAD_SHA=head
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.command(
            ["bash", str(CHECK)], EVENT_NAME="push", BASE_SHA=base, HEAD_SHA="missing"
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
