"""Discord Bot for Kie.ai image/video generation + SNS management.

Slash commands:
  === 画像・動画 ===
  /image <prompt>        - Generate an image
  /character <scene>     - Generate persona image (NO_FACE)
  /video <prompt>        - Generate a video
  /preset                - Generate from preset prompts (dropdown)
  /batch <prompt>        - Generate multiple images at once
  /variation <prompt>    - Generate 3 variations of the same prompt
  /extend <task_id>      - Extend an existing video
  /upscale <prompt>      - Generate high-res (4K) image

  === SNS投稿 ===
  /post <platform>       - Post last generated media to SNS
  /caption <topic>       - AI-generate caption + hashtags
  /schedule <time>       - Schedule a post from Discord

  === 管理・分析 ===
  /status                - API usage, budget, post count
  /history               - Recent generation/post history
  /cost                  - Cost breakdown

  === AI ===
  /ask <question>        - Ask Claude anything
  /trend                 - Trend research
  /translate <text>      - JP→EN prompt translation

  === ワークフロー ===
  /gallery               - Review recent images (adopt/reject/fav)
  /queue                 - View/cancel generation queue
  /help_gen              - Show all commands

Also supports ! prefix commands for all of the above.

Usage:
  python -m src.discord_bot
"""

import asyncio
import io
import logging
import os
import random
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone, timedelta

# Fix: Load .env with override BEFORE importing settings
# (shell may have empty ANTHROPIC_API_KEY that shadows .env)
from dotenv import load_dotenv
load_dotenv(override=True)

import anthropic
import discord
import httpx
from discord import app_commands
from discord.ext import commands

from config.settings import settings

from config.media_prompts import CHARACTER_BASE, NO_FACE, IMAGE_PROMPTS
from src.ai.image_generator import ImageGenerator
from src.ai.video_generator import KieVideoGenerator
from src.core.cost_tracker import cost_tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# --- Permission checks ---

def is_operator():
    """Check if user has admin or operator role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild:
            return False
        member = interaction.guild.get_member(interaction.user.id)
        if not member:
            return False
        role_names = {r.name.lower() for r in member.roles}
        return bool(role_names & {"admin", "operator", "管理者"})
    return app_commands.check(predicate)


# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Generators
image_gen = ImageGenerator()
video_gen = KieVideoGenerator()

# Async Anthropic client (non-blocking for Discord event loop)
_async_claude: anthropic.AsyncAnthropic | None = None


def get_async_claude() -> anthropic.AsyncAnthropic:
    """Get or create the async Anthropic client."""
    global _async_claude
    if _async_claude is None:
        _async_claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _async_claude


# Lazy-loaded heavy modules
_claude_gen = None
_trend_researcher = None
_platform_manager = None


def get_claude_gen():
    global _claude_gen
    if _claude_gen is None:
        from src.ai.claude_generator import ClaudeContentGenerator
        _claude_gen = ClaudeContentGenerator()
    return _claude_gen


def get_trend_researcher():
    global _trend_researcher
    if _trend_researcher is None:
        from src.ai.trend_researcher import TrendResearcher
        _trend_researcher = TrendResearcher()
    return _trend_researcher


def get_platform_manager():
    global _platform_manager
    if _platform_manager is None:
        from src.platforms.manager import PlatformManager
        _platform_manager = PlatformManager()
        _platform_manager.register_all()
    return _platform_manager


async def run_sync(func, *args, **kwargs):
    """Run a synchronous function in a thread to avoid blocking the event loop."""
    import functools
    partial = functools.partial(func, *args, **kwargs)
    return await asyncio.get_event_loop().run_in_executor(None, partial)


# --- Session state ---
# Per-channel last generated media (for /post) — capped at 200 entries
_channel_last_media: OrderedDict[int, dict] = OrderedDict()
# Gallery items per user — capped at 200 users
_gallery_items: OrderedDict[int, list[dict]] = OrderedDict()

_MEDIA_CACHE_MAX = 200
# Generation queue
_generation_queue: deque[dict] = deque(maxlen=50)
_active_tasks: dict[str, dict] = {}  # task_id -> {"user", "type", "prompt", "started_at"}


# --- Presets ---
PRESETS = {
    "female_beauty": {"label": "💄 コスメ・美容", "desc": "メイク・スキンケア"},
    "routine": {"label": "🌅 ルーティン", "desc": "朝のスキンケア・バスタイム"},
    "review": {"label": "📦 レビュー", "desc": "コスメレビュー・スウォッチ"},
    "monetization": {"label": "🛍️ おすすめ紹介", "desc": "月間ベスト・購入品"},
    "engagement": {"label": "💬 アンケート", "desc": "どっち派？・質問カード"},
}

# --- Ratio choices ---
RATIO_CHOICES = [
    app_commands.Choice(name="正方形 (1:1)", value="1:1"),
    app_commands.Choice(name="横長 (16:9)", value="16:9"),
    app_commands.Choice(name="縦長 (9:16)", value="9:16"),
    app_commands.Choice(name="Instagram (4:5)", value="4:5"),
]

RATIO_ALIASES = {
    "square": "1:1", "1:1": "1:1",
    "wide": "16:9", "16:9": "16:9", "landscape": "16:9",
    "tall": "9:16", "9:16": "9:16", "portrait": "9:16", "story": "9:16",
    "4:5": "4:5", "insta": "4:5",
}

PLATFORM_CHOICES = [
    app_commands.Choice(name="X (Twitter)", value="x"),
    app_commands.Choice(name="Instagram", value="instagram"),
    app_commands.Choice(name="TikTok", value="tiktok"),
    app_commands.Choice(name="YouTube", value="youtube"),
]


# --- Helpers ---
# SSRF guard for URLs returned by upstream services (Kie.ai, etc.). Mirrors the
# blocklist in src/ai/image_generator.py so all inbound URLs get the same
# treatment.
_BLOCKED_DOWNLOAD_HOSTS = (
    "169.254.169.254",
    "metadata.google.internal",
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
)


def _validate_download_url(url: str) -> None:
    """Raise ValueError if the URL targets an internal/metadata host."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Blocked download URL scheme: {parsed.scheme!r}")
    if parsed.hostname and parsed.hostname.lower() in _BLOCKED_DOWNLOAD_HOSTS:
        raise ValueError(f"Blocked download URL host: {parsed.hostname}")


async def download_and_send(interaction_or_ctx, url: str, filename: str, content: str):
    """Download media from URL and send to Discord."""
    _validate_download_url(url)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content

    size_mb = len(data) / (1024 * 1024)
    file = discord.File(io.BytesIO(data), filename=filename)

    if size_mb >= 25:
        msg = f"{content}\n\n⚠️ ファイルが大きすぎるため直接URLを表示:\n{url}"
        if isinstance(interaction_or_ctx, discord.Interaction):
            await interaction_or_ctx.followup.send(content=msg)
        else:
            await interaction_or_ctx.send(content=msg)
        return

    if isinstance(interaction_or_ctx, discord.Interaction):
        await interaction_or_ctx.followup.send(content=content, file=file)
    else:
        await interaction_or_ctx.send(content=content, file=file)


async def download_media_to_temp(url: str, suffix: str = ".png") -> str:
    """Download media to a temp file and return the path."""
    _validate_download_url(url)
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.close()
    return tmp.name


def parse_prefix_options(args: str) -> tuple[str, str]:
    """Extract --ratio from prefix command args."""
    ratio = "1:1"
    parts = args.split()
    clean = []
    i = 0
    while i < len(parts):
        if parts[i] in ("--ratio", "-r") and i + 1 < len(parts):
            ratio = RATIO_ALIASES.get(parts[i + 1], parts[i + 1])
            i += 2
        else:
            clean.append(parts[i])
            i += 1
    return " ".join(clean), ratio


def store_last_media(channel_id: int, url: str, media_type: str, prompt: str, ratio: str = "1:1"):
    """Store last generated media for the channel (used by /post)."""
    _channel_last_media[channel_id] = {
        "url": url,
        "type": media_type,
        "prompt": prompt,
        "ratio": ratio,
        "created_at": time.time(),
    }
    while len(_channel_last_media) > _MEDIA_CACHE_MAX:
        _channel_last_media.popitem(last=False)


def _gallery_append(user_id: int, item: dict) -> None:
    """Append an item to a user's gallery, capping total users at 200."""
    if user_id not in _gallery_items:
        _gallery_items[user_id] = []
        while len(_gallery_items) > _MEDIA_CACHE_MAX:
            _gallery_items.popitem(last=False)
    _gallery_items[user_id].append(item)


def format_cost(usd: float) -> str:
    """Format USD cost nicely."""
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}"


# ========================================
# Bot Events
# ========================================

@bot.event
async def on_ready():
    logger.info("Bot logged in as %s (ID: %s)", bot.user.name, bot.user.id)
    logger.info("Connected to %d guilds", len(bot.guilds))

    # Initialize database tables
    try:
        from src.core.database import init_db
        await init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error("Database init failed: %s", e)

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %d slash commands", len(synced))
    except Exception as e:
        logger.error("Failed to sync slash commands: %s", e)


# ========================================
# 1. /image — 画像生成
# ========================================

@is_operator()
@bot.tree.command(name="image", description="🎨 プロンプトから画像を生成")
@app_commands.describe(prompt="画像の説明（英語推奨）", ratio="アスペクト比")
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_image(interaction: discord.Interaction, prompt: str, ratio: app_commands.Choice[str] = None):
    aspect = ratio.value if ratio else "1:1"
    await interaction.response.defer(thinking=True)
    try:
        urls = await image_gen.generate(prompt=prompt, aspect_ratio=aspect)
        if not urls:
            await interaction.followup.send("❌ 画像生成に失敗しました")
            return
        store_last_media(interaction.channel_id, urls[0], "image", prompt, aspect)
        _gallery_append(interaction.user.id, {"url": urls[0], "prompt": prompt, "status": "pending"})
        filename = f"kie_{uuid.uuid4().hex[:8]}.png"
        await download_and_send(
            interaction, urls[0], filename,
            f"✅ **画像生成完了!**\n📝 `{prompt[:100]}`\n📐 {aspect}"
        )
    except Exception as e:
        logger.error("Slash image failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 2. /character — ペルソナ画像
# ========================================

@is_operator()
@bot.tree.command(name="character", description="🎨 ペルソナ画像を生成（NO_FACE自動適用）")
@app_commands.describe(scene="シーンの説明（例: pink dress, bedroom）", ratio="アスペクト比")
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_character(interaction: discord.Interaction, scene: str = "", ratio: app_commands.Choice[str] = None):
    aspect = ratio.value if ratio else "1:1"
    scene_text = scene if scene else "cozy knit sweater, doing skincare at a bright vanity"
    full_prompt = f"{CHARACTER_BASE}, {scene_text}. {NO_FACE}"
    await interaction.response.defer(thinking=True)
    try:
        urls = await image_gen.generate(prompt=full_prompt, aspect_ratio=aspect)
        if not urls:
            await interaction.followup.send("❌ 画像生成に失敗しました")
            return
        store_last_media(interaction.channel_id, urls[0], "image", full_prompt, aspect)
        _gallery_items[interaction.user.id].append({"url": urls[0], "prompt": scene_text, "status": "pending"})
        filename = f"char_{uuid.uuid4().hex[:8]}.png"
        await download_and_send(
            interaction, urls[0], filename,
            f"✅ **キャラクター画像完了!**\n📝 `{scene_text[:100]}`\n📐 {aspect} | 🚫 NO_FACE"
        )
    except Exception as e:
        logger.error("Slash character failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 3. /video — 動画生成
# ========================================

@is_operator()
@bot.tree.command(name="video", description="🎬 プロンプトから動画を生成（1〜5分かかります）")
@app_commands.describe(prompt="動画の説明（英語推奨）", ratio="アスペクト比")
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_video(interaction: discord.Interaction, prompt: str, ratio: app_commands.Choice[str] = None):
    aspect = ratio.value if ratio else "9:16"
    await interaction.response.defer(thinking=True)
    task_id = f"video_{uuid.uuid4().hex[:8]}"
    _active_tasks[task_id] = {"user": interaction.user.name, "type": "video", "prompt": prompt[:50], "started_at": time.time()}
    try:
        result = await video_gen.generate_video(prompt=prompt, aspect_ratio=aspect)
        if not result.video_url:
            await interaction.followup.send("❌ 動画生成に失敗しました")
            return
        store_last_media(interaction.channel_id, result.video_url, "video", prompt, aspect)
        filename = f"kie_{uuid.uuid4().hex[:8]}.mp4"
        await download_and_send(
            interaction, result.video_url, filename,
            f"✅ **動画生成完了!**\n📝 `{prompt[:100]}`\n📐 {aspect} | ⏱️ {result.duration_seconds}s\n🆔 `{result.generation_id}`"
        )
    except Exception as e:
        logger.error("Slash video failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")
    finally:
        _active_tasks.pop(task_id, None)


# ========================================
# 4. /preset — プリセット (ドロップダウン)
# ========================================

class PresetSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=info["label"], value=key, description=info["desc"])
            for key, info in PRESETS.items()
        ]
        super().__init__(placeholder="カテゴリを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        category = self.values[0]
        prompts = IMAGE_PROMPTS.get(category, [])
        if not prompts:
            await interaction.response.send_message(f"❌ `{category}` のプロンプトが見つかりません", ephemeral=True)
            return
        prompt = random.choice(prompts)
        info = PRESETS.get(category, {})
        await interaction.response.defer(thinking=True)
        try:
            urls = await image_gen.generate(prompt=prompt, aspect_ratio="1:1")
            if not urls:
                await interaction.followup.send("❌ 画像生成に失敗しました")
                return
            store_last_media(interaction.channel_id, urls[0], "image", prompt, "1:1")
            _gallery_items[interaction.user.id].append({"url": urls[0], "prompt": prompt[:50], "status": "pending"})
            filename = f"preset_{category}_{uuid.uuid4().hex[:6]}.png"
            await download_and_send(
                interaction, urls[0], filename,
                f"✅ **{info.get('label', category)} 画像完了!**\n📂 プリセット: `{category}`"
            )
        except Exception as e:
            logger.error("Preset generation failed: %s", e)
            await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


class PresetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(PresetSelect())


@is_operator()
@bot.tree.command(name="preset", description="📂 プリセットからワンクリックで画像生成")
async def slash_preset(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📂 プリセット画像生成",
        description="カテゴリを選んでください。ランダムなプロンプトで画像を生成します。",
        color=0x667eea,
    )
    for key, info in PRESETS.items():
        embed.add_field(name=info["label"], value=info["desc"], inline=True)
    await interaction.response.send_message(embed=embed, view=PresetView())


# ========================================
# 5. /batch — 一括画像生成 (3〜5枚)
# ========================================

@is_operator()
@bot.tree.command(name="batch", description="📸 一度に複数枚の画像を生成（3〜5枚）")
@app_commands.describe(
    prompt="画像の説明（英語推奨）",
    count="生成枚数 (1-5)",
    ratio="アスペクト比",
)
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_batch(
    interaction: discord.Interaction,
    prompt: str,
    count: app_commands.Range[int, 1, 5] = 3,
    ratio: app_commands.Choice[str] = None,
):
    aspect = ratio.value if ratio else "1:1"
    await interaction.response.defer(thinking=True)
    try:
        # Generate concurrently
        tasks = [image_gen.generate(prompt=prompt, aspect_ratio=aspect) for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_urls = []
        errors = 0
        for r in results:
            if isinstance(r, Exception):
                errors += 1
            elif r:
                success_urls.extend(r)

        if not success_urls:
            await interaction.followup.send(f"❌ {count}枚すべて失敗しました")
            return

        # Send first image with summary
        for i, url in enumerate(success_urls[:5]):
            store_last_media(interaction.channel_id, url, "image", prompt, aspect)
            _gallery_items[interaction.user.id].append({"url": url, "prompt": prompt[:50], "status": "pending"})
            filename = f"batch_{uuid.uuid4().hex[:6]}_{i}.png"
            label = f"📸 **バッチ {i+1}/{len(success_urls)}**" if i == 0 else ""
            if i == 0:
                label = f"✅ **バッチ生成完了! ({len(success_urls)}/{count}枚)**\n📝 `{prompt[:80]}`\n📐 {aspect}"
            await download_and_send(interaction, url, filename, label)

    except Exception as e:
        logger.error("Batch failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 6. /variation — バリエーション生成
# ========================================

@is_operator()
@bot.tree.command(name="variation", description="🎭 同じプロンプトで3つのバリエーションを生成")
@app_commands.describe(prompt="画像の説明（英語推奨）", ratio="アスペクト比")
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_variation(interaction: discord.Interaction, prompt: str, ratio: app_commands.Choice[str] = None):
    aspect = ratio.value if ratio else "1:1"
    await interaction.response.defer(thinking=True)

    # Add variation modifiers
    variations = [
        f"{prompt}, style: cinematic lighting, vibrant colors",
        f"{prompt}, style: soft natural light, pastel tones, dreamy atmosphere",
        f"{prompt}, style: dramatic shadows, high contrast, editorial look",
    ]

    try:
        tasks = [image_gen.generate(prompt=v, aspect_ratio=aspect) for v in variations]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success = 0
        for i, r in enumerate(results):
            if isinstance(r, Exception) or not r:
                continue
            success += 1
            url = r[0]
            store_last_media(interaction.channel_id, url, "image", prompt, aspect)
            _gallery_items[interaction.user.id].append({"url": url, "prompt": f"variation_{i+1}", "status": "pending"})
            style_names = ["シネマティック", "ナチュラル", "エディトリアル"]
            filename = f"var_{uuid.uuid4().hex[:6]}_{i}.png"
            label = f"🎭 **バリエーション {i+1}/3** — {style_names[i]}"
            if i == 0:
                label = f"✅ **3つのバリエーション生成中...**\n📝 `{prompt[:80]}`\n\n🎭 **バリエーション 1/3** — {style_names[0]}"
            await download_and_send(interaction, url, filename, label)

        if success == 0:
            await interaction.followup.send("❌ バリエーション生成に失敗しました")

    except Exception as e:
        logger.error("Variation failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 7. /extend — 動画延長
# ========================================

@is_operator()
@bot.tree.command(name="extend", description="🔗 生成した動画を延長する")
@app_commands.describe(
    task_id="元の動画のタスクID（/video生成時に表示される🆔）",
    prompt="延長部分の説明",
)
async def slash_extend(interaction: discord.Interaction, task_id: str, prompt: str = "continue the scene naturally"):
    await interaction.response.defer(thinking=True)
    try:
        result = await video_gen.extend_video(task_id=task_id, prompt=prompt)
        if not result.video_url:
            await interaction.followup.send("❌ 動画延長に失敗しました")
            return
        store_last_media(interaction.channel_id, result.video_url, "video", prompt, "9:16")
        filename = f"ext_{uuid.uuid4().hex[:8]}.mp4"
        await download_and_send(
            interaction, result.video_url, filename,
            f"✅ **動画延長完了!**\n🔗 元タスク: `{task_id}`\n📝 `{prompt[:100]}`\n⏱️ +{result.duration_seconds}s\n🆔 `{result.generation_id}`"
        )
    except Exception as e:
        logger.error("Extend failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 8. /upscale — 高解像度画像生成
# ========================================

@is_operator()
@bot.tree.command(name="upscale", description="🔍 高解像度 (4K) で画像を生成")
@app_commands.describe(prompt="画像の説明（英語推奨）", ratio="アスペクト比")
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_upscale(interaction: discord.Interaction, prompt: str, ratio: app_commands.Choice[str] = None):
    aspect = ratio.value if ratio else "1:1"
    enhanced_prompt = f"{prompt}, ultra high resolution, 4K, extremely detailed, sharp focus, professional quality"
    await interaction.response.defer(thinking=True)
    try:
        urls = await image_gen.generate(prompt=enhanced_prompt, aspect_ratio=aspect)
        if not urls:
            await interaction.followup.send("❌ 高解像度画像生成に失敗しました")
            return
        store_last_media(interaction.channel_id, urls[0], "image", prompt, aspect)
        _gallery_items[interaction.user.id].append({"url": urls[0], "prompt": prompt[:50], "status": "pending"})
        filename = f"hires_{uuid.uuid4().hex[:8]}.png"
        await download_and_send(
            interaction, urls[0], filename,
            f"✅ **高解像度画像完了!** 🔍\n📝 `{prompt[:100]}`\n📐 {aspect} | 🖼️ 4K品質"
        )
    except Exception as e:
        logger.error("Upscale failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 9. /post — SNSに投稿
# ========================================

@is_operator()
@bot.tree.command(name="post", description="📤 最後に生成したメディアをSNSに投稿")
@app_commands.describe(
    platform="投稿先プラットフォーム",
    caption="投稿テキスト（空欄ならAI自動生成）",
)
@app_commands.choices(platform=PLATFORM_CHOICES)
async def slash_post(interaction: discord.Interaction, platform: app_commands.Choice[str], caption: str = ""):
    media = _channel_last_media.get(interaction.channel_id)
    if not media:
        await interaction.response.send_message(
            "❌ まだこのチャンネルで画像/動画を生成していません。\n先に `/image` や `/video` で生成してください。",
            ephemeral=True,
        )
        return

    # Check if media is too old (> 1 hour)
    if time.time() - media.get("created_at", 0) > 3600:
        await interaction.response.send_message(
            "⚠️ 最後の生成から1時間以上経過しています。新しく生成し直してください。",
            ephemeral=True,
        )
        return

    plat_name = platform.value
    await interaction.response.defer(thinking=True)

    try:
        mgr = get_platform_manager()
        plat = mgr.get(plat_name)
        if not plat:
            await interaction.followup.send(f"❌ `{plat_name}` は設定されていません。.envにAPIキーを追加してください。")
            return

        # Auto-generate caption if empty (run sync Claude in thread)
        if not caption:
            try:
                gen = get_claude_gen()
                content = await run_sync(
                    gen.generate_post,
                    platform=plat_name,
                    topic=media.get("prompt", "beauty content")[:200],
                    tone="engaging",
                )
                caption = content.text
                if content.hashtags:
                    caption += "\n" + " ".join(f"#{h}" for h in content.hashtags)
            except Exception as cap_err:
                logger.warning("Auto caption failed: %s", cap_err)
                caption = media.get("prompt", "")[:280]

        # Download media to temp file
        suffix = ".mp4" if media["type"] == "video" else ".png"
        tmp_path = await download_media_to_temp(media["url"], suffix=suffix)

        try:
            if media["type"] == "video":
                result = await plat.post_video(text=caption, video_path=tmp_path)
            else:
                result = await plat.post_image(text=caption, image_path=tmp_path)

            if result.success:
                post_url = result.url or f"Post ID: {result.post_id}"
                await interaction.followup.send(
                    f"✅ **{plat_name.upper()} に投稿完了!**\n"
                    f"📝 {caption[:200]}\n"
                    f"🔗 {post_url}"
                )
            else:
                await interaction.followup.send(f"❌ 投稿失敗: {result.error}")
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error("Post failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 10. /caption — AI キャプション生成
# ========================================

@is_operator()
@bot.tree.command(name="caption", description="✍️ AIでキャプション・ハッシュタグを自動生成")
@app_commands.describe(
    topic="投稿のトピック/テーマ",
    platform="対象プラットフォーム",
    tone="トーン（例: cute, friendly, professional, funny）",
)
@app_commands.choices(platform=PLATFORM_CHOICES)
async def slash_caption(
    interaction: discord.Interaction,
    topic: str,
    platform: app_commands.Choice[str] = None,
    tone: str = "engaging",
):
    plat = platform.value if platform else "instagram"
    await interaction.response.defer(thinking=True)
    try:
        gen = get_claude_gen()
        content = await run_sync(
            gen.generate_post,
            platform=plat,
            topic=topic,
            tone=tone,
        )
        hashtag_str = " ".join(f"#{h}" for h in content.hashtags) if content.hashtags else ""
        embed = discord.Embed(title="✍️ AIキャプション生成", color=0x667eea)
        embed.add_field(name="📝 本文", value=content.text[:1024], inline=False)
        if hashtag_str:
            embed.add_field(name="#️⃣ ハッシュタグ", value=hashtag_str[:1024], inline=False)
        if content.suggested_media_prompt:
            embed.add_field(name="🎨 おすすめ画像プロンプト", value=f"`{content.suggested_media_prompt[:200]}`", inline=False)
        if content.posting_time_suggestion:
            embed.add_field(name="⏰ おすすめ投稿時間", value=content.posting_time_suggestion, inline=True)
        embed.add_field(name="📊 バイラルスコア", value=f"{content.viral_score_estimate:.0%}", inline=True)
        embed.add_field(name="📱 プラットフォーム", value=plat.upper(), inline=True)
        embed.set_footer(text="💡 テキストをコピーして /post で使えます")
        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error("Caption failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 11. /schedule — 予約投稿
# ========================================

@is_operator()
@bot.tree.command(name="schedule", description="⏰ 予約投稿を設定")
@app_commands.describe(
    platform="投稿先プラットフォーム",
    time_str="投稿時間（例: 21:00, 2h後, 明日12:00）",
    caption="投稿テキスト（空欄ならAI自動生成）",
)
@app_commands.choices(platform=PLATFORM_CHOICES)
async def slash_schedule(
    interaction: discord.Interaction,
    platform: app_commands.Choice[str],
    time_str: str,
    caption: str = "",
):
    media = _channel_last_media.get(interaction.channel_id)
    if not media:
        await interaction.response.send_message(
            "❌ まだこのチャンネルで画像/動画を生成していません。先に生成してください。",
            ephemeral=True,
        )
        return

    await interaction.response.defer(thinking=True)

    try:
        # Parse time
        from zoneinfo import ZoneInfo
        jst = ZoneInfo("Asia/Tokyo")
        now_jst = datetime.now(jst)
        scheduled_at = None

        # Parse various time formats
        time_lower = time_str.strip().lower()
        if "h後" in time_lower or "時間後" in time_lower:
            hours = int("".join(c for c in time_lower if c.isdigit()) or "1")
            scheduled_at = now_jst + timedelta(hours=hours)
        elif "m後" in time_lower or "分後" in time_lower:
            mins = int("".join(c for c in time_lower if c.isdigit()) or "30")
            scheduled_at = now_jst + timedelta(minutes=mins)
        elif "明日" in time_lower:
            time_part = time_lower.replace("明日", "").strip()
            if ":" in time_part:
                h, m = time_part.split(":")
                scheduled_at = now_jst.replace(hour=int(h), minute=int(m), second=0) + timedelta(days=1)
            else:
                scheduled_at = now_jst.replace(hour=12, minute=0, second=0) + timedelta(days=1)
        elif ":" in time_lower:
            h, m = time_lower.split(":")
            scheduled_at = now_jst.replace(hour=int(h), minute=int(m), second=0)
            if scheduled_at <= now_jst:
                scheduled_at += timedelta(days=1)
        else:
            await interaction.followup.send(
                "❌ 時間の形式が分かりません。\n"
                "例: `21:00`, `2h後`, `30m後`, `明日12:00`"
            )
            return

        # Save to database as scheduled post
        from src.core.database import async_session
        from src.core.models import Post, PostStatus, ContentType

        content_type = ContentType.VIDEO if media["type"] == "video" else ContentType.IMAGE

        if not caption:
            try:
                gen = get_claude_gen()
                content = await run_sync(gen.generate_post, platform=platform.value, topic=media["prompt"][:200])
                caption = content.text
                if content.hashtags:
                    caption += "\n" + " ".join(f"#{h}" for h in content.hashtags)
            except Exception:
                caption = ""

        async with async_session() as session:
            post = Post(
                platform=platform.value,
                content_type=content_type.value,
                text_content=caption,
                media_urls=[media["url"]],
                status=PostStatus.SCHEDULED.value,
                scheduled_at=scheduled_at.astimezone(timezone.utc),
            )
            session.add(post)
            await session.commit()
            post_id = post.id

        time_display = scheduled_at.strftime("%Y-%m-%d %H:%M JST")
        await interaction.followup.send(
            f"✅ **予約投稿を設定しました!**\n"
            f"📱 {platform.value.upper()}\n"
            f"⏰ {time_display}\n"
            f"📝 {caption[:200] if caption else '(AI自動生成)'}\n"
            f"🆔 Post ID: {post_id}"
        )

    except Exception as e:
        logger.error("Schedule failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 12. /status — API使用量・予算
# ========================================

@bot.tree.command(name="status", description="📊 API使用量・予算残り・投稿数を表示")
async def slash_status(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        summary = cost_tracker.get_summary()

        embed = discord.Embed(title="📊 システムステータス", color=0x667eea, timestamp=datetime.now(timezone.utc))

        # Budget
        daily_pct = (summary.get("daily_cost_usd", 0) / max(summary.get("daily_budget_usd", 10), 0.01)) * 100
        bar = "█" * int(daily_pct / 10) + "░" * (10 - int(daily_pct / 10))
        embed.add_field(
            name="💰 日次予算",
            value=(
                f"{format_cost(summary.get('daily_cost_usd', 0))} / {format_cost(summary.get('daily_budget_usd', 10))}\n"
                f"[{bar}] {daily_pct:.1f}%\n"
                f"残り: {format_cost(summary.get('daily_budget_remaining', 0))}"
            ),
            inline=True,
        )

        embed.add_field(
            name="📅 月次予算",
            value=(
                f"{format_cost(summary.get('monthly_cost_usd', 0))} / {format_cost(summary.get('monthly_budget_usd', 250))}\n"
                f"残り: {format_cost(summary.get('monthly_budget_remaining', 0))}"
            ),
            inline=True,
        )

        # API calls
        embed.add_field(
            name="🔢 API呼び出し",
            value=f"合計: {summary.get('total_api_calls', 0)}回",
            inline=True,
        )

        # Tokens
        embed.add_field(
            name="🔤 トークン使用量",
            value=(
                f"入力: {summary.get('total_input_tokens', 0):,}\n"
                f"出力: {summary.get('total_output_tokens', 0):,}"
            ),
            inline=True,
        )

        # Post count from DB
        try:
            from src.core.database import async_session
            from src.core.models import Post, PostStatus
            from sqlalchemy import select, func

            async with async_session() as session:
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                result = await session.execute(
                    select(func.count(Post.id)).where(
                        Post.published_at >= today_start,
                        Post.status == PostStatus.PUBLISHED.value,
                    )
                )
                today_posts = result.scalar() or 0
                total_result = await session.execute(select(func.count(Post.id)))
                total_posts = total_result.scalar() or 0

            embed.add_field(
                name="📱 投稿数",
                value=f"今日: {today_posts}\n合計: {total_posts}",
                inline=True,
            )
        except Exception:
            embed.add_field(name="📱 投稿数", value="(DB未初期化)", inline=True)

        # Budget warning
        if summary.get("budget_exceeded"):
            embed.add_field(
                name="⚠️ 警告",
                value="**日次予算を超過しています！** 生成が一時停止されています。",
                inline=False,
            )

        # Connected platforms
        mgr = get_platform_manager()
        platforms = list(mgr._platforms.keys()) if mgr._platforms else ["(なし)"]
        embed.add_field(
            name="🔌 接続済みプラットフォーム",
            value=", ".join(p.upper() for p in platforms),
            inline=False,
        )

        embed.set_footer(text="Powered by SNS Automation System")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Status failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 13. /history — 生成・投稿履歴
# ========================================

@bot.tree.command(name="history", description="📜 最近の投稿・生成履歴を表示")
@app_commands.describe(count="表示件数 (1-20)")
async def slash_history(interaction: discord.Interaction, count: app_commands.Range[int, 1, 20] = 10):
    await interaction.response.defer(thinking=True)
    try:
        from src.core.database import async_session
        from src.core.models import Post
        from sqlalchemy import select

        async with async_session() as session:
            result = await session.execute(
                select(Post).order_by(Post.created_at.desc()).limit(count)
            )
            posts = result.scalars().all()

        if not posts:
            await interaction.followup.send("📜 まだ投稿履歴がありません。")
            return

        embed = discord.Embed(title=f"📜 最近の投稿履歴 (直近{len(posts)}件)", color=0x667eea)
        for p in posts:
            status_emoji = {"published": "✅", "scheduled": "⏰", "failed": "❌", "draft": "📝"}.get(p.status, "❓")
            time_str = p.created_at.strftime("%m/%d %H:%M") if p.created_at else "?"
            text_preview = (p.text_content or "")[:60]
            embed.add_field(
                name=f"{status_emoji} [{p.platform.upper()}] {time_str}",
                value=f"`{p.content_type}` {text_preview}{'...' if len(p.text_content or '') > 60 else ''}\nID: {p.id}",
                inline=False,
            )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("History failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 14. /cost — コスト内訳
# ========================================

@bot.tree.command(name="cost", description="💵 Kie.ai / Claude API のコスト内訳を表示")
async def slash_cost(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    try:
        summary = cost_tracker.get_summary()

        # Estimate breakdown from records
        claude_cost = 0.0
        kie_image_cost = 0.0
        kie_video_cost = 0.0
        claude_calls = 0
        image_calls = 0
        video_calls = 0

        for rec in cost_tracker._records:
            if rec.service == "claude":
                claude_cost += rec.cost_usd
                claude_calls += 1
            elif rec.service == "kie" and "image" in rec.method:
                kie_image_cost += rec.cost_usd
                image_calls += 1
            elif rec.service == "kie" and "video" in rec.method:
                kie_video_cost += rec.cost_usd
                video_calls += 1

        embed = discord.Embed(title="💵 コスト内訳", color=0xff9900, timestamp=datetime.now(timezone.utc))

        embed.add_field(
            name="🤖 Claude AI",
            value=(
                f"コスト: {format_cost(claude_cost)}\n"
                f"呼び出し: {claude_calls}回\n"
                f"入力: {summary.get('total_input_tokens', 0):,} tokens\n"
                f"出力: {summary.get('total_output_tokens', 0):,} tokens"
            ),
            inline=True,
        )

        embed.add_field(
            name="🎨 Kie.ai 画像",
            value=(
                f"コスト: {format_cost(kie_image_cost)}\n"
                f"生成: {image_calls}枚\n"
                f"単価: ~$0.12/枚"
            ),
            inline=True,
        )

        embed.add_field(
            name="🎬 Kie.ai 動画",
            value=(
                f"コスト: {format_cost(kie_video_cost)}\n"
                f"生成: {video_calls}本\n"
                f"単価: ~$0.40/本"
            ),
            inline=True,
        )

        total = claude_cost + kie_image_cost + kie_video_cost
        embed.add_field(
            name="📊 合計",
            value=(
                f"**{format_cost(total)}**\n"
                f"日次予算: {format_cost(summary.get('daily_budget_usd', 10))}\n"
                f"月次予算: {format_cost(summary.get('monthly_budget_usd', 250))}"
            ),
            inline=False,
        )

        # Pricing reference
        embed.add_field(
            name="💡 料金表",
            value=(
                "Claude Sonnet: $3/$15 per 1M tokens (in/out)\n"
                "Kie 画像: ~$0.12/枚\n"
                "Kie 動画(Fast): ~$0.40/本\n"
                "Kie 動画(Quality): ~$2.00/本"
            ),
            inline=False,
        )

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Cost failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 15. /ask — Claude AI に質問
# ========================================

@bot.tree.command(name="ask", description="🧠 Claude AIに何でも質問")
@app_commands.describe(question="質問内容（日本語OK）")
async def slash_ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    try:
        client = get_async_claude()
        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=1500,
            system="あなたはSNSマーケティングと美容コスメに詳しいアシスタントです。簡潔に日本語で回答してください。",
            messages=[{"role": "user", "content": question}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage("discord_ask", response.usage.input_tokens, response.usage.output_tokens)

        answer = response.content[0].text if response.content else "回答を取得できませんでした。"

        # Split if too long for Discord
        if len(answer) <= 4000:
            embed = discord.Embed(title="🧠 Claude AI", color=0x667eea)
            embed.add_field(name=f"❓ {question[:250]}", value=answer[:1024], inline=False)
            if len(answer) > 1024:
                embed.add_field(name="続き", value=answer[1024:2048], inline=False)
            if len(answer) > 2048:
                embed.add_field(name="続き②", value=answer[2048:3072], inline=False)
            await interaction.followup.send(embed=embed)
        else:
            # Send as text file if very long
            file = discord.File(io.BytesIO(answer.encode()), filename="claude_answer.txt")
            await interaction.followup.send(content=f"🧠 **回答** (長文のためファイルで送信)\n❓ {question[:200]}", file=file)

    except Exception as e:
        logger.error("Ask failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 16. /trend — トレンド調査
# ========================================

@bot.tree.command(name="trend", description="📈 今のトレンドキーワード・ハッシュタグを調査")
@app_commands.describe(niche="ジャンル（デフォルト: 美容・コスメ）")
async def slash_trend(interaction: discord.Interaction, niche: str = ""):
    target_niche = niche or settings.niche
    await interaction.response.defer(thinking=True)
    try:
        researcher = get_trend_researcher()
        trends = await researcher.research_niche_trends(niche=target_niche)

        embed = discord.Embed(
            title=f"📈 トレンドリサーチ — {target_niche}",
            color=0x00d4aa,
            timestamp=datetime.now(timezone.utc),
        )

        # Trending topics
        topics = trends.get("trending_topics", trends.get("topics", []))
        if topics:
            topic_text = "\n".join(f"• {t}" if isinstance(t, str) else f"• {t.get('topic', t)}" for t in topics[:10])
            embed.add_field(name="🔥 トレンドトピック", value=topic_text[:1024], inline=False)

        # Hashtags
        hashtags = trends.get("hashtags", trends.get("recommended_hashtags", []))
        if hashtags:
            tag_text = " ".join(f"#{h}" if isinstance(h, str) else f"#{h.get('tag', h)}" for h in hashtags[:15])
            embed.add_field(name="#️⃣ おすすめハッシュタグ", value=tag_text[:1024], inline=False)

        # Content ideas
        ideas = trends.get("content_ideas", trends.get("ideas", []))
        if ideas:
            idea_text = "\n".join(f"💡 {idea}" if isinstance(idea, str) else f"💡 {idea.get('idea', idea)}" for idea in ideas[:5])
            embed.add_field(name="💡 コンテンツアイデア", value=idea_text[:1024], inline=False)

        # Analysis
        analysis = trends.get("analysis", trends.get("summary", ""))
        if analysis:
            embed.add_field(name="📊 分析", value=str(analysis)[:1024], inline=False)

        embed.set_footer(text="Powered by Claude AI + Web Research")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Trend failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 17. /translate — 日本語→英語プロンプト翻訳
# ========================================

@bot.tree.command(name="translate", description="🌐 日本語を英語のAIプロンプトに翻訳して画像生成")
@app_commands.describe(
    text="日本語の説明（例: 桜の下でピンクのドレスの女性）",
    ratio="アスペクト比",
    generate="翻訳後に画像を自動生成する",
)
@app_commands.choices(ratio=RATIO_CHOICES)
async def slash_translate(
    interaction: discord.Interaction,
    text: str,
    ratio: app_commands.Choice[str] = None,
    generate: bool = True,
):
    aspect = ratio.value if ratio else "1:1"
    await interaction.response.defer(thinking=True)
    try:
        client = get_async_claude()
        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=500,
            system=(
                "You are a translation assistant specializing in AI image generation prompts. "
                "Translate the user's Japanese text into a detailed English prompt for image generation. "
                "Add artistic details, lighting, and quality modifiers to make the prompt more effective. "
                "Return ONLY the English prompt, nothing else."
            ),
            messages=[{"role": "user", "content": text}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage("discord_translate", response.usage.input_tokens, response.usage.output_tokens)

        en_prompt = response.content[0].text.strip()

        msg = f"🌐 **翻訳完了!**\n📝 原文: `{text}`\n🔤 英語: `{en_prompt[:300]}`"

        if generate:
            msg += f"\n\n🎨 画像生成中... (📐 {aspect})"
            await interaction.followup.send(msg)

            urls = await image_gen.generate(prompt=en_prompt, aspect_ratio=aspect)
            if urls:
                store_last_media(interaction.channel_id, urls[0], "image", en_prompt, aspect)
                _gallery_items[interaction.user.id].append({"url": urls[0], "prompt": text[:50], "status": "pending"})
                filename = f"tr_{uuid.uuid4().hex[:8]}.png"
                await download_and_send(
                    interaction, urls[0], filename,
                    f"✅ **翻訳→画像生成完了!**\n📝 `{text[:80]}`\n📐 {aspect}"
                )
            else:
                await interaction.followup.send("❌ 画像生成に失敗しました")
        else:
            msg += "\n\n💡 このプロンプトを `/image` にコピーして使えます"
            await interaction.followup.send(msg)

    except Exception as e:
        logger.error("Translate failed: %s", e)
        await interaction.followup.send(f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


# ========================================
# 18. /gallery — 画像レビュー (採用/却下/お気に入り)
# ========================================

class GalleryView(discord.ui.View):
    """Interactive gallery with adopt/reject/favorite buttons."""

    def __init__(self, user_id: int, items: list[dict], index: int = 0):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.items = items
        self.index = index
        self._update_buttons()

    def _update_buttons(self):
        item = self.items[self.index] if self.index < len(self.items) else None
        if item:
            status = item.get("status", "pending")
            self.adopt_btn.label = "✅ 採用済み" if status == "adopted" else "✅ 採用"
            self.fav_btn.label = "⭐ お気に入り済み" if status == "favorite" else "⭐ お気に入り"
            self.reject_btn.label = "❌ 却下済み" if status == "rejected" else "❌ 却下"

    @discord.ui.button(label="⬅️ 前", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他のユーザーのギャラリーは操作できません", ephemeral=True)
            return
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await self._update_gallery(interaction)

    @discord.ui.button(label="✅ 採用", style=discord.ButtonStyle.success)
    async def adopt_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他のユーザーのギャラリーは操作できません", ephemeral=True)
            return
        self.items[self.index]["status"] = "adopted"
        self._update_buttons()
        await self._update_gallery(interaction)

    @discord.ui.button(label="⭐ お気に入り", style=discord.ButtonStyle.primary)
    async def fav_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他のユーザーのギャラリーは操作できません", ephemeral=True)
            return
        self.items[self.index]["status"] = "favorite"
        self._update_buttons()
        await self._update_gallery(interaction)

    @discord.ui.button(label="❌ 却下", style=discord.ButtonStyle.danger)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他のユーザーのギャラリーは操作できません", ephemeral=True)
            return
        self.items[self.index]["status"] = "rejected"
        self._update_buttons()
        await self._update_gallery(interaction)

    @discord.ui.button(label="➡️ 次", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他のユーザーのギャラリーは操作できません", ephemeral=True)
            return
        self.index = min(len(self.items) - 1, self.index + 1)
        self._update_buttons()
        await self._update_gallery(interaction)

    async def _update_gallery(self, interaction: discord.Interaction):
        item = self.items[self.index]
        status_label = {"pending": "⏳ 未分類", "adopted": "✅ 採用", "rejected": "❌ 却下", "favorite": "⭐ お気に入り"}.get(item["status"], "❓")

        # Count stats
        adopted = sum(1 for x in self.items if x["status"] == "adopted")
        rejected = sum(1 for x in self.items if x["status"] == "rejected")
        fav = sum(1 for x in self.items if x["status"] == "favorite")
        pending = sum(1 for x in self.items if x["status"] == "pending")

        embed = discord.Embed(
            title=f"🖼️ ギャラリー ({self.index + 1}/{len(self.items)})",
            description=f"📝 `{item.get('prompt', '?')[:100]}`\n状態: {status_label}",
            color=0x667eea,
        )
        embed.set_image(url=item["url"])
        embed.set_footer(text=f"✅ {adopted} | ⭐ {fav} | ❌ {rejected} | ⏳ {pending}")

        await interaction.response.edit_message(embed=embed, view=self)


@bot.tree.command(name="gallery", description="🖼️ 生成画像をレビュー（採用/却下/お気に入り）")
async def slash_gallery(interaction: discord.Interaction):
    items = _gallery_items.get(interaction.user.id, [])
    if not items:
        await interaction.response.send_message(
            "🖼️ まだギャラリーに画像がありません。\n`/image` や `/batch` で画像を生成するとギャラリーに追加されます。",
            ephemeral=True,
        )
        return

    item = items[0]
    status_label = {"pending": "⏳ 未分類", "adopted": "✅ 採用", "rejected": "❌ 却下", "favorite": "⭐ お気に入り"}.get(item["status"], "❓")

    embed = discord.Embed(
        title=f"🖼️ ギャラリー (1/{len(items)})",
        description=f"📝 `{item.get('prompt', '?')[:100]}`\n状態: {status_label}",
        color=0x667eea,
    )
    embed.set_image(url=item["url"])

    adopted = sum(1 for x in items if x["status"] == "adopted")
    rejected = sum(1 for x in items if x["status"] == "rejected")
    fav = sum(1 for x in items if x["status"] == "favorite")
    pending = sum(1 for x in items if x["status"] == "pending")
    embed.set_footer(text=f"✅ {adopted} | ⭐ {fav} | ❌ {rejected} | ⏳ {pending}")

    await interaction.response.send_message(embed=embed, view=GalleryView(interaction.user.id, items))


# ========================================
# 19. /queue — 生成キュー表示
# ========================================

@bot.tree.command(name="queue", description="📋 生成キューと実行中タスクを表示")
async def slash_queue(interaction: discord.Interaction):
    embed = discord.Embed(title="📋 生成キュー", color=0x667eea, timestamp=datetime.now(timezone.utc))

    # Active tasks
    if _active_tasks:
        active_text = ""
        for tid, info in _active_tasks.items():
            elapsed = int(time.time() - info["started_at"])
            active_text += f"🔄 `{tid}` — {info['type']} by {info['user']} ({elapsed}s経過)\n"
            active_text += f"   📝 {info['prompt']}\n"
        embed.add_field(name="🔄 実行中", value=active_text[:1024], inline=False)
    else:
        embed.add_field(name="🔄 実行中", value="なし", inline=False)

    # Queue
    if _generation_queue:
        queue_text = ""
        for i, item in enumerate(list(_generation_queue)[:10]):
            queue_text += f"{i+1}. `{item.get('type', '?')}` — {item.get('prompt', '?')[:50]}\n"
        embed.add_field(name="⏳ 待機中", value=queue_text[:1024], inline=False)
    else:
        embed.add_field(name="⏳ 待機中", value="なし", inline=False)

    # Gallery stats per user
    user_items = _gallery_items.get(interaction.user.id, [])
    if user_items:
        adopted = sum(1 for x in user_items if x["status"] == "adopted")
        rejected = sum(1 for x in user_items if x["status"] == "rejected")
        fav = sum(1 for x in user_items if x["status"] == "favorite")
        pending = sum(1 for x in user_items if x["status"] == "pending")
        embed.add_field(
            name="🖼️ あなたのギャラリー",
            value=f"合計: {len(user_items)} | ✅ {adopted} | ⭐ {fav} | ❌ {rejected} | ⏳ {pending}",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


# ========================================
# 20. /help_gen — コマンド一覧
# ========================================

@bot.tree.command(name="help_gen", description="📖 全コマンド一覧を表示")
async def slash_help_gen(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎨 SNS Automation Bot — 全コマンド一覧",
        description="スラッシュコマンド (`/`) または `!` プレフィックスで使えます",
        color=0x667eea,
    )

    embed.add_field(
        name="🎨 画像・動画生成",
        value=(
            "`/image` — プロンプトから画像生成\n"
            "`/character` — ペルソナ画像（NO_FACE）\n"
            "`/video` — 動画生成（1〜5分）\n"
            "`/preset` — プリセットから生成\n"
            "`/batch` — 一括生成（3〜5枚）\n"
            "`/variation` — 3つのバリエーション\n"
            "`/extend` — 動画を延長\n"
            "`/upscale` — 高解像度(4K)生成"
        ),
        inline=False,
    )

    embed.add_field(
        name="📱 SNS投稿",
        value=(
            "`/post` — 最後の画像/動画をSNSに投稿\n"
            "`/caption` — AIキャプション＋ハッシュタグ生成\n"
            "`/schedule` — 予約投稿を設定"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 管理・分析",
        value=(
            "`/status` — API使用量・予算・投稿数\n"
            "`/history` — 投稿・生成履歴\n"
            "`/cost` — コスト内訳"
        ),
        inline=False,
    )

    embed.add_field(
        name="🧠 AI機能",
        value=(
            "`/ask` — Claude AIに質問\n"
            "`/trend` — トレンドキーワード調査\n"
            "`/translate` — 日本語→英語翻訳＋生成"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎭 ワークフロー",
        value=(
            "`/gallery` — 画像レビュー（採用/却下/⭐）\n"
            "`/queue` — 生成キュー＆タスク表示"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔧 共通オプション",
        value=(
            "`ratio` — 1:1(正方形) / 16:9(横長) / 9:16(縦長) / 4:5(Insta)\n"
            "`!` コマンド: `--ratio wide` / `tall` / `square` / `insta`"
        ),
        inline=False,
    )

    embed.set_footer(text="Powered by Kie.ai + Claude AI | SNS Automation System")
    await interaction.response.send_message(embed=embed)


# ========================================
# 21. /fortune — Miku Oracle 占い (公開)
# ========================================

FORTUNE_TYPE_CHOICES = [
    app_commands.Choice(name="タロット (Tarot)", value="tarot"),
    app_commands.Choice(name="恋愛 (Love)", value="love"),
    app_commands.Choice(name="数秘術 (Numerology)", value="numerology"),
]

_fortune_gen = None


def get_fortune_gen():
    global _fortune_gen
    if _fortune_gen is None:
        from src.ai.fortune_generator import FortuneGenerator
        _fortune_gen = FortuneGenerator()
    return _fortune_gen


@bot.tree.command(name="fortune", description="🔮 Miku Oracleがあなたの運勢を占います（無料・全員利用可）")
@app_commands.describe(type="占いの種類（デフォルト: タロット）")
@app_commands.choices(type=FORTUNE_TYPE_CHOICES)
async def slash_fortune(
    interaction: discord.Interaction, type: app_commands.Choice[str] = None
):
    fortune_type = type.value if type else "tarot"
    await interaction.response.defer(thinking=True)
    try:
        gen = get_fortune_gen()
        result = await run_sync(gen.generate, fortune_type)

        type_labels = {
            "tarot": "🃏 タロット占い",
            "love": "💖 恋愛占い",
            "numerology": "🔢 数秘術",
        }
        title = f"🔮 Miku Oracle — {type_labels.get(fortune_type, fortune_type)}"
        embed = discord.Embed(
            title=title, color=0x9B59B6, timestamp=datetime.now(timezone.utc)
        )

        if fortune_type == "tarot":
            card = result.get("card_name", "")
            position = result.get("position", "")
            if card:
                embed.add_field(
                    name="🃏 引いたカード",
                    value=f"**{card}** ({position})",
                    inline=False,
                )
            text = result.get("text", "")
            if text:
                embed.add_field(name="✨ リーディング", value=text[:1024], inline=False)
            cta = result.get("cta", "")
            if cta:
                embed.add_field(name="💌 もっと詳しく", value=cta, inline=False)
            tags = result.get("hashtags", [])
            if tags:
                embed.add_field(
                    name="#️⃣", value=" ".join(f"#{h}" for h in tags[:6]), inline=False
                )

        elif fortune_type == "love":
            cards = result.get("cards_drawn", [])
            if cards:
                embed.add_field(name="🃏 カード", value=", ".join(cards), inline=False)
            energy = result.get("energy_read", "")
            if energy:
                embed.add_field(name="💫 エネルギー", value=energy, inline=True)
            text = result.get("text", "")
            if text:
                embed.add_field(name="💖 リーディング", value=text[:1024], inline=False)
            action = result.get("action", "")
            if action:
                embed.add_field(name="🌸 今週のアクション", value=action, inline=False)

        elif fortune_type == "numerology":
            num = result.get("life_path_number", "")
            archetype = result.get("archetype", "")
            if num:
                embed.add_field(
                    name="🔢 ライフパスナンバー",
                    value=f"**{num}** — {archetype}",
                    inline=False,
                )
            reading = result.get("reading", "")
            if reading:
                embed.add_field(
                    name="✨ リーディング", value=reading[:1024], inline=False
                )
            tip = result.get("today_tip", "")
            if tip:
                embed.add_field(name="💡 今日のヒント", value=tip, inline=False)
            lucky_color = result.get("lucky_color", "")
            lucky_number = result.get("lucky_number", "")
            if lucky_color or lucky_number:
                embed.add_field(
                    name="🍀 ラッキー",
                    value=f"カラー: {lucky_color}  数字: {lucky_number}",
                    inline=True,
                )

        disclosure = result.get("disclosure", "— AI Oracle, for entertainment ✨")
        embed.set_footer(text=f"{disclosure} | 詳細な鑑定は → hoshimilive.com")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error("Fortune failed: %s", e)
        await interaction.followup.send(
            f"❌ 占いエラー: {type(e).__name__}: {str(e)[:200]}"
        )

# ========================================
# Prefix Commands (! commands) — fallback
# ========================================

@bot.command(name="image", aliases=["img", "i"])
async def prefix_image(ctx: commands.Context, *, prompt: str):
    """!image <prompt> — 画像生成"""
    prompt_text, ratio = parse_prefix_options(prompt)
    if not prompt_text:
        await ctx.reply("❌ プロンプトを入力してください\n例: `!image beautiful sunset over Tokyo`")
        return
    msg = await ctx.reply(f"🎨 **画像生成中...**\n📝 `{prompt_text[:100]}`\n📐 {ratio}")
    try:
        urls = await image_gen.generate(prompt=prompt_text, aspect_ratio=ratio)
        if not urls:
            await msg.edit(content="❌ 画像生成に失敗しました")
            return
        store_last_media(ctx.channel.id, urls[0], "image", prompt_text, ratio)
        _gallery_items[ctx.author.id].append({"url": urls[0], "prompt": prompt_text[:50], "status": "pending"})
        filename = f"kie_{uuid.uuid4().hex[:8]}.png"
        await msg.edit(content=f"✅ **画像生成完了!**\n📝 `{prompt_text[:80]}`\n📐 {ratio}")
        await download_and_send(ctx, urls[0], filename, "")
    except Exception as e:
        logger.error("Prefix image failed: %s", e)
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="character", aliases=["char"])
async def prefix_character(ctx: commands.Context, *, prompt: str = ""):
    """!character <scene> — ペルソナ画像生成"""
    prompt_text, ratio = parse_prefix_options(prompt)
    scene = prompt_text if prompt_text else "cozy knit sweater, doing skincare at a bright vanity"
    full_prompt = f"{CHARACTER_BASE}, {scene}. {NO_FACE}"
    msg = await ctx.reply(f"🎨 **ペルソナ画像生成中...**\n📝 `{scene[:100]}`\n📐 {ratio}")
    try:
        urls = await image_gen.generate(prompt=full_prompt, aspect_ratio=ratio)
        if not urls:
            await msg.edit(content="❌ 画像生成に失敗しました")
            return
        store_last_media(ctx.channel.id, urls[0], "image", full_prompt, ratio)
        _gallery_items[ctx.author.id].append({"url": urls[0], "prompt": scene[:50], "status": "pending"})
        filename = f"char_{uuid.uuid4().hex[:8]}.png"
        await msg.edit(content=f"✅ **キャラクター画像完了!**\n📝 `{scene[:80]}`\n📐 {ratio} | 🚫 NO_FACE")
        await download_and_send(ctx, urls[0], filename, "")
    except Exception as e:
        logger.error("Prefix character failed: %s", e)
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="video", aliases=["vid", "v"])
async def prefix_video(ctx: commands.Context, *, prompt: str):
    """!video <prompt> — 動画生成"""
    prompt_text, ratio = parse_prefix_options(prompt)
    if not prompt_text:
        await ctx.reply("❌ プロンプトを入力してください\n例: `!video gentle waves on a beach`")
        return
    msg = await ctx.reply(f"🎬 **動画生成中...**（1〜5分かかります）\n📝 `{prompt_text[:100]}`\n📐 {ratio}")
    try:
        result = await video_gen.generate_video(prompt=prompt_text, aspect_ratio=ratio)
        if not result.video_url:
            await msg.edit(content="❌ 動画生成に失敗しました")
            return
        store_last_media(ctx.channel.id, result.video_url, "video", prompt_text, ratio)
        filename = f"kie_{uuid.uuid4().hex[:8]}.mp4"
        await msg.edit(content=f"✅ **動画生成完了!**\n📝 `{prompt_text[:80]}`\n📐 {ratio} | ⏱️ {result.duration_seconds}s\n🆔 `{result.generation_id}`")
        await download_and_send(ctx, result.video_url, filename, "")
    except Exception as e:
        logger.error("Prefix video failed: %s", e)
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="batch", aliases=["b"])
async def prefix_batch(ctx: commands.Context, *, prompt: str):
    """!batch <prompt> [--count N] — 一括生成"""
    prompt_text, ratio = parse_prefix_options(prompt)
    count = 3
    parts = prompt_text.split()
    clean = []
    i = 0
    while i < len(parts):
        if parts[i] in ("--count", "-c") and i + 1 < len(parts):
            count = min(5, max(1, int(parts[i + 1])))
            i += 2
        else:
            clean.append(parts[i])
            i += 1
    prompt_text = " ".join(clean)

    if not prompt_text:
        await ctx.reply("❌ プロンプトを入力してください\n例: `!batch beautiful sunset --count 4`")
        return
    msg = await ctx.reply(f"📸 **{count}枚バッチ生成中...**\n📝 `{prompt_text[:80]}`")
    try:
        tasks = [image_gen.generate(prompt=prompt_text, aspect_ratio=ratio) for _ in range(count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        success_urls = []
        for r in results:
            if not isinstance(r, Exception) and r:
                success_urls.extend(r)
        if not success_urls:
            await msg.edit(content="❌ 全て失敗しました")
            return
        await msg.edit(content=f"✅ **{len(success_urls)}/{count}枚完了!**")
        for ii, url in enumerate(success_urls[:5]):
            store_last_media(ctx.channel.id, url, "image", prompt_text, ratio)
            _gallery_items[ctx.author.id].append({"url": url, "prompt": prompt_text[:50], "status": "pending"})
            filename = f"batch_{uuid.uuid4().hex[:6]}_{ii}.png"
            await download_and_send(ctx, url, filename, f"📸 {ii+1}/{len(success_urls)}")
    except Exception as e:
        logger.error("Prefix batch failed: %s", e)
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="variation", aliases=["var"])
async def prefix_variation(ctx: commands.Context, *, prompt: str):
    """!variation <prompt> — 3バリエーション"""
    prompt_text, ratio = parse_prefix_options(prompt)
    if not prompt_text:
        await ctx.reply("❌ プロンプトを入力してください")
        return
    msg = await ctx.reply(f"🎭 **3バリエーション生成中...**\n📝 `{prompt_text[:80]}`")
    variations = [
        f"{prompt_text}, style: cinematic lighting, vibrant colors",
        f"{prompt_text}, style: soft natural light, pastel tones",
        f"{prompt_text}, style: dramatic shadows, high contrast",
    ]
    try:
        tasks = [image_gen.generate(prompt=v, aspect_ratio=ratio) for v in variations]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        style_names = ["シネマティック", "ナチュラル", "エディトリアル"]
        success = 0
        for ii, r in enumerate(results):
            if isinstance(r, Exception) or not r:
                continue
            success += 1
            url = r[0]
            store_last_media(ctx.channel.id, url, "image", prompt_text, ratio)
            _gallery_items[ctx.author.id].append({"url": url, "prompt": f"var_{ii}", "status": "pending"})
            filename = f"var_{uuid.uuid4().hex[:6]}_{ii}.png"
            await download_and_send(ctx, url, filename, f"🎭 {style_names[ii]}")
        if success == 0:
            await msg.edit(content="❌ 全て失敗しました")
        else:
            await msg.edit(content=f"✅ **{success}/3バリエーション完了!**")
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="extend", aliases=["ext"])
async def prefix_extend(ctx: commands.Context, task_id: str, *, prompt: str = "continue the scene naturally"):
    """!extend <task_id> [prompt] — 動画延長"""
    msg = await ctx.reply(f"🔗 **動画延長中...**\n🆔 `{task_id}`")
    try:
        result = await video_gen.extend_video(task_id=task_id, prompt=prompt)
        if not result.video_url:
            await msg.edit(content="❌ 動画延長に失敗しました")
            return
        store_last_media(ctx.channel.id, result.video_url, "video", prompt, "9:16")
        filename = f"ext_{uuid.uuid4().hex[:8]}.mp4"
        await msg.edit(content=f"✅ **動画延長完了!** 🆔 `{result.generation_id}`")
        await download_and_send(ctx, result.video_url, filename, "")
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="upscale", aliases=["up", "4k"])
async def prefix_upscale(ctx: commands.Context, *, prompt: str):
    """!upscale <prompt> — 高解像度画像"""
    prompt_text, ratio = parse_prefix_options(prompt)
    if not prompt_text:
        await ctx.reply("❌ プロンプトを入力してください")
        return
    enhanced = f"{prompt_text}, ultra high resolution, 4K, extremely detailed, sharp focus"
    msg = await ctx.reply(f"🔍 **高解像度生成中...**\n📝 `{prompt_text[:80]}`")
    try:
        urls = await image_gen.generate(prompt=enhanced, aspect_ratio=ratio)
        if not urls:
            await msg.edit(content="❌ 生成に失敗しました")
            return
        store_last_media(ctx.channel.id, urls[0], "image", prompt_text, ratio)
        _gallery_items[ctx.author.id].append({"url": urls[0], "prompt": prompt_text[:50], "status": "pending"})
        filename = f"hires_{uuid.uuid4().hex[:8]}.png"
        await msg.edit(content="✅ **高解像度画像完了!** 🔍 4K品質")
        await download_and_send(ctx, urls[0], filename, "")
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="ask", aliases=["a", "ai"])
async def prefix_ask(ctx: commands.Context, *, question: str):
    """!ask <question> — Claude AIに質問"""
    msg = await ctx.reply("🧠 **考え中...**")
    try:
        client = get_async_claude()
        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=1500,
            system="あなたはSNSマーケティングと美容コスメに詳しいアシスタントです。簡潔に日本語で回答してください。",
            messages=[{"role": "user", "content": question}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage("discord_ask", response.usage.input_tokens, response.usage.output_tokens)
        answer = response.content[0].text if response.content else "回答を取得できませんでした。"
        if len(answer) <= 2000:
            await msg.edit(content=f"🧠 **Claude AI**\n❓ {question[:200]}\n\n{answer}")
        else:
            await msg.edit(content="🧠 **回答** (長文のためファイル送信)")
            file = discord.File(io.BytesIO(answer.encode()), filename="answer.txt")
            await ctx.send(file=file)
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="caption", aliases=["cap"])
async def prefix_caption(ctx: commands.Context, *, topic: str):
    """!caption <topic> — AIキャプション生成"""
    msg = await ctx.reply("✍️ **キャプション生成中...**")
    try:
        gen = get_claude_gen()
        content = await run_sync(gen.generate_post, platform="instagram", topic=topic, tone="engaging")
        hashtag_str = " ".join(f"#{h}" for h in content.hashtags) if content.hashtags else ""
        text = f"✍️ **AIキャプション**\n\n{content.text}\n\n{hashtag_str}\n\n📊 バイラルスコア: {content.viral_score_estimate:.0%}"
        await msg.edit(content=text[:2000])
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="translate", aliases=["tr", "翻訳"])
async def prefix_translate(ctx: commands.Context, *, text: str):
    """!translate <日本語> — 翻訳＋画像生成"""
    msg = await ctx.reply("🌐 **翻訳中...**")
    try:
        client = get_async_claude()
        response = await client.messages.create(
            model=settings.claude_model,
            max_tokens=500,
            system=(
                "You are a translation assistant for AI image generation prompts. "
                "Translate Japanese to a detailed English prompt with artistic details. "
                "Return ONLY the English prompt."
            ),
            messages=[{"role": "user", "content": text}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage("discord_translate", response.usage.input_tokens, response.usage.output_tokens)
        en_prompt = response.content[0].text.strip()
        await msg.edit(content=f"🌐 **翻訳完了!**\n📝 原文: `{text[:100]}`\n🔤 英語: `{en_prompt[:300]}`\n\n🎨 画像生成中...")
        urls = await image_gen.generate(prompt=en_prompt, aspect_ratio="1:1")
        if urls:
            store_last_media(ctx.channel.id, urls[0], "image", en_prompt, "1:1")
            _gallery_items[ctx.author.id].append({"url": urls[0], "prompt": text[:50], "status": "pending"})
            filename = f"tr_{uuid.uuid4().hex[:8]}.png"
            await msg.edit(content=f"✅ **翻訳→生成完了!**\n📝 `{text[:80]}`")
            await download_and_send(ctx, urls[0], filename, "")
        else:
            await msg.edit(content=f"🌐 翻訳完了: `{en_prompt[:200]}`\n❌ 画像生成は失敗しました")
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="trend", aliases=["trends"])
async def prefix_trend(ctx: commands.Context, *, niche: str = ""):
    """!trend [niche] — トレンド調査"""
    target_niche = niche or settings.niche
    msg = await ctx.reply(f"📈 **トレンド調査中...** ({target_niche})")
    try:
        researcher = get_trend_researcher()
        trends = await researcher.research_niche_trends(niche=target_niche)
        topics = trends.get("trending_topics", trends.get("topics", []))
        hashtags = trends.get("hashtags", trends.get("recommended_hashtags", []))
        ideas = trends.get("content_ideas", trends.get("ideas", []))

        text = f"📈 **トレンド — {target_niche}**\n\n"
        if topics:
            text += "**🔥 トピック:**\n" + "\n".join(f"• {t}" if isinstance(t, str) else f"• {t.get('topic', t)}" for t in topics[:8]) + "\n\n"
        if hashtags:
            text += "**#️⃣ ハッシュタグ:**\n" + " ".join(f"#{h}" if isinstance(h, str) else f"#{h.get('tag', h)}" for h in hashtags[:10]) + "\n\n"
        if ideas:
            text += "**💡 アイデア:**\n" + "\n".join(f"• {idea}" if isinstance(idea, str) else f"• {idea.get('idea', idea)}" for idea in ideas[:5])

        await msg.edit(content=text[:2000])
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="status", aliases=["stat"])
async def prefix_status(ctx: commands.Context):
    """!status — システムステータス"""
    summary = cost_tracker.get_summary()
    daily_pct = (summary.get("daily_cost_usd", 0) / max(summary.get("daily_budget_usd", 10), 0.01)) * 100
    bar = "█" * int(daily_pct / 10) + "░" * (10 - int(daily_pct / 10))
    text = (
        f"📊 **ステータス**\n"
        f"💰 日次: {format_cost(summary.get('daily_cost_usd', 0))} / {format_cost(summary.get('daily_budget_usd', 10))} [{bar}]\n"
        f"📅 月次: {format_cost(summary.get('monthly_cost_usd', 0))} / {format_cost(summary.get('monthly_budget_usd', 250))}\n"
        f"🔢 API: {summary.get('total_api_calls', 0)}回\n"
        f"{'⚠️ **予算超過中!**' if summary.get('budget_exceeded') else '✅ 予算内'}"
    )
    await ctx.reply(text)


@bot.command(name="cost")
async def prefix_cost(ctx: commands.Context):
    """!cost — コスト内訳"""
    claude_cost = sum(r.cost_usd for r in cost_tracker._records if r.service == "claude")
    kie_img = sum(r.cost_usd for r in cost_tracker._records if r.service == "kie" and "image" in r.method)
    kie_vid = sum(r.cost_usd for r in cost_tracker._records if r.service == "kie" and "video" in r.method)
    total = claude_cost + kie_img + kie_vid
    text = (
        f"💵 **コスト内訳**\n"
        f"🤖 Claude: {format_cost(claude_cost)}\n"
        f"🎨 画像: {format_cost(kie_img)}\n"
        f"🎬 動画: {format_cost(kie_vid)}\n"
        f"📊 合計: **{format_cost(total)}**"
    )
    await ctx.reply(text)


@bot.command(name="preset", aliases=["p"])
async def prefix_preset(ctx: commands.Context, category: str = ""):
    """!preset [category] — プリセット画像生成"""
    if not category or category not in IMAGE_PROMPTS:
        categories = "\n".join(f"  `{k}` — {v['label']}" for k, v in PRESETS.items())
        await ctx.reply(f"📂 **プリセット一覧:**\n{categories}\n\n使い方: `!preset female_beauty`")
        return
    prompt = random.choice(IMAGE_PROMPTS[category])
    info = PRESETS.get(category, {})
    msg = await ctx.reply(f"📂 **{info.get('label', category)} 生成中...**")
    try:
        urls = await image_gen.generate(prompt=prompt, aspect_ratio="1:1")
        if not urls:
            await msg.edit(content="❌ 画像生成に失敗しました")
            return
        store_last_media(ctx.channel.id, urls[0], "image", prompt, "1:1")
        _gallery_items[ctx.author.id].append({"url": urls[0], "prompt": category, "status": "pending"})
        filename = f"preset_{uuid.uuid4().hex[:6]}.png"
        await msg.edit(content=f"✅ **{info.get('label', category)} 完了!**")
        await download_and_send(ctx, urls[0], filename, "")
    except Exception as e:
        await msg.edit(content=f"❌ エラー: {type(e).__name__}: {str(e)[:200]}")


@bot.command(name="help_gen", aliases=["hg", "h"])
async def prefix_help_gen(ctx: commands.Context):
    """!help_gen — コマンド一覧"""
    embed = discord.Embed(title="🎨 全コマンド一覧", color=0x667eea)
    embed.add_field(name="🎨 生成", value="`!image` `!character` `!video` `!batch` `!variation` `!upscale`", inline=False)
    embed.add_field(name="📱 投稿", value="`!caption` (投稿は / コマンドのみ: `/post` `/schedule`)", inline=False)
    embed.add_field(name="📊 管理", value="`!status` `!cost`", inline=False)
    embed.add_field(name="🧠 AI", value="`!ask` `!trend` `!translate`", inline=False)
    embed.add_field(name="🎭 ワークフロー", value="ギャラリー・キューは `/gallery` `/queue` をどうぞ", inline=False)
    embed.add_field(name="🔧 オプション", value="`--ratio wide/tall/square/insta` `--count N`", inline=False)
    embed.set_footer(text="スラッシュコマンド (/) のほうがオプション選択が楽です！")
    await ctx.reply(embed=embed)


# ========================================
# Entry point
# ========================================

def main():
    token = os.environ.get("DISCORD_BOT_TOKEN", settings.discord_bot_token)
    if not token:
        print("❌ DISCORD_BOT_TOKEN が設定されていません")
        print("")
        print("=== セットアップ手順 ===")
        print("1. https://discord.com/developers/applications にアクセス")
        print("2. 「New Application」→ 名前入力 → Create")
        print("3. 左メニュー「Bot」→「Reset Token」→ コピー")
        print("4. 「MESSAGE CONTENT INTENT」をON")
        print("5. 「OAuth2」→「URL Generator」→ bot + permissions → サーバーに招待")
        print("6. .env に DISCORD_BOT_TOKEN=your_token を追加")
        print("7. python -m src.discord_bot で起動")
        return

    bot.run(token)


if __name__ == "__main__":
    main()
