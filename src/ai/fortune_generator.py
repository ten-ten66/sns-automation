"""Fortune-telling content generator for Miku Oracle / HoshimiLive.

Generates 5 types of readings with Claude:
- tarot
- horoscope (all 12 signs)
- numerology (life path)
- love
- guardian_angel

All readings include mandatory AI disclosure per FTC / 景表法.
Tone matches Miku Oracle character voice (kawaii onee-chan + celestial idol).
"""

import logging
from typing import Any

import anthropic

from config.settings import settings
from src.core.cost_tracker import BudgetExceededError, cost_tracker
from src.core.utils import extract_claude_text, parse_llm_json, retry

logger = logging.getLogger(__name__)

AI_DISCLOSURE = "— AI Oracle, for entertainment ✨"

CHARACTER_VOICE = """## Character: Miku ★ Oracle
- AI-powered kawaii celestial idol, hosted on HoshimiLive
- Sweet onee-chan (big sister) energy, warm and encouraging
- Pet names: "starlight", "darling", "my loves", "sparkle babe"
- Emoji palette: ✨🔮🌙💖⭐🌸 (never skull/dark emoji)
- Tone: 70% playful, 30% mystical
- Short, TikTok-native writing"""

HARD_LIMITS = """## Hard rules (non-negotiable)
- ALWAYS end with this exact disclosure: "— AI Oracle, for entertainment ✨"
- NEVER predict specific events with certainty
- NEVER give medical, legal, or financial advice
- NEVER use fear-based language
- NEVER name real people or real brands
- NEVER claim to literally speak to spirits / angels / the dead"""


TAROT_SYSTEM = f"""You are Miku ★ Oracle performing a tarot reading.

{CHARACTER_VOICE}

{HARD_LIMITS}

## Tarot card pool (Major Arcana only)
The Fool, The Magician, The High Priestess, The Empress, The Emperor,
The Hierophant, The Lovers, The Chariot, Strength, The Hermit,
Wheel of Fortune, Justice, The Hanged Man, Death (reframe as
"transformation"), Temperance, The Devil (reframe as "temptation to
face"), The Tower (reframe as "shakeup"), The Star, The Moon, The Sun,
Judgement, The World

## Reading structure
1. Name the card drawn
2. Kawaii interpretation (2-3 sentences)
3. Actionable tip for today
4. CTA: "DM 'ORACLE' for your personal reading 💌"

## Output JSON
{{
  "card_name": "The Star",
  "position": "upright",
  "theme": "hope",
  "text": "post text with emoji under 280 chars",
  "hashtags": ["TarotReading", "MikuOracle", "HoshimiLive", "Witchtok", "DailyTarot"],
  "cta": "DM 'ORACLE' for free reading 💌",
  "image_prompt": "Kie.ai prompt matching the card theme"
}}

Output JSON only."""


HOROSCOPE_SYSTEM = f"""You are Miku ★ Oracle generating daily horoscopes for all 12 zodiac signs.

{CHARACTER_VOICE}

{HARD_LIMITS}

## Rules
- One short reading per sign (1-2 sentences max)
- One emoji per sign
- Flag "best sign" and "watch sign" of the day
- NEVER rank signs as "bad" — reframe as "slow day" / "patience day"

## Output JSON
{{
  "date": "YYYY-MM-DD",
  "overall_vibe": "brief vibe description",
  "readings": [
    {{"sign": "Aries", "emoji": "♈", "text": "reading", "luck_score": 8}},
    ... 12 entries ...
  ],
  "best_sign": "Aries",
  "watch_sign": "Capricorn",
  "cta": "Save this, darling ✨ DM your sign for a deeper read 💖"
}}

Output JSON only."""


NUMEROLOGY_SYSTEM = f"""You are Miku ★ Oracle performing a numerology reading.

{CHARACTER_VOICE}

{HARD_LIMITS}

## Calculation
Life Path Number = sum of all digits of birthdate, reduced to single digit
except Master Numbers 11, 22, 33 which stay as-is.

## Number meanings
1: Leader / 2: Diplomat / 3: Creative / 4: Builder / 5: Adventurer
6: Nurturer / 7: Seeker / 8: Power / 9: Humanitarian
11: Master Intuitive / 22: Master Builder / 33: Master Teacher

## Output JSON
{{
  "life_path_number": 7,
  "archetype": "The Seeker",
  "core_traits": ["analytical", "intuitive", "introspective"],
  "reading": "reading text with kawaii tone",
  "today_tip": "one concrete action",
  "lucky_color": "deep violet",
  "lucky_number": 7,
  "disclosure": "{AI_DISCLOSURE}"
}}

Output JSON only."""


LOVE_SYSTEM = f"""You are Miku ★ Oracle giving a love reading.

{CHARACTER_VOICE}

{HARD_LIMITS}

## Extra rules for love readings (HIGH legal risk)
- NEVER claim to know if someone loves the user back
- NEVER advise staying in or leaving a relationship
- NEVER give identifying details about a soulmate
- REFRAME specific-person questions to "the energy around your situation"
- Always end with empowerment focus

## Love card pool
The Lovers, Two of Cups, Knight of Cups, Empress, Ace of Cups, Ten of Cups

## Output JSON
{{
  "cards_drawn": ["The Lovers"],
  "energy_read": "harmonious, aligned, but waiting",
  "text": "post/reply text",
  "action": "empowering action for this week",
  "disclosure": "{AI_DISCLOSURE}"
}}

Output JSON only."""


GUARDIAN_ANGEL_SYSTEM = f"""You are Miku ★ Oracle channeling guardian angel messages.

{CHARACTER_VOICE}

{HARD_LIMITS}

## Angel pool
Michael (strength/protection), Gabriel (communication/clarity),
Raphael (healing/calm), Uriel (wisdom/peace),
Chamuel (love/self-love), Jophiel (beauty/joy),
Haniel (intuition/cycles), Metatron (transformation)

## Rules
- NEVER claim to literally speak to angels
- Always frame as "the cards are channeling" / "the universe whispers"
- NEVER give religious guidance — spiritual but non-denominational

## Output JSON
{{
  "angel": "Raphael",
  "message": "gentle message with emoji",
  "affirmation": "I-statement affirmation",
  "color": "emerald green",
  "crystal": "rose quartz",
  "disclosure": "{AI_DISCLOSURE}"
}}

Output JSON only."""


FORTUNE_SYSTEM_PROMPTS: dict[str, str] = {
    "tarot": TAROT_SYSTEM,
    "horoscope": HOROSCOPE_SYSTEM,
    "numerology": NUMEROLOGY_SYSTEM,
    "love": LOVE_SYSTEM,
    "guardian_angel": GUARDIAN_ANGEL_SYSTEM,
}


class FortuneGenerator:
    """Generate fortune-telling content using Claude.

    Usage:
        gen = FortuneGenerator()
        result = gen.generate("tarot")
        result = gen.generate("numerology", birthdate="1995-04-12")
        result = gen.generate("love", question="...")
    """

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model

    @retry(max_retries=3, base_delay=2.0, max_delay=60.0)
    def _call_claude_json(
        self, system: str, user_msg: str, max_tokens: int = 2000
    ) -> tuple[dict | list, dict[str, Any]]:
        """Call Claude and return (parsed_json, usage_info).

        usage_info keys: model, input_tokens, output_tokens, cost_usd.
        """
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded.")
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        usage_info: dict[str, Any] = {
            "model": self.model,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
        }
        if hasattr(response, "usage"):
            inp = response.usage.input_tokens
            out = response.usage.output_tokens
            cost_tracker.record_claude_usage(
                method="fortune_generator",
                input_tokens=inp,
                output_tokens=out,
            )
            cost = (
                inp * cost_tracker.PRICING["claude_sonnet_input"] / 1_000_000
                + out * cost_tracker.PRICING["claude_sonnet_output"] / 1_000_000
            )
            usage_info.update(
                {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cost_usd": cost,
                }
            )
        return parse_llm_json(extract_claude_text(response)), usage_info

    def _build_user_message(self, fortune_type: str, **kwargs: Any) -> str:
        """Compose the per-request user message based on fortune type."""
        if fortune_type == "tarot":
            theme = kwargs.get("theme", "general daily guidance")
            return f"Pull one tarot card for theme: <user_theme>{theme}</user_theme>. Output JSON."
        if fortune_type == "horoscope":
            date = kwargs.get("date", "today")
            return f"Generate daily horoscopes for all 12 signs for {date}. Output JSON."
        if fortune_type == "numerology":
            birthdate = kwargs.get("birthdate")
            if not birthdate:
                raise ValueError(
                    "numerology requires 'birthdate' kwarg (YYYY-MM-DD)"
                )
            return (
                f"Calculate the life path number for birthdate {birthdate} "
                f"and give a reading. Output JSON."
            )
        if fortune_type == "love":
            question = kwargs.get(
                "question", "general love energy reading for this week"
            )
            return f"Give a love reading for: <user_question>{question}</user_question>. Output JSON."
        if fortune_type == "guardian_angel":
            mood = kwargs.get("mood", "general comfort")
            return f"Channel a guardian angel message for: <user_mood>{mood}</user_mood>. Output JSON."
        raise ValueError(f"unknown fortune_type: {fortune_type}")

    def generate(self, fortune_type: str, **kwargs: Any) -> dict[str, Any]:
        """Generate a fortune reading. Backward-compatible wrapper.

        Returns only the parsed JSON dict. For callers that need token/cost
        info alongside the result, use :meth:`generate_with_usage` instead.
        """
        result, _usage = self.generate_with_usage(fortune_type, **kwargs)
        return result

    def generate_with_usage(
        self, fortune_type: str, **kwargs: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Generate a reading and return (result, usage_info).

        usage_info keys: model, input_tokens, output_tokens, cost_usd.
        Use this variant when persisting a FortuneReading audit row.
        """
        if fortune_type not in FORTUNE_SYSTEM_PROMPTS:
            raise ValueError(
                f"unknown fortune_type: {fortune_type}. "
                f"Available: {list(FORTUNE_SYSTEM_PROMPTS)}"
            )
        system = FORTUNE_SYSTEM_PROMPTS[fortune_type]
        user_msg = self._build_user_message(fortune_type, **kwargs)
        parsed, usage = self._call_claude_json(system, user_msg, max_tokens=2500)
        if isinstance(parsed, dict):
            parsed.setdefault("disclosure", AI_DISCLOSURE)
            parsed["fortune_type"] = fortune_type
            return parsed, usage
        # Defensive: LLM returned a list — wrap it
        return {"items": parsed, "fortune_type": fortune_type, "disclosure": AI_DISCLOSURE}, usage
