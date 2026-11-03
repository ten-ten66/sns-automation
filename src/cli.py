"""CLI interface for the SNS automation system."""

import asyncio

import click
import uvicorn
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


@click.group()
def main():
    """SNS Automation System - AI-powered content creation and posting."""
    pass


@main.command()
@click.option("--host", default="0.0.0.0", help="Host to bind")
@click.option("--port", default=8000, help="Port to bind")
@click.option("--reload", is_flag=True, help="Auto-reload on changes")
def serve(host, port, reload):
    """Start the dashboard server."""
    console.print(Panel.fit(
        f"[bold green]SNS Automation Dashboard[/]\n"
        f"Starting at http://{host}:{port}",
        border_style="green",
    ))
    uvicorn.run("src.api.app:app", host=host, port=port, reload=reload)


@main.command()
@click.argument("platform")
@click.argument("topic")
@click.option("--tone", default="engaging", help="Content tone")
@click.option("--audience", default="", help="Target audience")
def generate(platform, topic, tone, audience):
    """Generate content for a platform."""
    from src.ai.claude_generator import ClaudeContentGenerator

    gen = ClaudeContentGenerator()
    console.print(f"[bold]Generating {platform} content about: {topic}[/]")

    content = gen.generate_post(
        platform=platform,
        topic=topic,
        tone=tone,
        target_audience=audience,
    )

    console.print(Panel(content.text, title="Generated Post", border_style="blue"))

    if content.hashtags:
        console.print(f"[cyan]Hashtags:[/] {' '.join('#' + t for t in content.hashtags)}")
    if content.keywords:
        console.print(f"[yellow]Keywords:[/] {', '.join(content.keywords)}")
    console.print(f"[green]Viral Score:[/] {content.viral_score_estimate:.0%}")

    if content.variations:
        console.print("\n[bold]Variations:[/]")
        for i, var in enumerate(content.variations, 1):
            console.print(f"  {i}. {var}")


@main.command()
@click.option("--niche", default="", help="Niche to research")
@click.option("--platforms", default="x,instagram,tiktok", help="Platforms (comma-separated)")
def trends(niche, platforms):
    """Research current trends."""
    from src.ai.trend_researcher import TrendResearcher

    async def _run():
        researcher = TrendResearcher()
        try:
            platform_list = [p.strip() for p in platforms.split(",")]
            console.print(f"[bold]Researching trends for: {niche or 'general'}[/]")

            result = await researcher.research_niche_trends(niche, platform_list)

            # Hot topics
            table = Table(title="Hot Topics")
            table.add_column("Topic", style="cyan")
            table.add_column("Relevance", justify="center")
            table.add_column("Platforms")
            table.add_column("Urgency", justify="center")

            for topic in result.get("hot_topics", []):
                table.add_row(
                    topic.get("topic", ""),
                    f"{topic.get('relevance', 0):.0%}",
                    ", ".join(topic.get("platforms", [])),
                    topic.get("urgency", ""),
                )
            console.print(table)

            # Content gaps
            if result.get("content_gaps"):
                console.print("\n[bold]Content Gaps:[/]")
                for gap in result["content_gaps"]:
                    console.print(f"  - {gap}")

            # Hashtags
            if result.get("recommended_hashtags"):
                console.print(f"\n[bold]Recommended Hashtags:[/] {' '.join('#' + t for t in result['recommended_hashtags'])}")
        finally:
            await researcher.close()

    asyncio.run(_run())


@main.command()
@click.argument("platform")
@click.argument("text")
@click.option("--keywords", default="", help="Target keywords (comma-separated)")
def seo(platform, text, keywords):
    """Optimize content for SEO."""
    from src.seo.optimizer import SEOOptimizer

    opt = SEOOptimizer()
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else None

    result = opt.optimize_post(text, platform, kw_list)

    console.print(Panel(result.optimized_text, title="Optimized Text", border_style="green"))
    console.print(f"[green]SEO Score:[/] {result.seo_score:.0%}")
    console.print(f"[cyan]Hashtags:[/] {' '.join('#' + t for t in result.hashtags)}")
    console.print(f"[yellow]Primary Keywords:[/] {', '.join(result.primary_keywords)}")

    if result.suggestions:
        console.print("\n[bold]Suggestions:[/]")
        for s in result.suggestions:
            console.print(f"  - {s}")


@main.command()
@click.argument("platform")
@click.argument("text")
def predict(platform, text):
    """Predict viral score for a post."""
    from src.analytics.analyzer import AnalyticsEngine

    engine = AnalyticsEngine()
    result = engine.predict_viral_score(text, platform)

    score = result.get("viral_score", 0)
    color = "green" if score > 0.7 else "yellow" if score > 0.4 else "red"
    console.print(f"\n[bold {color}]Viral Score: {score:.0%}[/] (Confidence: {result.get('confidence', 0):.0%})")

    if result.get("strengths"):
        console.print("\n[green]Strengths:[/]")
        for s in result["strengths"]:
            console.print(f"  + {s}")

    if result.get("weaknesses"):
        console.print("\n[red]Weaknesses:[/]")
        for w in result["weaknesses"]:
            console.print(f"  - {w}")

    if result.get("improvement_suggestions"):
        console.print("\n[yellow]Improvements:[/]")
        for imp in result["improvement_suggestions"]:
            console.print(f"  [{imp.get('expected_impact', '')}] {imp.get('change', '')}")


@main.command()
@click.option("--days", default=30, help="Period in days")
def analytics(days):
    """View analytics summary."""
    from src.analytics.analyzer import AnalyticsEngine

    async def _run():
        engine = AnalyticsEngine()
        summary = await engine.get_performance_summary(days)

        table = Table(title=f"Performance Summary ({days} days)")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right", style="green")

        table.add_row("Total Posts", str(summary["total_posts"]))
        table.add_row("Total Impressions", f"{summary['total_impressions']:,}")
        table.add_row("Total Likes", f"{summary['total_likes']:,}")
        table.add_row("Total Comments", f"{summary['total_comments']:,}")
        table.add_row("Total Shares", f"{summary['total_shares']:,}")
        table.add_row("Avg Engagement Rate", f"{summary['avg_engagement_rate']:.2%}")

        console.print(table)

        if summary.get("follower_growth"):
            console.print("\n[bold]Follower Growth:[/]")
            for platform, data in summary["follower_growth"].items():
                console.print(f"  {platform}: {data['start']} -> {data['end']} (+{data['growth']}, {data['growth_rate']}%)")

    asyncio.run(_run())


@main.command()
@click.option("--days", default=30, help="Period in days")
def revenue(days):
    """View revenue summary."""
    from src.monetization.revenue_tracker import RevenueTracker

    async def _run():
        tracker = RevenueTracker()
        summary = await tracker.get_revenue_summary(days)

        console.print(Panel(
            f"[bold green]Total Revenue: ¥{summary['total_revenue']:,.0f}[/]\n"
            f"Daily Average: ¥{summary['avg_daily_revenue']:,.0f}",
            title=f"Revenue ({days} days)",
            border_style="green",
        ))

        if summary.get("by_source"):
            table = Table(title="By Source")
            table.add_column("Source")
            table.add_column("Amount", justify="right")
            table.add_column("Clicks", justify="right")
            table.add_column("Conv Rate", justify="right")
            for source, data in summary["by_source"].items():
                table.add_row(source, f"¥{data['amount']:,.0f}", str(data["clicks"]), f"{data['conversion_rate']}%")
            console.print(table)

    asyncio.run(_run())


@main.command()
@click.argument("platform")
@click.argument("topic")
@click.option("--days", default=7, help="Calendar days")
def calendar(platform, topic, days):
    """Generate a content calendar."""
    from src.ai.claude_generator import ClaudeContentGenerator

    gen = ClaudeContentGenerator()
    console.print(f"[bold]Generating {days}-day calendar for {platform}...[/]")

    cal = gen.generate_content_calendar(days, platform, topic)

    for day_data in cal:
        day = day_data.get("day", "?")
        console.print(f"\n[bold cyan]Day {day}[/]")
        for post in day_data.get("posts", []):
            console.print(f"  [{post.get('time', '')}] {post.get('content_type', '')} - {post.get('topic', '')}")
            console.print(f"    Hook: {post.get('hook', '')}")
            if post.get("hashtags"):
                console.print(f"    Tags: {' '.join('#' + t for t in post['hashtags'])}")




@main.command()
@click.argument("product")
@click.argument("platform")
@click.option("--pr", is_flag=True, help="Mark as PR/sponsored post")
def review(product, platform, pr):
    """Generate a beauty product review post."""
    from src.ai.claude_generator import ClaudeContentGenerator

    gen = ClaudeContentGenerator()
    console.print(f"[bold]Generating review for: {product} ({platform})[/]")
    if pr:
        console.print("[yellow]PR post - will include disclosure[/]")

    content = gen.generate_beauty_review(product, platform, is_pr=pr)

    console.print(Panel(content.text, title="Product Review", border_style="magenta"))
    if content.hashtags:
        console.print(f"[cyan]Hashtags:[/] {' '.join('#' + t for t in content.hashtags)}")
    console.print(f"[green]Viral Score:[/] {content.viral_score_estimate:.0%}")
    if content.suggested_media_prompt:
        console.print(f"[yellow]Media Prompt:[/] {content.suggested_media_prompt}")


@main.command()
@click.argument("routine_type")
@click.argument("platform")
def routine(routine_type, platform):
    """Generate a beauty routine post (morning, night, bodycare, etc.)."""
    from src.ai.claude_generator import ClaudeContentGenerator

    gen = ClaudeContentGenerator()
    console.print(f"[bold]Generating {routine_type} routine for {platform}...[/]")

    content = gen.generate_routine_post(routine_type, platform)

    console.print(Panel(content.text, title=f"{routine_type} Routine", border_style="magenta"))
    if content.hashtags:
        console.print(f"[cyan]Hashtags:[/] {' '.join('#' + t for t in content.hashtags)}")
    console.print(f"[green]Viral Score:[/] {content.viral_score_estimate:.0%}")


@main.command()
def persona():
    """Display current persona configuration."""
    from config.persona import PERSONA, PLATFORM_STRATEGY

    console.print(Panel.fit(
        f"[bold]{PERSONA['age']}の{PERSONA['gender']}[/]\n"
        f"ジャンル: {PERSONA['niche']}\n"
        f"体型: {PERSONA['physical_features']['body_type']}\n"
        f"口調: {PERSONA['tone_of_voice']}",
        title="Persona",
        border_style="magenta",
    ))

    table = Table(title="Content Pillars")
    table.add_column("Pillar", style="cyan")
    table.add_column("Ratio", justify="center")
    table.add_column("Description")
    for p in PERSONA["content_pillars"]:
        table.add_row(p["pillar"], f"{p['ratio']:.0%}", p["description"])
    console.print(table)

    console.print("\n[bold]Target Audience:[/]")
    ta = PERSONA["target_audience"]
    for segment_key in ["primary", "secondary"]:
        seg = ta.get(segment_key, {})
        if seg:
            console.print(f"  [{seg.get('ratio', 0):.0%}] {seg.get('label', segment_key)}: {seg.get('age_range', '')} {seg.get('gender', '')}")
            interests = seg.get("interests", [])
            if interests:
                console.print(f"    Interests: {', '.join(interests)}")

    console.print("\n[bold]Monetization Targets:[/]")
    for m in PERSONA["monetization_targets"]:
        console.print(f"  [{m['priority']}] {m['source']} ({', '.join(m['networks'])})")

    console.print("\n[bold]Platform Strategies:[/]")
    for name, strategy in PLATFORM_STRATEGY.items():
        console.print(f"  [cyan]{name}:[/] {strategy['posting_frequency']}")
        for focus in strategy["content_focus"][:3]:
            console.print(f"    - {focus}")


if __name__ == "__main__":
    main()
