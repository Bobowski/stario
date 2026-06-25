"""Tests for stario.datastar module - SSE events, attributes, and signals."""

import json
from typing import Any, cast

import pytest

from stario.datastar import (
    DATASTAR_CDN_URL,
    SSE,
    ModuleScript,
    at,
    data,
)
from stario.datastar.attributes import DatastarAttributes
from stario.datastar.format import (
    debounce_to_string,
    js_object,
    throttle_to_string,
    time_to_string,
)
from stario.exceptions import StarioError, StarioRuntime
from stario.markup import html as h
from stario.markup import render
from stario.markup.escape import escape_attribute_value, escape_sq_attribute_value
from stario.markup.types import Attrs
from stario.testing.transport import decode_chunked as _decode_chunked
from tests.helpers import (
    make_writer_raw as _make_writer,
)
from tests.helpers import (
    split_response as _split_response,
)

Div = h.Div


def _wire_attrs(mapping: dict[str, str | bool]) -> Attrs:
    parts: list[str] = []
    for key, value in mapping.items():
        if value is True:
            parts.append(f" {key}")
        elif isinstance(value, str) and "signals" in key and '"' in value:
            parts.append(f" {key}='{escape_sq_attribute_value(value)}'")
        elif isinstance(value, str) and '"' in value:
            parts.append(f' {key}="{escape_attribute_value(value)}"')
        else:
            parts.append(f' {key}="{value}"')
    return Attrs("".join(parts))


class TestSseSignals:
    """Test writer-bound signal patching helpers."""

    def test_patch_signals_rejects_non_snake_key(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="snake_case"):
                SSE(w).patch_signals({"myCount": 42})
        finally:
            loop.close()

    def test_patch_signals_rejects_completed_writer(self):
        w, _sink, loop = _make_writer()
        try:
            w.end()
            with pytest.raises(StarioRuntime, match="after the response is completed"):
                SSE(w).patch_signals({"count": 1})
        finally:
            loop.close()


class TestSseNavigate:
    """Test client-side navigation over SSE."""

    def test_navigate_with_special_chars(self):
        """URL is embedded as a JSON string literal; single quotes survive as-is."""
        w, sink, loop = _make_writer()
        try:
            SSE(w).navigate("/page?name=O'Brien")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b'window.location = "/page?name=O\'Brien"' in result
        finally:
            loop.close()

    def test_navigate_with_unicode(self):
        """Non-ASCII path segments are percent-encoded before embedding."""
        w, sink, loop = _make_writer()
        try:
            SSE(w).navigate("/users/日本語")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b'window.location = "/users/%E6%97%A5%E6%9C%AC%E8%AA%9E"' in result
        finally:
            loop.close()

    def test_navigate_percent_encodes_script_breakout(self):
        """`</script>` in a redirect target cannot break out of the script patch."""
        w, sink, loop = _make_writer()
        try:
            SSE(w).navigate("/page?q=</script><script>alert(1)</script>")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            # The angle brackets must be percent-encoded inside the JS string;
            # the only raw </script> on the wire is the patch's own closing tag.
            assert b"%3C/script%3E" in result
            assert result.count(b"</script>") == 1
            assert b"<script>alert(1)</script>" not in result
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "JaVaScRiPt:alert(1)",
        ],
    )
    def test_navigate_rejects_forbidden_schemes(self, url: str):
        w, sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="app-relative path or absolute"):
                SSE(w).navigate(url)
            assert bytes(sink) == b""
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "url",
        [
            "/safe\r\nx: injected",
            r"/\evil",
        ],
    )
    def test_navigate_rejects_crlf_and_unsafe_paths(self, url: str):
        w, sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError):
                SSE(w).navigate(url)
            assert bytes(sink) == b""
        finally:
            loop.close()


class TestSseWireFormat:
    """Pin the exact SSE wire contract: data-line splitting, modes, encodings."""

    def test_constructor_does_not_start_response(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w)

            assert not w.started
            assert bytes(sink) == b""
        finally:
            loop.close()

    def test_open_sends_headers_before_first_event(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).open()
            head, body = _split_response(bytes(sink))

            assert b"content-type: text/event-stream" in head
            assert b"cache-control: no-cache" in head
            assert body == b""
        finally:
            loop.close()

    def test_first_event_opens_stream_lazily(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"ok": True})
            head, body = _split_response(bytes(sink))

            assert b"content-type: text/event-stream" in head
            assert b"cache-control: no-cache" in head
            assert b'data: signals {"ok":true}' in _decode_chunked(body)
        finally:
            loop.close()

    def test_patch_elements_rejects_unknown_mode(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="patch mode"):
                SSE(w).patch_elements(h.Div("x"), mode=cast(Any, "sideways"))
        finally:
            loop.close()

    def test_patch_elements_rejects_unknown_namespace(self):
        w, _sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="patch namespace"):
                SSE(w).patch_elements(h.Div("x"), namespace=cast(Any, "html"))
        finally:
            loop.close()

    @pytest.mark.parametrize(
        "mode", ["inner", "replace", "prepend", "append", "before", "after"]
    )
    def test_non_outer_modes_emit_mode_line(self, mode):
        w, sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(h.Div("x"), mode=mode, selector="#t")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert f"data: mode {mode}".encode() in result
        finally:
            loop.close()

    def test_omitted_mode_omits_mode_line(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(h.Div("x"))
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: mode" not in result
        finally:
            loop.close()

    def test_mathml_namespace(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).patch_elements(b"<mi>x</mi>", namespace="mathml")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: namespace mathml" in result
        finally:
            loop.close()

    def test_multiline_html_splits_into_repeated_data_lines(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).patch_elements("<div>\n  <p>a</p>\n</div>")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert (
                b"data: elements <div>\n"
                b"data: elements   <p>a</p>\n"
                b"data: elements </div>\n\n"
            ) in result
        finally:
            loop.close()

    def test_patch_signals_rejects_raw_json_text(self):
        w, sink, loop = _make_writer()
        try:
            with pytest.raises(TypeError, match="mapping"):
                SSE(w).patch_signals('{"raw":true}')  # type: ignore[arg-type]
            assert bytes(sink) == b""
        finally:
            loop.close()

    def test_unicode_signals_are_utf8_on_the_wire(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).patch_signals({"msg": "日本語"})
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert 'data: signals {"msg":"日本語"}'.encode() in result
        finally:
            loop.close()

    def test_patch_signals_rejects_raw_json_bytes(self):
        w, sink, loop = _make_writer()
        try:
            with pytest.raises(TypeError, match="mapping"):
                SSE(w).patch_signals(b'{"raw":true}')  # type: ignore[arg-type]
            assert bytes(sink) == b""
        finally:
            loop.close()


class TestSseScriptTrustContract:
    """`execute_script()` streams developer-authored JS verbatim.

    The code is intentionally NOT escaped: it is a trusted-content API.
    Untrusted input must never be interpolated into `execute_script()` —
    a `</script>` sequence would break out of the script element.
    """

    def test_execute_streams_code_verbatim_including_script_close(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).execute_script('console.log("</script>")', auto_remove=False)
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b'data: elements <script>console.log("</script>")</script>' in result
        finally:
            loop.close()

    def test_multiline_code_splits_into_data_lines(self):
        w, sink, loop = _make_writer()
        try:
            SSE(w).execute_script(
                "let a = 1;\nconsole.log(a);",
                auto_remove=False,
            )
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert (
                b"data: elements <script>let a = 1;\n"
                b"data: elements console.log(a);</script>"
            ) in result
        finally:
            loop.close()


class TestDatastarThroughRender:
    """Datastar attribute payloads must survive HTML attribute escaping."""

    def test_signals_json_round_trips_through_attribute_escaping(self):
        import html as html_mod

        payload = {"q": 'He said "hi" & left', "n": 1}
        out = render(h.Div(data.signals(payload)))

        prefix = "<div data-signals='"
        assert out.startswith(prefix)
        value = out.split(prefix, 1)[1].split("'", 1)[0]
        assert "'" not in value  # attribute delimiter cannot be terminated early
        assert json.loads(html_mod.unescape(value)) == payload


class TestSseRemove:
    """Test remove helper."""

    def test_remove_rejects_line_breaks_in_selector(self):
        w, sink, loop = _make_writer()
        try:
            with pytest.raises(StarioError, match="line breaks"):
                SSE(w).remove("#old\ndata: mode append")
            assert bytes(sink) == b""
        finally:
            loop.close()


class TestDatastarAttributeValidation:
    """Validation rules not covered by the wire-format matrix."""

    def test_bind_rejects_non_snake_signal_name(self):
        with pytest.raises(StarioError, match="snake_case"):
            data.bind("mySignal")

    def test_bind_rejects_non_snake_signal_path_segment(self):
        with pytest.raises(StarioError, match="snake_case"):
            data.bind("crane.selectedCrane")

    @pytest.mark.parametrize(
        "time",
        [None, True, -1, -0.1, float("inf"), "0.5s", "150", "fast", "1.2ms"],
    )
    def test_time_to_string_rejects_ambiguous_values(self, time):
        with pytest.raises(StarioError, match="Invalid Datastar time value"):
            time_to_string(time)

    def test_timing_modifier_helpers_reject_unknown_modifiers(self):
        with pytest.raises(StarioError, match="Invalid debounce modifier"):
            debounce_to_string(("150ms", "trailing"))  # type: ignore[arg-type]

        with pytest.raises(StarioError, match="Invalid throttle modifier"):
            throttle_to_string(("150ms", "leading"))  # type: ignore[arg-type]


class TestDatastarAttributeMatrix:
    """Table-driven contract for every attribute helper's wire format."""

    @pytest.mark.parametrize(
        ("actual", "expected"),
        [
            (data.ignore_morph(), {"data-ignore-morph": True}),
            (
                data.computed("full_name", "$a + $b"),
                {"data-computed:full-name__case.snake": "$a + $b"},
            ),
            (data.signals({"count": 0}), {"data-signals": '{"count":0}'}),
            (data.bind("email"), {"data-bind": "email"}),
            (data.show("$visible"), {"data-show": "$visible"}),
            (
                data.on("submit", "go()", prevent=True, stop=True),
                {"data-on:submit__prevent__stop": "go()"},
            ),
            (
                data.on("click", "go()", prevent=True),
                {"data-on:click__prevent": "go()"},
            ),
            (
                data.on("keydown", "go()", debounce=("150ms", "leading")),
                {"data-on:keydown__debounce.150ms.leading": "go()"},
            ),
            (
                data.on_intersect("seen()", threshold="half"),
                {"data-on-intersect__half": "seen()"},
            ),
            (
                data.persist(include=["draft", "settings"]),
                {"data-persist": "{'include':'draft|settings'}"},
            ),
            (
                DatastarAttributes("data-star-").text("$title"),
                {"data-star-text": "$title"},
            ),
            (
                data.scroll_into_view(behavior="smooth", vertical="center", focus=True),
                {"data-scroll-into-view__smooth__vcenter__focus": True},
            ),
        ],
        ids=lambda value: next(iter(value)) if isinstance(value, dict) else "case",
    )
    def test_attribute_helper_wire_format(self, actual, expected):
        assert actual == _wire_attrs(expected)

    def test_on_rejects_passive_with_prevent(self):
        with pytest.raises(StarioError, match="passive and prevent"):
            data.on("touchstart", "go()", passive=True, prevent=True)

    @pytest.mark.parametrize("threshold", [-0.1, 1.5, 25, 100.0])
    def test_on_intersect_rejects_out_of_range_numeric_threshold(self, threshold):
        # `threshold.1.5` would misparse client-side (`.` separates modifier args).
        with pytest.raises(StarioError, match="Invalid intersection threshold"):
            data.on_intersect("load()", threshold=threshold)


class TestSignalsConversion:
    def test_signals_rejects_plain_object(self):
        """No implicit `__dict__` serialization; `vars(obj)` is the explicit spelling."""

        class State:
            def __init__(self):
                self.count = 1

        with pytest.raises(TypeError, match="mapping"):
            data.signals(State())  # type: ignore[arg-type]

    def test_signals_from_vars_of_plain_object(self):
        class State:
            def __init__(self):
                self.count = 1

        assert data.signals(vars(State())) == _wire_attrs(
            {"data-signals": '{"count":1}'}
        )

    def test_signals_rejects_pydantic_like_model(self):
        class Model:
            def model_dump(self):
                return {"name": "ada"}

        with pytest.raises(TypeError, match="mapping"):
            data.signals(Model())  # type: ignore[arg-type]

    def test_signals_rejects_unconvertible_value(self):
        with pytest.raises(TypeError, match="mapping"):
            data.signals(42)  # type: ignore[arg-type]


class TestDatastarActions:
    """Test Datastar action helpers."""

    def test_get_with_query(self):
        action = at.get("/search", {"q": "test"})
        assert action == "@get('/search?q=test')"

    def test_get_merges_existing_query(self):
        action = at.get("/search?sort=recent", {"q": "test"})
        assert action == "@get('/search?sort=recent&q=test')"

    def test_get_query_uses_doseq(self):
        action = at.get("/search", {"tag": ["a", "b"]})
        assert action == "@get('/search?tag=a&tag=b')"

    def test_post_with_options(self):
        payload = {"extra": 123}

        action = at.post(
            "/api/submit",
            include="form.*",
            retry="error",
            payload=payload,
        )

        assert "@post" in action
        assert "/api/submit" in action

        expected_payload_str = f"payload: {js_object(payload)}"
        assert expected_payload_str in action

        assert "retry: 'error'" in action

    def test_selector_requires_form_content_type(self):
        action = at.post("/checkout", content_type="form", selector="#checkout-form")
        assert "contentType: 'form'" in action
        assert "selector: '#checkout-form'" in action

        with pytest.raises(StarioError, match="content_type='form'"):
            at.post("/checkout", selector="#checkout-form")

    def test_retry_max_wait_uses_v1_option_name(self):
        """Datastar 1.0.x expects `retryMaxWait`, not `retryMaxWaitMs`."""
        action = at.get("/api", retry_max_wait_ms=5_000)
        assert "retryMaxWait: 5000" in action
        assert "retryMaxWaitMs" not in action

    def test_open_when_hidden_tristate(self):
        # Omitted options defer to the client default and are elided.
        assert at.post("/api") == "@post('/api')"
        # Explicit True/False are emitted (the client default is method-dependent).
        assert "openWhenHidden: true" in at.get("/api", open_when_hidden=True)
        assert "openWhenHidden: false" in at.post("/api", open_when_hidden=False)

    def test_none_fetch_options_are_omitted(self):
        action = at.get(
            "/api",
            content_type=None,
            open_when_hidden=None,
            payload=None,
            retry=None,
            retry_interval_ms=None,
            retry_scaler=None,
            retry_max_wait_ms=None,
            retry_max_count=None,
            request_cancellation=None,
        )

        assert action == "@get('/api')"

    def test_delete(self):
        action = at.delete("/api/item/123")
        assert action == "@delete('/api/item/123')"

    def test_intl_basic(self):
        action = at.intl("number", "$price", {"style": "currency", "currency": "USD"})
        assert (
            action == "@intl('number', $price, {'style':'currency','currency':'USD'})"
        )

    def test_set_all_with_filters(self):
        action = at.set_all("null", include=["draft"], exclude="tmp.*")
        assert action == "@setAll(null, {'include':'draft','exclude':'tmp.*'})"


class TestDatastarScriptTag:
    """Test the shared Datastar CDN script helper."""

    def test_ModuleScript_uses_cdn(self):
        html = render(ModuleScript())

        assert 'type="module"' in html
        assert f'src="{DATASTAR_CDN_URL}"' in html

    def test_ModuleScript_accepts_custom_src(self):
        html = render(ModuleScript("/static/vendor/datastar.js"))

        assert 'src="/static/vendor/datastar.js"' in html
