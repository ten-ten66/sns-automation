"""validate_media_path must only accept files inside the media directory."""

import pytest
from fastapi import HTTPException

import src.api.app as app_module


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(app_module, "ALLOWED_MEDIA_DIR", media)
    return media


def test_accepts_file_inside_media_dir(media_dir):
    f = media_dir / "ok.jpg"
    f.write_bytes(b"x")
    assert app_module.validate_media_path(str(f)) == str(f.resolve())


def test_rejects_sibling_dir_sharing_prefix(media_dir):
    evil = media_dir.parent / "media_evil"
    evil.mkdir()
    f = evil / "secret.jpg"
    f.write_bytes(b"x")
    with pytest.raises(HTTPException) as exc:
        app_module.validate_media_path(str(f))
    assert exc.value.status_code == 400


def test_rejects_parent_traversal(media_dir):
    with pytest.raises(HTTPException) as exc:
        app_module.validate_media_path(str(media_dir / ".." / "outside.jpg"))
    assert exc.value.status_code == 400
