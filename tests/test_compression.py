"""Unit tests for compression negotiation helpers."""

import pytest

from stario.exceptions import StarioError
from stario.http.compression import (
    CompressionConfig,
    brotli_decompress,
    compression_config_from_env,
    content_type_is_compressible,
    merge_vary,
    negotiate_content_encoding,
    parse_accept_encoding,
    zstd_decompress,
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


def test_negotiate_prefers_zstd_over_br_by_default() -> None:
    # Dynamic/streamed responses prefer zstd: near-equal wire size, less CPU.
    assert negotiate_content_encoding("br, zstd", (b"br", b"zstd")) == b"zstd"


def test_negotiate_custom_preference_can_lead_with_br() -> None:
    # Precompressed statics prefer br: encode-once bytes, so wire size wins.
    assert (
        negotiate_content_encoding(
            "br, zstd", (b"br", b"zstd"), (b"br", b"zstd", b"gzip")
        )
        == b"br"
    )


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
    assert content_type_is_compressible(b"image/svg+xml") is True
    assert content_type_is_compressible(b"image/svg+xml; charset=utf-8") is True
    assert content_type_is_compressible(b"text/html; charset=utf-8") is True
    assert content_type_is_compressible(b"") is False
    assert content_type_is_compressible(b"; charset=utf-8") is False


def test_select_requires_body_for_buffered_path() -> None:
    compression = CompressionConfig(min_size=1)
    assert compression.select("gzip") is None


def test_select_honors_custom_preference() -> None:
    compression = CompressionConfig(min_size=1, preference=("br",))
    compressor = compression.select(
        "br, zstd",
        data=b"x" * 2048,
        content_type=b"text/plain",
    )
    assert compressor is not None
    assert compressor.encoding == b"br"


def test_preference_rejects_unknown_and_duplicate_names() -> None:
    for bad in (("deflate",), ("br", "br"), ("zstd", "br", "zstd")):
        with pytest.raises(StarioError):
            CompressionConfig(preference=bad)


def test_preference_fills_unlisted_names_in_default_order() -> None:
    compression = CompressionConfig(preference=("br",))
    assert compression.preference == (b"br", b"zstd", b"gzip")


def test_enabled_encodings_follows_preference_order() -> None:
    compression = CompressionConfig(preference=("gzip", "br"))
    assert compression.enabled_encodings() == (b"gzip", b"br", b"zstd")


def test_compression_config_from_env_reads_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STARIO_COMPRESS_PREFERENCE", "br, gzip")
    config = compression_config_from_env()
    assert config.preference == (b"br", b"gzip", b"zstd")


def test_select_prefers_zstd_over_br() -> None:
    compression = CompressionConfig(min_size=1)
    compressor = compression.select(
        "br, zstd",
        data=b"x" * 2048,
        content_type=b"text/plain",
    )
    assert compressor is not None
    assert compressor.encoding == b"zstd"


def test_select_prefers_zstd_over_gzip() -> None:
    compression = CompressionConfig(min_size=1)
    compressor = compression.select(
        "gzip, zstd",
        data=b"x" * 2048,
        content_type=b"text/plain",
    )
    assert compressor is not None
    assert compressor.encoding == b"zstd"


def test_native_compressors_round_trip() -> None:
    body = b"native python compressor " * 32
    decode = {b"br": brotli_decompress, b"zstd": zstd_decompress}
    for encoding in (b"br", b"zstd"):
        framed = CompressionConfig(min_size=0).make_compressor(encoding)
        assert decode[encoding](framed.frame(body)) == body
        streamed = CompressionConfig(min_size=0).make_compressor(encoding)
        assert (
            decode[encoding](
                streamed.block(b"one") + streamed.block(b"two") + streamed.finish()
            )
            == b"onetwo"
        )


def test_stream_window_log_default_and_bounds() -> None:
    assert CompressionConfig().stream_window_log is None
    assert CompressionConfig(stream_window_log=16).stream_window_log == 16
    assert CompressionConfig(stream_window_log=24).stream_window_log == 24
    for bad in (9, 25):
        with pytest.raises(StarioError, match="stream_window_log"):
            CompressionConfig(stream_window_log=bad)
    assert CompressionConfig() == CompressionConfig()
    assert CompressionConfig() == CompressionConfig(stream_window_log=None)
    assert CompressionConfig() != CompressionConfig(stream_window_log=20)


def test_compression_config_from_env_stream_window_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STARIO_COMPRESS_STREAM_WINDOW_LOG", "18")
    assert compression_config_from_env().stream_window_log == 18
    monkeypatch.setenv("STARIO_COMPRESS_STREAM_WINDOW_LOG", "none")
    assert compression_config_from_env().stream_window_log is None
    monkeypatch.delenv("STARIO_COMPRESS_STREAM_WINDOW_LOG")
    assert compression_config_from_env().stream_window_log is None
    monkeypatch.setenv("STARIO_COMPRESS_STREAM_WINDOW_LOG", "bogus")
    with pytest.raises(StarioError, match="STREAM_WINDOW_LOG"):
        compression_config_from_env()
