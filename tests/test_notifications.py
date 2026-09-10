import os
import runpy
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.core import notifier


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in (("notify_errors_only", False), ("notify_summary", True),
                            ("notify_missing_base", True), ("notify_claim_fails", True)):
            patcher = patch.object(notifier.cfg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def results(self, *statuses):
        return [{"store": "Epic Games", "user": "Account", "games": [
            {"title": f"Game {i}", "status": status, "url": "https://example.com"}
            for i, status in enumerate(statuses)
        ]}]

    async def test_compact_mixed_results(self):
        with patch.object(notifier, "notify", new_callable=AsyncMock) as send:
            await notifier.notify_results(self.results("claimed", "failed"))
            send.assert_awaited_once_with(
                "Game 0 - Claimed - Epic Games\nGame 1 - Failed - Epic Games",
                title="Game 0 - Claimed - Epic Games")

    def test_errors_only_keeps_problems(self):
        notifier.cfg.notify_errors_only = True
        lines = notifier.format_result_lines(self.results(
            "claimed", "claimed and redeemed (GOG)", "code: ABC (Steam)",
            "failed", "requires base game", "needs linking (Ubisoft)",
            "code: ABC (GOG, not redeemed)", "code: ABC (GOG, check manually)"))
        self.assertEqual(len(lines), 5)
        self.assertTrue(all(" - Failed" in line or " - Action required:" in line for line in lines))

    def test_summary_disabled_does_not_hide_failures(self):
        notifier.cfg.notify_summary = False
        self.assertEqual(notifier.format_result_lines(self.results("claimed", "failed")),
                         ["Game 1 - Failed - Epic Games"])

    def test_claim_failures_disabled(self):
        notifier.cfg.notify_claim_fails = False
        self.assertEqual(notifier.format_result_lines(self.results("claimed", "failed")),
                         ["Game 0 - Claimed - Epic Games"])

    async def test_no_notification_for_unchanged_or_filtered_results(self):
        with patch.object(notifier, "notify", new_callable=AsyncMock) as send:
            await notifier.notify_results(self.results(
                "existed", "already redeemed (GOG)", "skipped:paid"))
            notifier.cfg.notify_errors_only = True
            await notifier.notify_results(self.results("claimed"))
            send.assert_not_awaited()

    def test_missing_base_alerts_enabled_by_default(self):
        lines = notifier.format_result_lines(self.results("failed:missing_base"))
        self.assertEqual(lines, ["Game 0 - Failed:missing_base - Epic Games"])

    def test_missing_base_filter_preserves_other_results(self):
        notifier.cfg.notify_missing_base = False
        results = self.results("failed:missing_base", "claimed", "failed:timeout",
                               "requires base game", "failed:requires-base-game")
        results[0]["store"] = "Steam"
        self.assertEqual(notifier.format_result_lines(results), [
            "Game 1 - Claimed - Steam", "Game 2 - Failed:timeout - Steam"])

    def test_missing_base_filter_in_errors_only_mode(self):
        notifier.cfg.notify_errors_only = True
        notifier.cfg.notify_missing_base = False
        self.assertEqual(notifier.format_result_lines(self.results(
            "failed:missing_base", "claimed", "failed")),
            ["Game 2 - Failed - Epic Games"])

    def test_missing_base_enabled_does_not_override_claim_failure_flag(self):
        notifier.cfg.notify_claim_fails = False
        self.assertEqual(notifier.format_result_lines(self.results(
            "failed:missing_base", "failed")), [])

    async def test_only_missing_base_results_send_nothing(self):
        notifier.cfg.notify_missing_base = False
        with patch.object(notifier, "notify", new_callable=AsyncMock) as send:
            await notifier.notify_results(self.results("failed:missing_base"))
            send.assert_not_awaited()

    def test_missing_base_filter_matches_status_not_title_or_error_details(self):
        notifier.cfg.notify_missing_base = False
        results = self.results("failed:timeout while checking missing_base")
        results[0]["games"][0]["title"] = "failed:missing_base"
        self.assertEqual(len(notifier.format_result_lines(results)), 1)

    def test_unknown_error_text_is_not_success(self):
        notifier.cfg.notify_errors_only = True
        lines = notifier.format_result_lines(self.results("Region unavailable"))
        self.assertEqual(lines, ["Game 0 - Failed: Region unavailable - Epic Games"])


class MissingBaseConfigTests(unittest.TestCase):
    def test_default_and_boolean_env_values(self):
        config_path = Path(notifier.__file__).with_name("config.py")
        for value, expected in ((None, True), ("true", True), ("false", False),
                                ("1", True), ("0", False)):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True), \
                 patch("dotenv.load_dotenv"):
                if value is not None:
                    os.environ["NOTIFY_MISSING_BASE"] = value
                config = runpy.run_path(str(config_path))["cfg"]
                self.assertIs(config.notify_missing_base, expected)


if __name__ == "__main__":
    unittest.main()
