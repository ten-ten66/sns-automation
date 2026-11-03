"""Research agent: wraps TrendResearcher + reads Competitor / TrendTopic tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from src.agents.base import AgentBase, AgentResult
from src.core.database import async_session
from src.core.models import Competitor, TrendTopic


class ResearchAgent(AgentBase):
    """Supported tasks:

    - ``trends(platform)`` — platform in {x, google, youtube}
    - ``google_trends(geo="US")``
    - ``youtube_trends()``
    - ``niche_research(niche)`` — Claude-synthesized niche report
    - ``competitor_analysis(username, platform)`` — Claude analysis of one account
    - ``competitors(limit=10)`` — top competitors from DB
    - ``status`` — 24h trend discovery count, competitor total
    """

    name = "research"

    def __init__(self) -> None:
        super().__init__()
        self._researcher = None

    def _get_researcher(self):
        if self._researcher is None:
            from src.ai.trend_researcher import TrendResearcher
            self._researcher = TrendResearcher()
        return self._researcher

    async def handle(self, task: str, **kwargs: Any) -> AgentResult:
        if task == "trends":
            platform = kwargs.get("platform", "x")
            r = self._get_researcher()
            if platform == "x":
                trends = await r.fetch_x_trends()
            elif platform == "google":
                trends = await r.fetch_google_trends(geo=kwargs.get("geo", "US"))
            elif platform == "youtube":
                trends = await r.fetch_youtube_trending()
            else:
                return AgentResult(
                    agent=self.name, task=task, ok=False,
                    error=f"unknown platform: {platform}",
                )
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "platform": platform,
                    "count": len(trends),
                    "trends": trends[:20],
                },
            )

        if task == "google_trends":
            r = self._get_researcher()
            geo = kwargs.get("geo", "US")
            trends = await r.fetch_google_trends(geo=geo)
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={"geo": geo, "count": len(trends), "trends": trends},
            )

        if task == "youtube_trends":
            r = self._get_researcher()
            trends = await r.fetch_youtube_trending()
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={"count": len(trends), "trends": trends},
            )

        if task == "niche_research":
            niche = kwargs.get("niche", "AI fortune telling tarot horoscope")
            r = self._get_researcher()
            analysis = await r.research_niche_trends(niche)
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={"niche": niche, "analysis": analysis},
            )

        if task == "competitor_analysis":
            username = kwargs.get("username", "")
            platform = kwargs.get("platform", "x")
            if not username:
                return AgentResult(
                    agent=self.name, task=task, ok=False,
                    error="username is required",
                )
            r = self._get_researcher()
            analysis = await r.analyze_competitor(username=username, platform=platform)
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={"username": username, "platform": platform, "analysis": analysis},
            )

        if task == "competitors":
            limit = max(1, min(100, int(kwargs.get("limit", 10))))
            async with async_session() as session:
                rows = (
                    await session.execute(
                        select(Competitor)
                        .order_by(Competitor.follower_count.desc())
                        .limit(limit)
                    )
                ).scalars().all()
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "count": len(rows),
                    "competitors": [
                        {
                            "id": c.id,
                            "username": c.username,
                            "platform": c.platform,
                            "follower_count": c.follower_count,
                            "avg_engagement_rate": c.avg_engagement_rate,
                            "last_analyzed_at": (
                                c.last_analyzed_at.isoformat()
                                if c.last_analyzed_at
                                else None
                            ),
                        }
                        for c in rows
                    ],
                },
            )

        if task == "status":
            since = datetime.now(timezone.utc) - timedelta(days=1)
            async with async_session() as session:
                trends_24h = (
                    await session.execute(
                        select(func.count(TrendTopic.id)).where(
                            TrendTopic.discovered_at >= since
                        )
                    )
                ).scalar_one()
                competitor_total = (
                    await session.execute(select(func.count(Competitor.id)))
                ).scalar_one()
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "trends_24h": int(trends_24h or 0),
                    "competitors_total": int(competitor_total or 0),
                    "ready": True,
                },
            )

        return await super().handle(task, **kwargs)
