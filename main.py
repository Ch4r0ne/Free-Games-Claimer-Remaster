"""Free Games Claimer Remaster – main entry point.

This is the central "brain" of the application. When the Docker container starts,
this file is the first thing that runs. Here is what it does:

  1. Prints a startup banner with the version number and author.
  2. Initialises the SQLite database (creates tables if they don't exist).
  3. Starts a scheduler that automatically runs the claiming process every X hours.
  4. On each run, it goes through each enabled store (Steam, Epic, Prime, GOG)
     and tries to claim any free games available.
  5. After all stores are done, it checks if there are any GOG codes from
     Prime Gaming that still need to be redeemed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.core.config import cfg
from src.core.database import init_db
from src.stores.epic import claim_epic
from src.stores.gamerpower import claim_gamerpower
from src.stores.gog import claim_gog
from src.stores.prime import claim_prime
from src.stores.steam import claim_steam
from src.core.notifier import notify
from src.version import __version__, __author__, __repo__

# ---------------------------------------------------------------------------
# Logging – user-friendly by default, verbose only on errors
# ---------------------------------------------------------------------------
from rich.logging import RichHandler
from rich.markup import escape
from rich.console import Console

# This filter automatically adds the store name (e.g. "[Steam]", "[Epic]")
# in front of every log message, so you can easily tell which module is talking.
class StorePrefixFilter(logging.Filter):
    def filter(self, record):
        if record.name.startswith("fgc."):
            store = record.name.split(".")[-1]
            if store in ("epic", "steam", "gog", "prime"):
                store_map = {"gog": "GOG", "epic": "Epic", "steam": "Steam", "prime": "Prime"}
                prefix = escape(f"[{store_map[store]}]")
                # Prepend to the message template
                record.msg = f"{prefix} {record.msg}"
        return True

handler = RichHandler(
    console=Console(width=500),
    rich_tracebacks=True,
    show_path=False,       # hide file:line references
    show_level=True,
    show_time=True,        # Re-enabled per user request
    markup=True,
)
handler.addFilter(StorePrefixFilter())

logging.basicConfig(
    level=logging.DEBUG if cfg.debug else logging.INFO,
    format="%(message)s",
    handlers=[handler],
)
logger = logging.getLogger("fgc")

# Silence verbose third-party loggers
logging.getLogger("nodriver").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logging.getLogger("uc").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Store registry – canonical name → (display name, coroutine function)
# ---------------------------------------------------------------------------

# Registry of all available store claimers.
# Each entry maps a short name to a (display name, function) pair.
# When the scheduler runs, it loops through these and calls each function.
ALL_CLAIMERS: dict[str, tuple[str, object]] = {
    "steam":      ("Steam",        claim_steam),
    "epic":       ("Epic Games",   claim_epic),
    "prime":      ("Prime Gaming", claim_prime),
    "gog":        ("GOG",          claim_gog),
    "gamerpower": ("GamerPower",   claim_gamerpower),
}

# Accepted aliases → canonical name
_ALIASES: dict[str, str] = {
    "steam":         "steam",
    "steam-games":   "steam",
    "epic":          "epic",
    "epic-games":    "epic",
    "epicgames":     "epic",
    "prime":         "prime",
    "prime-gaming":  "prime",
    "primegaming":   "prime",
    "amazon":        "prime",
    "gog":           "gog",
    "gamerpower":    "gamerpower",
    "gp":            "gamerpower",
}


def _resolve_stores(raw: list[str]) -> list[str]:
    """Resolve a list of user-provided store names to canonical keys."""
    resolved = []
    for name in raw:
        key = _ALIASES.get(name.lower().strip())
        if key is None:
            logger.warning("Unknown store '%s' – ignoring. Valid: %s",
                           name, ", ".join(ALL_CLAIMERS.keys()))
            continue
        if key not in resolved:
            resolved.append(key)
    return resolved


def _get_active_claimers() -> list[tuple[str, object]]:
    """Determine which claimers to run based on CLI args / STORES env var.

    Priority:
      1. CLI positional args  (e.g.  ``python main.py steam prime``)
      2. ``STORES`` env var   (e.g.  ``STORES=steam,prime``)
      3. All stores           (default)
    """
    # Collect positional args (skip flags like --once)
    cli_stores = [a for a in sys.argv[1:] if not a.startswith("-")]

    if cli_stores:
        selected = _resolve_stores(cli_stores)
    elif cfg.stores:
        selected = _resolve_stores([s for s in cfg.stores.split(",") if s.strip()])
    else:
        selected = ["steam", "epic", "prime", "gog"]

    return [(ALL_CLAIMERS[k][0], ALL_CLAIMERS[k][1]) for k in selected if k in ALL_CLAIMERS]


def _print_banner() -> None:
    """Print startup banner with version and author info."""
    commit = os.getenv("COMMIT", "")[:8]
    branch = os.getenv("BRANCH", "")
    build_info = f"  ({branch}@{commit})" if commit else ""

    W = 60  # inner width between ║ chars
    lines = [
        f"  Free Games Claimer Remaster  v{__version__}{build_info}",
        f"  by {__author__}",
        f"  {__repo__}",
    ]
    print(f"\n╔{'═' * W}╗")
    for line in lines:
        print(f"║{line.ljust(W)}║")
    print(f"╚{'═' * W}╝\n")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_claimers() -> None:
    """Run selected claimers sequentially (they each open their own browser)."""
    claimers = _get_active_claimers()

    if not claimers:
        logger.warning("No valid stores selected. Nothing to do.")
        return

    store_names = [name for name, _ in claimers]
    logger.info("🎮 Starting claiming run… %s", ", ".join(store_names))

    aggregated_results = []

    for name, func in claimers:
        try:
            res = await func()
            if isinstance(res, dict) and res.get("games"):
                aggregated_results.append(res)
        except Exception:
            logger.exception("✗ %s crashed", name)
            await notify(f"{name} claimer crashed with an unhandled exception. Check logs.")

    # After standard claimers finish, check for pending GOG codes from Prime Gaming.
    # Only run if there are actually codes with status="claimed" waiting,
    # or if GOG_FORCE_REDEEM is explicitly enabled.
    if "GOG" not in store_names:
        logger.debug("Skipping pending GOG codes redemption as 'gog' is not in STORES.")
    else:
        try:
            from src.core.database import async_session, ClaimedGame
            from sqlalchemy import select
            
            # Quick check: are there any pending GOG codes at all?
            has_pending = False
            async with async_session() as session:
                if cfg.gog_force_redeem:
                    has_pending = True  # Force mode: always check
                else:
                    stmt = select(ClaimedGame).where(
                        ClaimedGame.status == "claimed",
                        ClaimedGame.code.isnot(None),
                        ClaimedGame.code != ""
                    ).limit(1)
                    result = await session.execute(stmt)
                    has_pending = result.scalars().first() is not None
            
            if has_pending:
                from src.stores.gog import GOGClaimer
                gog = GOGClaimer()
                await gog.redeem_pending_codes()
                if gog.notify_games:
                    gog_entry = next((e for e in aggregated_results if e["store"] == "GOG"), None)
                    if gog_entry:
                        gog_entry["games"].extend(gog.notify_games)
                    else:
                        aggregated_results.append({"store": "GOG", "user": gog.user, "games": gog.notify_games})
            else:
                logger.debug("No pending GOG codes to redeem.")
        except Exception:
            logger.exception("Failed to run post-claim GOG code redemption")

    # Final Summary Notification
    if cfg.notify_summary and aggregated_results:
        from src.core.notifier import format_game_list
        msg_parts = []
        for result in aggregated_results:
            # Filter out games that were "existed" or "already redeemed"
            relevant_games = [
                g for g in result["games"]
                if "status" in g 
                and "exist" not in g["status"].lower() 
                and "already" not in g["status"].lower()
                and "skip" not in g["status"].lower()
            ]
            
            if not relevant_games:
                continue
                
            header = f"**{result['store']}** ({result['user']}):" if result.get('user') else f"**{result['store']}**:"
            msg_parts.append(f"{header}\n{format_game_list(relevant_games)}")
            
        if msg_parts:
            final_msg = "\n\n".join(msg_parts)
            await notify(final_msg)

    logger.info("✔ Claiming run complete.")


async def main() -> None:
    """Initialise DB and either run once or start the scheduler."""
    _print_banner()
    await init_db()
    logger.info("Database ready.")

    if cfg.reset_db_games:
        try:
            from datetime import datetime, timedelta, timezone
            from src.core.database import async_session, ClaimedGame
            from sqlalchemy import delete
            
            seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
            
            async with async_session() as session:
                stmt = delete(ClaimedGame).where(ClaimedGame.created_at >= seven_days_ago)
                res = await session.execute(stmt)
                if res.rowcount > 0:
                    logger.info("Reset %d entry(s) from the last 7 days from history.", res.rowcount)
                else:
                    logger.debug("DB reset requested, but no entries found from the last 7 days.")
                await session.commit()
        except Exception as e:
            logger.error("Failed to reset DB games: %s", e)

    # If --once flag is set, run a single pass and exit
    if "--once" in sys.argv:
        await run_claimers()
        return

    # Otherwise start the scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_claimers,
        trigger=CronTrigger(hour=f"*/{cfg.scheduler_hours}"),  # every X hours
        id="claim_all",
        name="Claim free games",
        replace_existing=True,
    )

    # Delay slightly to ensure TurboVNC/X11 is fully initialized BEFORE starting Chrome
    logger.info("Waiting for virtual display to initialize...")
    await asyncio.sleep(3)

    # Also run immediately on startup
    scheduler.add_job(
        run_claimers,
        id="claim_all_startup",
        name="Initial claiming run",
    )

    scheduler.start()
    logger.info("⏱ Scheduler active – runs every %s hours.", cfg.scheduler_hours)

    try:
        # Keep the event loop alive
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down…")
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(main())
