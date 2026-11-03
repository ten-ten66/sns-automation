"""Media file and database cleanup utilities."""

import asyncio
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_5GB = 5 * 1024 ** 3


async def cleanup_old_media(
    media_dir: str = "data/media",
    max_age_days: int = 7,
    max_dir_size_bytes: int = _5GB,
):
    """Delete media files older than max_age_days.

    If the directory exceeds max_dir_size_bytes, also delete the oldest files
    first (regardless of age) until the directory is back under the threshold.
    """

    def _do_cleanup() -> int:
        media_path = Path(media_dir)
        if not media_path.exists():
            return 0

        cutoff = time.time() - (max_age_days * 86400)
        deleted = 0

        # Age-based pass: remove files older than max_age_days
        for f in media_path.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                except OSError as e:
                    logger.warning("Failed to delete %s: %s", f, e)

        # Disk-space pass: if still over threshold, evict oldest first
        files = sorted(
            (f for f in media_path.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime,
        )
        dir_size = sum(f.stat().st_size for f in files)
        for f in files:
            if dir_size <= max_dir_size_bytes:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                dir_size -= size
                deleted += 1
                logger.info(
                    "Disk threshold exceeded: evicted %s (%.1f MB)", f, size / 1e6
                )
            except OSError as e:
                logger.warning("Failed to delete %s: %s", f, e)

        return deleted

    deleted = await asyncio.to_thread(_do_cleanup)
    if deleted:
        logger.info("Cleaned up %d old media files from %s", deleted, media_dir)


async def _run_vacuum():
    """Run VACUUM on the SQLite database via a dedicated raw connection.

    VACUUM cannot run inside a transaction, so it must be executed on its own
    connection with autocommit semantics — not through an async_session.
    """
    import aiosqlite
    from config.settings import settings

    db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("VACUUM")
        logger.info("VACUUM completed")
    except Exception as e:
        logger.warning("VACUUM failed: %s", e)


async def backup_database():
    """Copy SQLite DB to data/backups/ with date stamp. Keep last 7 copies."""
    from config.settings import settings

    db_path = str(settings.database_url).replace("sqlite+aiosqlite:///", "")
    src = Path(db_path)
    if not src.exists():
        logger.warning("backup_database: source DB not found at %s", src)
        return

    backup_dir = Path("data/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d")
    dest = backup_dir / f"sns_automation_{stamp}.db"

    try:
        await asyncio.to_thread(shutil.copy2, str(src), str(dest))
        # Also copy WAL/SHM files if they exist (SQLite WAL mode)
        for suffix in ("-wal", "-shm"):
            wal_src = Path(str(src) + suffix)
            if wal_src.exists():
                wal_dest = Path(str(dest) + suffix)
                await asyncio.to_thread(shutil.copy2, str(wal_src), str(wal_dest))
        logger.info("Database backed up to %s", dest)
    except OSError as e:
        logger.error("Database backup failed: %s", e)
        return

    # Prune backups older than 7 days
    cutoff = time.time() - (7 * 86400)

    def _prune():
        removed = 0
        for f in backup_dir.glob("sns_automation_*.db"):
            if f.stat().st_mtime < cutoff:
                try:
                    f.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning("Failed to remove old backup %s: %s", f, e)
        return removed

    removed = await asyncio.to_thread(_prune)
    if removed:
        logger.info("Pruned %d old database backups", removed)


async def cleanup_old_db_rows():
    """Delete expired/old rows from growing tables to keep DB lean."""
    from src.core.database import async_session
    from src.core.models import TrendTopic, ViralPost, PostMetric
    from sqlalchemy import delete

    try:
        async with async_session() as session:
            now = datetime.now(timezone.utc)

            # Delete expired trend topics
            result = await session.execute(
                delete(TrendTopic).where(TrendTopic.expires_at < now)
            )
            trends_deleted = result.rowcount

            # Delete trend topics older than 14 days (even without expires_at)
            cutoff_14d = now - timedelta(days=14)
            result = await session.execute(
                delete(TrendTopic).where(TrendTopic.discovered_at < cutoff_14d)
            )
            trends_deleted += result.rowcount

            # Delete viral posts older than 90 days
            cutoff_90d = now - timedelta(days=90)
            result = await session.execute(
                delete(ViralPost).where(ViralPost.discovered_at < cutoff_90d)
            )
            viral_deleted = result.rowcount

            # Delete post metrics older than 90 days
            result = await session.execute(
                delete(PostMetric).where(PostMetric.recorded_at < cutoff_90d)
            )
            metrics_deleted = result.rowcount

            await session.commit()

            total = trends_deleted + viral_deleted + metrics_deleted
            if total:
                logger.info(
                    "DB cleanup: deleted %d trend topics, %d viral posts, %d old metrics",
                    trends_deleted, viral_deleted, metrics_deleted,
                )

    except Exception as e:
        logger.error("DB cleanup failed: %s", e)

    # VACUUM must run outside the session transaction (SQLite only)
    from config.settings import settings
    if "sqlite" in str(settings.database_url):
        await _run_vacuum()
