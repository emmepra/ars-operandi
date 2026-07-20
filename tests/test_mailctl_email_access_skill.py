from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "mailctl-email-access"
SKILL_PATH = SKILL_DIR / "SKILL.md"


class MailctlEmailAccessSkillContractTests(unittest.TestCase):
    def test_skill_encodes_the_fail_closed_consumer_runtime_contract(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")

        required_markers = (
            "consumer-provided `mailctl`",
            "sole Gmail runtime",
            "<runtime-repo>",
            "<provider>",
            "Proceed only when `<provider>` is exactly `gmail`",
            "no cross-provider fallback",
            "--project-index \"<project-index>\"",
            "--config-root \"<config-root>\"",
            "--account \"<alias>\"",
            "uv run --project \"<runtime-repo>\" mailctl status --account \"<alias>\"",
            "gws auth login --readonly --services gmail",
            "users.getProfile",
            "users.messages.list",
            "users.messages.get",
            "exact identity",
            "--after YYYY-MM-DD",
            "--before YYYY-MM-DD",
            "--max-results 1",
            "metadata",
            "From",
            "To",
            "Subject",
            "Date",
            "planned",
            "fail closed",
            "Never fall back",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        self.assertRegex(text, r"(?m)^description: Use when ")

        command_lines = tuple(
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith(("mailctl ", "uv run "))
        )
        self.assertTrue(command_lines)
        for line in command_lines:
            with self.subTest(command=line):
                self.assertTrue(
                    line.startswith('uv run --project "<runtime-repo>" mailctl '),
                    line,
                )

    def test_skill_has_no_second_runner_or_private_consumer_data(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")
        lowered = text.lower()

        self.assertFalse((SKILL_DIR / "scripts").exists())
        for forbidden in (
            "subprocess",
            "googleapiclient",
            "requests.",
            "gws gmail",
            "/users/",
            "/home/",
            "gmail.com",
            "uniroma1",
            "icarofoundation",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, lowered)

        self.assertIsNone(re.search(r"\b(personal|sapienza|icaro)\b", lowered))

    @unittest.skipUnless(
        os.environ.get("MAILCTL_RUNTIME_REPO"),
        "set MAILCTL_RUNTIME_REPO for the external-cwd forward test",
    )
    def test_explicit_runtime_project_invocation_works_from_external_cwd(self) -> None:
        runtime_repo = Path(os.environ["MAILCTL_RUNTIME_REPO"]).expanduser().resolve()
        external_cwd = Path(
            os.environ.get("MAILCTL_FORWARD_TEST_CWD", str(REPO_ROOT.parent))
        ).expanduser().resolve()

        self.assertTrue((runtime_repo / "pyproject.toml").is_file())
        self.assertTrue(external_cwd.is_dir())
        completed = subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(runtime_repo),
                "mailctl",
                "--help",
            ],
            cwd=external_cwd,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Fail-closed bounded Gmail metadata access", completed.stdout)


if __name__ == "__main__":
    unittest.main()
