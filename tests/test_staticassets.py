"""Tests for stario.staticassets - AssetManifest fingerprinting + StaticAssets serving."""

import tempfile
from pathlib import Path

import pytest

from stario import App, UrlPath
from stario.exceptions import StarioError
from stario.http.compression import CompressionConfig
from stario.staticassets import AssetManifest, StaticAssets, fingerprint
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

    def test_content_hash_stability(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f1:
            f1.write("Same content")
            f1.flush()
            path1 = Path(f1.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f2:
            f2.write("Same content")
            f2.flush()
            path2 = Path(f2.name)

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f3:
            f3.write("Different content")
            f3.flush()
            path3 = Path(f3.name)

        fp1 = fingerprint(path1)
        fp2 = fingerprint(path2)
        fp3 = fingerprint(path3)

        path1.unlink()
        path2.unlink()
        path3.unlink()

        assert fp1 == fp2
        assert fp1 != fp3


class TestAssetManifest:
    """Test AssetManifest scanning and URL resolution."""

    def test_nonexistent_directory_raises(self):
        with pytest.raises(StarioError, match="not found"):
            AssetManifest("/nonexistent/path")

    def test_scans_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "test.txt").write_text("Hello")
            (Path(tmpdir) / "style.css").write_text("body {}")

            manifest = AssetManifest(tmpdir)

            assert len(manifest.assets) == 2
            assert "test.txt" in manifest.assets
            assert "style.css" in manifest.assets
            assert manifest.assets["test.txt"].size == 5
            assert manifest.assets["test.txt"].modified_ns > 0

    def test_skips_hidden_files_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / ".DS_Store").write_text("junk")
            public_dir = Path(tmpdir) / "css"
            public_dir.mkdir()
            (public_dir / "style.css").write_text("body {}")
            hidden_dir = Path(tmpdir) / ".cache"
            hidden_dir.mkdir()
            (hidden_dir / "secret.txt").write_text("nope")

            manifest = AssetManifest(tmpdir)

            assert ".DS_Store" not in manifest.assets
            assert ".cache/secret.txt" not in manifest.assets
            assert "css/style.css" in manifest.assets

    def test_can_include_hidden_files_explicitly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            well_known = Path(tmpdir) / ".well-known"
            well_known.mkdir()
            (well_known / "assetlinks.json").write_text("{}")

            manifest = AssetManifest(tmpdir, include_hidden=True)

            assert ".well-known/assetlinks.json" in manifest.assets

    def test_skips_symlinked_files_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "target.txt"
            target.write_text("target")
            link = root / "linked.txt"
            link.symlink_to(target)

            manifest = AssetManifest(root)

            assert "target.txt" in manifest.assets
            assert "linked.txt" not in manifest.assets

    def test_can_follow_symlinked_files_explicitly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "target.txt"
            target.write_text("target")
            link = root / "linked.txt"
            link.symlink_to(target)

            manifest = AssetManifest(root, follow_symlinks=True)

            assert "linked.txt" in manifest.assets
            assert manifest.assets["linked.txt"].size == len("target")

    def test_skips_symlinked_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "static"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret")
            link_dir = root / "link"
            link_dir.symlink_to(outside)

            manifest = AssetManifest(root)

            assert "link/secret.txt" not in manifest.assets
            assert not any("secret" in path for path in manifest.assets)

    def test_rejects_url_prefix_without_leading_slash(self):
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            pytest.raises(StarioError, match="path must start with '/'"),
        ):
            AssetManifest(tmpdir, url_prefix="static")

    def test_builds_fingerprinted_asset_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            manifest = AssetManifest(tmpdir, url_prefix="/static")

            url = manifest.href("style.css")
            assert url.startswith("/static/style.")
            assert url.endswith(".css")

    def test_unknown_asset_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest = AssetManifest(tmpdir, url_prefix="/static")

            with pytest.raises(StarioError, match="Static asset not found"):
                manifest.href("missing.css")

    def test_host_url_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            manifest = AssetManifest(
                tmpdir,
                url_prefix=UrlPath("/static", host="cdn.example.com"),
            )

            url = manifest.href("style.css")
            assert url.startswith("//cdn.example.com/static/style.")
            assert url.endswith(".css")

    def test_root_url_prefix_does_not_add_double_slash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            manifest = AssetManifest(tmpdir, url_prefix="/")

            url = manifest.href("style.css")
            assert url.startswith("/style.")
            assert not url.startswith("//")
            assert url.endswith(".css")

    def test_nested_asset_preserves_relative_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            css_dir = Path(tmpdir) / "css"
            css_dir.mkdir()
            (css_dir / "style.css").write_text("body {}")

            manifest = AssetManifest(tmpdir, url_prefix="/static")

            url = manifest.href("css/style.css")
            assert url.startswith("/static/css/style.")
            assert url.endswith(".css")

    def test_href_appends_query_and_fragment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")

            manifest = AssetManifest(tmpdir, url_prefix="/static")

            url = manifest.href(
                "style.css",
                query={"theme": "dark mode"},
                fragment="top",
            )
            assert url.startswith("/static/style.")
            assert url.endswith(".css?theme=dark+mode#top")


class TestStaticAssetsCaching:
    """Test file caching behavior."""

    def test_missing_file_after_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "test.txt"
            target.write_text("Hello")

            manifest = AssetManifest(tmpdir)
            target.unlink()

            with pytest.raises(StarioError, match="missing from disk"):
                StaticAssets(manifest)

    def test_changed_file_after_manifest_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "test.txt"
            target.write_text("Hello")

            manifest = AssetManifest(tmpdir)
            target.write_text("Changed")

            with pytest.raises(StarioError, match="changed after manifest build"):
                StaticAssets(manifest)

    def test_host_prefixed_manifest_cannot_be_served_locally(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "style.css").write_text("body {}")
            manifest = AssetManifest(
                tmpdir, url_prefix=UrlPath("/static", host="cdn.example.com")
            )

            with pytest.raises(StarioError, match="app-relative manifests"):
                StaticAssets(manifest)

    def test_precompression_can_select_codecs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "compressible.txt").write_text("x" * 1000)

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest, precompress=("br",))
            cached = static._cache[manifest.assets["compressible.txt"].hashed_path]

            assert cached.brotli is not None
            assert cached.zstd is None
            assert cached.gzip is None
            assert static.stats["brotli_files"] == 1
            assert static.stats["zstd_files"] == 0
            assert static.stats["gzip_files"] == 0

    def test_precompression_rejects_unknown_codecs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "compressible.txt").write_text("x" * 1000)

            with pytest.raises(StarioError, match="unsupported codecs"):
                StaticAssets(AssetManifest(tmpdir), precompress=("deflate",))  # type: ignore[arg-type]

    def test_already_compressed_not_precompressed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 500)

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest)

            hashed_name = manifest.assets["image.png"].hashed_path
            cached = static._cache[hashed_name]

            assert cached.zstd is None
            assert cached.gzip is None

    def test_content_type_lookup_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "IMAGE.PNG").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 500)

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest)
            cached = static._cache[manifest.assets["IMAGE.PNG"].hashed_path]

            assert cached.content_type == b"image/png"

    def test_content_types_can_be_overridden_per_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "site.webmanifest").write_text("{}")

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(
                manifest,
                content_types={".webmanifest": "application/manifest+json"},
            )
            cached = static._cache[manifest.assets["site.webmanifest"].hashed_path]

            assert cached.content_type == b"application/manifest+json"


@pytest.mark.asyncio
class TestStaticAssetsRedirect:
    """Test that unfingerprinted URLs redirect to their fingerprinted equivalents."""

    async def test_root_file_redirect_is_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.js").write_text("console.log('hi');")

            static = StaticAssets(AssetManifest(tmpdir))
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.get("/static/app.js", follow_redirects=False)

            assert resp.status_code == 307
            location = resp.headers.get("location")
            assert location is not None
            assert location.startswith("/static/")
            assert location.endswith(".js")

    async def test_subdirectory_file_redirect_preserves_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            js_dir = Path(tmpdir) / "js"
            js_dir.mkdir()
            (js_dir / "app.js").write_text("console.log('hi');")

            static = StaticAssets(AssetManifest(tmpdir))
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.get("/static/js/app.js", follow_redirects=False)

            assert resp.status_code == 307
            location = resp.headers.get("location")
            assert location is not None
            assert location.startswith("/static/js/")
            assert location.endswith(".js")

    async def test_fingerprinted_url_serves_directly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            js_dir = Path(tmpdir) / "js"
            js_dir.mkdir()
            (js_dir / "app.js").write_text("console.log('hi');")

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest)
            app = App()
            static.register(app)

            fingerprinted_url = manifest.href("js/app.js")

            async with TestClient(app) as client:
                resp = await client.get(fingerprinted_url)

            assert resp.status_code == 200
            assert b"console.log" in resp.content


@pytest.mark.asyncio
class TestStaticAssetsServing:
    async def test_missing_asset_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.js").write_text("ok")

            static = StaticAssets(AssetManifest(tmpdir))
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.get("/static/missing.js")

            assert resp.status_code == 404
            assert resp.text == "Not Found"

    async def test_head_returns_headers_without_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "app.js").write_text("console.log('hi');")

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest)
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.head(manifest.href("app.js"))

            assert resp.status_code == 200
            assert resp.content == b""
            assert resp.headers.get("content-length") == "18"
            assert "max-age" in (resp.headers.get("cache-control") or "")

    async def test_large_file_served_from_disk_with_head_content_length(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"x" * 128
            (Path(tmpdir) / "big.bin").write_bytes(payload)

            manifest = AssetManifest(tmpdir, url_prefix="/static")
            static = StaticAssets(manifest, cache_max_size=64)
            app = App()
            static.register(app)
            url = manifest.href("big.bin")

            async with TestClient(app) as client:
                head = await client.head(url)
                get = await client.get(url)

            assert head.status_code == 200
            assert head.content == b""
            assert head.headers.get("content-length") == "128"
            assert get.status_code == 200
            assert get.content == payload
            assert get.headers.get("accept-ranges") == "bytes"

    async def test_large_file_serves_single_byte_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"0123456789"
            (Path(tmpdir) / "video.mp4").write_bytes(payload)

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest, cache_max_size=4)
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.get(
                    manifest.href("video.mp4"),
                    headers={"Range": "bytes=2-5"},
                )

            assert resp.status_code == 206
            assert resp.content == b"2345"
            assert resp.headers.get("content-range") == "bytes 2-5/10"
            assert resp.headers.get("content-length") == "4"
            assert resp.headers.get("accept-ranges") == "bytes"

    async def test_large_file_serves_suffix_byte_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"0123456789"
            (Path(tmpdir) / "video.mp4").write_bytes(payload)

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest, cache_max_size=4)
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.get(
                    manifest.href("video.mp4"),
                    headers={"Range": "bytes=-3"},
                )

            assert resp.status_code == 206
            assert resp.content == b"789"
            assert resp.headers.get("content-range") == "bytes 7-9/10"

    async def test_large_file_unsatisfiable_range_returns_416(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = b"0123456789"
            (Path(tmpdir) / "video.mp4").write_bytes(payload)

            manifest = AssetManifest(tmpdir)
            static = StaticAssets(manifest, cache_max_size=4)
            app = App()
            static.register(app)

            async with TestClient(app) as client:
                resp = await client.get(
                    manifest.href("video.mp4"),
                    headers={"Range": "bytes=99-100"},
                )

            assert resp.status_code == 416
            assert resp.content == b""
            assert resp.headers.get("content-range") == "bytes */10"
            assert resp.headers.get("accept-ranges") == "bytes"


@pytest.mark.asyncio
class TestStaticAssetsContentNegotiation:
    """HTTP-level Accept-Encoding negotiation for in-memory cached files."""

    @staticmethod
    def _compressible_app() -> tuple[StaticAssets, App, str]:
        tmpdir = tempfile.mkdtemp()
        (Path(tmpdir) / "app.css").write_text("body { color: red; }\n" * 64)
        manifest = AssetManifest(tmpdir)
        static = StaticAssets(manifest, compression=CompressionConfig(min_size=1))
        app = App()
        static.register(app)
        return static, app, manifest.href("app.css")

    async def test_brotli_preferred_when_all_codecs_accepted(self):
        _, app, url = self._compressible_app()
        async with TestClient(app) as client:
            resp = await client.get(url, headers={"Accept-Encoding": "gzip, zstd, br"})

        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "br"
        assert "accept-encoding" in (resp.headers.get("vary") or "").lower()
        assert "color: red" in resp.text  # client decompressed it

    async def test_qvalues_select_higher_priority_codec(self):
        _, app, url = self._compressible_app()
        async with TestClient(app) as client:
            resp = await client.get(
                url, headers={"Accept-Encoding": "br;q=0.1, gzip;q=0.9"}
            )

        assert resp.headers.get("content-encoding") == "gzip"

    async def test_q_zero_disables_codec(self):
        _, app, url = self._compressible_app()
        async with TestClient(app) as client:
            resp = await client.get(
                url, headers={"Accept-Encoding": "br;q=0, zstd;q=0, gzip;q=0"}
            )

        assert resp.headers.get("content-encoding") is None
        assert "color: red" in resp.text

    async def test_no_accept_encoding_serves_identity(self):
        _, app, url = self._compressible_app()
        async with TestClient(app) as client:
            resp = await client.get(url, headers={"Accept-Encoding": ""})

        assert resp.headers.get("content-encoding") is None
        assert "accept-encoding" in (resp.headers.get("vary") or "").lower()

    async def test_explicit_identity_q_can_win(self):
        _, app, url = self._compressible_app()
        async with TestClient(app) as client:
            resp = await client.get(
                url,
                headers={"Accept-Encoding": "identity;q=1, br;q=0.5"},
            )

        assert resp.headers.get("content-encoding") is None
        assert "accept-encoding" in (resp.headers.get("vary") or "").lower()

    async def test_wildcard_accept_encoding_serves_compressed(self):
        _, app, url = self._compressible_app()
        async with TestClient(app) as client:
            resp = await client.get(url, headers={"Accept-Encoding": "*"})

        assert resp.headers.get("content-encoding") == "br"

    async def test_head_reports_negotiated_content_length(self):
        static, app, url = self._compressible_app()
        hashed = url.removeprefix("/static/")
        compressed_size = len(static._cache[hashed].brotli or b"")
        assert compressed_size > 0

        async with TestClient(app) as client:
            head = await client.head(url, headers={"Accept-Encoding": "br"})

        assert head.status_code == 200
        assert head.headers.get("content-encoding") == "br"
        assert head.headers.get("content-length") == str(compressed_size)

    async def test_unknown_extension_uses_octet_stream(self):
        tmpdir = tempfile.mkdtemp()
        (Path(tmpdir) / "data.unknownext").write_bytes(b"\x00\x01")
        manifest = AssetManifest(tmpdir)
        static = StaticAssets(manifest)
        app = App()
        static.register(app)

        async with TestClient(app) as client:
            resp = await client.get(manifest.href("data.unknownext"))

        assert resp.headers.get("content-type") == "application/octet-stream"
