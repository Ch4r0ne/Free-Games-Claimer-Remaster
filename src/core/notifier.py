"""Notifications – sends you messages when games are claimed or errors occur.

Supports two notification systems:
  - Discord webhooks (set DISCORD_WEBHOOK in your .env file)
  - Apprise (supports Telegram, Slack, Email, and 80+ other services)

Discord is tried first. If no Discord webhook is configured, it falls back to Apprise.
If neither is set, notifications are silently skipped (the bot still works fine).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import apprise

from src.core.config import cfg

logger = logging.getLogger("fgc.notifier")


async def send_discord(
    message: str,
    *,
    screenshot_path: Path | None = None,
    username: str = "Free Games Claimer",
) -> None:
    """Send a message (and optional screenshot) to a Discord webhook."""
    webhook_url = cfg.discord_webhook
    if not webhook_url:
        logger.debug("DISCORD_WEBHOOK not set – skipping Discord notification.")
        return

    # Discord enforces a 2000-character limit per message.
    # Split long messages into chunks so nothing gets dropped.
    MAX_LEN = 2000
    chunks = []
    if len(message) <= MAX_LEN:
        chunks = [message]
    else:
        # Split on newline boundaries to keep formatting intact
        current = ""
        for line in message.split("\n"):
            # +1 accounts for the newline we'll re-add
            if len(current) + len(line) + 1 > MAX_LEN:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

    async with httpx.AsyncClient(timeout=30) as client:
        for i, chunk in enumerate(chunks):
            data = {"content": chunk, "username": username}
            # Attach the screenshot only to the first chunk
            if i == 0 and screenshot_path and screenshot_path.exists():
                files = {"file": (screenshot_path.name, screenshot_path.read_bytes(), "image/png")}
                resp = await client.post(webhook_url, data=data, files=files)
            else:
                resp = await client.post(webhook_url, json=data)

            if resp.status_code not in (200, 204):
                logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text)
            else:
                logger.info("Discord notification sent (%d/%d).", i + 1, len(chunks))


async def send_apprise(message: str, *, title: str | None = None) -> None:
    """Send a notification via any Apprise-supported service (fallback)."""
    notify_url = cfg.notify_url
    if not notify_url:
        logger.debug("NOTIFY not set – skipping Apprise notification.")
        return

    ap = apprise.Apprise()
    ap.add(notify_url)

    # apprise is sync – run in executor to avoid blocking the loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        lambda: ap.notify(body=message, title=title or "Free Games Claimer"),
    )
    logger.info("Apprise notification sent.")


async def notify(
    message: str,
    *,
    screenshot_path: Path | None = None,
    title: str | None = None,
) -> None:
    """Unified notification dispatcher – tries Discord first, then Apprise."""
    try:
        if cfg.discord_webhook:
            await send_discord(message, screenshot_path=screenshot_path)
        elif cfg.notify_url:
            await send_apprise(message, title=title)
        else:
            logger.debug("No notification service configured.")
    except Exception:
        logger.exception("Failed to send notification")


def format_result_lines(results: list[dict]) -> list[str]:
    """Build compact push lines, honoring success and failure preferences."""
    lines = []
    for result in results:
        for game in result.get("games", []):
            status = str(game.get("status") or "").strip()
            normalized = status.lower()
            if not status or normalized.startswith(("exist", "already", "skip")):
                continue
            # Match explicit causes, never game titles or arbitrary error text.
            if not cfg.notify_missing_base and normalized in {
                "failed:missing_base", "failed:requires-base-game", "requires base game",
            }:
                continue
            needs_action = any(word in normalized for word in (
                "failed", "not redeemed", "check manually", "needs linking",
                "requires base", "manual", "error",
            ))
            success = not needs_action and (
                normalized.startswith("claimed") or normalized.startswith("code:")
            )
            if success:
                if cfg.notify_errors_only or not cfg.notify_summary:
                    continue
                label = status[0].upper() + status[1:]
            else:
                if not cfg.notify_claim_fails:
                    continue
                if normalized.startswith("failed"):
                    label = status[0].upper() + status[1:]
                elif any(word in normalized for word in ("manual", "needs linking", "requires base")):
                    label = f"Action required: {status}"
                else:
                    label = f"Failed: {status}"
            title = " ".join(str(game.get("title") or "Unknown").split())
            store = " ".join(str(result.get("store") or "Unknown").split())
            label = " ".join(label.split())
            lines.append(f"{title} - {label} - {store}")
    return lines


async def notify_results(results: list[dict]) -> None:
    """Send successes and problems together without a redundant heading."""
    lines = format_result_lines(results)
    if lines:
        await notify("\n".join(lines), title=lines[0])
