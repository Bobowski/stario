"""Tests for obsolete stario.staticassets: fingerprint, manifest, cache build."""

import tempfile
from pathlib import Path

import pytest

from stario.exceptions import StarioError
from stario.staticassets import AssetManifest, StaticAssets, fingerprint

pytestmark = pytest.mark.filterwarnings(
    "ignore:stario.staticassets is obsolete:DeprecationWarning"
)


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
            pytest.raises(StarioError, match="must start with '/'"),
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
                url_prefix="//cdn.example.com/static",
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
            manifest = AssetManifest(tmpdir, url_prefix="//cdn.example.com/static")

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
