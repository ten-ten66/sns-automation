"""Tests for core utility functions."""

import pytest
from src.core.utils import (
    parse_llm_json,
    validate_compliance,
    extract_claude_text,
    to_generated_content,
)
from unittest.mock import MagicMock


class TestParseLLMJson:
    """Test parse_llm_json with various LLM outputs."""

    def test_clean_json(self):
        result = parse_llm_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_with_markdown_fences(self):
        result = parse_llm_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_json_array(self):
        result = parse_llm_json('[{"a": 1}, {"b": 2}]')
        assert isinstance(result, list)
        assert len(result) == 2

    def test_json_with_leading_text(self):
        result = parse_llm_json('Here is the result:\n{"key": "value"}')
        assert result == {"key": "value"}

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Empty LLM response"):
            parse_llm_json("")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Failed to parse JSON"):
            parse_llm_json("this is not json at all")

    def test_nested_json(self):
        text = '{"outer": {"inner": [1, 2, 3]}, "key": "value"}'
        result = parse_llm_json(text)
        assert result["outer"]["inner"] == [1, 2, 3]

    def test_json_with_trailing_text(self):
        result = parse_llm_json('{"key": "value"}\n\nI hope this helps!')
        assert result == {"key": "value"}


class TestValidateCompliance:
    """Test 薬機法 and ステマ規制 compliance validation."""

    def test_clean_text_no_changes(self):
        text, warnings = validate_compliance("これはとても良い化粧水です")
        assert text == "これはとても良い化粧水です"
        assert warnings == []

    def test_prohibited_claim_removed(self):
        text, warnings = validate_compliance("この化粧水でシミが消える！")
        assert "シミが消え" not in text
        assert len(warnings) > 0
        assert "薬機法" in warnings[0]

    def test_multiple_prohibited_claims(self):
        text, warnings = validate_compliance("シミが消える上に毛穴がなくなる！さらに肌が白くなる")
        assert "シミが消え" not in text
        assert "毛穴がなくなる" not in text
        assert "肌が白くなる" not in text
        assert len(warnings) >= 3

    def test_pr_marker_added_when_missing(self):
        text, warnings = validate_compliance("おすすめのリップです", is_pr=True)
        assert text.startswith("【PR】")
        assert any("PR表記" in w for w in warnings)

    def test_pr_marker_not_duplicated(self):
        text, warnings = validate_compliance("【PR】おすすめのリップです", is_pr=True)
        assert text.startswith("【PR】")
        assert not any("PR表記" in w for w in warnings)

    def test_non_pr_no_marker(self):
        text, warnings = validate_compliance("おすすめのリップです", is_pr=False)
        assert not text.startswith("【PR】")


class TestExtractClaudeText:
    """Test Claude response text extraction."""

    def test_normal_response(self):
        response = MagicMock()
        response.content = [MagicMock(text="Hello world")]
        response.stop_reason = "end_turn"
        assert extract_claude_text(response) == "Hello world"

    def test_empty_content_raises(self):
        response = MagicMock()
        response.content = []
        with pytest.raises(ValueError, match="empty content"):
            extract_claude_text(response)

    def test_no_text_attribute_raises(self):
        response = MagicMock()
        block = MagicMock(spec=[])  # No text attribute
        del block.text
        response.content = [block]
        with pytest.raises(ValueError):
            extract_claude_text(response)

    def test_truncated_response_raises(self):
        from src.core.utils import TruncatedResponseError
        response = MagicMock()
        response.content = [MagicMock(text='{"partial": "data')]
        response.stop_reason = "max_tokens"
        with pytest.raises(TruncatedResponseError):
            extract_claude_text(response)


class TestParseLLMJsonEdgeCases:
    """Test edge cases in JSON parsing."""

    def test_braces_inside_string(self):
        """Test that braces inside JSON strings don't confuse the parser."""
        text = '{"text": "Use {name} format", "key": "value"}'
        result = parse_llm_json(text)
        assert result["key"] == "value"

    def test_multiple_json_objects_in_text(self):
        """Test extraction when multiple JSON-like structures exist."""
        text = 'Result: {"text": "hello", "count": 1} done'
        result = parse_llm_json(text)
        assert result["text"] == "hello"


class TestToGeneratedContent:
    """Test safe conversion from dict to GeneratedContent."""

    def test_valid_data(self):
        data = {
            "text": "Test post",
            "hashtags": ["beauty"],
            "keywords": ["skincare"],
            "viral_score_estimate": 0.7,
        }
        content = to_generated_content(data)
        assert content.text == "Test post"
        assert content.hashtags == ["beauty"]
        assert content.viral_score_estimate == 0.7

    def test_missing_text_raises(self):
        with pytest.raises(ValueError, match="missing required 'text'"):
            to_generated_content({"hashtags": ["test"]})

    def test_unknown_fields_ignored(self):
        data = {
            "text": "Test",
            "hashtags": [],
            "keywords": [],
            "unknown_field": "should be ignored",
        }
        content = to_generated_content(data)
        assert content.text == "Test"

    def test_defaults_for_optional_fields(self):
        data = {"text": "Test"}
        content = to_generated_content(data)
        assert content.hashtags == []
        assert content.keywords == []
