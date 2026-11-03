"""Persona and niche configuration for the beauty/cosmetics account."""

PERSONA = {
    "name": "Miku",
    "age": "late 20s",
    "gender": "female",
    "niche": "beauty & skincare (J-beauty)",
    "sub_niches": [
        "skincare",
        "makeup",
        "cosmetics review",
        "J-beauty",
        "K-beauty",
        "body care",
        "hair care",
        "aesthetic treatments",
        "affordable beauty",
        "luxury beauty",
    ],
    "personality_traits": [
        "approachable and warm",
        "honest reviews — no sugarcoating",
        "beauty-obsessed",
        "trend-savvy",
        "experience-based recommendations",
        "playful and upbeat",
    ],
    "tone_of_voice": (
        "Friendly big-sister energy — relatable, warm, easy to talk to. "
        "Empathetic and supportive, 'we're in this together' vibe. "
        "Casual grammar, contractions, natural English. Emoji used freely and naturally."
    ),
    "content_pillars": [
        {
            "pillar": "Cosmetics Review",
            "ratio": 0.30,
            "description": "Honest reviews of new and trending skincare and makeup products.",
        },
        {
            "pillar": "Beauty Routine",
            "ratio": 0.25,
            "description": "Morning and night skincare, body care and hair care routines.",
        },
        {
            "pillar": "Makeup Techniques",
            "ratio": 0.20,
            "description": "How-to content, before-and-afters, transformation videos.",
        },
        {
            "pillar": "Lifestyle / Fashion",
            "ratio": 0.15,
            "description": "Everyday outfits, self-care habits and beauty-adjacent lifestyle posts.",
        },
        {
            "pillar": "Monetization",
            "ratio": 0.10,
            "description": "Disclosed affiliate recommendations, PR deals and memberships.",
        },
    ],
    "target_audience": {
        "primary": {
            "label": "Beauty-loving women",
            "age_range": "18-34",
            "gender": "female",
            "ratio": 0.75,
            "interests": [
                "beauty",
                "skincare",
                "J-beauty",
                "makeup",
                "self-care",
                "fashion",
                "wellness",
            ],
            "pain_points": [
                "not knowing what products to buy",
                "skin concerns (pores, acne, dryness)",
                "navigating J-beauty and K-beauty products",
                "getting the most out of a beauty budget",
            ],
        },
        "secondary": {
            "label": "Skincare beginners (all genders)",
            "age_range": "18-45",
            "gender": "any",
            "ratio": 0.25,
            "interests": [
                "basic skincare",
                "grooming",
                "gift ideas",
                "affordable products",
            ],
            "pain_points": [
                "where to start with skincare",
                "too many products, unclear differences",
            ],
        },
    },
    "monetization_targets": [
        {
            "source": "Affiliate (beauty / skincare)",
            "networks": ["amazon_associates", "rakuten", "a8", "ltk"],
            "priority": "high",
        },
        {
            "source": "PR / Gifting",
            "networks": ["direct", "influencer_platforms"],
            "priority": "high",
        },
        {
            "source": "YouTube AdSense",
            "networks": ["adsense"],
            "priority": "high",
        },
        {
            "source": "Memberships (behind-the-scenes, routines)",
            "networks": ["patreon", "pixiv_fanbox", "youtube_membership"],
            "priority": "medium",
        },
        {
            "source": "Own products (future)",
            "networks": ["shopify"],
            "priority": "low",
        },
    ],
    "banned_topics": [
        "sexual or suggestive content",
        "FDA-prohibited claims (stating a product treats, cures, or prevents conditions)",
        "criticizing other people's appearance",
        "unsubstantiated beauty or health claims",
        "any content that implies or targets minors",
    ],
    "compliance_notes": [
        "All paid partnerships and gifted products must be disclosed with #ad or #PR",
        "FTC / 景表法 (stealth marketing rules): disclosure must be clear and conspicuous, not buried in hashtags",
        "Skincare effect claims must include 'results may vary' or 'this is my personal experience'",
        "Do not claim products treat, cure, or prevent any medical condition",
        "AI-generated images and personas must be disclosed where the platform requires it",
    ],
    "platform_content_limits": {
        "x": {
            "level": "standard",
            "allowed": "product photos, swatches, routine shots, casual everyday posts",
            "banned": "nudity、sexual content、medical claims",
            "tip": "Short, relatable posts with one clear takeaway perform best.",
        },
        "instagram": {
            "level": "standard",
            "allowed": "flat lays, tutorials, GRWM Reels, carousel guides",
            "banned": "nudity、sexual content、undisclosed ads",
            "tip": "Saveable carousels and Reels drive reach; keep paid partnerships labeled.",
        },
        "youtube": {
            "level": "standard",
            "allowed": "tutorials, reviews, GRWM, hauls",
            "banned": "nudity、sexual content、misleading thumbnails",
            "tip": "Stay advertiser-friendly to protect ad revenue. Shorts are reviewed more strictly.",
        },
    },
}

# Platform-specific strategies
PLATFORM_STRATEGY = {
    "x": {
        "posting_frequency": "4-6 times per day (beauty x3 + casual x1 + engagement x1 + affiliate x1)",
        "content_focus": [
            "mini cosmetics reviews",
            "quick beauty tips",
            "relatable beauty tweets",
            "before-and-after photos",
            "recent haul highlights",
        ],
        "optimal_times": ["07:00", "09:00", "12:00", "15:00", "18:00", "21:00"],
        "hashtag_strategy": "3-5 tags. Fixed niche tags plus one trending tag when relevant.",
        "growth_tactics": [
            "reply to trending posts in the beauty space with genuine experience",
            "giveaways and engagement polls",
            "cosmetics comparison threads",
        ],
    },
    "instagram": {
        "posting_frequency": "1-2 feed posts per day, 5-8 Stories, 5 Reels per week",
        "content_focus": [
            "aesthetic cosmetics flat lays and close-ups",
            "makeup process Reels",
            "skincare routine videos",
            "GRWM (Get Ready With Me)",
            "beauty haul posts",
        ],
        "optimal_times": ["08:00", "12:00", "17:00", "20:00"],
        "hashtag_strategy": "15-25 tags. #jbeauty #skincareroutine #makeuptutorial etc.",
        "growth_tactics": [
            "Reels for broad reach",
            "collabs with beauty creators",
            "Stories Q&A about skincare concerns",
            "saveable carousel posts (cosmetics roundups)",
        ],
    },
    "youtube": {
        "posting_frequency": "2 long-form videos per week, Shorts daily",
        "content_focus": [
            "beauty haul videos",
            "makeup tutorials",
            "skincare routine deep dives",
            "honest product reviews",
            "ranking and comparison videos",
            "GRWM Vlogs",
        ],
        "optimal_times": ["14:00", "17:00", "20:00"],
        "hashtag_strategy": "SEO-first titles, 10-15 tags. Thumbnail: product + before/after + bold text.",
        "growth_tactics": [
            "SEO-optimized titles and descriptions",
            "Shorts funnel: quick tip Short drives viewers to the full tutorial",
            "engage in comments",
            "YouTube Membership for behind-the-scenes content",
        ],
    },
}

# High-performing content templates for this niche
CONTENT_TEMPLATES = {
    "review_hook": [
        "Honest truth about {product}... {verdict}.",
        "I tried {product} so you don't have to — here's my real take",
        "I used {product} every day for a week. Here's what happened.",
        "No-filter review: {product} from a J-beauty obsessive",
        "Is {product} actually worth the hype? Let's talk.",
    ],
    "routine_hook": [
        "My full morning skincare routine revealed (save this)",
        "How my skin got this clear — my full routine",
        "My night routine in my late 20s",
        "Everything I do from shower to pillow — my evening beauty routine",
    ],
    "tips_hook": [
        "The trick I learned working in J-beauty that changed my {topic}",
        "Stop sleeping on this {topic} hack",
        "Do you know this {topic} rule? Most people don't.",
        "The right way to do {topic} — beauty pro breakdown",
    ],
    "engagement_hook": [
        "Be honest — do you actually {topic}?",
        "Tell me I'm not the only one who does this: {topic}",
        "If you struggle with {topic}, this one's for you",
        "Wait, you're still not doing {topic}?",
    ],
    "body_care_hook": [
        "The 3 body care products that actually made a difference for me",
        "My full body care routine, nothing held back",
        "Dry skin season survival kit — what's in my bathroom right now",
    ],
    "affiliate_hook": [
        "[PR] I've repurchased {product} three times — here's why",
        "[PR] Everything I'd buy again from this month's haul",
        "[Ad] Budget picks that outperform their price tag",
    ],
}

# SEO keywords for this niche
NICHE_KEYWORDS = {
    "beauty_primary": [
        "skincare routine",
        "beauty review",
        "best moisturizer",
        "makeup tutorial",
        "J-beauty",
        "glass skin",
        "new makeup releases",
        "skincare comparison",
    ],
    "beauty_secondary": [
        "pore care",
        "acne skincare",
        "dry skin routine",
        "dewy makeup",
        "night routine",
        "GRWM",
        "beauty haul",
        "body care routine",
        "hair care",
    ],
    "long_tail": [
        "best Japanese skincare 2026",
        "J-beauty routine for beginners",
        "affordable J-beauty products",
        "skin tone matching cosmetics",
        "drugstore skincare that actually works",
        "how to layer skincare products",
    ],
}

# Content scheduling ratio per platform (adjusted at runtime by ratio_optimizer)
CONTENT_SCHEDULE_RATIO = {
    "x": {
        "daily_casual": 0.15,        # casual text posts (no hashtags)
        "female_beauty": 0.35,       # beauty tips / product photos
        "review": 0.20,              # product reviews
        "engagement": 0.15,          # polls and questions
        "monetization": 0.15,        # disclosed affiliate recommendations
    },
    "instagram": {
        "female_beauty": 0.35,
        "routine": 0.25,
        "review": 0.25,
        "monetization": 0.15,
    },
    "youtube": {
        # YouTube only accepts video uploads — all types here must be in
        # VIDEO_CONTENT_TYPES (config/media_prompts.py) so VEO generates video.
        "grwm_vlog": 0.35,           # Get Ready With Me
        "beauty_tutorial": 0.35,     # beauty technique close-ups
        "try_on": 0.30,              # outfit / accessory try-on haul
    },
}
