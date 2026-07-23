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
    REPO_ROOT / "README.md",
)


class MailctlEmailAccessSkillContractTests(unittest.TestCase):
    def test_skill_encodes_the_fail_closed_consumer_runtime_contract(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")

        required_markers = (
            "consumer-provided `mailctl`",
            "sole mail runtime",
            "<runtime-repo>",
            "<provider>",
            "exactly `gws` or `proton`",
            "one canonical `mailctl` dispatches internally",
            "unknown provider",
            "provider mismatch",
            "no cross-provider fallback",
            "--project-index \"<project-index>\"",
            "--config-root \"<config-root>\"",
            "--account \"<alias>\"",
            "uv run --project \"<runtime-repo>\" mailctl status --account \"<alias>\"",
            "gws auth login --readonly --services gmail",
            "users.getProfile",
            "users.messages.list",
            "users.messages.get",
            "users.messages.attachments.get",
            "exact identity",
            "--after YYYY-MM-DD",
            "--before YYYY-MM-DD",
            "--max-results 1",
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

    def test_skill_keeps_provider_specific_onboarding_and_reads_fail_closed(self) -> None:
        text = SKILL_PATH.read_text(encoding="utf-8")

        required_markers = (
            "GWS OAuth is available only for a `gws` route",
            "unavailable for a `proton` route",
            "Proton activation is a separate confirmed human gate",
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
            "binding is `planned`",
            "binding is `verified`",
            "GWS selectors",
            "`OR`, braces, pipe, `in:anywhere`, `older_than`, and `newer_than`",
        )
        for marker in required_markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

        proton_search = re.search(
            r"Proton bounded search[^`]*```bash\n(?P<command>[^`]+)```",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(proton_search)
        assert proton_search is not None
        command = proton_search.group("command")
        self.assertIn('mailctl search --account "<proton-alias>"', command)
        self.assertNotIn("--query", command)

        gws_search = re.search(
            r"GWS bounded search[^`]*```bash\n(?P<command>[^`]+)```",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(gws_search)
        assert gws_search is not None
        self.assertIn('--query "<selector>"', gws_search.group("command"))

        self.assertNotIn("Proceed only when `<provider>` is exactly `gmail`", text)
        self.assertNotIn(
            "Never request bodies, snippets, threads, attachments",
            text,
        )

    def test_skill_encodes_transitional_runtime_ownership_and_atomic_cutover(self) -> None:
        skill = re.sub(r"\s+", " ", SKILL_PATH.read_text(encoding="utf-8"))
        readme = re.sub(
            r"\s+", " ", (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        )

        for marker in (
            "Ars Operandi is the durable owner of the public mail operating surface",
            "transitional source of the on-demand `mailctl` command",
            "The only permitted entrypoint is `mailctl`",
            "must not activate or install any other Workflow Agent subsystem",
            "daemon, scheduler, job, intake, Linear, vault, WhatsApp, task, thread, portfolio, or Pi",
            "separate bounded cutover change",
            "generalize the consumer-specific aliases",
            "transfer the `mailctl` runtime and its tests into Ars Operandi",
            "switch discovery and consumer invocation atomically",
            "Dual-running and residual runtime copies are forbidden",
            "remove Workflow Agent from runtime discovery and the consumer Project Index",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, skill)

        self.assertIn("durable owner of the public mail operating surface", readme)
        self.assertIn("transitional `mailctl` source", readme)

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
        self.assertIn("Fail-closed bounded read-only mail access", completed.stdout)


if __name__ == "__main__":
    unittest.main()
