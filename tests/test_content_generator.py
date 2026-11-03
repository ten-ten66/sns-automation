"""Tests for the Claude content generator."""

import json
import pytest
from unittest.mock import MagicMock, patch
from src.ai.claude_generator import ClaudeContentGenerator, GeneratedContent


class TestClaudeContentGenerator:
    """Test content generation with mocked Claude API."""

    @pytest.fixture(autouse=True)
    def setup(self):
        with patch("anthropic.Anthropic") as mock_anthropic:
            self.mock_client = MagicMock()
            mock_anthropic.return_value = self.mock_client
            self.generator = ClaudeContentGenerator()
            yield

    def _mock_response(self, data: dict):
        response = MagicMock()
        response.content = [MagicMock(text=json.dumps(data, ensure_ascii=False))]
        response.stop_reason = "end_turn"
        response.usage = MagicMock(input_tokens=200, output_tokens=100)
        self.mock_client.messages.create.return_value = response

    def test_generate_post_basic(self):
        self._mock_response({
            "text": "今日のスキンケアおすすめ♡",
            "hashtags": ["スキンケア", "美容"],
            "keywords": ["スキンケア", "おすすめ"],
            "suggested_media_prompt": "A beauty flatlay",
            "viral_score_estimate": 0.7,
            "variations": ["バリエーション1"],
        })
        result = self.generator.generate_post(platform="x", topic="スキンケア")
        assert isinstance(result, GeneratedContent)
        assert result.text == "今日のスキンケアおすすめ♡"
        assert "スキンケア" in result.hashtags
        assert result.viral_score_estimate == 0.7

    def test_generate_post_compliance_applied(self):
        """Test that 薬機法 violations are caught."""
        self._mock_response({
            "text": "この美容液でシミが消える！",
            "hashtags": ["美容液"],
            "keywords": ["美容液"],
        })
        result = self.generator.generate_post(platform="instagram", topic="美容液")
        assert "シミが消え" not in result.text

    def test_generate_post_pr_marker(self):
        """Test PR posts get proper disclosure."""
        self._mock_response({
            "text": "おすすめのファンデーション！",
            "hashtags": ["PR", "ファンデ"],
            "keywords": ["ファンデーション"],
        })
        result = self.generator._generate_content_with_compliance(
            "test prompt", is_pr=True
        )
        assert result.text.startswith("【PR】")

    def test_ab_test_variants(self):
        self._mock_response([
            {"variant_id": "A", "text": "Version A", "change_description": "Changed hook"},
            {"variant_id": "B", "text": "Version B", "change_description": "Changed CTA"},
        ])
        # Since _call_claude_json returns the parsed response directly
        result = self.generator.ab_test_variants("test content", "x", 2)
        assert isinstance(result, list)

    def test_generate_comment_reply(self):
        response = MagicMock()
        response.content = [MagicMock(text="ありがとう♡嬉しい🥰")]
        response.stop_reason = "end_turn"
        response.usage = MagicMock(input_tokens=50, output_tokens=20)
        self.mock_client.messages.create.return_value = response
        result = self.generator.generate_comment_reply("このリップ何色ですか？")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_persona_context_contains_compliance(self):
        ctx = self.generator._persona_context
        assert "FTC" in ctx
        assert "Banned" in ctx
        assert "PR" in ctx

    def test_generate_beauty_review(self):
        self._mock_response({
            "text": "【PR】話題のリップ使ってみた♡",
            "hashtags": ["リップ", "コスメレビュー"],
            "keywords": ["リップ", "レビュー"],
        })
        result = self.generator.generate_beauty_review("XXX リップ", "instagram", is_pr=True)
        assert isinstance(result, GeneratedContent)
        assert result.text.startswith("【PR】")

    def test_budget_exceeded_raises(self):
        """Test that budget exceeded prevents API calls."""
        with patch("src.core.cost_tracker.cost_tracker.is_budget_exceeded", return_value=True):
            with pytest.raises(RuntimeError, match="budget exceeded"):
                self.generator._call_claude("test prompt")
