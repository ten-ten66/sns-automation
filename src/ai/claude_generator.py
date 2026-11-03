"""Claude-powered content generation for SNS posts."""

import json
import logging
from dataclasses import dataclass

import anthropic

from config.settings import settings
from config.persona import (
    PERSONA, PLATFORM_STRATEGY, CONTENT_TEMPLATES, NICHE_KEYWORDS,
)
from src.core.utils import parse_llm_json, extract_claude_text, to_generated_content, validate_compliance, retry
from src.core.cost_tracker import cost_tracker, BudgetExceededError

logger = logging.getLogger(__name__)


@dataclass
class GeneratedContent:
    text: str
    hashtags: list[str]
    keywords: list[str]
    suggested_media_prompt: str | None = None
    viral_score_estimate: float = 0.0
    variations: list[str] | None = None
    target_audience: str | None = None
    posting_time_suggestion: str | None = None
    monetization_hook: str | None = None


class ClaudeContentGenerator:
    """Generate SNS content using Claude API."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model
        self.persona = PERSONA
        self.platform_strategy = PLATFORM_STRATEGY
        self.templates = CONTENT_TEMPLATES
        self.niche_keywords = NICHE_KEYWORDS

    @retry(max_retries=3, base_delay=2.0, max_delay=60.0)
    def _call_claude(self, prompt: str, max_tokens: int = 4000) -> str:
        """Call Claude API with automatic retry, cost tracking, and budget check."""
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded. Skipping API call.")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=self._persona_context,
            messages=[{"role": "user", "content": prompt}],
        )
        # Track cost
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage(
                method="claude_generator",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
        return extract_claude_text(response)

    def _call_claude_json(self, prompt: str, max_tokens: int = 4000) -> dict | list:
        """Call Claude and parse JSON response with retry."""
        return parse_llm_json(self._call_claude(prompt, max_tokens))

    def _generate_content_with_compliance(
        self, prompt: str, max_tokens: int = 4000, is_pr: bool = False,
    ):
        """Generate content, apply compliance checks, return GeneratedContent."""
        data = self._call_claude_json(prompt, max_tokens)
        content = to_generated_content(data)
        # Apply FTC / FDA compliance validation
        fixed_text, warnings = validate_compliance(content.text, is_pr=is_pr)
        if warnings:
            logger.warning(f"Compliance fixes applied: {warnings}")
        content.text = fixed_text
        return content

    @property
    def _persona_context(self) -> str:
        """Build persona context string for prompts."""
        p = self.persona
        pillars = "\n".join(
            f"  - {c['pillar']} ({c['ratio']:.0%}): {c['description']}"
            for c in p["content_pillars"]
        )
        compliance = "\n".join(f"  - {c}" for c in p["compliance_notes"])
        banned = "\n".join(f"  - {b}" for b in p["banned_topics"])
        primary = p['target_audience'].get('primary', {})
        secondary = p['target_audience'].get('secondary', {})
        target_str = (
            f"Primary: {primary.get('age_range', '')} {primary.get('gender', '')}({primary.get('ratio', 0):.0%})"
            f" / Secondary: {secondary.get('age_range', '')} {secondary.get('gender', '')}({secondary.get('ratio', 0):.0%})"
        )
        return f"""## Your Persona
- {p['age']}, {p['gender']}, beauty & cosmetics influencer
- Tone of voice: {p['tone_of_voice']}
- Personality: {', '.join(p['personality_traits'])}

## Content Pillars
{pillars}

## Target Audience: {target_str}

## Compliance (strictly required)
{compliance}
- FTC/FDA: Do not make absolute or implied efficacy claims. Adding "results may vary" does not make illegal claims legal. Never use them.
- Banned expressions: "removes wrinkles", "eliminates pores", "cures acne", "whitens skin", "reverses aging"

## PR / Sponsored Post Rules
- For sponsored posts, the first line must include #ad or #sponsored in a clear, conspicuous manner (hashtags buried at the end are not FTC compliant)
- The sponsored nature must be immediately obvious to a general audience per FTC disclosure guidelines

## Banned Topics
{banned}"""

    def generate_post(
        self,
        platform: str,
        topic: str,
        tone: str = "engaging",
        target_audience: str = "",
        trending_context: str = "",
        seo_keywords: list[str] | None = None,
        include_cta: bool = True,
        max_length: int | None = None,
    ) -> GeneratedContent:
        """Generate an optimized SNS post."""
        platform_limits = {
            "x": 280,
            "instagram": 2200,
            "tiktok": 4000,
            "youtube": 5000,
        }
        char_limit = max_length or platform_limits.get(platform, 500)

        # Get platform-specific strategy
        strategy = self.platform_strategy.get(platform, {})
        strategy_str = ""
        if strategy:
            strategy_str = f"""
## Platform Strategy ({platform})
- Posting frequency: {strategy.get('posting_frequency', '')}
- Content focus: {', '.join(strategy.get('content_focus', []))}
- Hashtag strategy: {strategy.get('hashtag_strategy', '')}"""

        # Include niche keywords if no custom ones provided
        if not seo_keywords:
            seo_keywords = (
                self.niche_keywords.get("beauty_primary", [])[:3]
                + self.niche_keywords.get("beauty_secondary", [])[:2]
            )

        # Platform-specific hashtag count — fewer is algorithmically better
        # on X (over-tagging triggers spam signals). YouTube <5 ranks higher
        # than a wall of tags. Historical logs showed 10-15 tags per post
        # which correlated with <1% engagement; narrowed 2026-04-20.
        hashtag_rules = {
            "x": "EXACTLY 2-3 hashtags, no more. X algorithm penalizes over-tagging.",
            "youtube": "EXACTLY 3-5 hashtags, specific niche keywords > generic ones.",
            "instagram": "15-25 hashtags, mix of 5 viral + 10 niche + 5 small-community.",
            "tiktok": "3-5 hashtags, one trending + rest niche.",
        }
        hashtag_rule = hashtag_rules.get(platform, "3-5 relevant hashtags.")

        prompt = f"""You are a beauty/skincare SNS influencer creating a post.

{strategy_str}

## Post Requirements
- Topic: {topic}
- Tone: {tone}
- Target audience: {target_audience or 'beauty enthusiasts'}
- Character limit: {char_limit}
- Language: {settings.content_language}
{"- Trending context: " + trending_context if trending_context else ""}
{"- SEO keywords: " + ", ".join(seo_keywords) if seo_keywords else ""}
{"- Include a CTA" if include_cta else ""}

## Hook Rules (critical for watch-time / scroll-stop)
- The first 5 words MUST be a scroll-stopper: a question, bold claim, or
  surprising fact. Do NOT open with "Okay so..." or "So I've been..." — they
  under-perform and the algorithm tags them as low-retention.
- Avoid filler openers like "guys", "literally", "honestly" in the first
  sentence.
- Lead with a VALUE promise or a CURIOSITY GAP.

## Hashtag Rules ({platform})
- {hashtag_rule}
- Output hashtags WITHOUT the `#` prefix (the poster adds it).
- Mix: 1 trending + rest niche. Do NOT repeat the same tag set across posts.

## Output Format (JSON)
{{
    "text": "Post text (no hashtags, hook in first 5 words)",
    "hashtags": ["hashtag1", "hashtag2"],
    "keywords": ["keyword1", "keyword2"],
    "suggested_media_prompt": "Prompt to generate an image/video",
    "viral_score_estimate": 0.0-1.0,
    "variations": ["variation1", "variation2"]
}}

## Tips for Going Viral
- Hook in the first 5 words (question / bold claim / curiosity gap)
- Emotional triggers that resonate
- Relatable content that builds community
- Information value worth sharing
- Optimized for platform algorithm

Output JSON only."""

        return self._generate_content_with_compliance(prompt, max_tokens=4000)

    def generate_comment_reply(self, comment: str) -> str:
        """Generate a friendly, on-brand reply to a follower's comment."""
        prompt = f"""Generate a short, friendly reply to a follower's comment on a beauty post.


## Follower's comment
{comment}

## Reply rules
- Warm and appreciative, like a beauty-loving friend
- If the comment asks a question, answer it with a concrete tip or product detail
- Short: 1-2 sentences, max 1-2 emoji
- Never make medical claims about products
- Avoid copy-paste sounding replies

Output the reply text only."""

        return self._call_claude(prompt, max_tokens=300).strip()

    def generate_growth_reply(self, tweet_text: str, reply_type: str = "reply") -> str:
        """Generate a natural reply/quote for growth (engaging with strangers' tweets).

        Unlike generate_comment_reply, this creates casual beauty-enthusiast
        comments appropriate for interacting with people who don't know us.
        """
        if reply_type == "quote":
            instruction = "Quote this tweet and briefly add your own beauty experience or opinion."
        else:
            instruction = "Reply to this tweet with a short, relatable response or personal experience."

        prompt = f"""You are Miku, a 28-year-old beauty enthusiast.
You are commenting naturally on someone else's post.

## Target tweet
{tweet_text}

## Instructions
{instruction}

## Strict rules
- Keep it short: 1-2 lines
- This is a stranger — don't be overly familiar
- Never use these phrases: "I agree", "thanks", "so true", "I'm happy you saw this"
- Include a specific personal experience, product name, or number
- Max 1-2 emoji
- Must read like a natural post (zero AI feel)

## Good examples
- "Switched to the Muji toner and my pores are way less noticeable now! Best bang for the buck"
- "I've been curious about that! Saw it at the drugstore and couldn't decide whether to grab it"
- "Finding a lip color that actually works for your skin tone is so hard… especially as a warm spring 😭"

Output the reply text only."""

        return self._call_claude(prompt, max_tokens=200).strip()

    def analyze_viral_post(self, post_content: str, platform: str, metrics: dict) -> dict:
        """Analyze why a post went viral and extract reusable patterns."""
        prompt = f"""Analyze the following viral {platform} post and extract reusable patterns.

## Post content
{post_content}

## Metrics
{json.dumps(metrics, ensure_ascii=False)}

## Analysis fields (JSON output)
{{
    "hook_type": "Type of hook used at the start",
    "emotion_triggers": ["emotion trigger 1", ...],
    "content_structure": "Structural pattern of the post",
    "key_phrases": ["phrases that contributed to virality", ...],
    "target_demographics": "Assumed target audience",
    "timing_factor": "Posting timing elements",
    "shareability_factors": ["factors that make it shareable", ...],
    "replication_template": "Template for replication",
    "recommended_adaptations": ["ways to apply this pattern", ...]
}}

Output JSON only."""

        return self._call_claude_json(prompt, max_tokens=4000)

    def generate_reply(
        self,
        original_post: str,
        reply_tone: str = "friendly",
        goal: str = "engagement",
    ) -> str:
        """Generate an engaging reply to boost interaction."""
        prompt = f"""Generate an engaging reply to the following post.

## Original post
{original_post}

## Requirements
- Tone: {reply_tone}
- Goal: {goal}
- Write in natural English — must not sound like a bot
- Make the reply invite further conversation

Output the reply text only."""

        return self._call_claude(prompt, max_tokens=500).strip()

    def generate_beauty_review(self, product: str, platform: str, is_pr: bool = False) -> GeneratedContent:
        """Generate a beauty/cosmetics product review post."""
        pr_note = "Note: This is a sponsored post. You must include #ad or #sponsored at the very beginning." if is_pr else ""

        prompt = f"""As a beauty/cosmetics influencer, create a product review post for the following item.


## Product being reviewed: {product}
## Platform: {platform}
{pr_note}

## Review structure
1. Hook (attention-grabbing opening)
2. Basic product info
3. Texture and how it feels to use
4. Before/after or impressions of effectiveness
5. Pros and cons (be honest)
6. Who this is best for
7. CTA

## Notes
- Use FDA-compliant language (add "results may vary" or "personal opinion" where appropriate)
- Write as an honest, trustworthy review
- Friendly, approachable tone like a knowledgeable older sister

## Output format (JSON)
{{
    "text": "Post body text",
    "hashtags": ["hashtag"],
    "keywords": ["keyword"],
    "suggested_media_prompt": "Image/video prompt",
    "viral_score_estimate": 0.0-1.0,
    "variations": ["variation"]
}}

Output JSON only."""

        return self._generate_content_with_compliance(prompt, max_tokens=4000, is_pr=is_pr)

    def generate_routine_post(self, routine_type: str, platform: str) -> GeneratedContent:
        """Generate a beauty routine post (morning, night, body care, etc.)."""
        prompt = f"""As a beauty/cosmetics influencer, create a {routine_type} routine post.


## Routine type: {routine_type}
## Platform: {platform}

## Examples of routine types
- Morning skincare routine
- Night routine (skincare through bedtime)
- Body care routine
- Makeup routine (GRWM)
- Weekly special treatment

## Output format (JSON)
{{
    "text": "Post body text (step-by-step)",
    "hashtags": ["hashtag"],
    "keywords": ["keyword"],
    "suggested_media_prompt": "Image/video prompt",
    "viral_score_estimate": 0.0-1.0,
    "variations": ["variation"]
}}

Output JSON only."""

        return self._generate_content_with_compliance(prompt, max_tokens=4000)

    def generate_thread(self, topic: str) -> list[str]:
        """Generate a mini-thread of 3-5 connected tweets for X.

        Each tweet is self-contained but benefits from thread context.
        The first tweet opens with a strong hook; the last includes a CTA.
        All tweets are kept under 280 characters.
        """
        prompt = f"""You are a beauty/cosmetics influencer writing a Twitter/X thread.

## Thread topic
{topic}

## Rules
- Write exactly 3-5 tweets for a cohesive thread
- Each tweet must be under 280 characters (including spaces and emoji)
- Tweet 1: strong hook that stops the scroll — make people want to read on
- Tweet 2-4: deliver the value (tips, products, experiences, numbers, story)
- Last tweet: clear CTA (follow, save, share, comment, link in bio, etc.)
- Each tweet must stand alone AND build on what came before
- Include 1-2 emoji per tweet max — natural, not decorative spam
- No hashtags inside the tweets (they'll be added separately)
- Language: {settings.content_language}

## Output format (JSON array of strings)
[
    "Tweet 1 text",
    "Tweet 2 text",
    "Tweet 3 text",
    "Tweet 4 text (optional)",
    "Tweet 5 text (optional)"
]

Output JSON array only."""

        result = self._call_claude_json(prompt, max_tokens=1500)
        if not isinstance(result, list):
            logger.warning("generate_thread: unexpected response type %s", type(result))
            return []

        tweets = []
        for item in result:
            text = str(item).strip()
            if len(text) > 280:
                text = text[:277] + "..."
            if text:
                tweets.append(text)

        return tweets[:5]  # cap at 5 tweets

    def generate_content_calendar(
        self,
        days: int = 7,
        platform: str = "x",
        niche: str = "",
        trends: list[str] | None = None,
    ) -> list[dict]:
        """Generate a content calendar with optimized posting schedule.

        .. deprecated::
            Still used by ``src/cli.py`` and ``src/api/app.py``.
            Prefer building per-post content via ``generate_post()`` directly.
        """
        strategy = self.platform_strategy.get(platform, {})
        pillars = "\n".join(
            f"  - {c['pillar']} ({c['ratio']:.0%})" for c in self.persona["content_pillars"]
        )

        prompt = f"""Create a {days}-day content calendar for a beauty/cosmetics influencer.


## Requirements
- Platform: {platform}
- Niche: {niche or 'beauty & cosmetics'}
- Posting frequency: {strategy.get('posting_frequency', '1-3 times per day')}
- Language: {settings.content_language}
{"- Current trends: " + ", ".join(trends) if trends else ""}
- Max {settings.max_posts_per_day} posts per day

## Content pillars (aim for these ratios)
{pillars}

## Output format (JSON array)
[
    {{
        "day": 1,
        "posts": [
            {{
                "time": "09:00",
                "content_type": "text/image/video/carousel",
                "topic": "topic",
                "hook": "opening phrase",
                "outline": "post summary",
                "hashtags": ["tag1", ...],
                "media_suggestion": "image/video suggestion"
            }}
        ]
    }}
]

Output JSON array only."""

        return self._call_claude_json(prompt, max_tokens=4000)

    def optimize_cta(self, text: str, platform: str, ctr_data: dict | None = None) -> dict:
        """Optimize the CTA in a post to improve CTR."""
        ctr_context = ""
        if ctr_data:
            ctr_context = f"""
## Historical CTR data
- Current platform average CTR: {ctr_data.get('avg_ctr_pct', 'N/A')}%
- Pattern of top CTR posts: {ctr_data.get('top_pattern', 'N/A')}
"""

        prompt = f"""Optimize the CTA (Call to Action) in the following post to maximize CTR (click-through rate).

## Original post
{text}

## Platform: {platform}
{ctr_context}

## CTA optimization techniques
- Urgency: "today only", "limited time", "almost gone"
- Curiosity: "find out more", "the secret method", "you can't afford to miss this"
- Social proof: "over X people are doing this", "trending now"
- Specificity: use numbers, show concrete benefits
- Placement: opening hook + closing CTA structure

## Output (JSON)
{{
    "optimized_text": "Post text after CTA optimization",
    "cta_type": "CTA type used (urgency/curiosity/social_proof/benefit)",
    "cta_placement": "CTA placement (beginning/middle/end/multiple)",
    "original_ctr_estimate": 0.0-1.0,
    "optimized_ctr_estimate": 0.0-1.0,
    "expected_lift_pct": "Expected CTR improvement percentage",
    "variants": [
        {{
            "text": "variant 1",
            "cta_type": "type",
            "hypothesis": "why this increases CTR"
        }},
        {{
            "text": "variant 2",
            "cta_type": "type",
            "hypothesis": "reasoning"
        }}
    ]
}}

Output JSON only."""

        return self._call_claude_json(prompt, max_tokens=4000)

    def ab_test_variants(self, base_content: str, platform: str, num_variants: int = 3) -> list[dict]:
        """Generate A/B test variants of a post."""
        prompt = f"""Generate {num_variants} A/B test variants of the following {platform} post.

## Original post
{base_content}

## Output format (JSON array)
[
    {{
        "variant_id": "A",
        "text": "variant text",
        "change_description": "what was changed",
        "hypothesis": "why this change increases engagement"
    }}
]

Each variant should change one of the following:
- Hook type (question, number, emotion, surprise, etc.)
- CTA position and wording
- Writing style or tone

Output JSON array only."""

        return self._call_claude_json(prompt, max_tokens=3000)
