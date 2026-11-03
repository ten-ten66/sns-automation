"""TikTok follower growth automation.

Tactics:
1. Trending hashtag engagement - Like videos from beauty/cosmetics hashtags
2. Comment engagement cycle - Comment on viral beauty/body care TikToks
3. Duet/Stitch opportunity finder - Find high-engagement videos to duet/stitch

Note: TikTok's Content Posting API has limited search/discovery capabilities.
      Hashtag-based video search and liking are not available via the official
      API. Methods log appropriately and are structured to integrate when API
      features expand.
"""

import logging
import random

from src.platforms.tiktok_platform import TikTokPlatform
from src.ai.claude_generator import ClaudeContentGenerator
from src.core.safety import daily_tracker
from src.core.utils import is_active_hours

logger = logging.getLogger(__name__)

# Beauty/cosmetics hashtags (main discovery pool)
BEAUTY_HASHTAGS = [
    "コスメ", "メイク", "スキンケア", "GRWM", "美容", "ビューティー",
    "プチプラコスメ", "韓国コスメ", "今日のメイク", "コスメレビュー",
]

# Body care / routine context
BODY_CARE_HASHTAGS = [
    "ボディケア", "ナイトルーティン", "ボディクリーム",
    "ナイトケア", "保湿ケア", "ヘアケア",
]


class TikTokGrowthEngine:
    """Automated growth tactics for TikTok."""

    def __init__(self, platform: TikTokPlatform):
        self.platform = platform
        self.content_generator = ClaudeContentGenerator()
        self._liked_video_ids: set[str] = set()
        self._commented_video_ids: set[str] = set()
        self._duet_candidate_ids: set[str] = set()

    async def run_hashtag_like_cycle(self, max_likes: int = 30):
        """Like recent videos from target beauty/cosmetics hashtags.

        Users get notified, visit profile, follow if content resonates.
        Expected: 2-5% visit rate, 15-25% follow rate of visitors.

        Note: TikTok Content Posting API does not expose hashtag video search.
              This method logs the limitation and is ready to integrate when
              the API adds this capability.
        """
        if not is_active_hours():
            logger.debug("Outside active hours (07:00-23:00 JST), skipping hashtag like cycle")
            return

        if not daily_tracker.can_perform("tiktok", "likes"):
            logger.info("TikTok like daily limit reached, skipping")
            return

        # Mix beauty + body care hashtags
        all_tags = BEAUTY_HASHTAGS + BODY_CARE_HASHTAGS
        tags = random.sample(all_tags, min(4, len(all_tags)))

        likes_given = 0
        for tag in tags:
            if likes_given >= max_likes:
                break

            logger.info(
                "TikTok hashtag video search (#%s): not available via Content Posting API. "
                "Skipping like action — will integrate when API expands.",
                tag,
            )
            # Placeholder: when TikTok API exposes hashtag search, replace with:
            #   videos = await self.platform.search_hashtag_videos(tag, amount=15)
            #   for video in videos: ...
            continue

        logger.info("TikTok hashtag like cycle: liked %d videos", likes_given)

    async def run_comment_engagement_cycle(self, max_comments: int = 10):
        """Comment on viral beauty/body care TikToks with persona-consistent replies.

        Commenting on trending videos drives profile visits and follows.
        Comments should be natural, persona-consistent, and beauty-relevant.

        Note: TikTok Content Posting API does not expose video search or
              comment posting on other users' videos. This method logs the
              limitation and is ready to integrate when the API expands.
        """
        if not is_active_hours():
            logger.debug("Outside active hours (07:00-23:00 JST), skipping comment cycle")
            return

        if not daily_tracker.can_perform("tiktok", "comments"):
            logger.info("TikTok comment daily limit reached, skipping")
            return

        all_tags = BEAUTY_HASHTAGS + BODY_CARE_HASHTAGS
        tags = random.sample(all_tags, min(3, len(all_tags)))

        comments_sent = 0
        for tag in tags:
            if comments_sent >= max_comments:
                break

            logger.info(
                "TikTok viral video search (#%s): not available via Content Posting API. "
                "Skipping comment action — will integrate when API expands.",
                tag,
            )
            # Placeholder: when TikTok API exposes video discovery, replace with:
            #   viral_videos = await self.platform.search_viral_videos(tag, min_likes=10000)
            #   for video in viral_videos[:5]:
            #       video_id = video.get("id", "")
            #       video_desc = video.get("desc", "")
            #       if not video_id or video_id in self._commented_video_ids:
            #           continue
            #       if not daily_tracker.can_perform("tiktok", "comments"):
            #           break
            #       reply_text = self.content_generator.generate_comment_reply(
            #           comment=video_desc or f"#{tag}の動画",
            #           commenter_gender="unknown",
            #       )
            #       if len(reply_text) > 150:
            #           reply_text = reply_text[:147] + "..."
            #       result = await self.platform.post_comment(video_id, reply_text)
            #       if result.success:
            #           self._commented_video_ids.add(video_id)
            #           daily_tracker.record_action("tiktok", "comments")
            #           comments_sent += 1
            #           logger.info("Commented on TikTok video %s: %s", video_id, reply_text[:50])
            #       await asyncio.sleep(_lognormal_delay(180, 480))
            continue

        logger.info("TikTok comment engagement cycle: commented on %d videos", comments_sent)

    async def run_duet_stitch_finder(self, max_candidates: int = 5):
        """Find high-engagement videos suitable for duet or stitch collaboration.

        Duets/stitches on popular videos expose our content to large existing
        audiences. Targets videos with high engagement in beauty/body care niche.

        Note: TikTok Content Posting API does not expose video search for
              discovery. This method logs the limitation and queues candidates
              when the API expands to support it.
        """
        if not is_active_hours():
            logger.debug("Outside active hours (07:00-23:00 JST), skipping duet/stitch finder")
            return

        all_tags = BEAUTY_HASHTAGS + BODY_CARE_HASHTAGS
        tags = random.sample(all_tags, min(3, len(all_tags)))

        candidates_found = 0
        for tag in tags:
            if candidates_found >= max_candidates:
                break

            logger.info(
                "TikTok high-engagement video search (#%s): not available via Content Posting API. "
                "Skipping duet/stitch discovery — will integrate when API expands.",
                tag,
            )
            # Placeholder: when TikTok API exposes video search, replace with:
            #   videos = await self.platform.search_hashtag_videos(
            #       tag, amount=20, sort_by="engagement"
            #   )
            #   for video in videos:
            #       video_id = video.get("id", "")
            #       like_count = video.get("like_count", 0)
            #       share_count = video.get("share_count", 0)
            #       duet_enabled = video.get("duet_enabled", False)
            #       stitch_enabled = video.get("stitch_enabled", False)
            #       if not video_id or video_id in self._duet_candidate_ids:
            #           continue
            #       if like_count < 50000:
            #           continue
            #       if not (duet_enabled or stitch_enabled):
            #           continue
            #       self._duet_candidate_ids.add(video_id)
            #       candidates_found += 1
            #       collab_type = "duet+stitch" if (duet_enabled and stitch_enabled) else (
            #           "duet" if duet_enabled else "stitch"
            #       )
            #       logger.info(
            #           "Duet/stitch candidate: video=%s likes=%d type=%s #%s",
            #           video_id, like_count, collab_type, tag,
            #       )
            #       if candidates_found >= max_candidates:
            #           break
            continue

        logger.info(
            "TikTok duet/stitch finder: queued %d candidates (API expansion pending)",
            candidates_found,
        )

    def get_duet_stitch_candidates(self) -> list[str]:
        """Return the current list of duet/stitch candidate video IDs."""
        return list(self._duet_candidate_ids)
