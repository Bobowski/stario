"""Tests for stario.datastar module - SSE events, attributes, and signals."""

import asyncio
import json

import pytest

from stario import datastar as ds
from stario.datastar import DATASTAR_CDN_URL, js, s, sse
from stario.html import Div, P, Span, render
from stario.http.writer import Writer


def _make_writer() -> tuple[Writer, bytearray, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    sink = bytearray()
    writer = Writer(
        transport_write=sink.extend,
        get_date_header=lambda: b"date: Tue, 10 Mar 2026 00:00:00 GMT\r\n",
        on_completed=lambda: None,
        disconnect=loop.create_future(),
        shutdown=loop.create_future(),
    )
    return writer, sink, loop


def _split_response(raw: bytes) -> tuple[bytes, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


def _decode_chunked(body: bytes) -> bytes:
    remaining = body
    decoded = bytearray()
    while remaining:
        size_line, _, rest = remaining.partition(b"\r\n")
        if not rest:
            break
        size = int(size_line.split(b";", 1)[0], 16)
        if size == 0:
            break
        decoded.extend(rest[:size])
        remaining = rest[size + 2 :]
    return bytes(decoded)


class TestSseSignals:
    """Test writer-bound signal patching helpers."""

    def test_basic_signals(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_signals(w, {"count": 42, "name": "test"})
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-signals" in result
            assert b"data: signals" in result
            assert b'"count"' in result
            assert b"42" in result
            assert b'"name"' in result
            assert b'"test"' in result
            assert result.endswith(b"\n\n")
        finally:
            loop.close()

    def test_signals_only_if_missing(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_signals(w, b'{"new":"value"}', only_if_missing=True)
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: onlyIfMissing true" in result
        finally:
            loop.close()


class TestSsePatch:
    """Test writer-bound HTML patch helpers."""

    def test_basic_patch(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(w, Div("Hello"))
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-elements" in result
            assert b"data: elements <div>Hello</div>" in result
        finally:
            loop.close()

    def test_patch_with_mode(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(w, Span("Updated"), mode="inner")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: mode inner" in result
            assert b"<span>Updated</span>" in result
        finally:
            loop.close()

    def test_patch_with_selector(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(w, P("New content"), selector="#target")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: selector #target" in result
        finally:
            loop.close()

    def test_patch_with_namespace(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(
                w,
                b"<circle cx='10' cy='10' r='5'></circle>",
                selector="#icon",
                namespace="svg",
            )
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: selector #icon" in result
            assert b"data: namespace svg" in result
        finally:
            loop.close()

    def test_patch_append_mode(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(w, Div("item"), mode="append", selector="#list")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: mode append" in result
            assert b"data: selector #list" in result
        finally:
            loop.close()

    def test_patch_with_view_transition(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(w, Div("content"), use_view_transition=True)
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: useViewTransition true" in result
        finally:
            loop.close()

    def test_patch_html_accepts_str_options(self):
        w, sink, loop = _make_writer()
        try:
            sse.patch_elements(w, b"<div>Hello</div>", mode="inner", selector="#target")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data: mode inner" in result
            assert b"data: selector #target" in result
            assert b"data: elements <div>Hello</div>" in result
        finally:
            loop.close()


class TestSseScript:
    """Test script execution SSE helper."""

    def test_basic_script(self):
        w, sink, loop = _make_writer()
        try:
            sse.execute(w, "console.log('hello');")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-elements" in result
            assert b"console.log" in result
        finally:
            loop.close()

    def test_script_with_auto_remove(self):
        w, sink, loop = _make_writer()
        try:
            sse.execute(w, "alert('hi');", auto_remove=True)
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data-effect" in result
        finally:
            loop.close()

    def test_script_without_auto_remove(self):
        w, sink, loop = _make_writer()
        try:
            sse.execute(w, "persist();", auto_remove=False)
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"data-effect" not in result
        finally:
            loop.close()


class TestSseRedirect:
    """Test redirect SSE helper."""

    def test_basic_redirect(self):
        w, sink, loop = _make_writer()
        try:
            sse.redirect(w, "/new-page")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-elements" in result
            assert b"/new-page" in result
            assert b"window.location" in result
        finally:
            loop.close()

    def test_redirect_with_special_chars(self):
        """Test URL with quotes and special characters is properly escaped."""
        w, sink, loop = _make_writer()
        try:
            sse.redirect(w, "/page?name=O'Brien")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-elements" in result
            assert b"O'Brien" in result or b"O\\'Brien" in result
            assert b"window.location" in result
        finally:
            loop.close()

    def test_redirect_with_query_params(self):
        """Test URL with query parameters."""
        w, sink, loop = _make_writer()
        try:
            sse.redirect(w, "/search?q=hello&page=1")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"/search?q=hello&page=1" in result
        finally:
            loop.close()

    def test_redirect_with_unicode(self):
        """Test URL with unicode characters."""
        w, sink, loop = _make_writer()
        try:
            sse.redirect(w, "/users/日本語")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-elements" in result
            assert b"/users/" in result
            assert b"window.location" in result
        finally:
            loop.close()


class TestSseRemove:
    """Test remove helper."""

    def test_basic_remove(self):
        w, sink, loop = _make_writer()
        try:
            sse.remove(w, "#old-item")
            _, body = _split_response(bytes(sink))
            result = _decode_chunked(body)

            assert b"event: datastar-patch-elements" in result
            assert b"data: mode remove" in result
            assert b"data: selector #old-item" in result
        finally:
            loop.close()


class TestDatastarAttributes:
    """Test module-level Datastar attribute helpers."""

    def test_bind(self):
        attrs = ds.bind("username")
        assert attrs == {"data-bind": "username"}

    def test_bind_prop_event(self):
        assert ds.bind("isChecked", prop="checked", event="change") == {
            "data-bind:is-checked__prop.checked__event.change": "isChecked"
        }

    def test_bind_explicit_case(self):
        assert ds.bind("mySignal", case="kebab") == {
            "data-bind:my-signal__case.kebab": "mySignal"
        }

    def test_on_intersect_threshold_and_example_modifiers(self):
        attrs = ds.on_intersect("$x = true", threshold=0.25, once=True, full=True)
        key = next(iter(attrs))
        assert key == "data-on-intersect__threshold.25__once__full"
        assert attrs[key] == "$x = true"

    def test_on_intersect_threshold_string(self):
        attrs = ds.on_intersect("tick()", threshold="0.5")
        assert list(attrs.keys())[0] == "data-on-intersect__threshold.0.5"

    def test_pro_match_media(self):
        attrs = ds.match_media("isDark", "'prefers-color-scheme: dark'")
        assert attrs == {"data-match-media:is-dark": "'prefers-color-scheme: dark'"}

    def test_pro_persist_key_session(self):
        assert ds.persist(storage_key="mykey", session=True) == {
            "data-persist:mykey__session": True
        }

    def test_show(self):
        attrs = ds.show("isVisible")
        assert attrs == {"data-show": "isVisible"}

    def test_text(self):
        attrs = ds.text("message")
        assert attrs == {"data-text": "message"}

    def test_ref(self):
        attrs = ds.ref("myElement")
        assert attrs == {"data-ref": "myElement"}

    def test_effect(self):
        attrs = ds.effect("console.log($count)")
        assert attrs == {"data-effect": "console.log($count)"}

    def test_classes_mapping(self):
        attrs = ds.classes({"active": "$isActive", "hidden": "!$visible"})
        assert "data-class" in attrs
        # Should be JSON-like
        assert "active" in attrs["data-class"]

    def test_on_click(self):
        attrs = ds.on("click", "@get('/api/data')")
        assert "data-on:click" in attrs

    def test_on_with_modifiers(self):
        attrs = ds.on("submit", "@post('/form')", prevent=True)
        key = list(attrs.keys())[0]
        assert "prevent" in key

    def test_signal_single_key(self):
        attrs = ds.signal("count", "0")
        # "count" is all-lowercase → classified as kebab-case, modifier added
        assert attrs == {"data-signals:count__case.kebab": "0"}

    def test_signal_camel_key(self):
        attrs = ds.signal("myCount", "0")
        # camelCase keys get no case modifier (Datastar default)
        assert attrs == {"data-signals:my-count": "0"}

    def test_signal_with_string_literal(self):
        attrs = ds.signal("name", s("hello"))
        # "name" is all-lowercase → kebab modifier
        assert attrs == {"data-signals:name__case.kebab": "'hello'"}

    def test_signal_ifmissing(self):
        attrs = ds.signal("count", "0", ifmissing=True)
        key = list(attrs.keys())[0]
        assert "ifmissing" in key
        assert attrs[key] == "0"

    def test_signals_rejects_str_payload(self):
        with pytest.raises(TypeError, match=r"signal\(name"):
            ds.signals("count")  # type: ignore[arg-type]

    def test_signals(self):
        attrs = ds.signals({"count": 0, "name": "test"})
        assert "data-signals" in attrs
        value = attrs["data-signals"]
        parsed = json.loads(value)
        assert parsed["count"] == 0
        assert parsed["name"] == "test"

    def test_signals_ifmissing(self):
        attrs = ds.signals({"new": 1}, ifmissing=True)
        assert "data-signals__ifmissing" in attrs

    def test_signals_from_dataclass(self):
        from dataclasses import dataclass

        @dataclass
        class FormState:
            count: int = 0
            name: str = ""

        attrs = ds.signals(FormState())
        assert "data-signals" in attrs
        value = attrs["data-signals"]
        parsed = json.loads(value)
        assert parsed["count"] == 0
        assert parsed["name"] == ""

    def test_indicator(self):
        attrs = ds.indicator("isLoading")
        assert attrs == {"data-indicator": "isLoading"}

    def test_ignore(self):
        attrs = ds.ignore()
        assert attrs == {"data-ignore": True}

    def test_ignore_self_only(self):
        attrs = ds.ignore(self_only=True)
        assert attrs == {"data-ignore__self": True}


class TestDatastarActions:
    """Test module-level Datastar action helpers."""

    def test_get_simple(self):
        action = ds.get("/api/data")
        assert action == "@get('/api/data')"

    def test_get_with_query(self):
        action = ds.get("/search", {"q": "test"})
        assert "@get" in action
        assert "/search" in action

    def test_post_simple(self):
        action = ds.post("/api/submit")
        assert action == "@post('/api/submit')"

    def test_post_with_options(self):
        payload = {"extra": 123}

        action = ds.post(
            "/api/submit",
            include="form.*",
            selector="#result",
            retry="error",
            payload=payload,
        )

        assert "@post" in action
        assert "/api/submit" in action

        expected_payload_str = f"payload: {js(payload)}"
        assert expected_payload_str in action

        assert "retry: 'error'" in action

    def test_put(self):
        action = ds.put("/api/item/123")
        assert action == "@put('/api/item/123')"

    def test_patch(self):
        action = ds.patch("/api/item/123")
        assert "@patch" in action

    def test_delete(self):
        action = ds.delete("/api/item/123")
        assert action == "@delete('/api/item/123')"

    def test_peek(self):
        action = ds.peek("$count")
        assert "@peek" in action
        assert "$count" in action

    def test_set_all(self):
        action = ds.set_all("false")
        assert "@setAll" in action

    def test_toggle_all(self):
        action = ds.toggle_all()
        assert "@toggleAll" in action


class TestDatastarIntegration:
    """Test using Datastar attributes in HTML elements."""

    def test_button_with_click_handler(self):
        from stario.html import Button

        btn = Button(
            ds.on("click", ds.get("/api/increment")),
            "Click me",
        )
        html = render(btn)

        assert "data-on:click" in html
        assert "@get" in html
        assert "/api/increment" in html

    def test_input_with_bind(self):
        from stario.html import Input

        inp = Input(
            {"type": "text"},
            ds.bind("username"),
        )
        html = render(inp)

        assert 'data-bind="username"' in html

    def test_div_with_signals(self):
        d = Div(
            ds.signals({"count": 0}),
            {"id": "app"},
            Span(ds.text("$count")),
        )
        html = render(d)

        assert "data-signals" in html
        assert "data-text" in html


class TestDatastarScriptTag:
    """Test the shared Datastar CDN script helper."""

    def test_ModuleScript_uses_cdn(self):
        html = render(ds.ModuleScript())

        assert 'type="module"' in html
        assert f'src="{DATASTAR_CDN_URL}"' in html

    def test_ModuleScript_accepts_custom_src(self):
        html = render(ds.ModuleScript("/static/vendor/datastar.js"))

        assert 'src="/static/vendor/datastar.js"' in html

    def test_module_still_exports_low_level_helpers(self):
        assert callable(s)
        assert callable(js)
        assert hasattr(sse, "patch_signals")
        assert callable(ds.read_signals)
        assert DATASTAR_CDN_URL.endswith("/datastar.js")
