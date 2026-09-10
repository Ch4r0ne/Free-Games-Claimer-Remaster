import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main
from src.core import notifier


class RunNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target in ("main.logger", "main.notify"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("notify_errors_only", False), ("notify_summary", True),
                            ("notify_missing_base", True), ("notify_claim_fails", True), ("notify_errors", True),
                            ("gog_force_redeem", False)):
            patcher = patch.object(main.cfg, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_mixed_store_run_sends_one_compact_message(self):
        epic = AsyncMock(return_value={"store": "Epic Games", "games": [
            {"title": "Luftrausers", "status": "claimed"},
            {"title": "Unavailable game", "status": "failed"}]})
        steam = AsyncMock(return_value={"store": "Steam", "games": [
            {"title": "Owned game", "status": "existed"}]})
        with patch.object(main, "_get_active_claimers", return_value=[("Epic Games", epic), ("Steam", steam)]), \
             patch.object(notifier, "notify", new_callable=AsyncMock) as send:
            await main.run_claimers()
        send.assert_awaited_once_with(
            "Luftrausers - Claimed - Epic Games\nUnavailable game - Failed - Epic Games",
            title="Luftrausers - Claimed - Epic Games")

    async def test_crash_respects_error_flag(self):
        claimer = AsyncMock(side_effect=RuntimeError("test failure"))
        with patch.object(main, "_get_active_claimers", return_value=[("Steam", claimer)]), \
             patch.object(main, "notify", new_callable=AsyncMock) as send:
            main.cfg.notify_errors = False
            await main.run_claimers()
            send.assert_not_awaited()
            main.cfg.notify_errors = True
            main.cfg.notify_errors_only = True
            await main.run_claimers()
            send.assert_awaited_once()
            self.assertIn("ERROR: Steam", send.call_args.args[0])

    async def test_pending_gog_results_are_included_once(self):
        gog_result = {"store": "GOG", "games": [{"title": "Giveaway", "status": "existed"}]}
        fake_gog = MagicMock()
        fake_gog.redeem_pending_codes = AsyncMock()
        fake_gog.notify_games = [{"title": "Redeemed game", "status": "claimed and redeemed (GOG)"}]
        session = MagicMock()
        session.execute = AsyncMock(return_value=MagicMock())
        session.execute.return_value.scalars.return_value.first.return_value = object()
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch.object(main, "_get_active_claimers", return_value=[("GOG", AsyncMock(return_value=gog_result))]), \
             patch("src.core.database.async_session", return_value=context), \
             patch("src.stores.gog.GOGClaimer", return_value=fake_gog), \
             patch.object(notifier, "notify", new_callable=AsyncMock) as send:
            await main.run_claimers()
        fake_gog.redeem_pending_codes.assert_awaited_once()
        send.assert_awaited_once_with(
            "Redeemed game - Claimed and redeemed (GOG) - GOG",
            title="Redeemed game - Claimed and redeemed (GOG) - GOG")


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_receives_compact_body(self):
        with patch.object(notifier.cfg, "discord_webhook", "https://example.invalid/webhook"), \
             patch.object(notifier, "send_discord", new_callable=AsyncMock) as discord, \
             patch.object(notifier, "send_apprise", new_callable=AsyncMock) as apprise:
            await notifier.notify("Game - Claimed - Steam", title="Game - Claimed - Steam")
        discord.assert_awaited_once_with("Game - Claimed - Steam", screenshot_path=None)
        apprise.assert_not_awaited()

    async def test_apprise_receives_game_title_and_body(self):
        with patch.object(notifier.cfg, "discord_webhook", None), \
             patch.object(notifier.cfg, "notify_url", "test"), \
             patch.object(notifier, "send_apprise", new_callable=AsyncMock) as send:
            await notifier.notify("Game - Failed - Steam", title="Game - Failed - Steam")
        send.assert_awaited_once_with("Game - Failed - Steam", title="Game - Failed - Steam")
