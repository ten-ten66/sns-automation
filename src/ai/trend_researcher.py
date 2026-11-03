"""Trend and viral post research module."""

import asyncio
import json

import anthropic
import feedparser
import httpx

from config.settings import settings
from src.core.utils import parse_llm_json, extract_claude_text, retry
from src.core.cost_tracker import cost_tracker, BudgetExceededError


class TrendResearcher:
    """Research trending topics and viral posts across platforms."""

    def __init__(self):
        self.claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.http_client = httpx.AsyncClient(
            timeout=30,
            headers={"User-Agent": "SNS-Automation-Bot/1.0"},
        )

    @retry(max_retries=3, base_delay=2.0, max_delay=60.0)
    def _call_claude_json(self, prompt: str, max_tokens: int = 4000) -> dict | list:
        """Call Claude with retry and cost tracking."""
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded.")
        response = self.claude.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage(
                "trend_researcher", response.usage.input_tokens, response.usage.output_tokens,
            )
        return parse_llm_json(extract_claude_text(response))

    async def fetch_x_trends(self, bearer_token: str | None = None) -> list[dict]:
        """Fetch trending topics from X (Twitter) API v2."""
        token = bearer_token or settings.x_bearer_token
        if not token:
            return []

        url = "https://api.twitter.com/2/trends/by/woeid/23424977"  # US WOEID
        headers = {"Authorization": f"Bearer {token}"}

        response = await self.http_client.get(url, headers=headers)
        if response.status_code != 200:
            # Fallback: search for high-engagement recent posts
            return await self._search_x_viral(token)

        data = response.json()
        trends = []
        for trend in data.get("data", []):
            trends.append({
                "topic": trend.get("trend_name", ""),
                "volume": trend.get("tweet_count", 0),
                "platform": "x",
                "url": trend.get("url", ""),
            })
        return trends

    async def _search_x_viral(self, bearer_token: str) -> list[dict]:
        """Search for viral posts on X."""
        url = "https://api.twitter.com/2/tweets/search/recent"
        headers = {"Authorization": f"Bearer {bearer_token}"}
        params = {
            "query": "lang:en min_retweets:1000",
            "max_results": 50,
            "tweet.fields": "public_metrics,created_at,entities",
            "sort_order": "relevancy",
        }

        response = await self.http_client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            return []

        data = response.json()
        viral_posts = []
        for tweet in data.get("data", []):
            metrics = tweet.get("public_metrics", {})
            viral_posts.append({
                "platform": "x",
                "content": tweet.get("text", ""),
                "likes": metrics.get("like_count", 0),
                "shares": metrics.get("retweet_count", 0),
                "comments": metrics.get("reply_count", 0),
                "platform_post_id": tweet.get("id", ""),
            })
        return viral_posts

    async def fetch_google_trends(self, geo: str = "US") -> list[dict]:
        """Fetch trending topics from Google Trends RSS."""
        url = f"https://trends.google.com/trending/rss?geo={geo}"
        response = await self.http_client.get(url)
        if response.status_code != 200:
            return []

        feed = await asyncio.to_thread(feedparser.parse, response.text)
        trends = []
        for entry in feed.entries[:20]:
            trends.append({
                "topic": entry.get("title", ""),
                "traffic": entry.get("ht_approx_traffic", ""),
                "source": "google_trends",
                "link": entry.get("link", ""),
                "published": entry.get("published", ""),
            })
        return trends

    async def fetch_youtube_trending(self) -> list[dict]:
        """Fetch trending videos from YouTube."""
        if not settings.youtube_api_key:
            return []

        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {
            "part": "snippet,statistics",
            "chart": "mostPopular",
            "regionCode": "US",
            "maxResults": 20,
            "key": settings.youtube_api_key,
        }

        response = await self.http_client.get(url, params=params)
        if response.status_code != 200:
            return []

        data = response.json()
        videos = []
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            videos.append({
                "platform": "youtube",
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "tags": snippet.get("tags", []),
                "category": snippet.get("categoryId", ""),
            })
        return videos

    async def research_niche_trends(self, niche: str, platforms: list[str] | None = None) -> dict:
        """Use Claude to research and synthesize trends for a specific niche."""
        platforms = platforms or ["x", "instagram", "tiktok", "youtube"]

        # Gather raw trend data in parallel
        raw_trends = {}
        tasks = {}
        if "x" in platforms:
            tasks["x"] = self.fetch_x_trends()
        tasks["google"] = self.fetch_google_trends()
        if "youtube" in platforms:
            tasks["youtube"] = self.fetch_youtube_trending()

        if tasks:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, result in zip(tasks.keys(), results):
                raw_trends[name] = result if not isinstance(result, Exception) else []

        prompt = f"""Analyze the following trend data and identify content opportunities related to "{niche}".

## Current Trend Data
{json.dumps(raw_trends, ensure_ascii=False, default=str)[:3000]}

## Analysis Output (JSON)
{{
    "hot_topics": [
        {{
            "topic": "topic name",
            "relevance": 0.0-1.0,
            "platforms": ["best platforms for this topic"],
            "content_angle": "how to approach this topic",
            "urgency": "high/medium/low (should this be posted immediately?)"
        }}
    ],
    "emerging_trends": ["themes likely to grow soon"],
    "content_gaps": ["content opportunities with low competition"],
    "recommended_hashtags": ["recommended hashtags"],
    "optimal_posting_times": ["best posting time windows"],
    "weekly_content_themes": {{
        "monday": "theme",
        "tuesday": "theme",
        ...
    }}
}}

Output JSON only."""

        return await asyncio.to_thread(self._call_claude_json, prompt, 3000)

    async def analyze_competitor(self, username: str, platform: str) -> dict:
        """Analyze a competitor's content strategy using Claude."""
        # In production, fetch actual posts via platform API
        prompt = f"""Perform an SNS competitor analysis.

## Target
- Platform: {platform}
- Account: {username}

## Analysis Output (JSON)
{{
    "posting_patterns": {{
        "frequency": "posting frequency",
        "best_times": ["best posting times"],
        "content_mix": {{"text": 0.3, "image": 0.4, "video": 0.3}}
    }},
    "content_strategy": {{
        "main_themes": ["primary content themes"],
        "tone": "tone of voice",
        "cta_patterns": ["call-to-action patterns"],
        "hook_patterns": ["hook patterns used"]
    }},
    "engagement_analysis": {{
        "best_performing_types": ["high engagement post types"],
        "worst_performing_types": ["low engagement post types"],
        "engagement_drivers": ["factors driving engagement"]
    }},
    "opportunities": ["differentiation opportunities"],
    "threats": ["areas to watch out for"],
    "actionable_insights": ["immediately actionable steps"]
}}

Output JSON only."""

        return await asyncio.to_thread(self._call_claude_json, prompt, 2000)

    async def close(self):
        await self.http_client.aclose()
