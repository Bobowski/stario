"""
Fingerprinted static assets in two explicit halves:

`AssetManifest` scans a directory once and maps logical paths to fingerprinted URLs. It is cheap
(hashing only), so build it at module level and resolve URLs with `href("path/to/file")`.
Symlinked files are skipped by default; symlinked directories are never followed. Resolved
paths must stay inside the static root. Pass `follow_symlinks=True` when your static tree
intentionally uses file symlinks (still contained under the root).

`StaticAssets` is the route handler: it takes a manifest and pays the serving costs — loading small
files into memory, pre-compressing them, streaming large files from disk. Build it during bootstrap
and call `register(app)`.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

import aiofiles
import xxhash

import stario.responses as responses
from stario.exceptions import StarioError
from stario.http.app import App
from stario.http.compression import (
    CompressionConfig,
    content_type_is_compressible,
    merge_vary,
    negotiate_content_encoding,
)
from stario.http.context import Context
from stario.http.headers import encode_header_value
from stario.http.writer import Writer
from stario.routing import UrlPath, append_query_fragment

type CompressionCodec = Literal["br", "zstd", "gzip"]

CONTENT_TYPES: Final[Mapping[str, bytes]] = MappingProxyType(
    {
        ".html": b"text/html; charset=utf-8",
        ".htm": b"text/html; charset=utf-8",
        ".css": b"text/css; charset=utf-8",
        ".js": b"application/javascript; charset=utf-8",
        ".mjs": b"application/javascript; charset=utf-8",
        ".json": b"application/json; charset=utf-8",
        ".xml": b"application/xml; charset=utf-8",
        ".txt": b"text/plain; charset=utf-8",
        ".md": b"text/markdown; charset=utf-8",
        ".png": b"image/png",
        ".jpg": b"image/jpeg",
        ".jpeg": b"image/jpeg",
        ".gif": b"image/gif",
        ".svg": b"image/svg+xml; charset=utf-8",
        ".ico": b"image/x-icon",
        ".webp": b"image/webp",
        ".avif": b"image/avif",
        ".woff": b"font/woff",
        ".woff2": b"font/woff2",
        ".ttf": b"font/ttf",
        ".otf": b"font/otf",
        ".eot": b"application/vnd.ms-fontobject",
        ".pdf": b"application/pdf",
        ".zip": b"application/zip",
        ".gz": b"application/gzip",
        ".br": b"application/brotli",
        ".mp3": b"audio/mpeg",
        ".mp4": b"video/mp4",
        ".webm": b"video/webm",
        ".wasm": b"application/wasm",
    }
)

_DEFAULT_CONTENT_TYPE: Final = b"application/octet-stream"
_DEFAULT_HASH_CHUNK_SIZE: Final = 4 << 20
_DEFAULT_PRECOMPRESS: Final[tuple[CompressionCodec, ...]] = ("br", "zstd", "gzip")
_VALID_PRECOMPRESS: Final = frozenset(_DEFAULT_PRECOMPRESS)
_RANGE_NOT_SATISFIABLE: Final = object()
_STATIC_ASSET_COMPRESSION: Final = CompressionConfig(
    # Higher levels than live responses; assets are compressed once at bootstrap.
    min_size=256,
    zstd_level=9,
    zstd_window_log=21,
    brotli_level=9,
    brotli_window_log=22,
    gzip_level=7,
    gzip_window_bits=15,
)


def fingerprint(path: Path, *, chunk_size: int = _DEFAULT_HASH_CHUNK_SIZE) -> str:
    """xxHash64 of file bytes (hex digest) for fingerprinted static URLs."""
    hasher = xxhash.xxh64()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def _has_hidden_part(path: Path) -> bool:
    """Return True when any relative path segment is hidden (`.name`)."""
    return any(part.startswith(".") for part in path.parts)


def _header_bytes(value: str | bytes, *, field: str) -> bytes:
    if isinstance(value, str):
        try:
            return encode_header_value(value)
        except ValueError as exc:
            raise StarioError(
                "Static asset header value must not contain control characters",
                context={"field": field},
                help_text="Use a single header value without CR/LF characters.",
            ) from exc
    if b"\r" in value or b"\n" in value:
        raise StarioError(
            "Static asset header value must not contain control characters",
            context={"field": field},
            help_text="Use a single header value without CR/LF characters.",
        )
    return value


def _build_content_types(
    overrides: Mapping[str, str | bytes] | None,
) -> dict[str, bytes]:
    content_types = dict(CONTENT_TYPES)
    if not overrides:
        return content_types

    for suffix, content_type in overrides.items():
        if not suffix.startswith("."):
            raise StarioError(
                "Static asset content type keys must be file suffixes",
                context={"suffix": suffix},
                help_text="Use keys such as '.css' or '.webmanifest'.",
            )
        content_types[suffix.lower()] = _header_bytes(
            content_type,
            field=f"content_types[{suffix!r}]",
        )
    return content_types


def _normalize_precompress(
    codecs: Iterable[CompressionCodec],
) -> tuple[CompressionCodec, ...]:
    if isinstance(codecs, str | bytes):
        raise StarioError(
            "StaticAssets precompress must be an iterable of codec names",
            context={"precompress": codecs},
            help_text="Pass values like ('br', 'zstd', 'gzip') or an empty tuple.",
        )

    normalized: list[CompressionCodec] = []
    seen: set[str] = set()
    invalid: list[object] = []
    for codec in codecs:
        if codec not in _VALID_PRECOMPRESS:
            invalid.append(codec)
            continue
        if codec not in seen:
            normalized.append(codec)
            seen.add(codec)

    if invalid:
        raise StarioError(
            "StaticAssets precompress contains unsupported codecs",
            context={"unsupported": invalid, "supported": sorted(_VALID_PRECOMPRESS)},
            help_text="Use only 'br', 'zstd', and 'gzip'.",
        )
    return tuple(normalized)


_CACHED_ENCODINGS: Final = ((b"br", "brotli"), (b"zstd", "zstd"), (b"gzip", "gzip"))


@dataclass(slots=True, frozen=True)
class ByteRange:
    """Inclusive byte range from an HTTP Range request."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1


def _parse_byte_range(header: bytes, size: int) -> ByteRange | object | None:
    """Parse a single `Range: bytes=...` value."""
    if not header:
        return None

    unit, sep, raw_spec = header.partition(b"=")
    if sep != b"=" or unit.strip().lower() != b"bytes":
        return None

    spec = raw_spec.strip()
    if b"," in spec or b"-" not in spec or size <= 0:
        return _RANGE_NOT_SATISFIABLE

    start_s, _, end_s = spec.partition(b"-")
    try:
        if not start_s:
            suffix_length = int(end_s)
            if suffix_length <= 0:
                return _RANGE_NOT_SATISFIABLE
            return ByteRange(max(size - suffix_length, 0), size - 1)

        start = int(start_s)
        if start < 0 or start >= size:
            return _RANGE_NOT_SATISFIABLE

        if not end_s:
            return ByteRange(start, size - 1)

        end = int(end_s)
    except ValueError:
        return _RANGE_NOT_SATISFIABLE

    if end < start:
        return _RANGE_NOT_SATISFIABLE
    return ByteRange(start, min(end, size - 1))


@dataclass(slots=True, frozen=True)
class Asset:
    """One file in an `AssetManifest`."""

    logical_path: str
    """Path relative to the manifest directory, e.g. `css/style.css`."""
    hashed_path: str
    """Fingerprinted relative path, e.g. `css/style.abc123.css`."""
    url: str
    """Public URL (prefix + hashed path), e.g. `/static/css/style.abc123.css`."""
    source: Path
    """Absolute path to the file on disk."""
    size: int
    """File size recorded when the manifest was built."""
    modified_ns: int
    """Filesystem mtime (nanoseconds) recorded when the manifest was built."""


class AssetManifest:
    """
    Scan a directory once: fingerprint every public file and map logical paths to public URLs.

    Hashing only — no file contents are kept in memory — so a manifest is cheap enough to build at
    module level and use for `href` constants. Hand it to `StaticAssets` during bootstrap to
    actually serve the files. Hidden files and hidden directories are skipped by default; pass
    `include_hidden=True` when that is intentional. Symlinked files are also skipped unless
    `follow_symlinks=True` is passed; keep this false for static directories that should be
    self-contained.
    """

    __slots__ = ("assets", "directory", "prefix")

    def __init__(
        self,
        directory: Path | str = "./static",
        *,
        url_prefix: str | UrlPath = "/static",
        hash_chunk_size: int = _DEFAULT_HASH_CHUNK_SIZE,
        include_hidden: bool = False,
        follow_symlinks: bool = False,
    ) -> None:
        self.directory = Path(directory).resolve()
        if not self.directory.is_dir():
            raise StarioError(
                f"Static assets directory not found: {self.directory}",
                context={
                    "path": str(self.directory),
                    "exists": self.directory.exists(),
                },
                help_text="Create the directory or check the path before building AssetManifest.",
            )
        self.prefix = (
            url_prefix if isinstance(url_prefix, UrlPath) else UrlPath(url_prefix)
        )

        assets: dict[str, Asset] = {}
        root = self.directory
        for p in root.rglob("*"):
            if p.is_symlink() and not follow_symlinks:
                continue
            if not p.is_file():
                continue
            try:
                relative_path = p.relative_to(root)
            except ValueError:
                continue
            resolved = p.resolve()
            if not resolved.is_relative_to(root):
                continue
            if not include_hidden and _has_hidden_part(relative_path):
                continue
            logical_path = relative_path.as_posix()
            before = resolved.stat()
            hashed_name = (
                f"{resolved.stem}.{fingerprint(resolved, chunk_size=hash_chunk_size)}{resolved.suffix}"
            )
            after = resolved.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise StarioError(
                    "Static asset changed while building manifest",
                    context={"path": logical_path, "source": str(resolved)},
                    help_text="Finish writing assets before constructing AssetManifest.",
                )
            hashed_path = relative_path.with_name(hashed_name).as_posix()
            assets[logical_path] = Asset(
                logical_path=logical_path,
                hashed_path=hashed_path,
                url=(self.prefix / hashed_path).href(),
                source=resolved,
                size=after.st_size,
                modified_ns=after.st_mtime_ns,
            )
        self.assets = MappingProxyType(assets)

    def href(
        self,
        path: str,
        /,
        *,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
    ) -> str:
        """Build the public fingerprinted URL for a logical asset path under this tree."""
        logical_path = path.strip("/")
        try:
            href = self.assets[logical_path].url
        except KeyError as exc:
            raise StarioError(
                "Static asset not found",
                context={"path": logical_path},
                help_text="Ensure the asset exists in the AssetManifest directory.",
            ) from exc
        return append_query_fragment(href, query=query, fragment=fragment)


@dataclass(slots=True)
class CachedFile:
    """
    Cached file entry.

    For small files: content + pre-compressed variants loaded in memory.
    For large files: only metadata, content read from disk on demand.
    """

    size: int
    modified_ns: int
    content_type: bytes
    # None = large file, read from disk
    content: bytes | None = None
    # Source path for large files (disk read on demand)
    source: Path | None = None
    # Path for large files (disk read on demand)
    # Pre-compressed variants (None if not worth compressing or large file)
    zstd: bytes | None = None
    brotli: bytes | None = None
    gzip: bytes | None = None


class StaticAssets:
    """
    Serve an `AssetManifest`: cache small files (with pre-compression), stream large files.

    Construction reads and compresses files, so build it during bootstrap — not at module level —
    then call `register(app)`. Build URLs with `href(path)` on either the manifest
    or this serving wrapper.
    Non-fingerprint paths 307 to hashed URLs. Large streamed files support one
    `Range: bytes=...` request at a time.

    Use `precompress=()` to disable startup compression, or choose an explicit subset such as
    `precompress=("br",)`. Pass `compression=CompressionConfig(...)` to control levels,
    windows, and the minimum size. Use `content_types={".webmanifest": "application/manifest+json"}`
    for per-instance MIME overrides without mutating framework defaults.

    Stario favors small fingerprinted assets: keep generated CSS/JS/images compact enough to
    cache and pre-compress at bootstrap. `cache_max_size` is an escape hatch for large files,
    which are streamed from disk and only support range requests in their uncompressed form.

    `stats` summarizes what construction did (file counts, raw vs compressed bytes); attach it to
    a span if you want the cost in traces:

    ```python
    with span.step("static_assets") as s:
        assets = StaticAssets(ASSETS)
        s.attrs(assets.stats)
    assets.register(app)
    ```
    """

    __slots__ = (
        "_cache",
        "_cache_control_bytes",
        "_content_types",
        "_route",
        "cache_max_size",
        "compression",
        "filesystem_chunk_size",
        "manifest",
        "precompress",
        "stats",
    )

    def __init__(
        self,
        manifest: AssetManifest,
        *,
        cache_control: str = "public, max-age=31536000, immutable",
        cache_max_size: int = 1 << 20,
        filesystem_chunk_size: int = 65536,
        precompress: Iterable[CompressionCodec] = _DEFAULT_PRECOMPRESS,
        content_types: Mapping[str, str | bytes] | None = None,
        compression: CompressionConfig = _STATIC_ASSET_COMPRESSION,
    ) -> None:
        if manifest.prefix.host:
            raise StarioError(
                "StaticAssets can only serve app-relative manifests",
                context={"url_prefix": manifest.prefix.text},
                help_text=(
                    "Use an app-relative AssetManifest prefix such as '/static' when "
                    "serving locally. Host-prefixed manifests are for URL generation, "
                    "for example when assets are hosted by a CDN."
                ),
            )
        self.manifest = manifest
        self._route = manifest.prefix / "{path...}"
        if cache_max_size <= 0:
            raise StarioError(
                "StaticAssets numeric limits must be positive",
                context={"field": "cache_max_size", "value": cache_max_size},
                help_text="Use a positive integer for cache, compression, and filesystem sizes.",
            )
        self.cache_max_size = cache_max_size
        if compression.min_size < 0:
            raise StarioError(
                "StaticAssets numeric limits must be non-negative",
                context={
                    "field": "compression.min_size",
                    "value": compression.min_size,
                },
                help_text="Use zero or a positive integer for size thresholds.",
            )
        self.compression = compression
        if filesystem_chunk_size <= 0:
            raise StarioError(
                "StaticAssets numeric limits must be positive",
                context={
                    "field": "filesystem_chunk_size",
                    "value": filesystem_chunk_size,
                },
                help_text="Use a positive integer for cache, compression, and filesystem sizes.",
            )
        self.filesystem_chunk_size = filesystem_chunk_size
        self.precompress = _normalize_precompress(precompress)
        self._content_types = _build_content_types(content_types)
        self._cache_control_bytes = _header_bytes(cache_control, field="cache_control")

        self._cache: dict[str, CachedFile] = {}
        for asset in manifest.assets.values():
            self._verify_asset(asset)
            self._cache[asset.hashed_path] = self._create_cached_file(asset)

        stats = {
            # File counts: cached (in memory) + streamed (large, from disk) = files.
            "files": len(self._cache),
            "cached_files": 0,
            "streamed_files": 0,
            "compressed_files": 0,
            "zstd_files": 0,
            "brotli_files": 0,
            "gzip_files": 0,
            # Byte totals: raw size of all files, raw size of the cached subset,
            # and bytes held in memory per pre-compressed variant.
            "raw_bytes": 0,
            "cached_bytes": 0,
            "zstd_bytes": 0,
            "brotli_bytes": 0,
            "gzip_bytes": 0,
        }
        for f in self._cache.values():
            stats["raw_bytes"] += f.size
            if f.content is None:
                stats["streamed_files"] += 1
                continue
            stats["cached_files"] += 1
            stats["cached_bytes"] += f.size
            if f.zstd is not None or f.brotli is not None or f.gzip is not None:
                stats["compressed_files"] += 1
            if f.zstd is not None:
                stats["zstd_files"] += 1
            if f.brotli is not None:
                stats["brotli_files"] += 1
            if f.gzip is not None:
                stats["gzip_files"] += 1
            stats["zstd_bytes"] += len(f.zstd) if f.zstd is not None else 0
            stats["brotli_bytes"] += len(f.brotli) if f.brotli is not None else 0
            stats["gzip_bytes"] += len(f.gzip) if f.gzip is not None else 0
        self.stats = MappingProxyType(stats)

    def _verify_asset(self, asset: Asset) -> None:
        try:
            stat = asset.source.stat()
        except FileNotFoundError as exc:
            raise StarioError(
                "Static asset listed in the manifest is missing from disk",
                context={
                    "directory": str(self.manifest.directory),
                    "path": asset.logical_path,
                    "hashed_path": asset.hashed_path,
                },
                help_text="The file changed after the AssetManifest was built. Rebuild the manifest.",
            ) from exc

        if stat.st_size != asset.size or stat.st_mtime_ns != asset.modified_ns:
            raise StarioError(
                "Static asset changed after manifest build",
                context={
                    "directory": str(self.manifest.directory),
                    "path": asset.logical_path,
                    "hashed_path": asset.hashed_path,
                    "expected_size": asset.size,
                    "actual_size": stat.st_size,
                },
                help_text="Rebuild the AssetManifest after writing static files.",
            )

    def register(self, app: App) -> None:
        """Register GET/HEAD catch-all routes on the application."""
        app.get(self._route, self)
        app.head(self._route, self)

    def href(
        self,
        path: str,
        /,
        *,
        query: Mapping[str, object] | None = None,
        fragment: str | None = None,
    ) -> str:
        """Build the public fingerprinted URL for a logical asset path."""
        return self.manifest.href(path, query=query, fragment=fragment)

    def _create_cached_file(self, asset: Asset) -> CachedFile:
        """Create a cached file entry: in-memory for small files, metadata-only for large files."""
        path = asset.source
        size = asset.size
        suffix = path.suffix.lower()
        content_type = self._content_types.get(suffix, _DEFAULT_CONTENT_TYPE)

        if size > self.cache_max_size:
            return CachedFile(
                size=size,
                modified_ns=asset.modified_ns,
                content_type=content_type,
                source=path,
            )

        content = path.read_bytes()

        if (
            not content_type_is_compressible(content_type)
            or size < self.compression.min_size
            or not self.precompress
        ):
            return CachedFile(
                size=size,
                modified_ns=asset.modified_ns,
                content_type=content_type,
                content=content,
            )

        # Startup compression can spend a little more CPU to shrink long-lived assets.
        zstd_data: bytes | None = None
        if "zstd" in self.precompress and self.compression.zstd_level >= 0:
            zstd_data = self.compression.make_compressor(b"zstd").frame(content)

        brotli_data: bytes | None = None
        if "br" in self.precompress and self.compression.brotli_level >= 0:
            brotli_data = self.compression.make_compressor(b"br").frame(content)

        gzip_data: bytes | None = None
        if "gzip" in self.precompress and self.compression.gzip_level >= 0:
            gzip_data = self.compression.make_compressor(b"gzip").frame(content)

        return CachedFile(
            size=size,
            modified_ns=asset.modified_ns,
            content_type=content_type,
            content=content,
            zstd=zstd_data if zstd_data is not None and len(zstd_data) < size else None,
            brotli=(
                brotli_data
                if brotli_data is not None and len(brotli_data) < size
                else None
            ),
            gzip=gzip_data if gzip_data is not None and len(gzip_data) < size else None,
        )

    def _select_body(self, f: CachedFile, accept: bytes) -> tuple[bytes, bytes | None]:
        """Select best body variant based on Accept-Encoding."""
        assert f.content is not None, "Cannot select body for large file"

        if not accept:
            return f.content, None

        available = [
            enc for enc, attr in _CACHED_ENCODINGS if getattr(f, attr) is not None
        ]
        if not available:
            return f.content, None

        best_encoding = negotiate_content_encoding(accept, available)
        if best_encoding is None:
            return f.content, None

        attr = next(a for e, a in _CACHED_ENCODINGS if e == best_encoding)
        return getattr(f, attr), best_encoding

    async def _serve_streamed_file(
        self,
        c: Context,
        w: Writer,
        f: CachedFile,
    ) -> None:
        """Serve a large file from disk, optionally honoring one byte range."""
        assert f.source is not None, "Large file must have a source path"

        # Large files are read at request time, so verify the file identity before
        # serving bytes under an immutable fingerprinted URL.
        try:
            stat = f.source.stat()
        except FileNotFoundError:
            responses.text(w, "Not Found", 404)
            return
        if stat.st_size != f.size or stat.st_mtime_ns != f.modified_ns:
            responses.text(w, "Not Found", 404)
            return

        h = w.headers
        h.unsafe_set(b"accept-ranges", b"bytes")
        byte_range = _parse_byte_range(c.req.headers.unsafe_get(b"range", b""), f.size)

        if byte_range is _RANGE_NOT_SATISFIABLE:
            h.unsafe_set(b"content-range", b"bytes */%d" % f.size)
            h.unsafe_set(b"content-length", b"0")
            w.write_headers(416).end()
            return

        if isinstance(byte_range, ByteRange):
            h.unsafe_set(
                b"content-range",
                b"bytes %d-%d/%d" % (byte_range.start, byte_range.end, f.size),
            )
            h.unsafe_set(b"content-length", b"%d" % byte_range.length)
            if c.req.method == "HEAD":
                w.write_headers(206).end()
                return
            w.write_headers(206)
            await self._write_file_range(
                w, f.source, byte_range.start, byte_range.length
            )
            return

        h.unsafe_set(b"content-length", b"%d" % f.size)
        if c.req.method == "HEAD":
            w.write_headers(200).end()
            return

        w.write_headers(200)
        await self._write_file_range(w, f.source, 0, f.size)

    async def _write_file_range(
        self,
        w: Writer,
        path: Path,
        start: int,
        length: int,
    ) -> None:
        remaining = length
        async with aiofiles.open(path, "rb") as fp:
            if start:
                await fp.seek(start)
            while remaining > 0:
                chunk = await fp.read(min(self.filesystem_chunk_size, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                if w.closing:
                    return
                w.write(chunk)
        w.end()

    async def __call__(self, c: Context, w: Writer) -> None:
        """GET/HEAD handler: resolve `{path...}` against the manifest, redirect, 404, or send bytes from memory or disk."""
        path = c.route.params.get("path", "").strip("/")

        # Path traversal is prevented by serving only keys from the manifest-built cache.
        f = self._cache.get(path)

        # Logical (unfingerprinted) path → redirect to the manifest-resolved absolute URL.
        # e.g. `/static/js/app.js` (path `js/app.js`) must land on
        # `/static/js/app.abc123.js`, not a relative `app.abc123.js`.
        if f is None and (asset := self.manifest.assets.get(path)):
            # Use 307 (not 301) so browsers do not cache a redirect whose target
            # changes whenever the file hash changes.
            responses.redirect(w, asset.url, 307)
            return

        if f is None:
            responses.text(w, "Not Found", 404)
            return

        h = w.headers
        h.unsafe_set(b"cache-control", self._cache_control_bytes)
        h.unsafe_set(b"content-type", f.content_type)

        if f.content is None:
            await self._serve_streamed_file(c, w, f)
            return

        accept = c.req.headers.unsafe_get(b"accept-encoding", b"")
        body, encoding = self._select_body(f, accept)

        if f.brotli is not None or f.zstd is not None or f.gzip is not None:
            merge_vary(h, b"accept-encoding")

        if encoding:
            h.unsafe_set(b"content-encoding", encoding)

        h.unsafe_set(b"content-length", b"%d" % len(body))

        if c.req.method == "HEAD":
            w.write_headers(200).end()
            return

        w.write_headers(200).end(body)


__all__ = [
    "Asset",
    "AssetManifest",
    "ByteRange",
    "CachedFile",
    "StaticAssets",
    "fingerprint",
]
