"""SEO optimization module for SNS content."""

import logging
from dataclasses import dataclass

import anthropic

from config.settings import settings
from src.core.utils import parse_llm_json, extract_claude_text, retry
from src.core.cost_tracker import cost_tracker, BudgetExceededError

logger = logging.getLogger(__name__)


@dataclass
class SEOResult:
    optimized_text: str
    primary_keywords: list[str]
    secondary_keywords: list[str]
    hashtags: list[str]
    meta_description: str
    seo_score: float
    suggestions: list[str]


class SEOOptimizer:
    """Optimize SNS content for search and discovery."""

    def __init__(self):
        self.claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @retry(max_retries=3, base_delay=2.0, max_delay=60.0)
    def _call_claude_json(self, prompt: str, max_tokens: int = 2000) -> dict | list:
        """Call Claude API with retry, cost tracking."""
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded.")
        response = self.claude.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage(
                "seo_optimizer", response.usage.input_tokens, response.usage.output_tokens,
            )
        return parse_llm_json(extract_claude_text(response))

    def optimize_post(
        self,
        text: str,
        platform: str,
        target_keywords: list[str] | None = None,
    ) -> SEOResult:
        """Optimize a post for platform-specific SEO."""
        keywords = target_keywords or settings.target_keywords

        prompt = f"""以下のSNS投稿をSEO最適化してください。

## 投稿
{text}

## プラットフォーム: {platform}
## ターゲットキーワード: {", ".join(keywords) if keywords else "自動判定"}

## プラットフォーム別SEOポイント
- X: キーワード自然埋め込み、トレンドタグ活用、リプライチェーン最適化
- Instagram: ALTテキスト、キャプションキーワード、ハッシュタグ戦略（30個以内）
- TikTok: キャプションキーワード、サウンド選択、ハッシュタグ（3-5個）
- YouTube: タイトル最適化、説明文、タグ、サムネイルテキスト

## 出力（JSON）
{{
    "optimized_text": "SEO最適化後のテキスト",
    "primary_keywords": ["主要キーワード"],
    "secondary_keywords": ["副キーワード"],
    "hashtags": ["最適化されたハッシュタグ（#なし）"],
    "meta_description": "メタ説明文",
    "seo_score": 0.0-1.0,
    "suggestions": ["追加のSEO改善提案"]
}}

JSONのみを出力してください。"""

        data = self._call_claude_json(prompt, max_tokens=2000)
        # Safe construction with field filtering and defaults
        allowed_fields = {f.name for f in SEOResult.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in allowed_fields}
        defaults = {
            "optimized_text": text,  # fallback to original
            "primary_keywords": [],
            "secondary_keywords": [],
            "hashtags": [],
            "meta_description": "",
            "seo_score": 0.0,
            "suggestions": [],
        }
        for key, default in defaults.items():
            if key not in filtered:
                filtered[key] = default
        if "seo_score" in filtered:
            try:
                filtered["seo_score"] = float(filtered["seo_score"])
            except (ValueError, TypeError):
                filtered["seo_score"] = 0.0
        return SEOResult(**filtered)

    def generate_hashtag_strategy(
        self,
        topic: str,
        platform: str,
        competitor_tags: list[str] | None = None,
    ) -> dict:
        """Generate an optimized hashtag strategy."""
        prompt = f"""「{topic}」に関する{platform}用のハッシュタグ戦略を作成してください。

{"## 競合のハッシュタグ: " + ", ".join(competitor_tags) if competitor_tags else ""}

## 出力（JSON）
{{
    "primary_tags": ["メインタグ（高検索ボリューム、5個）"],
    "niche_tags": ["ニッチタグ（中検索、競合少、10個）"],
    "community_tags": ["コミュニティタグ（エンゲージメント向け、5個）"],
    "trending_tags": ["トレンドタグ（今のトレンド、3個）"],
    "branded_tags": ["ブランドタグ（オリジナル、2個）"],
    "avoid_tags": ["使うべきでないタグとその理由"],
    "rotation_strategy": "ハッシュタグローテーション戦略",
    "platform_specific_tips": "プラットフォーム固有のヒント"
}}

JSONのみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=2000)

    def optimize_profile(self, current_bio: str, platform: str, niche: str) -> dict:
        """Optimize SNS profile for discoverability."""
        prompt = f"""以下のSNSプロフィールをSEO最適化してください。

## 現在のプロフィール
{current_bio}

## プラットフォーム: {platform}
## ジャンル: {niche}

## 出力（JSON）
{{
    "optimized_bio": "最適化されたプロフィール文",
    "display_name_suggestion": "表示名の提案",
    "keywords_included": ["含めたキーワード"],
    "cta": "プロフィールCTA",
    "link_strategy": "リンク戦略",
    "highlights_categories": ["ハイライトカテゴリ（Instagram向け）"],
    "tips": ["追加の最適化ヒント"]
}}

JSONのみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=1500)

    def keyword_research(self, seed_keywords: list[str], platform: str) -> list[dict]:
        """Research and expand keywords for content planning."""
        prompt = f"""以下のシードキーワードから{platform}向けのキーワードリサーチを行ってください。

## シードキーワード: {", ".join(seed_keywords)}

## 出力（JSON配列）
[
    {{
        "keyword": "キーワード",
        "estimated_volume": "high/medium/low",
        "competition": "high/medium/low",
        "opportunity_score": 0.0-1.0,
        "content_ideas": ["このキーワードを使ったコンテンツアイデア"],
        "related_keywords": ["関連キーワード"],
        "search_intent": "informational/transactional/navigational"
    }}
]

20個以上のキーワードを含めてください。JSON配列のみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=3000)
