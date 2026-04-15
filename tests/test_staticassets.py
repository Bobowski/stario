"""Tests for stario.http.staticassets - Static file serving with fingerprinting."""

import tempfile
from pathlib import Path

import pytest

from stario import App
from stario.exceptions import StarioError
from stario.http.router import Router
from stario.http.staticassets import StaticAssets, fingerprint
from stario.testing import TestClient


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

            static = StaticAssets(tmpdir)

            assert len(static._cache) == 2
            assert len(static._path_to_hash) == 2


class TestUrlForAssets:
    """Test asset URL resolution through url_for()."""

    def test_assets_reject_missing_leading_slash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()

            with pytest.raises(StarioError, match=r"Expected '/static'"):
                app.mount("static", StaticAssets(tmpdir, name="static"))

    def test_assets_reject_mounts_that_conflict_with_internal_catchall(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()

            with pytest.raises(
                StarioError, match="Catchall mount prefix cannot have child routes"
            ):
                app.mount("/static/{existing...}", StaticAssets(tmpdir, name="static"))

    def test_named_asset_resolves_full_public_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            url = app.url_for("static:style.css")
            assert url.startswith("/static/style.")
            assert url.endswith(".css")

    def test_mounted_router_resolves_prefixed_asset_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            chat = Router()
            chat.mount("/static", StaticAssets(tmpdir, name="chat"))

            app = App()
            app.mount("/chat", chat)

            url = app.url_for("chat:style.css")

            assert url.startswith("/chat/static/style.")
            assert url.endswith(".css")

    def test_host_asset_name_includes_host_and_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()
            app.mount("cdn.example.com/static", StaticAssets(tmpdir, name="static"))

            url = app.url_for("static:style.css")

            assert url.startswith("cdn.example.com/static/style.")
            assert url.endswith(".css")

    def test_root_asset_name_does_not_add_double_slash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()
            app.mount("/", StaticAssets(tmpdir, name="static"))

            url = app.url_for("static:style.css")

            assert url.startswith("/style.")
            assert not url.startswith("//")
            assert url.endswith(".css")

    def test_nested_asset_name_preserves_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            css_dir = Path(tmpdir) / "css"
            css_dir.mkdir()
            (css_dir / "style.css").write_text("body {}")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            url = app.url_for("static:css/style.css")
            assert url.startswith("/static/css/style.")
            assert url.endswith(".css")

    def test_unknown_asset_name_raises(self):
        app = App()

        with pytest.raises(
            StarioError,
            match="Register the route or asset first with name='missing:style.css'",
        ):
            app.url_for("missing:style.css")

    def test_asset_mount_name_is_not_registered_as_collection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            with pytest.raises(StarioError, match="Reverse route not registered"):
                app.url_for("static")

    def test_asset_routes_append_queries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            url = app.url_for("static:style.css", query={"v": 1, "debug": True})
            assert url.startswith("/static/style.")
            assert url.endswith(".css?v=1&debug=True")

    def test_static_assets_mount_directly_as_subtree(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            url = app.url_for("static:style.css")
            assert url.startswith("/static/style.")
            assert url.endswith(".css")


class TestStaticAssetsCaching:
    """Test file caching behavior."""

    def test_small_file_cached_in_memory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = "Small file content"
            (Path(tmpdir) / "small.txt").write_text(content)

            static = StaticAssets(tmpdir)

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

            static = StaticAssets(tmpdir)

            hashed_name = static._path_to_hash["compressible.txt"]
            cached = static._cache[hashed_name]

            # Should have pre-compressed variants
            assert cached.zstd is not None
            assert cached.brotli is not None
            assert cached.gzip is not None
            assert cached.content is not None
            # Compressed should be smaller
            assert len(cached.zstd) < len(cached.content)

    def test_select_body_prefers_brotli_then_zstd_then_gzip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            content = "x" * 1000
            (Path(tmpdir) / "compressible.txt").write_text(content)

            static = StaticAssets(tmpdir)
            hashed_name = static._path_to_hash["compressible.txt"]
            cached = static._cache[hashed_name]

            assert static._select_body(cached, b"gzip, zstd, br")[1] == b"br"
            assert static._select_body(cached, b"gzip, zstd")[1] == b"zstd"
            assert static._select_body(cached, b"gzip")[1] == b"gzip"

    def test_already_compressed_not_precompressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 500)

            static = StaticAssets(tmpdir)

            hashed_name = static._path_to_hash["image.png"]
            cached = static._cache[hashed_name]

            # PNG is already compressed, should skip pre-compression
            assert cached.zstd is None
            assert cached.gzip is None


@pytest.mark.asyncio
class TestStaticAssetsRedirect:
    """Test that unfingerprinted URLs redirect to their fingerprinted equivalents."""

    async def test_root_file_redirect_is_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.js").write_text("console.log('hi');")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            async with TestClient(app) as client:
                resp = await client.get("/static/app.js", follow_redirects=False)

            assert resp.status_code == 307
            location = resp.headers.get("location")
            # Must be an absolute path (starts with /) not just a filename
            assert location is not None
            assert location.startswith("/static/")
            assert location.endswith(".js")

    async def test_subdirectory_file_redirect_preserves_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            js_dir = Path(tmpdir) / "js"
            js_dir.mkdir()
            (js_dir / "app.js").write_text("console.log('hi');")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            async with TestClient(app) as client:
                resp = await client.get("/static/js/app.js", follow_redirects=False)

            assert resp.status_code == 307
            location = resp.headers.get("location")
            # Must preserve the js/ subdirectory prefix
            assert location is not None
            assert location.startswith("/static/js/")
            assert location.endswith(".js")

    async def test_fingerprinted_url_serves_directly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            js_dir = Path(tmpdir) / "js"
            js_dir.mkdir()
            (js_dir / "app.js").write_text("console.log('hi');")

            app = App()
            app.mount("/static", StaticAssets(tmpdir, name="static"))

            fingerprinted_url = app.url_for("static:js/app.js")

            async with TestClient(app) as client:
                resp = await client.get(fingerprinted_url)

            assert resp.status_code == 200
            assert b"console.log" in resp.content
