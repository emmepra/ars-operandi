from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "skills" / "mailctl-email-access"
SKILL_PATH = SKILL_DIR / "SKILL.md"
PUBLIC_TEXT_SURFACES = (
    SKILL_PATH,
    SKILL_DIR / "agents" / "openai.yaml",
    SKILL_DIR / "references" / "runtime-and-installation.md",
    REPO_ROOT / "README.md",
)


class MailctlEmailAccessSkillContractTests(unittest.TestCase):
    def test_skill_encodes_the_fail_closed_consumer_runtime_contract(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")

        required_markers = (
            "Ars Operandi `mailctl` runtime",
            "sole mail runtime",
            "<ars-operandi-repo>",
            "exactly `gws` or `proton`",
            "One canonical `mailctl`",
            "Unknown provider",
            "provider mismatch",
            "cross-provider fallback",
            '--project-index "<project-index>"',
            '--config-root "<config-root>"',
            '--account "<alias>"',
            'uv run --project "<ars-operandi-repo>" mailctl status --account "<alias>"',
            "gws auth login --readonly --services gmail",
            "users.getProfile",
            "users.messages.list",
            "users.messages.get",
            "users.messages.attachments.get",
            "exact identity",
            "--after YYYY-MM-DD",
            "--before YYYY-MM-DD",
            "--max-results 10",
            "metadata",
            "mailctl content",
            "mailctl attachment",
            "sanitized HTML",
            "untrusted",
            "must not auto-open or execute",
            "--max-bytes 1048576",
            "--max-bytes 25000000",
            "--output \"/absolute/new/file\"",
            "From",
            "To",
            "Subject",
            "Date",
            "planned",
            "fail closed",
            "do not retry on a different account or provider",
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
                    line.startswith('uv run --project "<ars-operandi-repo>" mailctl '),
                    line,
                )

    def test_skill_keeps_provider_specific_onboarding_and_reads_fail_closed(
        self,
    ) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")

        required_markers = (
            "GWS browser consent remains a human CLI gate and is unavailable for Proton",
            "Proton activation remains external",
            "must not install, sign in to, or configure Proton Mail Bridge",
            "must not create, read, print, or reveal credentials",
            "at most 31 days",
            "100 results",
            "1000 matched UIDs",
            "pinned `localhost` STARTTLS",
            "dedicated macOS Keychain reference",
            "fixed `From`, `To`, `Subject`, and `Date` headers",
            "SMTP",
            "raw IMAP",
            "`planned` binding",
            "`verified` binding",
            "GWS selectors",
            "`OR`, braces, pipe, `in:anywhere`, `older_than`, and `newer_than`",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        proton_line = next(
            line
            for line in text.splitlines()
            if 'mailctl search --account "<proton-alias>"' in line
        )
        gws_line = next(
            line
            for line in text.splitlines()
            if 'mailctl search --account "<gws-alias>"' in line
        )
        self.assertNotIn("--query", proton_line)
        self.assertIn('--query "<selector>"', gws_line)

        self.assertNotIn("Proceed only when `<provider>` is exactly `gmail`", text)
        self.assertNotIn(
            "Never request bodies, snippets, threads, attachments",
            text,
        )

    def test_skill_encodes_runtime_ownership_session_and_atomic_cutover(self) -> None:
        skill = re.sub(r"\s+", " ", SKILL_PATH.read_text(encoding="utf-8"))
        raw_readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        readme = re.sub(r"\s+", " ", raw_readme)

        for marker in (
            "Ars Operandi owns the canonical runtime package, tests, skill, and installer",
            "inactive transitional source",
            "one atomic cutover",
            "one live authorization followed by two bounded operations without a second prompt",
            "switch consumer invocation and runtime discovery to Ars in the same change",
            "Dual-running and residual active runtime copies are forbidden",
            "remove Workflow Agent mail runtime discovery and its consumer binding",
            "Pi surface",
            "one Mac-local `ars-mail` MCP process",
            "only in RAM",
            "Codex/MCP restart is the reauthorization boundary",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)

        self.assertIn("owns the canonical provider-aware mail runtime", readme)
        self.assertIn("inactive transitional source", readme)
        self.assertNotIn(
            "cp -R skills/mailctl-email-access ~/.codex/skills/", raw_readme
        )

    def test_ui_metadata_is_provider_aware(self) -> None:
        metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")

        self.assertIn("GWS or Proton mail", metadata)
        self.assertIn("provider-aware", metadata)
        self.assertNotIn("bounded Gmail metadata", metadata)

    def test_skill_has_no_second_runner_or_private_consumer_data(self) -> None:
        self.assertFalse((SKILL_DIR / "scripts").exists())
        for surface in PUBLIC_TEXT_SURFACES:
            text = surface.read_text(encoding="utf-8")
            lowered = text.lower()
            for forbidden in (
                "subprocess",
                "googleapiclient",
                "requests.",
                "gws gmail",
                "/users/",
                "/home/",
            ):
                with self.subTest(surface=surface.name, forbidden=forbidden):
                    self.assertNotIn(forbidden, lowered)

            with self.subTest(surface=surface.name, check="email-addresses"):
                self.assertIsNone(
                    re.search(
                        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
                        text,
                    )
                )
            with self.subTest(surface=surface.name, check="absolute-paths"):
                self.assertIsNone(re.search(r"(?i)(?:/users|/home)/[^<\s]", text))

    @unittest.skipUnless(
        os.environ.get("MAILCTL_RUNTIME_REPO"),
        "set MAILCTL_RUNTIME_REPO for the external-cwd forward test",
    )
    def test_explicit_runtime_project_invocation_works_from_external_cwd(self) -> None:
        runtime_repo = Path(os.environ["MAILCTL_RUNTIME_REPO"]).expanduser().resolve()
        external_cwd = (
            Path(os.environ.get("MAILCTL_FORWARD_TEST_CWD", str(REPO_ROOT.parent)))
            .expanduser()
            .resolve()
        )

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
        self.assertIn("Fail-closed bounded read-only mail access", completed.stdout)


if __name__ == "__main__":
    unittest.main()
