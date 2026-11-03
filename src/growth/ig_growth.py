"""Instagram follower growth automation.

Tactics:
1. Hashtag-targeted liking
2. Strategic follow/unfollow of competitor audiences
"""

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timezone, timedelta

from src.platforms.instagram_platform import InstagramPlatform
from src.core.safety import daily_tracker
from src.core.cost_tracker import cost_tracker
from src.core.utils import lognormal_delay

logger = logging.getLogger(__name__)

# Target hashtags for liking
BEAUTY_HASHTAGS = [
    "skincare", "beautytips", "glowup", "skincareRoutine",
    "jbeauty", "glassskin", "makeuptutorial", "beautyreview",
    "kbeauty", "skincarejunkie",
]
ROUTINE_HASHTAGS = [
    "morningroutine", "nightroutine", "selfcare",
    "makeuplook", "cosmeticsreview",
]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FOLLOW_TRACKING_FILE = os.path.join(_PROJECT_ROOT, "data", "ig_follows.json")

# Competitor accounts to mine followers from
def _load_competitor_accounts() -> list[str]:
    """Load competitor accounts from config file or return empty list.

    Place one username per line in data/ig_competitors.txt to enable
    the follow cycle. Blank lines and lines starting with # are ignored.
    """
    config_path = os.path.join(_PROJECT_ROOT, "data", "ig_competitors.txt")
    if not os.path.exists(config_path):
        return []
    try:
        with open(config_path) as f:
            accounts = [
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        return accounts
    except Exception:
        return []


COMPETITOR_ACCOUNTS = _load_competitor_accounts()


def _load_follow_tracking() -> dict:
    if os.path.exists(FOLLOW_TRACKING_FILE):
        try:
            with open(FOLLOW_TRACKING_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"followed": {}, "unfollowed": []}


def _save_follow_tracking(data: dict):
    os.makedirs(os.path.dirname(FOLLOW_TRACKING_FILE), exist_ok=True)
    with open(FOLLOW_TRACKING_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


class IGGrowthEngine:
    """Automated growth tactics for Instagram."""

    def __init__(self, platform: InstagramPlatform):
        self.platform = platform
        self._liked_media_ids: set[str] = set()

    async def run_hashtag_like_cycle(self, max_likes: int = 10):
        """Like recent posts from target hashtags.

        Users get notified, visit profile, follow if content is good.
        Expected: 3-8% visit rate, 20-30% follow rate of visitors.
        """
        if cost_tracker.is_budget_exceeded():
            logger.info("Budget exceeded, skipping IG cycle")
            return
        if not daily_tracker.can_perform("instagram", "likes"):
            logger.info("IG like limit reached, skipping")
            return

        await self.platform._ensure_login()
        cl = self.platform.client

        # Mix beauty + routine hashtags
        all_tags = BEAUTY_HASHTAGS + ROUTINE_HASHTAGS
        tags = random.sample(all_tags, min(4, len(all_tags)))

        likes_given = 0
        for tag in tags:
            if likes_given >= max_likes:
                break

            try:
                medias = await asyncio.to_thread(
                    cl.hashtag_medias_recent, tag, amount=15
                )
            except Exception as e:
                logger.warning("IG hashtag search failed for #%s: %s", tag, e)
                continue

            for media in medias:
                if likes_given >= max_likes:
                    break

                media_id = str(media.pk)
                if media_id in self._liked_media_ids:
                    continue

                if not daily_tracker.can_perform("instagram", "likes"):
                    break

                try:
                    await asyncio.to_thread(cl.media_like, media.pk)
                    self._liked_media_ids.add(media_id)
                    daily_tracker.record_action("instagram", "likes")
                    likes_given += 1

                    # Human-like delay (30-90 sec with lognormal)
                    await asyncio.sleep(lognormal_delay(30, 90))

                except Exception as e:
                    logger.warning("IG like failed: %s", e)
                    # Might be rate limited, back off
                    await asyncio.sleep(lognormal_delay(60, 180))
                    continue

        logger.info("IG hashtag like cycle: liked %d posts", likes_given)

    async def run_follow_cycle(self, max_follows: int = 5):
        """Follow users from competitor audiences.

        Follow users who follow similar accounts. Unfollow after 5-7 days
        if they didn't follow back.
        """
        if cost_tracker.is_budget_exceeded():
            logger.info("Budget exceeded, skipping IG cycle")
            return
        if not daily_tracker.can_perform("instagram", "follows"):
            logger.info("IG follow limit reached, skipping")
            return

        if not COMPETITOR_ACCOUNTS:
            logger.debug("No competitor accounts configured, skipping follow cycle")
            return

        await self.platform._ensure_login()
        cl = self.platform.client

        tracking = _load_follow_tracking()
        follows_done = 0
        target_account = random.choice(COMPETITOR_ACCOUNTS)

        try:
            # Get competitor's user ID
            user_info = await asyncio.to_thread(
                cl.user_info_by_username, target_account
            )
            # Get their followers
            followers = await asyncio.to_thread(
                cl.user_followers, user_info.pk, amount=30
            )
        except Exception as e:
            logger.warning("Failed to get followers of @%s: %s", target_account, e)
            return

        for user_id, user_info in followers.items():
            if follows_done >= max_follows:
                break

            uid = str(user_id)
            if uid in tracking["followed"] or uid in tracking["unfollowed"]:
                continue

            if not daily_tracker.can_perform("instagram", "follows"):
                break

            try:
                await asyncio.to_thread(cl.user_follow, user_id)
                tracking["followed"][uid] = datetime.now(timezone.utc).isoformat()
                daily_tracker.record_action("instagram", "follows")
                follows_done += 1

                # Delay between follows (60-180 sec)
                await asyncio.sleep(lognormal_delay(60, 180))

            except Exception as e:
                logger.warning("IG follow failed for %s: %s", uid, e)
                await asyncio.sleep(lognormal_delay(120, 300))
                continue

        _save_follow_tracking(tracking)
        logger.info("IG follow cycle: followed %d users from @%s", follows_done, target_account)

    async def run_unfollow_cycle(self, max_unfollows: int = 10):
        """Unfollow users who didn't follow back after 5+ days."""
        await self.platform._ensure_login()
        cl = self.platform.client

        tracking = _load_follow_tracking()
        cutoff = datetime.now(timezone.utc) - timedelta(days=5)
        unfollowed = 0

        for uid, followed_at_str in list(tracking["followed"].items()):
            if unfollowed >= max_unfollows:
                break

            try:
                followed_at = datetime.fromisoformat(followed_at_str)
            except (ValueError, TypeError):
                continue

            if followed_at > cutoff:
                continue  # Too recent

            try:
                # Check if they follow us back
                user_id = int(uid)
                friendship = await asyncio.to_thread(
                    cl.user_friendship_v1, user_id
                )

                if not friendship.followed_by:
                    # They didn't follow back, unfollow
                    await asyncio.to_thread(cl.user_unfollow, user_id)
                    unfollowed += 1
                    logger.info("Unfollowed %s (no follow-back after 5 days)", uid)

                # Either way, remove from tracking
                del tracking["followed"][uid]
                tracking["unfollowed"].append(uid)

                await asyncio.sleep(lognormal_delay(60, 120))

            except Exception as e:
                logger.warning("Unfollow check failed for %s: %s", uid, e)
                continue

        # Keep unfollowed list manageable
        tracking["unfollowed"] = tracking["unfollowed"][-500:]

        _save_follow_tracking(tracking)
        logger.info("IG unfollow cycle: unfollowed %d users", unfollowed)
