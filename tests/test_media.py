"""导出时的图片处理：相对路径改写与 data URL 内嵌。"""

import media


def _asset(tmp_path, monkeypatch, name: str, payload: bytes):
    import db as dbmod

    monkeypatch.setattr(dbmod, "ASSETS_DIR", tmp_path)
    (tmp_path / "art_1").mkdir(exist_ok=True)
    (tmp_path / "art_1" / name).write_bytes(payload)


def test_to_relative_rewrites_and_collects(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, "gen_1.jpg", b"fake-image")
    text, files = media.to_relative("正文\n\n![配图](/assets/art_1/gen_1.jpg)\n")
    assert "/assets/" not in text
    assert "图片/gen_1.jpg" in text
    assert [name for name, _ in files] == ["gen_1.jpg"]


def test_to_relative_skips_missing_files(tmp_path, monkeypatch):
    import db as dbmod

    monkeypatch.setattr(dbmod, "ASSETS_DIR", tmp_path)
    text, files = media.to_relative("![x](/assets/art_1/nope.jpg)")
    # 磁盘上没有的文件不该被写进包里
    assert files == []
    assert "/assets/" in text


def test_inline_as_data_urls(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, "gen_1.png", b"\x89PNG\r\n")
    result, count = media.inline_as_data_urls('<img src="/assets/art_1/gen_1.png">')
    assert count == 1
    assert "data:image/png;base64," in result
    assert "/assets/" not in result


def test_inline_bails_out_when_too_large(tmp_path, monkeypatch):
    _asset(tmp_path, monkeypatch, "big.jpg", b"x" * 2048)
    html = '<img src="/assets/art_1/big.jpg">'
    result, count = media.inline_as_data_urls(html, limit_mb=0.001)
    # 超过体积上限就不内嵌，避免产出几十 MB 的 HTML
    assert count == 0
    assert result == html
