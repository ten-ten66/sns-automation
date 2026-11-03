"""Tests for all platform classes: X, Instagram, TikTok, YouTube."""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from src.platforms.base import PostResult, RateLimiter


# ---------------------------------------------------------------------------
# PostResult dataclass
# ---------------------------------------------------------------------------

class TestPostResult:
    def test_success_result(self):
        result = PostResult(success=True, platform="x", post_id="123", url="https://x.com/i/status/123")
        assert result.success is True
        assert result.platform == "x"
        assert result.post_id == "123"
        assert result.url == "https://x.com/i/status/123"
        assert result.error is None

    def test_failure_result(self):
        result = PostResult(success=False, platform="instagram", error="Instagram requires media.")
        assert result.success is False
        assert result.platform == "instagram"
        assert result.post_id is None
        assert result.url is None
        assert result.error == "Instagram requires media."

    def test_default_optional_fields(self):
        result = PostResult(success=True, platform="tiktok")
        assert result.post_id is None
        assert result.url is None
        assert result.error is None


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_init_parameters(self):
        rl = RateLimiter(max_requests=10, window_seconds=60)
        assert rl.max_requests == 10
        assert rl.window_seconds == 60
        assert len(rl._timestamps) == 0

    @pytest.mark.asyncio
    async def test_acquire_within_limit(self):
        rl = RateLimiter(max_requests=5, window_seconds=60)
        # Should not block when under limit
        for _ in range(5):
            await rl.acquire()
        assert len(rl._timestamps) == 5

    @pytest.mark.asyncio
    async def test_acquire_records_timestamp(self):
        rl = RateLimiter(max_requests=3, window_seconds=60)
        await rl.acquire()
        assert len(rl._timestamps) == 1


# ---------------------------------------------------------------------------
# XPlatform
# ---------------------------------------------------------------------------

class TestXPlatform:
    @pytest.fixture
    def x_platform(self):
        with patch("tweepy.OAuth1UserHandler"), patch("tweepy.API"), patch("tweepy.Client"):
            from src.platforms.x_platform import XPlatform
            platform = XPlatform()
            return platform

    def test_platform_name(self, x_platform):
        assert x_platform.platform_name == "x"

    def test_rate_limiter_initialized(self, x_platform):
        assert x_platform.rate_limiter is not None
        assert x_platform.rate_limiter.max_requests == 95
        assert x_platform.rate_limiter.window_seconds == 86400

    @pytest.mark.asyncio
    async def test_post_text_success(self, x_platform):
        # _post_tweet returns a plain dict matching X API v2 response shape
        mock_response = {"data": {"id": "tweet123"}}
        x_platform.client.create_tweet = MagicMock(return_value=mock_response)

        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_response)):
            result = await x_platform.post_text("Hello world!")

        assert result.success is True
        assert result.platform == "x"
        assert result.post_id == "tweet123"
        assert "tweet123" in result.url

    @pytest.mark.asyncio
    async def test_post_text_failure(self, x_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("API error"))):
            result = await x_platform.post_text("Hello world!")

        assert result.success is False
        assert result.platform == "x"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_post_image_success(self, x_platform):
        mock_media = MagicMock()
        mock_media.media_id = "media_id_123"
        mock_media.media_id_string = "media_id_123"

        mock_tweet_response = MagicMock()
        mock_tweet_response.data = {"id": "tweet456"}

        async def fake_to_thread(fn, *args, **kwargs):
            if fn == x_platform.api.media_upload:
                return mock_media
            return mock_tweet_response

        with patch("asyncio.to_thread", new=AsyncMock(side_effect=fake_to_thread)):
            result = await x_platform.post_image("Caption", "/fake/image.jpg")

        assert result.success is True
        assert result.platform == "x"

    @pytest.mark.asyncio
    async def test_post_image_failure(self, x_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("Upload failed"))):
            result = await x_platform.post_image("Caption", "/fake/image.jpg")

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_post_video_success(self, x_platform):
        mock_media = MagicMock()
        mock_media.media_id_string = "video_media_id"

        mock_tweet_response = MagicMock()
        mock_tweet_response.data = {"id": "tweet789"}

        call_count = 0

        async def fake_to_thread(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_media
            return mock_tweet_response

        with patch("asyncio.to_thread", new=AsyncMock(side_effect=fake_to_thread)):
            result = await x_platform.post_video("Caption", "/fake/video.mp4")

        assert result.success is True
        assert result.platform == "x"

    @pytest.mark.asyncio
    async def test_post_video_failure(self, x_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("Video upload failed"))):
            result = await x_platform.post_video("Caption", "/fake/video.mp4")

        assert result.success is False

    @pytest.mark.asyncio
    async def test_get_comments_success(self, x_platform):
        mock_user = MagicMock()
        mock_user.id = 999
        mock_user.username = "testuser"

        mock_tweet = MagicMock()
        mock_tweet.id = "reply123"
        mock_tweet.text = "Great post!"
        mock_tweet.author_id = 999
        mock_tweet.created_at = None

        mock_response = MagicMock()
        mock_response.data = [mock_tweet]
        mock_response.includes = {"users": [mock_user]}

        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_response)):
            comments = await x_platform.get_comments("tweet123")

        assert len(comments) == 1
        assert comments[0]["text"] == "Great post!"
        assert comments[0]["username"] == "testuser"

    @pytest.mark.asyncio
    async def test_get_comments_empty_on_error(self, x_platform):
        from src.platforms.x_platform import reset_x_search_disabled
        reset_x_search_disabled()
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("API Error"))):
            comments = await x_platform.get_comments("tweet123")

        assert comments == []

    @pytest.mark.asyncio
    async def test_get_comments_latches_search_disabled_on_401(self, x_platform):
        """First 401 on search should set the global latch; subsequent calls skip search."""
        import tweepy
        from src.platforms.x_platform import (
            reset_x_search_disabled,
            is_x_search_disabled,
        )
        reset_x_search_disabled()
        assert not is_x_search_disabled()

        # Simulate the exact tweepy.Unauthorized that the Basic plan returns.
        # tweepy.Unauthorized takes a response argument; build a minimal mock.
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "Unauthorized"
        fake_resp.headers = {}
        fake_resp.json.return_value = {"errors": [{"message": "Unauthorized"}]}
        unauth_exc = tweepy.Unauthorized(fake_resp)

        # Pre-populate _my_user_id so _get_my_id() short-circuits without an
        # extra asyncio.to_thread call — this keeps the call-count assertion
        # focused on the search/mentions API calls only.
        x_platform._my_user_id = 12345

        with patch("asyncio.to_thread", new=AsyncMock(side_effect=unauth_exc)) as mock_thread:
            comments = await x_platform.get_comments("tweet123")
            assert comments == []
            assert is_x_search_disabled(), "first 401 must latch the global flag"
            # The first call makes 2 API calls: search (raises 401 → latch)
            # then the mentions fallback (also raises 401 → empty return).
            assert mock_thread.await_count == 2

            # Second call skips search (latched) but still tries the mentions
            # fallback — so it makes exactly 1 more API call (not 2).
            comments2 = await x_platform.get_comments("tweet456")
            assert comments2 == []
            assert mock_thread.await_count == 3, "second call: only mentions fallback, no search"

        reset_x_search_disabled()  # cleanup so other tests start fresh

    @pytest.mark.asyncio
    async def test_search_viral_short_circuits_when_disabled(self, x_platform):
        from src.platforms.x_platform import (
            reset_x_search_disabled,
            _disable_x_search,
        )
        reset_x_search_disabled()
        _disable_x_search("test pre-seed")

        with patch("asyncio.to_thread", new=AsyncMock()) as mock_thread:
            results = await x_platform.search_viral("skincare")
            assert results == []
            assert mock_thread.await_count == 0, "must short-circuit before any API call"

        reset_x_search_disabled()

    @pytest.mark.asyncio
    async def test_search_viral_latches_on_401(self, x_platform):
        import tweepy
        from src.platforms.x_platform import reset_x_search_disabled, is_x_search_disabled
        reset_x_search_disabled()

        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "Unauthorized"
        fake_resp.headers = {}
        fake_resp.json.return_value = {"errors": []}
        unauth_exc = tweepy.Unauthorized(fake_resp)

        with patch("asyncio.to_thread", new=AsyncMock(side_effect=unauth_exc)):
            results = await x_platform.search_viral("skincare")
            assert results == []
            assert is_x_search_disabled()

        reset_x_search_disabled()

    def test_is_search_unauthorized_helper(self):
        import tweepy
        from src.platforms.x_platform import _is_search_unauthorized
        fake_resp = MagicMock()
        fake_resp.status_code = 401
        fake_resp.text = "Unauthorized"
        fake_resp.headers = {}
        fake_resp.json.return_value = {"errors": []}
        assert _is_search_unauthorized(tweepy.Unauthorized(fake_resp))
        # String fallback (some tweepy versions wrap exceptions)
        assert _is_search_unauthorized(Exception("401 Unauthorized"))
        # Negatives
        assert not _is_search_unauthorized(Exception("500 Internal Server Error"))
        assert not _is_search_unauthorized(Exception("Network unreachable"))
        assert not _is_search_unauthorized(ValueError("bad input"))

    @pytest.mark.asyncio
    async def test_post_reply_success(self, x_platform):
        mock_response = MagicMock()
        mock_response.data = {"id": "reply_tweet_id"}

        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_response)):
            result = await x_platform.post_reply("post123", "comment456", "Thanks!")

        assert result.success is True
        assert result.platform == "x"

    @pytest.mark.asyncio
    async def test_post_reply_failure(self, x_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("Reply failed"))):
            result = await x_platform.post_reply("post123", "comment456", "Thanks!")

        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# InstagramPlatform
# ---------------------------------------------------------------------------

class TestInstagramPlatform:
    @pytest.fixture
    def instagram_platform(self):
        with patch("instagrapi.Client") as mock_client_class:
            mock_client = MagicMock()
            mock_client_class.return_value = mock_client
            with patch("os.path.exists", return_value=False):
                from src.platforms.instagram_platform import InstagramPlatform
                platform = InstagramPlatform()
                platform.client = mock_client
                return platform

    def test_platform_name(self, instagram_platform):
        assert instagram_platform.platform_name == "instagram"

    def test_rate_limiter_initialized(self, instagram_platform):
        assert instagram_platform.rate_limiter is not None
        assert instagram_platform.rate_limiter.max_requests == 30
        assert instagram_platform.rate_limiter.window_seconds == 3600

    @pytest.mark.asyncio
    async def test_post_text_returns_failure(self, instagram_platform):
        """Instagram doesn't support text-only posts."""
        with patch("asyncio.sleep", new=AsyncMock()):
            result = await instagram_platform.post_text("Hello!")

        assert result.success is False
        assert result.platform == "instagram"
        assert "media" in result.error.lower() or "Instagram" in result.error

    @pytest.mark.asyncio
    async def test_post_image_success(self, instagram_platform):
        mock_media = MagicMock()
        mock_media.pk = 12345
        mock_media.code = "AbCdEfGhI"

        instagram_platform._ensure_login = AsyncMock()
        instagram_platform._logged_in = True

        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_media)):
                result = await instagram_platform.post_image("Caption", "/fake/image.jpg")

        assert result.success is True
        assert result.platform == "instagram"
        assert result.post_id == "12345"
        assert "instagram.com" in result.url

    @pytest.mark.asyncio
    async def test_post_video_success(self, instagram_platform):
        mock_media = MagicMock()
        mock_media.pk = 67890
        mock_media.code = "ZyXwVuTsR"

        instagram_platform._ensure_login = AsyncMock()
        instagram_platform._logged_in = True

        with patch("asyncio.sleep", new=AsyncMock()):
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_media)):
                result = await instagram_platform.post_video("Caption", "/fake/video.mp4")

        assert result.success is True
        assert result.platform == "instagram"

    @pytest.mark.asyncio
    async def test_get_comments_success(self, instagram_platform):
        mock_comment = MagicMock()
        mock_comment.pk = 111
        mock_comment.text = "Love this!"
        mock_comment.user = MagicMock(username="commenter1")
        mock_comment.created_at_utc = None

        instagram_platform._ensure_login = AsyncMock()

        with patch("asyncio.to_thread", new=AsyncMock(side_effect=[12345, [mock_comment]])):
            comments = await instagram_platform.get_comments("AbCdEfGhI")

        assert len(comments) == 1
        assert comments[0]["text"] == "Love this!"
        assert comments[0]["username"] == "commenter1"

    @pytest.mark.asyncio
    async def test_get_comments_failure_returns_empty(self, instagram_platform):
        instagram_platform._ensure_login = AsyncMock(side_effect=Exception("Not logged in"))

        comments = await instagram_platform.get_comments("post123")
        assert comments == []

    @pytest.mark.asyncio
    async def test_post_reply_success(self, instagram_platform):
        mock_result = MagicMock()
        mock_result.pk = 999

        instagram_platform._ensure_login = AsyncMock()

        with patch("asyncio.to_thread", new=AsyncMock(side_effect=[12345, mock_result])):
            result = await instagram_platform.post_reply("post123", "456", "Thanks!")

        assert result.success is True
        assert result.platform == "instagram"


# ---------------------------------------------------------------------------
# TikTokPlatform
# ---------------------------------------------------------------------------

class TestTikTokPlatform:
    @pytest.fixture
    def tiktok_platform(self):
        with patch("httpx.AsyncClient") as mock_http:
            mock_http.return_value = AsyncMock()
            from src.platforms.tiktok_platform import TikTokPlatform
            platform = TikTokPlatform()
            platform.client = AsyncMock()
            return platform

    def test_platform_name(self, tiktok_platform):
        assert tiktok_platform.platform_name == "tiktok"

    def test_rate_limiter_initialized(self, tiktok_platform):
        assert tiktok_platform.rate_limiter is not None
        assert tiktok_platform.rate_limiter.max_requests == 20
        assert tiktok_platform.rate_limiter.window_seconds == 3600

    @pytest.mark.asyncio
    async def test_post_text_returns_failure(self, tiktok_platform):
        """TikTok requires video content."""
        result = await tiktok_platform.post_text("Hello!")

        assert result.success is False
        assert result.platform == "tiktok"
        assert "video" in result.error.lower() or "TikTok" in result.error

    @pytest.mark.asyncio
    async def test_post_video_success(self, tiktok_platform):
        init_response = {"data": {"publish_id": "pub123", "upload_url": "https://upload.tiktok.com/video"}}

        tiktok_platform._request = AsyncMock(return_value=init_response)
        tiktok_platform.client.put = AsyncMock(return_value=MagicMock(status_code=200))

        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            f.write(b"fake video content")
            tmp_path = f.name

        try:
            with patch("aiofiles.open") as mock_aio:
                mock_file = AsyncMock()
                mock_file.__aenter__ = AsyncMock(return_value=mock_file)
                mock_file.__aexit__ = AsyncMock(return_value=False)
                mock_file.read = AsyncMock(return_value=b"fake video content")
                mock_aio.return_value = mock_file

                result = await tiktok_platform.post_video("Caption", tmp_path)
        finally:
            os.unlink(tmp_path)

        assert result.success is True
        assert result.platform == "tiktok"
        assert result.post_id == "pub123"

    @pytest.mark.asyncio
    async def test_post_video_missing_publish_id(self, tiktok_platform):
        import tempfile
        import os
        # Create a real temp file so os.path.getsize works
        fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
        os.write(fd, b"fake video content")
        os.close(fd)
        try:
            tiktok_platform._request = AsyncMock(return_value={"data": {}})
            result = await tiktok_platform.post_video("Caption", tmp_path)
        finally:
            os.unlink(tmp_path)

        assert result.success is False
        assert "missing" in result.error.lower() or "init failed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_post_image_success(self, tiktok_platform):
        init_response = {"data": {"publish_id": "img_pub456", "upload_url": "https://upload.tiktok.com/image"}}

        tiktok_platform._request = AsyncMock(return_value=init_response)
        tiktok_platform.client.put = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("aiofiles.open") as mock_aio:
            mock_file = AsyncMock()
            mock_file.__aenter__ = AsyncMock(return_value=mock_file)
            mock_file.__aexit__ = AsyncMock(return_value=False)
            mock_file.read = AsyncMock(return_value=b"fake image content")
            mock_aio.return_value = mock_file

            result = await tiktok_platform.post_image("Caption", "/fake/image.jpg")

        assert result.success is True
        assert result.platform == "tiktok"
        assert result.post_id == "img_pub456"

    @pytest.mark.asyncio
    async def test_get_comments_returns_empty(self, tiktok_platform):
        """TikTok get_comments is not supported via Content Posting API."""
        comments = await tiktok_platform.get_comments("video123")
        assert comments == []


# ---------------------------------------------------------------------------
# YouTubePlatform
# ---------------------------------------------------------------------------

class TestYouTubePlatform:
    @pytest.fixture
    def youtube_platform(self):
        with patch("googleapiclient.discovery.build"):
            from src.platforms.youtube_platform import YouTubePlatform
            platform = YouTubePlatform()
            platform.youtube = MagicMock()
            return platform

    def test_platform_name(self, youtube_platform):
        assert youtube_platform.platform_name == "youtube"

    def test_rate_limiter_initialized(self, youtube_platform):
        assert youtube_platform.rate_limiter is not None
        assert youtube_platform.rate_limiter.max_requests == 50
        assert youtube_platform.rate_limiter.window_seconds == 86400

    @pytest.mark.asyncio
    async def test_post_text_returns_failure(self, youtube_platform):
        """YouTube requires video content."""
        result = await youtube_platform.post_text("Hello!")

        assert result.success is False
        assert result.platform == "youtube"
        assert "video" in result.error.lower()

    @pytest.mark.asyncio
    async def test_post_image_returns_failure(self, youtube_platform):
        """YouTube requires video content."""
        result = await youtube_platform.post_image("Caption", "/fake/image.jpg")

        assert result.success is False
        assert result.platform == "youtube"
        assert "video" in result.error.lower()

    @pytest.mark.asyncio
    async def test_post_video_success(self, youtube_platform):
        mock_result = PostResult(
            success=True,
            platform="youtube",
            post_id="yt_video_id",
            url="https://www.youtube.com/watch?v=yt_video_id",
        )
        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_result)):
            result = await youtube_platform.post_video("Title", "/fake/video.mp4")

        assert result.success is True
        assert result.platform == "youtube"
        assert "youtube.com" in result.url

    @pytest.mark.asyncio
    async def test_post_video_failure(self, youtube_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("OAuth not configured"))):
            result = await youtube_platform.post_video("Title", "/fake/video.mp4")

        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_get_comments_success(self, youtube_platform):
        mock_response = {
            "items": [
                {
                    "snippet": {
                        "topLevelComment": {
                            "id": "comment_id_1",
                            "snippet": {
                                "textDisplay": "Great video!",
                                "authorDisplayName": "viewer1",
                                "publishedAt": "2024-01-01T00:00:00Z",
                            },
                        }
                    }
                }
            ]
        }

        youtube_platform.youtube.commentThreads.return_value.list.return_value.execute.return_value = mock_response

        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_response)):
            comments = await youtube_platform.get_comments("yt_video_id")

        assert len(comments) == 1
        assert comments[0]["text"] == "Great video!"
        assert comments[0]["username"] == "viewer1"

    @pytest.mark.asyncio
    async def test_get_comments_no_youtube_client(self, youtube_platform):
        """When youtube client is None, return empty list."""
        youtube_platform.youtube = None
        comments = await youtube_platform.get_comments("yt_video_id")
        assert comments == []

    @pytest.mark.asyncio
    async def test_get_comments_failure_returns_empty(self, youtube_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("API error"))):
            comments = await youtube_platform.get_comments("yt_video_id")

        assert comments == []

    @pytest.mark.asyncio
    async def test_post_reply_success(self, youtube_platform):
        mock_response = {"id": "reply_comment_id"}

        with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_response)):
            result = await youtube_platform.post_reply("video123", "comment456", "Thank you!")

        assert result.success is True
        assert result.platform == "youtube"
        assert result.post_id == "reply_comment_id"

    @pytest.mark.asyncio
    async def test_post_reply_oauth_not_configured(self, youtube_platform):
        with patch("asyncio.to_thread", new=AsyncMock(side_effect=RuntimeError("YouTube OAuth credentials not configured"))):
            result = await youtube_platform.post_reply("video123", "comment456", "Thanks!")

        assert result.success is False
        assert "OAuth" in result.error or "credentials" in result.error
