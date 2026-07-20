from __future__ import annotations

import re
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
            "--project-index \"<project-index>\"",
            "--config-root \"<config-root>\"",
            "--account \"<alias>\"",
            "mailctl status --account \"<alias>\"",
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


if __name__ == "__main__":
    unittest.main()
