"""Automatic affiliate link insertion for SNS posts."""

import logging
import random

from sqlalchemy import select
from src.core.database import async_session
from src.core.models import AffiliateProduct

logger = logging.getLogger(__name__)

# Product keyword → affiliate category mapping
KEYWORD_CATEGORY_MAP = {
    # Skincare
    "化粧水": "skincare",
    "美容液": "skincare",
    "スキンケア": "skincare",
    "保湿": "skincare",
    "毛穴": "skincare",
    "クレンジング": "skincare",
    # Makeup
    "リップ": "makeup",
    "ファンデ": "makeup",
    "アイシャドウ": "makeup",
    "メイク": "makeup",
    "コスメ": "makeup",
    "マスカラ": "makeup",
    # Body care
    "ボディ": "bodycare",
    "バスト": "bodycare",
    "下着": "lingerie",
    "ランジェリー": "lingerie",
    "ナイトブラ": "lingerie",
}


class AffiliateLinker:
    """Automatically insert relevant affiliate links into post text."""

    async def find_matching_products(self, text: str, limit: int = 3) -> list[dict]:
        """Find affiliate products that match the post content."""
        # Extract matching categories from text
        matched_categories = set()
        text_lower = text.lower()
        for keyword, category in KEYWORD_CATEGORY_MAP.items():
            if keyword.lower() in text_lower or keyword in text:
                matched_categories.add(category)

        if not matched_categories:
            return []

        async with async_session() as session:
            result = await session.execute(
                select(AffiliateProduct).where(
                    AffiliateProduct.is_active.is_(True),
                )
            )
            products = result.scalars().all()

        # Filter by category and randomize
        matching = []
        for product in products:
            product_category = (product.category or "").lower()
            if product_category in matched_categories or not product_category:
                matching.append({
                    "name": product.product_name,
                    "url": product.affiliate_link,
                    "network": product.network,
                    "category": product.category,
                })

        random.shuffle(matching)
        return matching[:limit]

    def insert_affiliate_link(
        self,
        text: str,
        product_name: str,
        affiliate_url: str,
        platform: str,
    ) -> str:
        """Insert an affiliate link into post text in a natural way."""
        # Platform-specific insertion
        if platform == "x":
            # X: Short, append at end
            return f"{text}\n\n👉 {product_name}はここから♡\n{affiliate_url}"
        elif platform == "instagram":
            # IG: Can't click links in captions, direct to profile
            return f"{text}\n\n🔗 {product_name}のリンクはプロフのURLから♡"
        elif platform == "youtube":
            # YouTube: Description link
            return f"{text}\n\n📌 {product_name}はこちら → {affiliate_url}"
        elif platform == "tiktok":
            # TikTok: Profile link
            return f"{text}\n\n🔗 {product_name}はプロフのリンクから♡"

        return text

    async def auto_insert(self, text: str, platform: str) -> tuple[str, list[dict]]:
        """Automatically find matching products and insert links.

        Returns:
            tuple: (modified_text, list of inserted products)
        """
        products = await self.find_matching_products(text, limit=1)

        if not products:
            return text, []

        product = products[0]
        modified = self.insert_affiliate_link(
            text=text,
            product_name=product["name"],
            affiliate_url=product["url"],
            platform=platform,
        )

        return modified, products


# Global singleton
affiliate_linker = AffiliateLinker()
