"""Miku ★ Oracle — 40 cosplay/fortune image prompt variations for Kie.ai.

The Miku ★ Oracle character is the HoshimiLive mascot: a mystical Japanese
Oracle who delivers tarot, astrology, and fortune readings. Every prompt in
this module is designed so the face is never the focal point (hair covering,
back view, hands-only, silhouette, etc.). The ``NO_FACE`` directive is
appended to every prompt as a second defense. As a third defense,
``src.media.face_mosaic.apply_face_mosaic`` pixelates any detected face
region before images are posted.

Variations: 40 total, split into 4 themed banks of 10 cosplays each.

    TAROT (tarot_*)          — witch / sorceress / tarot reader aesthetic
    ASTROLOGY (astro_*)      — constellation / star maiden / celestial
    DIVINATION (divine_*)    — crystal ball, runes, palm reading, pendulum
    SEASONAL (season_*)      — moon phases, seasons, blessings, ritual

Usage::

    from config.miku_oracle_prompts import (
        get_oracle_prompt,
        ALL_VARIATIONS,
        MIKU_ORACLE_BASE,
    )

    prompt, aspect = get_oracle_prompt(category="tarot", platform="instagram")
"""

from __future__ import annotations

import random
from typing import Any

# ---------------------------------------------------------------------------
# Character base — appended to every variation so Kie.ai renders a consistent
# Miku ★ Oracle across 40 cosplays. Face is always hidden.
# ---------------------------------------------------------------------------

MIKU_ORACLE_BASE = (
    "Miku Oracle character, young Japanese woman in her early 20s, "
    "long silver-lavender hair with pastel gradient tips, ethereal aura, "
    "slim figure with delicate hands, "
    "face hidden by hair / hood / back view / cropped frame — face NEVER visible, "
    "dreamy cinematic lighting, soft film grain, "
    "high quality illustration-photo hybrid, mystical fantasy aesthetic"
)

NO_FACE = (
    "STRICT: The face must never appear. Use back view, over-the-shoulder angle, "
    "hair completely covering the face, hood shadow, silhouette, or hands-only close-up. "
    "Absolutely no facial features — no eyes, no nose, no mouth visible. "
    "Focus on hands, hair, fabric, background props, or magical effects instead."
)

# Platform aspect ratios — mirrors media_prompts.PLATFORM_SIZES for continuity
PLATFORM_SIZES: dict[str, str] = {
    "x": "1:1",
    "instagram": "1:1",
    "instagram_story": "9:16",
    "tiktok": "9:16",
    "youtube": "16:9",
    "youtube_shorts": "9:16",
}


# ---------------------------------------------------------------------------
# TAROT bank — witch / sorceress / tarot-reader cosplays (10 variations)
# ---------------------------------------------------------------------------

TAROT_VARIATIONS: list[dict[str, Any]] = [
    {
        "id": "tarot_01",
        "title": "Moonlit Witch Spread",
        "prompt": (
            f"Over-the-shoulder view of {MIKU_ORACLE_BASE}, wearing a dark velvet witch robe "
            "with silver moon embroidery, spreading a Rider-Waite tarot deck on a rosewood table, "
            "The Moon card revealed in center, candle flames and crystal cluster around the spread, "
            "deep indigo and gold lighting, tarot reading ambiance"
        ),
    },
    {
        "id": "tarot_02",
        "title": "Crimson Cloak Reader",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE}, wearing a crimson hooded cloak with gold trim, "
            "hood pulled up deep over head, shuffling oversized tarot cards between slender hands, "
            "Gothic library background with leather-bound grimoires, warm candlelight"
        ),
    },
    {
        "id": "tarot_03",
        "title": "Fool's Journey",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} standing at a cliff edge at dawn, "
            "The Fool tarot card held out in one hand toward sunrise, "
            "white flowing dress, hair streaming in wind covering face, "
            "pastel sky gradient, dreamy fantasy composition"
        ),
    },
    {
        "id": "tarot_04",
        "title": "High Priestess Pose",
        "prompt": (
            f"Neck-down portrait of {MIKU_ORACLE_BASE}, dressed as The High Priestess tarot archetype, "
            "blue and white flowing robes with crescent moon crown (crown shadowed, face not visible), "
            "hands folded over a Torah scroll on lap, pomegranate tapestry behind, temple aesthetic"
        ),
    },
    {
        "id": "tarot_05",
        "title": "Card Pull Close-Up",
        "prompt": (
            "Extreme close-up of a pale elegant hand with black manicured nails drawing a single tarot card "
            f"from a deck fanned on a black velvet cloth, {MIKU_ORACLE_BASE} is present but only her "
            "hand and hair tips are in frame, The Star card being revealed, candle flickering nearby"
        ),
    },
    {
        "id": "tarot_06",
        "title": "Tower Reading Drama",
        "prompt": (
            f"{MIKU_ORACLE_BASE}, shot from behind, cloaked silhouette revealing The Tower tarot card "
            "dramatically over a round table, storm clouds and lightning visible through arched window, "
            "stormy blue tones, cinematic wide shot, hair obscuring any profile"
        ),
    },
    {
        "id": "tarot_07",
        "title": "Witch's Cottage Spread",
        "prompt": (
            f"Top-down flat lay of hands (belonging to {MIKU_ORACLE_BASE}) laying out a Celtic Cross "
            "tarot spread on an herbal-strewn wooden table, dried lavender, amethyst clusters, "
            "steaming herbal tea cup, cozy cottagecore witch aesthetic, natural window light"
        ),
    },
    {
        "id": "tarot_08",
        "title": "Temperance Water Ritual",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} kneeling at a moonlit lake, "
            "pouring water between two silver chalices recreating The Temperance tarot card, "
            "long white gown, hair flowing down back, bioluminescent ripples, ethereal reflection"
        ),
    },
    {
        "id": "tarot_09",
        "title": "Death Card Meditation",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} in a black mourning veil (face fully covered by veil), "
            "holding The Death tarot card (rebirth symbolism), autumn leaves and white roses around, "
            "misty graveyard chapel background, solemn violet lighting, gothic portrait"
        ),
    },
    {
        "id": "tarot_10",
        "title": "Sun Card Radiance",
        "prompt": (
            f"Neck-down portrait of {MIKU_ORACLE_BASE} in a yellow sunflower-embroidered sundress, "
            "holding The Sun tarot card overhead with both hands, standing in a sunflower field, "
            "golden hour lighting, warm optimistic palette, hair shielding face from camera"
        ),
    },
]

# ---------------------------------------------------------------------------
# ASTROLOGY bank — star maiden / constellation cosplays (10 variations)
# ---------------------------------------------------------------------------

ASTRO_VARIATIONS: list[dict[str, Any]] = [
    {
        "id": "astro_01",
        "title": "Night Sky Seer",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} on a rooftop observatory, "
            "wearing a midnight-blue gown with embroidered constellations, "
            "looking up at a telescope, star maps floating around her, "
            "Milky Way visible overhead, hair cascading down obscuring face"
        ),
    },
    {
        "id": "astro_02",
        "title": "Zodiac Wheel Pose",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} standing inside a glowing zodiac wheel "
            "made of golden light, arms outstretched to read star positions, "
            "twelve zodiac symbols orbiting, deep space nebula background"
        ),
    },
    {
        "id": "astro_03",
        "title": "Aries Flame",
        "prompt": (
            f"Over-the-shoulder view of {MIKU_ORACLE_BASE} in a red and gold warrior-priestess outfit, "
            "holding a bronze ram horn (Aries symbol), fire sparks rising, volcanic dusk sky, "
            "hair swept forward hiding face, cinematic fantasy lighting"
        ),
    },
    {
        "id": "astro_04",
        "title": "Cancer Moon Bath",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} sitting on a rocky beach under a huge silver full moon, "
            "waves crashing, pearl-white flowing dress, holding a nautilus shell, "
            "moonlight reflecting off hair covering her face, dreamy blue tone"
        ),
    },
    {
        "id": "astro_05",
        "title": "Libra Balance",
        "prompt": (
            "Neck-down portrait of slender hands (belonging to Miku Oracle) balancing a "
            "golden set of scales, feather in one pan and a gemstone in the other, "
            "white silk draped sleeves, marble temple column background, symmetric composition"
        ),
    },
    {
        "id": "astro_06",
        "title": "Scorpio Shadow",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} in a black lace gown and a scorpion-shaped silver brooch, "
            "hand holding a vial of glowing dark liquid, red candles in the background, "
            "gothic cathedral setting, dramatic chiaroscuro lighting"
        ),
    },
    {
        "id": "astro_07",
        "title": "Sagittarius Archer",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} drawing a glowing star-bow, "
            "arrow tipped with stardust aimed at the sky, "
            "forest clearing at dusk, fireflies, green and gold palette, hair in a long braid"
        ),
    },
    {
        "id": "astro_08",
        "title": "Capricorn Mountain Sage",
        "prompt": (
            f"{MIKU_ORACLE_BASE} shown from behind, climbing a snowy mountain path, "
            "wrapped in a wool shawl with goat-horn headpiece (face hidden under hood), "
            "staff planted on rock, sunrise breaking over peaks, earth-tone palette"
        ),
    },
    {
        "id": "astro_09",
        "title": "Aquarius Water Bearer",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} pouring flowing blue energy from a crystal vase, "
            "the water turning into star particles mid-air, "
            "cyan and violet gradient background, graceful pose, hair floating in zero gravity"
        ),
    },
    {
        "id": "astro_10",
        "title": "Pisces Deep Dive",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} submerged up to her shoulders in a glowing pool, "
            "two koi fish circling around her, iridescent scales shimmering, "
            "underwater ambient lighting, hair floating and covering face, zen atmosphere"
        ),
    },
]

# ---------------------------------------------------------------------------
# DIVINATION bank — crystal ball / runes / palm reading (10 variations)
# ---------------------------------------------------------------------------

DIVINATION_VARIATIONS: list[dict[str, Any]] = [
    {
        "id": "divine_01",
        "title": "Crystal Ball Gaze",
        "prompt": (
            f"Neck-down shot of {MIKU_ORACLE_BASE}, slender hands cupped around a glowing crystal ball, "
            "smoky swirls and galaxies visible inside the sphere, "
            "dark purple velvet cloth, brass candlesticks, fortune teller tent aesthetic"
        ),
    },
    {
        "id": "divine_02",
        "title": "Rune Casting",
        "prompt": (
            "Extreme close-up of a pale hand casting hand-carved wooden runes onto a linen cloth, "
            "Norse symbols glowing softly, other hand of Miku Oracle visible at edge of frame, "
            "forest cabin floor, morning light filtering through window"
        ),
    },
    {
        "id": "divine_03",
        "title": "Palm Reading Session",
        "prompt": (
            f"Over-the-shoulder view of {MIKU_ORACLE_BASE} holding a client's open palm "
            "between her own hands, tracing life line with a silver stylus, "
            "incense smoke curling, warm amber lighting, intimate fortune reading atmosphere"
        ),
    },
    {
        "id": "divine_04",
        "title": "Pendulum Swing",
        "prompt": (
            f"Side silhouette of {MIKU_ORACLE_BASE} holding a copper pendulum over a "
            "divination board, pendulum mid-swing casting a long shadow, "
            "candles in the background, sepia and bronze palette, hair falling forward"
        ),
    },
    {
        "id": "divine_05",
        "title": "Tea Leaf Reading",
        "prompt": (
            "Top-down view of a porcelain tea cup with leaves forming a symbolic shape, "
            f"hands of {MIKU_ORACLE_BASE} gently tilting the cup, "
            "lace tablecloth, sugar cubes, vintage British tea parlor aesthetic"
        ),
    },
    {
        "id": "divine_06",
        "title": "Scrying Mirror",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} peering into a round obsidian scrying mirror, "
            "mirror surface shows swirling visions of the future, "
            "dark altar with bones and feathers, deep green ritual room lighting"
        ),
    },
    {
        "id": "divine_07",
        "title": "I Ching Coins",
        "prompt": (
            "Close-up of hands tossing three ancient Chinese I Ching coins onto a "
            "silk scroll with hexagrams drawn in ink, "
            f"{MIKU_ORACLE_BASE} present only as hair and hands in frame, "
            "red and gold lantern lighting, traditional Asian fortune room"
        ),
    },
    {
        "id": "divine_08",
        "title": "Bone Throwing",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} kneeling over a leather mat, "
            "scattering small engraved bones and stones, "
            "tribal shaman aesthetic, wolf pelt cloak over shoulders, hair in a long braid, "
            "firelight reflecting on dark wooden floor"
        ),
    },
    {
        "id": "divine_09",
        "title": "Ink Divination",
        "prompt": (
            "Overhead shot of a silver bowl filled with black water, "
            f"hands of {MIKU_ORACLE_BASE} dropping a single ink bead that blooms into a pattern, "
            "white marble table, white robe sleeves, ritualistic ceremony mood"
        ),
    },
    {
        "id": "divine_10",
        "title": "Cloud Reading",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} lying on a grassy hill, "
            "one arm pointing up at dramatic cloud formations shaped like omens, "
            "wide pastoral landscape, golden hour, hair fanned out under her hiding profile"
        ),
    },
]

# ---------------------------------------------------------------------------
# SEASONAL bank — moon phases, seasons, blessings (10 variations)
# ---------------------------------------------------------------------------

SEASONAL_VARIATIONS: list[dict[str, Any]] = [
    {
        "id": "season_01",
        "title": "New Moon Intention",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} kneeling at an altar during a new moon night, "
            "writing intentions on parchment with a quill, "
            "black candles and obsidian shards around, very dim starlight, "
            "long black hooded robe obscuring head"
        ),
    },
    {
        "id": "season_02",
        "title": "Full Moon Charging",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} holding crystals up to a giant full moon, "
            "arms raised in offering, white lace dress glowing in moonlight, "
            "forest clearing with fireflies, ethereal silver-blue palette"
        ),
    },
    {
        "id": "season_03",
        "title": "Spring Equinox Bloom",
        "prompt": (
            f"Over-the-shoulder view of {MIKU_ORACLE_BASE} weaving a flower crown of cherry blossoms, "
            "pink and white petals falling, kimono-inspired pastel outfit, "
            "traditional Japanese garden, soft morning light, hair covering face"
        ),
    },
    {
        "id": "season_04",
        "title": "Summer Solstice Fire",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} dancing around a bonfire on the summer solstice, "
            "flower-crowned hair flying, flame sparks rising to starry sky, "
            "tribal bohemian aesthetic, warm orange and gold tones"
        ),
    },
    {
        "id": "season_05",
        "title": "Autumn Harvest Blessing",
        "prompt": (
            f"Neck-down shot of {MIKU_ORACLE_BASE} arranging pumpkins, dried herbs, "
            "and wheat stalks on a rustic altar, amber light streaming through a barn window, "
            "earthy cottagecore witch outfit, harvest gratitude mood"
        ),
    },
    {
        "id": "season_06",
        "title": "Winter Solstice Ritual",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} wrapped in a heavy fur cloak under falling snow, "
            "lighting a yule log in a stone circle, frost and ice crystals, "
            "pine and holly wreath on altar, deep midnight blue palette"
        ),
    },
    {
        "id": "season_07",
        "title": "Waxing Crescent Blessing",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} on a balcony whispering to a waxing crescent moon, "
            "hair blowing in night breeze, holding a jar of collected moonwater, "
            "soft city lights below, dreamy urban-witch aesthetic"
        ),
    },
    {
        "id": "season_08",
        "title": "Eclipse Ceremony",
        "prompt": (
            f"{MIKU_ORACLE_BASE} shown from behind, arms raised during a total solar eclipse, "
            "standing on a stone platform, dramatic corona ring in the sky, "
            "dark robes fluttering, hair flowing upward, mystical awe-struck composition"
        ),
    },
    {
        "id": "season_09",
        "title": "Blood Moon Rite",
        "prompt": (
            f"Silhouette of {MIKU_ORACLE_BASE} in a deep crimson gown holding a chalice "
            "of red wine up to a blood moon, candlelit pentagram in the foreground, "
            "gothic cathedral ruins, dramatic red and shadow palette"
        ),
    },
    {
        "id": "season_10",
        "title": "Dawn Meditation",
        "prompt": (
            f"Back view of {MIKU_ORACLE_BASE} sitting cross-legged on a mountain top at sunrise, "
            "arms relaxed on knees, long hair cascading, "
            "clouds rolling below, pastel pink and gold sky, zen morning prayer mood"
        ),
    },
]


# ---------------------------------------------------------------------------
# Flattened index & helpers
# ---------------------------------------------------------------------------

CATEGORIES: dict[str, list[dict[str, Any]]] = {
    "tarot": TAROT_VARIATIONS,
    "astrology": ASTRO_VARIATIONS,
    "divination": DIVINATION_VARIATIONS,
    "seasonal": SEASONAL_VARIATIONS,
}

ALL_VARIATIONS: list[dict[str, Any]] = (
    TAROT_VARIATIONS + ASTRO_VARIATIONS + DIVINATION_VARIATIONS + SEASONAL_VARIATIONS
)

assert len(ALL_VARIATIONS) == 40, (
    f"Miku Oracle must have exactly 40 variations, got {len(ALL_VARIATIONS)}"
)


def _compose(variation: dict[str, Any]) -> str:
    """Append NO_FACE directive to a base variation prompt."""
    return f"{variation['prompt']}. {NO_FACE}"


def get_oracle_prompt(
    category: str | None = None,
    variation_id: str | None = None,
    platform: str = "instagram",
) -> tuple[str, str]:
    """Return a (prompt, aspect_ratio) tuple for a Miku Oracle variation.

    Args:
        category: ``tarot`` / ``astrology`` / ``divination`` / ``seasonal``.
            If ``None``, picks across all 40.
        variation_id: exact ``id`` of a variation (e.g. ``"tarot_03"``).
            Overrides ``category`` when provided.
        platform: key for ``PLATFORM_SIZES``, defaults to Instagram 1:1.

    Returns:
        tuple ``(prompt_string, aspect_ratio_string)`` — prompt already has
        ``NO_FACE`` appended.
    """
    pool: list[dict[str, Any]]
    if variation_id is not None:
        match = next((v for v in ALL_VARIATIONS if v["id"] == variation_id), None)
        if match is None:
            raise ValueError(f"unknown Miku Oracle variation_id: {variation_id}")
        pool = [match]
    elif category is not None:
        if category not in CATEGORIES:
            raise ValueError(
                f"unknown Miku Oracle category: {category} "
                f"(choose from {sorted(CATEGORIES)})"
            )
        pool = CATEGORIES[category]
    else:
        pool = ALL_VARIATIONS

    variation = random.choice(pool)
    prompt = _compose(variation)
    aspect_ratio = PLATFORM_SIZES.get(platform, "1:1")
    return prompt, aspect_ratio


def list_variations(category: str | None = None) -> list[dict[str, Any]]:
    """Return all variation descriptors (id/title/prompt) — useful for dashboards.

    Does NOT append NO_FACE, so the raw editorial prompt is visible. For the
    final send-to-Kie version use ``get_oracle_prompt``.
    """
    if category is None:
        return list(ALL_VARIATIONS)
    if category not in CATEGORIES:
        raise ValueError(f"unknown category: {category}")
    return list(CATEGORIES[category])
