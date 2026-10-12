"""Tests for the process-wide Stario JSON codec."""

import json as json_module

import pytest

import stario.json as stario_json
from stario import App, Context, Route, Writer
from stario.datastar import SSE, data, read_signals
from stario.telemetry.formatters import dumps_json
from stario.testing import TestClient
from tests.helpers import make_request, make_writer_raw


class RecordingCodec:
    def __init__(self) -> None:
        self.dumped: list[tuple[object, stario_json.JsonDefault | None]] = []
        self.dumped_bytes: list[tuple[object, stario_json.JsonDefault | None]] = []
        self.loaded: list[str | bytes | bytearray] = []

    def dumps(
        self,
        value: object,
        /,
        *,
        default: stario_json.JsonDefault | None = None,
    ) -> str:
        self.dumped.append((value, default))
        return json_module.dumps(
            value,
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def dumps_bytes(
        self,
        value: object,
        /,
        *,
        default: stario_json.JsonDefault | None = None,
    ) -> bytes:
        self.dumped_bytes.append((value, default))
        return json_module.dumps(
            value,
            default=default,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def loads(self, value: str | bytes | bytearray, /) -> object:
        self.loaded.append(value)
        return json_module.loads(value)


@pytest.fixture(autouse=True)
def _restore_codec():
    previous = stario_json._codec
    stario_json.set_codec(stario_json.StdlibJsonCodec())
    yield
    stario_json.set_codec(previous)


def test_default_codec_uses_compact_utf8_bytes_and_accepts_text() -> None:
    assert stario_json.dumps({"msg": "日本語"}) == '{"msg":"日本語"}'
    assert stario_json.dumps_bytes({"ok": True}) == b'{"ok":true}'
    assert stario_json.dumps({"same": [1, 2]}).encode() == stario_json.dumps_bytes(
        {"same": [1, 2]}
    )
    assert stario_json.loads('{"count":2}') == {"count": 2}
    assert stario_json.loads(b'["a",1]') == ["a", 1]
    assert stario_json.loads(bytearray(b'{"mutable":true}')) == {"mutable": True}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_default_codec_rejects_non_json_floats(value: float) -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        stario_json.dumps(value)
    with pytest.raises(ValueError, match="JSON compliant"):
        stario_json.dumps_bytes(value)


def test_set_codec_can_replace_a_codec_after_use() -> None:
    first = RecordingCodec()
    second = RecordingCodec()

    stario_json.set_codec(first)
    assert stario_json.dumps({"first": True}) == '{"first":true}'
    stario_json.set_codec(second)
    assert stario_json.dumps_bytes({"second": True}) == b'{"second":true}'

    assert first.dumped == [({"first": True}, None)]
    assert second.dumped_bytes == [({"second": True}, None)]


async def test_framework_json_paths_use_configured_codec() -> None:
    codec = RecordingCodec()
    stario_json.set_codec(codec)

    json_writer, _sink, loop = make_writer_raw()
    writer, _sink2, _loop2 = make_writer_raw()
    try:
        json_writer.json({"response": True})
        assert json_writer.body == b'{"response":true}'
        SSE(writer).patch_signals({"sse": True})
    finally:
        loop.close()

    data.signals({"attribute": True})

    assert await read_signals(
        make_request(method="POST", body=b'{"request":true}')
    ) == {"request": True}
    assert dumps_json({"unknown": object()}).startswith('{"unknown":"<object object')

    byte_values = [value for value, _default in codec.dumped_bytes]
    text_values = [value for value, _default in codec.dumped]
    assert {"response": True} in byte_values
    assert {"sse": True} in byte_values
    assert {"attribute": True} in text_values
    assert codec.loaded == [b'{"request":true}']
    assert any(default is str for _value, default in codec.dumped)


async def test_writer_json_uses_configured_codec_through_testclient() -> None:
    """`w.json` on the real RequestHandle honour `set_codec`, not the stdlib."""
    codec = RecordingCodec()
    stario_json.set_codec(codec)

    async def handler(_c: Context, w: Writer) -> None:
        w.json({"response": True})

    app = App()
    app.add(Route("GET /"), handler)
    async with TestClient(app) as client:
        r = await client.get("/")

    assert r.status_code == 200
    assert [value for value, _default in codec.dumped_bytes] == [{"response": True}]
    assert (
        r.content
        == json_module.dumps(
            {"response": True}, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )
