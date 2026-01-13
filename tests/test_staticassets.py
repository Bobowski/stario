"""Tests for stario.http.staticassets - Static file serving with fingerprinting."""

import tempfile
from pathlib import Path

import pytest

from stario.exceptions import StarioError
from stario.http.staticassets import StaticAssets, _collections, asset, fingerprint


class TestFingerprint:
    """Test file fingerprinting function."""

    def test_generates_hash(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("Hello, World!")
            f.flush()
            path = Path(f.name)

        fp = fingerprint(path)
        path.unlink()

        assert len(fp) == 16  # xxHash64 hex is 16 chars
        assert fp.isalnum()

    def test_same_content_same_hash(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f1:
            f1.write("Same content")
            f1.flush()
            path1 = Path(f1.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f2:
            f2.write("Same content")
            f2.flush()
            path2 = Path(f2.name)

        fp1 = fingerprint(path1)
        fp2 = fingerprint(path2)

        path1.unlink()
        path2.unlink()

        assert fp1 == fp2

    def test_different_content_different_hash(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f1:
            f1.write("Content A")
            f1.flush()
            path1 = Path(f1.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f2:
            f2.write("Content B")
            f2.flush()
            path2 = Path(f2.name)

        fp1 = fingerprint(path1)
        fp2 = fingerprint(path2)

        path1.unlink()
        path2.unlink()

        assert fp1 != fp2


class TestStaticAssetsInit:
    """Test StaticAssets initialization."""

    def test_nonexistent_directory_raises(self):
        with pytest.raises(StarioError, match="not found"):
            StaticAssets("/nonexistent/path")

    def test_creates_cache(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("Hello")
            (Path(tmpdir) / "style.css").write_text("body {}")

            static = StaticAssets(tmpdir, collection="test_init")

            assert len(static._cache) == 2
            assert len(static._path_to_hash) == 2

    def test_registers_collection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "file.txt").write_text("content")

            static = StaticAssets(tmpdir, collection="test_register")

            assert "test_register" in _collections
            assert _collections["test_register"] is static


class TestStaticAssetsUrl:
    """Test URL generation."""

    def test_returns_fingerprinted_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.js").write_text("console.log('hello');")

            static = StaticAssets(tmpdir, collection="test_url")
            url = static.url("app.js")

            # Should be like "app.{hash}.js"
            assert url.startswith("app.")
            assert url.endswith(".js")
            assert len(url) > len("app.js")

    def test_nested_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "css"
            subdir.mkdir()
            (subdir / "main.css").write_text("* { margin: 0; }")

            static = StaticAssets(tmpdir, collection="test_nested")
            url = static.url("css/main.css")

            assert "css/" in url
            assert "main." in url
            assert ".css" in url

    def test_not_found_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "exists.txt").write_text("I exist")

            static = StaticAssets(tmpdir, collection="test_notfound")

            with pytest.raises(StarioError, match="not found"):
                static.url("missing.txt")


class TestAssetFunction:
    """Test global asset() function."""

    def test_asset_returns_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            StaticAssets(tmpdir, collection="test_asset_fn")

            url = asset("style.css", collection="test_asset_fn")
            assert "style." in url
            assert ".css" in url

    def test_asset_unknown_collection_raises(self):
        with pytest.raises(KeyError, match="not registered"):
            asset("file.txt", collection="nonexistent_collection")


class TestStaticAssetsCaching:
    """Test file caching behavior."""

    def test_small_file_cached_in_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = "Small file content"
            (Path(tmpdir) / "small.txt").write_text(content)

            static = StaticAssets(tmpdir, collection="test_small")

            # Get the fingerprinted key
            hashed_name = static._path_to_hash["small.txt"]
            cached = static._cache[hashed_name]

            assert cached.content is not None
            assert cached.content == content.encode()

    def test_precompression(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file large enough to be worth compressing
            content = "x" * 1000  # 1KB of repeated chars
            (Path(tmpdir) / "compressible.txt").write_text(content)

            static = StaticAssets(tmpdir, collection="test_compress")

            hashed_name = static._path_to_hash["compressible.txt"]
            cached = static._cache[hashed_name]

            # Should have pre-compressed variants
            assert cached.zstd is not None
            assert cached.gzip is not None
            assert cached.content is not None
            # Compressed should be smaller
            assert len(cached.zstd) < len(cached.content)

    def test_already_compressed_not_precompressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 500)

            static = StaticAssets(tmpdir, collection="test_nocompress")

            hashed_name = static._path_to_hash["image.png"]
            cached = static._cache[hashed_name]

            # PNG is already compressed, should skip pre-compression
            assert cached.zstd is None
            assert cached.gzip is None
