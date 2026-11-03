"""Cross-platform promotion utilities."""

import logging
import random
import threading
from collections import deque
from datetime import datetime, timezone
from config.settings import settings

logger = logging.getLogger(__name__)

# Platform handle references for cross-promotion
PLATFORM_HANDLES = {
    "x": "",        # Set from settings or config
    "instagram": "",
    "tiktok": "",
    "youtube": "",
}


def generate_cross_promo_cta(source_platform: str, target_platform: str, content_url: str = "") -> str:
    """Generate a cross-promotion CTA for appending to posts."""
    ctas = {
        ("x", "youtube"): f"▶️ フル動画はYouTubeで → {content_url}" if content_url else "▶️ YouTubeもフォローしてね♡",
        ("x", "instagram"): "📸 Instagramでもっと見てね♡",
        ("instagram", "youtube"): f"▶️ YouTube で全編公開中！ {content_url}" if content_url else "▶️ YouTubeもチェックしてね♡",
        ("instagram", "x"): "🐦 X (Twitter) でも毎日更新中♡",
        ("tiktok", "youtube"): f"▶️ フルバージョンはYouTubeで！{content_url}" if content_url else "▶️ YouTubeも見てね♡",
        ("tiktok", "instagram"): "📸 Instagramもフォローしてね♡",
        ("youtube", "instagram"): "📸 毎日の投稿は Instagram でチェック♡",
        ("youtube", "x"): "🐦 最新情報は X (Twitter) で♡",
    }

    return ctas.get((source_platform, target_platform), "")


def get_paywall_url() -> str:
    """Get the best available paywall URL from settings."""
    return settings.fanbox_url or settings.fantia_url or settings.patreon_url or ""


def get_all_platform_links() -> dict[str, str]:
    """Get all configured platform profile links."""
    links = {}
    if settings.fanbox_url:
        links["fanbox"] = settings.fanbox_url
    if settings.fantia_url:
        links["fantia"] = settings.fantia_url
    if settings.youtube_membership_url:
        links["youtube_membership"] = settings.youtube_membership_url
    return links


# ---------------------------------------------------------------------------
# Content-type to platform affinity mapping.
# Keys are content type slugs; values are ordered lists of (target, reason).
# ---------------------------------------------------------------------------
_CONTENT_TYPE_TARGETS: dict[str, list[tuple[str, str]]] = {
    "beauty_review": [
        ("youtube", "full_video"),
        ("instagram", "carousel"),
    ],
    "review": [
        ("youtube", "full_video"),
        ("instagram", "carousel"),
    ],
    "tutorial": [
        ("youtube", "full_video"),
        ("tiktok", "short_clip"),
    ],
    "skincare": [
        ("youtube", "full_video"),
        ("instagram", "carousel"),
    ],
    "makeup": [
        ("tiktok", "short_clip"),
        ("youtube", "full_video"),
    ],
    "vlog": [
        ("youtube", "full_video"),
        ("tiktok", "short_clip"),
    ],
    "lifestyle": [
        ("instagram", "carousel"),
        ("youtube", "full_video"),
    ],
    "meme": [
        ("tiktok", "short_clip"),
        ("instagram", "reel"),
    ],
    "announcement": [
        ("instagram", "story"),
        ("youtube", "community_post"),
    ],
}

# Fallback priority when content type is unknown.
_DEFAULT_TARGET_PRIORITY: list[str] = ["youtube", "instagram", "tiktok", "fanbox"]


# ---------------------------------------------------------------------------
# Cross-promo rotation tracker (in-memory singleton, mirrors FatigueTracker
# pattern from src/core/fatigue_tracker.py).
# ---------------------------------------------------------------------------

class _PromoRotationTracker:
    """Track recently promoted target platforms per source to avoid fatigue."""

    def __init__(self, history_per_source: int = 10):
        self._lock = threading.Lock()
        # source_platform -> deque of (target_platform, datetime)
        self._history: dict[str, deque[tuple[str, datetime]]] = {}
        self._history_per_source = history_per_source

    def record(self, source_platform: str, target_platform: str) -> None:
        with self._lock:
            if source_platform not in self._history:
                self._history[source_platform] = deque(maxlen=self._history_per_source)
            self._history[source_platform].append((target_platform, datetime.now(timezone.utc)))

    def get_recent_targets(self, source_platform: str, last_n: int = 3) -> list[str]:
        """Return the last N promoted targets for the given source platform."""
        with self._lock:
            history = self._history.get(source_platform, deque())
            entries = list(history)[-last_n:]
            return [target for target, _ in entries]

    def least_recent_target(self, source_platform: str, candidates: list[str]) -> str | None:
        """Pick the candidate that has been promoted least recently (or never)."""
        with self._lock:
            history = self._history.get(source_platform, deque())
            # Build position map: target -> most recent index (higher = more recent)
            last_seen: dict[str, int] = {}
            for idx, (target, _) in enumerate(history):
                last_seen[target] = idx

            # Sort by last seen ascending (never seen = -1, oldest = 0)
            return min(candidates, key=lambda t: last_seen.get(t, -1))


_rotation_tracker = _PromoRotationTracker()


# ---------------------------------------------------------------------------
# Platform-specific CTA variants for A/B testing.
# Each key is (source, target); value is a list of CTA strings.
# ---------------------------------------------------------------------------
_CTA_VARIANTS: dict[tuple[str, str], list[str]] = {
    ("x", "youtube"): [
        "▶️ フル動画はYouTubeで公開中♡",
        "▶️ YouTubeでもっと詳しく見てね✨",
        "▶️ 続きはYouTubeで！チャンネル登録お願いします♡",
        "▶️ フルバージョンはYouTubeにあるよ〜！",
    ],
    ("x", "instagram"): [
        "📸 Instagramでもっと見てね♡",
        "📸 Instagramに写真たくさんあるよ！フォローしてね♡",
        "📸 インスタもチェックしてみて✨",
        "📸 きれいな写真はInstagramで♡",
    ],
    ("x", "fanbox"): [
        "🔒 限定コンテンツはFANBOXで♡",
        "🔒 もっと見たい方はFANBOXへ✨",
        "🔒 FANBOXで全部見られるよ♡ 支援よろしくね！",
        "🔒 続きはFANBOXの限定投稿で！",
    ],
    ("x", "fantia"): [
        "🔒 限定写真はFantiaで公開中♡",
        "🔒 Fantiaでフルコンテンツ見てね✨",
        "🔒 もっと見たい方はFantiaへどうぞ♡",
    ],
    ("x", "tiktok"): [
        "🎵 TikTokでも動画あげてるよ♡",
        "🎵 TikTokをフォローしてね✨",
        "🎵 もっと動画はTikTokで！",
    ],
    ("instagram", "youtube"): [
        "▶️ YouTube で全編公開中！チャンネル登録してね♡",
        "▶️ YouTubeで詳しい解説動画あるよ✨",
        "▶️ フルバージョンはYouTubeで公開してます♡",
        "▶️ YouTubeもチェックしてね！",
    ],
    ("instagram", "x"): [
        "🐦 X (Twitter) でも毎日更新中♡",
        "🐦 Xでもつぶやいてるよ！フォローしてね♡",
        "🐦 最新情報はXでチェック✨",
        "🐦 X でもフォローよろしく♡",
    ],
    ("instagram", "tiktok"): [
        "🎵 TikTokでも動画あげてるよ！見てね♡",
        "🎵 TikTokをフォローしてね✨",
        "🎵 もっと動画はTikTokで！",
    ],
    ("instagram", "fanbox"): [
        "🔒 もっと見たい方はFANBOXへ♡",
        "🔒 限定コンテンツはFANBOXで公開中✨",
        "🔒 FANBOXで支援してくれると嬉しいな♡",
    ],
    ("tiktok", "youtube"): [
        "▶️ フルバージョンはYouTubeで！チャンネル登録お願いします♡",
        "▶️ YouTubeで長尺バージョン公開中✨",
        "▶️ もっと見たい方はYouTubeへ♡",
        "▶️ YouTubeに全部入ってるよ！見てね♡",
    ],
    ("tiktok", "instagram"): [
        "📸 Instagramもフォローしてね♡",
        "📸 写真はInstagramにたくさんあるよ✨",
        "📸 インスタでもっと見てね♡",
    ],
    ("tiktok", "fanbox"): [
        "🔒 限定動画はFANBOXで♡",
        "🔒 もっと見たい方はFANBOXへ✨",
        "🔒 FANBOXで限定コンテンツ公開中♡",
    ],
    ("youtube", "instagram"): [
        "📸 毎日の投稿は Instagram でチェック♡",
        "📸 Instagramもフォローしてね✨",
        "📸 インスタでも発信中♡",
        "📸 写真はInstagramにいっぱいあるよ！フォローしてね♡",
    ],
    ("youtube", "x"): [
        "🐦 最新情報は X (Twitter) で♡",
        "🐦 XでもフォローしてくれたらHappy♡",
        "🐦 Xでも毎日つぶやいてます✨",
        "🐦 X でフォローよろしくね♡",
    ],
    ("youtube", "tiktok"): [
        "🎵 TikTokでショート動画あげてるよ！見てね♡",
        "🎵 TikTokをフォローしてね✨",
        "🎵 TikTokにクリップもあるよ♡",
    ],
    ("youtube", "fanbox"): [
        "🔒 限定コンテンツはFANBOXで♡ 支援よろしくね！",
        "🔒 もっと見たい方はFANBOXへ✨",
        "🔒 FANBOXで限定動画公開中♡",
    ],
}

# Platforms that are paywall services rather than social networks.
_PAYWALL_PLATFORMS = {"fanbox", "fantia", "patreon"}


def select_best_cross_promo(
    source_platform: str,
    post_content_type: str,
    *,
    record: bool = True,
) -> dict[str, str]:
    """Pick the best target platform and promotion reason for a given post.

    Strategy:
    - Beauty reviews / tutorials → YouTube full version or Instagram carousel.
    - Falls back to least-recently-promoted platform to prevent repetition.

    Args:
        source_platform: Platform the post is being published on.
        post_content_type: Slug describing the content (e.g. "review",
            "tutorial", "lifestyle").  Case-insensitive.
        record: Whether to record this selection in the rotation tracker.

    Returns:
        dict with keys ``target``, ``reason``, and ``cta``.
    """
    content_key = post_content_type.lower().strip()

    # Find candidates from the content-type map.
    candidates: list[tuple[str, str]] = _CONTENT_TYPE_TARGETS.get(content_key, [])

    # Filter out the source platform itself and unavailable paywall URLs.
    def _is_available(target: str) -> bool:
        if target == source_platform:
            return False
        if target == "fanbox" and not settings.fanbox_url:
            return False
        if target == "fantia" and not settings.fantia_url:
            return False
        if target == "patreon" and not settings.patreon_url:
            return False
        return True

    filtered = [(t, r) for t, r in candidates if _is_available(t)]

    if filtered:
        # Among valid candidates, prefer the one promoted least recently.
        candidate_targets = [t for t, _ in filtered]
        best_target = _rotation_tracker.least_recent_target(source_platform, candidate_targets)
        reason = next(r for t, r in filtered if t == best_target)
    else:
        # Fall back: pick from default priority, excluding source & unavailable.
        fallback_candidates = [t for t in _DEFAULT_TARGET_PRIORITY if _is_available(t)]
        if not fallback_candidates:
            logger.warning(
                "select_best_cross_promo: no valid target found for source=%s type=%s",
                source_platform,
                post_content_type,
            )
            return {"target": "", "reason": "", "cta": ""}
        best_target = _rotation_tracker.least_recent_target(source_platform, fallback_candidates)
        reason = "general"

    if record:
        _rotation_tracker.record(source_platform, best_target)

    cta = get_cta_variant(source_platform, best_target)
    logger.debug(
        "select_best_cross_promo: source=%s type=%s → target=%s reason=%s",
        source_platform,
        post_content_type,
        best_target,
        reason,
    )
    return {"target": best_target, "reason": reason, "cta": cta}


def get_cta_variant(source_platform: str, target_platform: str, *, seed: int | None = None) -> str:
    """Return a randomly chosen CTA for the given platform pair.

    Provides multiple variants per pair for A/B testing.  Falls back to the
    legacy ``generate_cross_promo_cta`` string when no variants are defined.

    Args:
        source_platform: Platform the post is published on.
        target_platform: Platform being promoted.
        seed: Optional random seed for deterministic selection in tests.

    Returns:
        A CTA string, or empty string if none available.
    """
    variants = _CTA_VARIANTS.get((source_platform, target_platform))
    if not variants:
        return generate_cross_promo_cta(source_platform, target_platform)

    rng = random.Random(seed)
    return rng.choice(variants)


def get_all_cta_variants(source_platform: str, target_platform: str) -> list[str]:
    """Return all CTA variants for a platform pair (for A/B test setup).

    Returns:
        List of CTA strings.  Empty list if no variants defined.
    """
    return list(_CTA_VARIANTS.get((source_platform, target_platform), []))


# ---------------------------------------------------------------------------
# Viral content repurposer
# ---------------------------------------------------------------------------

# Maps original platform → list of (destination_platform, format, instruction)
_REPURPOSE_MAP: dict[str, list[tuple[str, str, str]]] = {
    "x": [
        ("instagram", "carousel", "ツイートの内容を複数枚のカルーセル画像に展開。各スライドに1つのポイントを配置。"),
        ("instagram", "quote_graphic", "インパクトのある文章を引用グラフィックとしてデザイン。"),
        ("tiktok", "talking_head", "ツイートの内容をそのままトーキングヘッド動画のスクリプトに。"),
        ("youtube", "short", "ツイートの内容を60秒以内のYouTube Shortsに。"),
    ],
    "instagram": [
        ("x", "thread", "投稿のキャプションをXのスレッド形式に分割。各ツイートは280文字以内。"),
        ("tiktok", "reel_clip", "リールをTikTok向けにリポスト（縦型・音楽追加推奨）。"),
        ("youtube", "short", "リールをYouTube Shortsとしてアップロード。"),
        ("youtube", "full_video", "カルーセルや解説投稿をYouTubeの詳細解説動画に拡張。"),
    ],
    "tiktok": [
        ("instagram", "reel", "TikTok動画をInstagramリールとして再投稿（ウォーターマーク除去推奨）。"),
        ("youtube", "short", "TikTok動画をYouTube Shortsとしてアップロード。"),
        ("x", "video_tweet", "TikTok動画をXに動画ツイートとして投稿。"),
        ("youtube", "full_video", "TikTok動画シリーズをまとめてYouTubeのロング動画に。"),
    ],
    "youtube": [
        ("tiktok", "clips", "動画のハイライトを15〜60秒のクリップに切り出してTikTokへ。"),
        ("instagram", "reel", "ベストシーンをInstagramリール（最大90秒）として再編集。"),
        ("instagram", "carousel", "動画の要点をスライド形式のカルーセル投稿に変換。"),
        ("x", "thread", "動画の内容をXのスレッドで要約・解説。タイムスタンプリンク付き。"),
    ],
}

# Engagement thresholds for repurpose recommendation strength.
_REPURPOSE_THRESHOLD_HIGH = 0.08   # 8%+ → strongly recommend
_REPURPOSE_THRESHOLD_MED = 0.04    # 4%+ → recommend


def generate_repurpose_plan(
    original_platform: str,
    post_text: str,
    engagement_rate: float,
) -> dict:
    """Suggest how to repurpose a high-performing post across other platforms.

    Args:
        original_platform: Platform where the post originally performed well.
        post_text: Text content of the original post (used for context hints).
        engagement_rate: Engagement rate as a float (e.g. 0.07 = 7%).

    Returns:
        dict with keys:
            - ``should_repurpose`` (bool)
            - ``priority`` (str: "high" | "medium" | "low")
            - ``engagement_rate_pct`` (float, percentage)
            - ``suggestions`` (list of dicts with keys ``destination``,
              ``format``, ``instruction``, ``cta``)
            - ``notes`` (str)
    """
    if engagement_rate >= _REPURPOSE_THRESHOLD_HIGH:
        priority = "high"
        should_repurpose = True
        notes = f"エンゲージメント率 {engagement_rate:.1%} — バイラル級。全プラットフォームへの展開を強く推奨。"
    elif engagement_rate >= _REPURPOSE_THRESHOLD_MED:
        priority = "medium"
        should_repurpose = True
        notes = f"エンゲージメント率 {engagement_rate:.1%} — 好調。主要プラットフォームへの展開を推奨。"
    else:
        priority = "low"
        should_repurpose = False
        notes = f"エンゲージメント率 {engagement_rate:.1%} — 通常レベル。改善後に再投稿を検討。"

    destinations = _REPURPOSE_MAP.get(original_platform, [])

    # For medium priority, limit to top 2 suggestions.
    if priority == "medium":
        destinations = destinations[:2]

    suggestions = []
    for dest_platform, fmt, instruction in destinations:
        cta = get_cta_variant(original_platform, dest_platform)
        suggestions.append({
            "destination": dest_platform,
            "format": fmt,
            "instruction": instruction,
            "cross_promo_cta": cta,
        })

    # Add a content-specific hint when the post text contains relevant keywords.
    content_hint = ""
    lower_text = post_text.lower()
    if any(kw in lower_text for kw in ["コスメ", "スキンケア", "美容", "レビュー", "おすすめ"]):
        content_hint = "美容・コスメ系コンテンツ — 商品名・ブランド名を各プラットフォームの投稿に明記するとSEO効果UP。"

    return {
        "should_repurpose": should_repurpose,
        "priority": priority,
        "engagement_rate_pct": round(engagement_rate * 100, 2),
        "suggestions": suggestions,
        "notes": notes,
        "content_hint": content_hint,
    }


# ---------------------------------------------------------------------------
# Link-in-bio optimizer
# ---------------------------------------------------------------------------

# Priority order of links per platform.
# Entries are (link_key, label) where link_key maps to a getter.
_LINK_PRIORITY: dict[str, list[tuple[str, str]]] = {
    "instagram": [
        ("fanbox", "FANBOX（限定コンテンツ）"),
        ("fantia", "Fantia（限定コンテンツ）"),
        ("youtube_membership", "YouTube メンバーシップ"),
        ("youtube", "YouTube チャンネル"),
        ("patreon", "Patreon"),
    ],
    "tiktok": [
        ("youtube", "YouTube チャンネル"),
        ("fanbox", "FANBOX"),
        ("fantia", "Fantia"),
        ("instagram", "Instagram"),
    ],
    "x": [
        ("fanbox", "FANBOX（限定コンテンツ）"),
        ("fantia", "Fantia（限定コンテンツ）"),
        ("youtube_membership", "YouTube メンバーシップ"),
        ("instagram", "Instagram"),
        ("youtube", "YouTube"),
        ("patreon", "Patreon"),
    ],
    "youtube": [
        ("fanbox", "FANBOX（限定コンテンツ）"),
        ("fantia", "Fantia"),
        ("instagram", "Instagram"),
        ("patreon", "Patreon"),
    ],
}


def _get_link_url(link_key: str) -> str:
    """Resolve a link key to a URL from settings."""
    mapping = {
        "fanbox": settings.fanbox_url,
        "fantia": settings.fantia_url,
        "patreon": settings.patreon_url,
        "youtube_membership": settings.youtube_membership_url,
        # Social profile URLs are not stored in settings; return empty.
        "youtube": "",
        "instagram": "",
        "tiktok": "",
        "x": "",
    }
    return mapping.get(link_key, "")


def get_optimal_link_in_bio(platform: str) -> dict:
    """Return the optimal primary link and a ranked link list for a platform.

    The primary link is the highest-priority configured URL.  This is suited
    for platforms that allow only one clickable link (e.g. Instagram bio).
    All available links are returned for use in a Linktree-style page.

    Args:
        platform: Platform slug ("instagram", "tiktok", "x", "youtube").

    Returns:
        dict with keys:
            - ``primary_link`` (str): Best single URL, or empty string.
            - ``primary_label`` (str): Human-readable label for the primary link.
            - ``all_links`` (list[dict]): Ordered list of ``{url, label}`` dicts
              for all configured links.
            - ``recommendation`` (str): Short rationale string.
    """
    priority_list = _LINK_PRIORITY.get(platform, [])

    all_links: list[dict[str, str]] = []
    primary_url = ""
    primary_label = ""

    for link_key, label in priority_list:
        url = _get_link_url(link_key)
        if url:
            all_links.append({"url": url, "label": label})
            if not primary_url:
                primary_url = url
                primary_label = label

    if not all_links:
        recommendation = "設定されているURLがありません。.envにFANBOX_URLなどを追加してください。"
    elif primary_label and ("FANBOX" in primary_label or "Fantia" in primary_label):
        recommendation = "収益化リンクを最優先に設定しています。フォロワーを有料コンテンツへ誘導します。"
    else:
        recommendation = "最も優先度の高い設定済みリンクを先頭にしています。"

    logger.debug(
        "get_optimal_link_in_bio: platform=%s primary=%s total_links=%d",
        platform,
        primary_url,
        len(all_links),
    )
    return {
        "primary_link": primary_url,
        "primary_label": primary_label,
        "all_links": all_links,
        "recommendation": recommendation,
    }
