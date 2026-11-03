"""YouTube subscriber growth automation.

Tactics:
1. Comment engagement on trending beauty/cosmetics videos
2. Comment reply cycle on own videos
3. Community post engagement (like/comment on similar channels)
"""

import asyncio
import logging
import random

from src.platforms.youtube_platform import YouTubePlatform
from src.ai.claude_generator import ClaudeContentGenerator
from src.core.safety import daily_tracker
from src.core.cost_tracker import cost_tracker
from src.core.utils import is_active_hours, lognormal_delay, BoundedSet

logger = logging.getLogger(__name__)

# Search queries for finding trending beauty/cosmetics videos
BEAUTY_QUERIES = [
    "skincare routine",
    "beauty review",
    "J-beauty",
    "glass skin tutorial",
    "makeup tutorial",
    "K-beauty",
    "best moisturizer",
    "morning skincare",
    "drugstore skincare",
    "foundation review",
]

# Own channel video IDs to reply to comments on (populated at runtime or via config)
OWN_VIDEO_IDS: list[str] = []

# Similar/competitor channel IDs for community post engagement
SIMILAR_CHANNEL_IDS: list[str] = []


class YouTubeGrowthEngine:
    """Automated growth tactics for YouTube."""

    def __init__(self, platform: YouTubePlatform):
        self.platform = platform
        self.content_generator = ClaudeContentGenerator()
        # Bounded FIFO sets — growth engine runs indefinitely, so unbounded
        # growth would leak memory over days/weeks of uptime.
        self._commented_video_ids = BoundedSet(capacity=2000)
        self._replied_comment_ids = BoundedSet(capacity=5000)
        self._liked_post_ids = BoundedSet(capacity=2000)

    async def run_trending_comment_cycle(self, max_comments: int = 5):
        """Find trending beauty/cosmetics videos and leave persona-consistent comments.

        Commenting on high-view videos exposes the channel name to a large audience,
        drawing curious viewers back to the channel.
        """
        if not is_active_hours():
            logger.debug("Outside active hours, skipping trending comment cycle")
            return

        if cost_tracker.is_budget_exceeded():
            logger.info("Budget exceeded, skipping YouTube trending comment cycle")
            return

        if not daily_tracker.can_perform("youtube", "replies"):
            logger.info("YouTube reply daily limit reached, skipping trending comment cycle")
            return

        if not self.platform.youtube:
            logger.warning("YouTube API client not initialised (missing api_key), skipping")
            return

        # search.list costs 100 quota units per call (daily quota: 10,000 units)
        # Limit to 2 queries per cycle to conserve quota
        queries = random.sample(BEAUTY_QUERIES, min(2, len(BEAUTY_QUERIES)))
        comments_posted = 0

        for query in queries:
            if comments_posted >= max_comments:
                break

            try:
                search_response = await asyncio.to_thread(
                    lambda q=query: self.platform.youtube.search().list(
                        q=q,
                        type="video",
                        order="viewCount",
                        part="snippet",
                        maxResults=10,
                        relevanceLanguage="en",
                        regionCode="US",
                    ).execute()
                )
            except Exception as e:
                logger.warning("YouTube trending search failed for '%s': %s", query, e)
                continue

            items = search_response.get("items", [])
            for item in items:
                if comments_posted >= max_comments:
                    break

                video_id = item.get("id", {}).get("videoId", "")
                if not video_id or video_id in self._commented_video_ids:
                    continue

                if not daily_tracker.can_perform("youtube", "replies"):
                    break

                video_title = item.get("snippet", {}).get("title", "")
                channel_title = item.get("snippet", {}).get("channelTitle", "")

                try:
                    # Generate a persona-consistent comment about the video topic
                    comment_prompt = (
                        f"Write a comment for the YouTube beauty video '{video_title}' "
                        f"by {channel_title}. Share your thoughts on the content."
                    )
                    comment_text = self.content_generator.generate_comment_reply(
                        comment=comment_prompt,
                    )

                    # Keep comment concise
                    if len(comment_text) > 200:
                        comment_text = comment_text[:197] + "..."

                    # Post comment via OAuth-authenticated service (commentThreads.insert)
                    def _post_comment(vid_id: str, text: str):
                        service = self.platform._get_authenticated_service()
                        body = {
                            "snippet": {
                                "videoId": vid_id,
                                "topLevelComment": {
                                    "snippet": {
                                        "textOriginal": text,
                                    }
                                },
                            }
                        }
                        return service.commentThreads().insert(
                            part="snippet", body=body
                        ).execute()

                    await asyncio.to_thread(_post_comment, video_id, comment_text)

                    self._commented_video_ids.add(video_id)
                    daily_tracker.record_action("youtube", "replies")
                    comments_posted += 1
                    logger.info(
                        "Commented on trending video %s: %s",
                        video_id, comment_text[:60],
                    )

                    # Human-like delay between comments (5-12 min)
                    await asyncio.sleep(lognormal_delay(300, 720))

                except RuntimeError as e:
                    logger.warning("YouTube comment requires OAuth: %s", e)
                    break
                except Exception as e:
                    logger.warning("Failed to comment on video %s: %s", video_id, e)
                    continue

        logger.info("Trending comment cycle: posted %d comments", comments_posted)

    async def run_own_video_reply_cycle(
        self,
        video_ids: list[str] | None = None,
        max_replies: int = 10,
    ):
        """Reply to comments on own videos to boost engagement signals.

        YouTube's algorithm favours videos where the creator participates in
        the comments section. Replies also convert commenters into subscribers.
        """
        if not is_active_hours():
            logger.debug("Outside active hours, skipping own video reply cycle")
            return

        if cost_tracker.is_budget_exceeded():
            logger.info("Budget exceeded, skipping YouTube own video reply cycle")
            return

        if not daily_tracker.can_perform("youtube", "replies"):
            logger.info("YouTube reply daily limit reached, skipping own video reply cycle")
            return

        target_video_ids = video_ids or OWN_VIDEO_IDS
        if not target_video_ids:
            logger.debug("No own video IDs configured for reply cycle")
            return

        replies_sent = 0
        for video_id in target_video_ids:
            if replies_sent >= max_replies:
                break

            # Fetch recent comments on this video
            try:
                comments = await self.platform.get_comments(video_id, max_results=20)
            except Exception as e:
                logger.warning("Failed to fetch comments for video %s: %s", video_id, e)
                continue

            for comment in comments:
                if replies_sent >= max_replies:
                    break

                comment_id = comment.get("id", "")
                comment_text = comment.get("text", "")
                commenter = comment.get("username", "")

                if not comment_id or comment_id in self._replied_comment_ids:
                    continue

                if not daily_tracker.can_perform("youtube", "replies"):
                    break

                try:
                    reply_text = self.content_generator.generate_comment_reply(
                        comment=comment_text,
                    )

                    if len(reply_text) > 500:
                        reply_text = reply_text[:497] + "..."

                    result = await self.platform.post_reply(
                        post_id=video_id,
                        comment_id=comment_id,
                        text=reply_text,
                    )

                    if result.success:
                        self._replied_comment_ids.add(comment_id)
                        daily_tracker.record_action("youtube", "replies")
                        replies_sent += 1
                        logger.info(
                            "Replied to comment %s on video %s from %s: %s",
                            comment_id, video_id, commenter, reply_text[:50],
                        )

                    # Human-like delay between replies (2-6 min)
                    await asyncio.sleep(lognormal_delay(120, 360))

                except Exception as e:
                    logger.warning(
                        "Failed to reply to comment %s: %s", comment_id, e
                    )
                    continue

        logger.info("Own video reply cycle: sent %d replies", replies_sent)

    async def run_community_post_engagement_cycle(
        self,
        channel_ids: list[str] | None = None,
        max_engagements: int = 10,
    ):
        """Like and comment on community posts from similar channels.

        Engaging with community posts from comparable channels surfaces the
        channel name to an already beauty-interested audience.
        """
        # YouTube Data API v3 deprecated bulletin activities.
        # Community posts are not accessible via the public API.
        # This method is a no-op until an alternative API becomes available.
        logger.info(
            "Community post engagement skipped: YouTube Data API v3 "
            "deprecated bulletin activities (no public API for community posts)"
        )
        return

        # --- Dead code below: kept for reference until an alternative API exists ---
        # if not is_active_hours():
        #     logger.debug("Outside active hours, skipping community post engagement cycle")
        #     return
        #
        # if cost_tracker.is_budget_exceeded():
        #     logger.info("Budget exceeded, skipping YouTube community post cycle")
        #     return
        #
        # if not daily_tracker.can_perform("youtube", "replies"):
        #     logger.info("YouTube reply daily limit reached, skipping community post cycle")
        #     return
        #
        # if not self.platform.youtube:
        #     logger.warning("YouTube API client not initialised, skipping community post cycle")
        #     return
        #
        # target_channels = channel_ids or SIMILAR_CHANNEL_IDS
        # if not target_channels:
        #     logger.debug("No similar channel IDs configured for community post engagement")
        #     return
        #
        # engagements_done = 0
        # channels = random.sample(target_channels, min(3, len(target_channels)))
        #
        # for channel_id in channels:
        #     if engagements_done >= max_engagements:
        #         break
        #
        #     # Fetch community posts (activities) for the channel
        #     try:
        #         activities_response = await asyncio.to_thread(
        #             lambda cid=channel_id: self.platform.youtube.activities().list(
        #                 part="snippet,contentDetails",
        #                 channelId=cid,
        #                 maxResults=10,
        #             ).execute()
        #         )
        #     except Exception as e:
        #         logger.warning(
        #             "Failed to fetch activities for channel %s: %s", channel_id, e
        #         )
        #         continue
        #
        #     items = activities_response.get("items", [])
        #     for item in items:
        #         if engagements_done >= max_engagements:
        #             break
        #
        #         activity_type = item.get("snippet", {}).get("type", "")
        #         # Only engage with community posts (bulletins)
        #         if activity_type != "bulletin":
        #             continue
        #
        #         # Extract the post text for context
        #         description = item.get("snippet", {}).get("description", "")
        #         resource = item.get("contentDetails", {}).get("bulletin", {}).get("resource", {})
        #         resource_id = resource.get("videoId") or resource.get("resourceId", {}).get("videoId", "")
        #
        #         post_key = f"{channel_id}:{item.get('id', '')}"
        #         if post_key in self._liked_post_ids:
        #             continue
        #
        #         if not daily_tracker.can_perform("youtube", "replies"):
        #             break
        #
        #         # If the community post links to a video, comment on that video
        #         if resource_id and resource_id not in self._commented_video_ids:
        #             try:
        #                 comment_prompt = (
        #                     f"YouTubeのコミュニティ投稿の動画に対してコメント: {description[:100]}"
        #                 )
        #                 comment_text = self.content_generator.generate_comment_reply(
        #                     comment=comment_prompt,
        #                     commenter_gender="unknown",
        #                 )
        #
        #                 if len(comment_text) > 200:
        #                     comment_text = comment_text[:197] + "..."
        #
        #                 def _post_community_comment(vid_id: str, text: str):
        #                     service = self.platform._get_authenticated_service()
        #                     body = {
        #                         "snippet": {
        #                             "videoId": vid_id,
        #                             "topLevelComment": {
        #                                 "snippet": {"textOriginal": text}
        #                             },
        #                         }
        #                     }
        #                     return service.commentThreads().insert(
        #                         part="snippet", body=body
        #                     ).execute()
        #
        #                 await asyncio.to_thread(
        #                     _post_community_comment, resource_id, comment_text
        #                 )
        #
        #                 self._commented_video_ids.add(resource_id)
        #                 self._liked_post_ids.add(post_key)
        #                 daily_tracker.record_action("youtube", "replies")
        #                 engagements_done += 1
        #                 logger.info(
        #                     "Commented on community post video %s from channel %s: %s",
        #                     resource_id, channel_id, comment_text[:60],
        #                 )
        #
        #                 # Human-like delay (4-10 min)
        #                 await asyncio.sleep(lognormal_delay(240, 600))
        #
        #             except RuntimeError as e:
        #                 logger.warning("Community post comment requires OAuth: %s", e)
        #                 break
        #             except Exception as e:
        #                 logger.warning(
        #                     "Failed to comment on community post video %s: %s",
        #                     resource_id, e,
        #                 )
        #                 continue
        #         else:
        #             # No linked video — just mark as seen to avoid re-processing
        #             self._liked_post_ids.add(post_key)
        #
        # logger.info(
        #     "Community post engagement cycle: %d engagements done", engagements_done
        # )
