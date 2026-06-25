"""Unit tests for compression negotiation helpers."""

from stario.http.compression import (
    CompressionConfig,
    content_type_is_compressible,
    merge_vary,
    negotiate_content_encoding,
    parse_accept_encoding,
)
from stario.http.headers import Headers


def test_parse_accept_encoding_simple_list() -> None:
    assert parse_accept_encoding("gzip, br") == {b"gzip": 1.0, b"br": 1.0}


def test_parse_accept_encoding_q_values() -> None:
    parsed = parse_accept_encoding("gzip;q=0.5, br;q=1")
    assert parsed[b"gzip"] == 0.5
    assert parsed[b"br"] == 1.0


def test_parse_accept_encoding_wildcard() -> None:
    assert parse_accept_encoding("*;q=0.8")[b"*"] == 0.8


def test_negotiate_prefers_br_over_gzip() -> None:
    assert negotiate_content_encoding("gzip, br", (b"br", b"gzip")) == b"br"


def test_negotiate_identity_wins_when_higher_q() -> None:
    assert negotiate_content_encoding("identity;q=1, gzip;q=0.5", (b"gzip",)) is None


def test_merge_vary_appends_and_skips_duplicates() -> None:
    headers = Headers()
    headers.unsafe_set(b"vary", b"accept-language")
    merge_vary(headers, b"accept-encoding")
    assert headers.unsafe_get(b"vary") == b"accept-language, accept-encoding"

    headers.unsafe_set(b"vary", b"Accept-Encoding")
    merge_vary(headers, b"accept-encoding")
    assert headers.unsafe_get(b"vary") == b"Accept-Encoding"


def test_content_type_is_compressible_skips_images() -> None:
    assert content_type_is_compressible(b"image/png") is False
    assert content_type_is_compressible(b"text/html; charset=utf-8") is True
    assert content_type_is_compressible(b"") is False
    assert content_type_is_compressible(b"; charset=utf-8") is False


def test_select_requires_body_for_buffered_path() -> None:
    compression = CompressionConfig(min_size=1)
    assert compression.select("gzip") is None


def test_select_prefers_zstd_over_gzip() -> None:
    compression = CompressionConfig(min_size=1)
    compressor = compression.select(
        "gzip, zstd",
        data=b"x" * 2048,
        content_type=b"text/plain",
    )
    assert compressor is not None
    assert compressor.encoding == b"zstd"
