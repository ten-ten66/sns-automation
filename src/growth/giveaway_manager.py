"""Automated giveaway/present campaign management for follower growth."""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from src.ai.claude_generator import ClaudeContentGenerator, GeneratedContent

logger = logging.getLogger(__name__)


class GiveawayManager:
    """Create and manage giveaway campaigns to accelerate follower growth."""

    def __init__(self):
        self.content_generator = ClaudeContentGenerator()

    async def generate_giveaway_post(
        self,
        platform: str,
        prize: str,
        conditions: list[str] | None = None,
        duration_days: int = 7,
    ) -> GeneratedContent:
        """Generate a giveaway announcement post."""
        if conditions is None:
            conditions = ["Follow this account", "Repost this post"]

        end_date = datetime.now(timezone.utc) + timedelta(days=duration_days)
        end_str = end_date.strftime("%m/%d")

        conditions_text = "\n".join(f"  {c}" for c in conditions)

        prompt = f"""Create a giveaway announcement post with the following details.

Platform: {platform}
Prize: {prize}
Entry conditions:
{conditions_text}
Deadline: {end_str}

Persona: Miku, a 28-year-old beauty enthusiast. Friendly big-sister vibe, uses emoji naturally.

Write an exciting post that makes followers want to participate and highlights the prize.
List entry conditions clearly as bullet points."""

        content = await asyncio.to_thread(
            self.content_generator.generate_post,
            platform=platform,
            topic=f"Giveaway: {prize}",
            tone="exciting",
            target_audience="all followers",
            trending_context=prompt,
        )
        return content

    def generate_winner_announcement(
        self,
        platform: str,
        winners: list[str],
        prize: str,
    ) -> str:
        """Generate a winner announcement post."""
        winner_mentions = " ".join(f"@{w}" for w in winners)

        templates = [
            f"GIVEAWAY RESULTS!\n\nThe winner of the {prize} is...\n\n{winner_mentions}\n\nCongrats! I'll DM you soon!\n\nThank you everyone who entered! More giveaways coming so stay tuned!",
            f"WINNER ANNOUNCEMENT\n\nThank you SO much for all the entries for the {prize} giveaway!\n\nWinner: {winner_mentions}\n\nI'll DM you! Stay following for the next giveaway!",
        ]
        return random.choice(templates)


giveaway_manager = GiveawayManager()
