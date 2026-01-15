"""Tests for stario.datastar module - SSE events, attributes, and signals."""

import json

from stario.datastar import at, data, sse
from stario.datastar.format import js
from stario.html import Div, P, Span, render


class TestSseSignals:
    """Test sse.signals() SSE event formatter."""

    def test_basic_signals(self):
        result = sse.signals({"count": 42, "name": "test"})

        assert b"event: datastar-patch-signals" in result
        assert b"data: signals" in result
        assert b'"count"' in result
        assert b"42" in result
        assert b'"name"' in result
        assert b'"test"' in result
        assert result.endswith(b"\n\n")

    def test_signals_only_if_missing(self):
        result = sse.signals({"new": "value"}, only_if_missing=True)

        assert b"data: onlyIfMissing true" in result


class TestSsePatch:
    """Test sse.patch() SSE event formatter."""

    def test_basic_patch(self):
        result = sse.patch(Div("Hello"))

        assert b"event: datastar-patch-elements" in result
        assert b"data: elements <div>Hello</div>" in result

    def test_patch_with_mode(self):
        result = sse.patch(Span("Updated"), mode="inner")

        assert b"data: mode inner" in result
        assert b"<span>Updated</span>" in result

    def test_patch_with_selector(self):
        result = sse.patch(P("New content"), selector="#target")

        assert b"data: selector #target" in result

    def test_patch_append_mode(self):
        result = sse.patch(Div("item"), mode="append", selector="#list")

        assert b"data: mode append" in result
        assert b"data: selector #list" in result

    def test_patch_with_view_transition(self):
        result = sse.patch(Div("content"), use_view_transition=True)

        assert b"data: useViewTransition true" in result


class TestSseScript:
    """Test sse.script() SSE event formatter."""

    def test_basic_script(self):
        result = sse.script("console.log('hello');")

        assert b"event: datastar-patch-elements" in result
        assert b"console.log" in result

    def test_script_with_auto_remove(self):
        result = sse.script("alert('hi');", auto_remove=True)

        assert b"data-effect" in result

    def test_script_without_auto_remove(self):
        result = sse.script("persist();", auto_remove=False)

        # Should not have auto-remove effect
        assert b"data-effect" not in result


class TestSseRedirect:
    """Test sse.redirect() SSE event formatter."""

    def test_basic_redirect(self):
        result = sse.redirect("/new-page")

        assert b"event: datastar-patch-elements" in result
        assert b"/new-page" in result
        assert b"window.location" in result

    def test_redirect_with_special_chars(self):
        """Test URL with quotes and special characters is properly escaped."""
        result = sse.redirect("/page?name=O'Brien")

        # Should be JSON-escaped, not raw string with quote injection
        assert b"event: datastar-patch-elements" in result
        # The URL should be properly escaped in JSON
        assert b"O'Brien" in result or b"O\\'Brien" in result
        # Should not have broken JavaScript from unescaped quotes
        assert b"window.location" in result

    def test_redirect_with_query_params(self):
        """Test URL with query parameters."""
        result = sse.redirect("/search?q=hello&page=1")

        assert b"/search?q=hello&page=1" in result

    def test_redirect_with_unicode(self):
        """Test URL with unicode characters."""
        result = sse.redirect("/users/日本語")

        assert b"event: datastar-patch-elements" in result
        # Unicode is escaped in JSON format (\uXXXX)
        # This is safe and valid - browsers handle it correctly
        assert b"/users/" in result
        assert b"window.location" in result


class TestSseRemove:
    """Test sse.remove() SSE event formatter."""

    def test_basic_remove(self):
        result = sse.remove("#old-item")

        assert b"event: datastar-patch-elements" in result
        assert b"data: mode remove" in result
        assert b"data: selector #old-item" in result


class TestDatastarAttributes:
    """Test DatastarAttributes helper (data.*)."""

    def test_bind(self):
        attrs = data.bind("username")
        assert attrs == {"data-bind": "username"}

    def test_show(self):
        attrs = data.show("isVisible")
        assert attrs == {"data-show": "isVisible"}

    def test_text(self):
        attrs = data.text("message")
        assert attrs == {"data-text": "message"}

    def test_ref(self):
        attrs = data.ref("myElement")
        assert attrs == {"data-ref": "myElement"}

    def test_effect(self):
        attrs = data.effect("console.log($count)")
        assert attrs == {"data-effect": "console.log($count)"}

    def test_class_dict(self):
        attrs = data.class_({"active": "$isActive", "hidden": "!$visible"})
        assert "data-class" in attrs
        # Should be JSON-like
        assert "active" in attrs["data-class"]

    def test_on_click(self):
        attrs = data.on("click", "@get('/api/data')")
        assert "data-on:click" in attrs

    def test_on_with_modifiers(self):
        attrs = data.on("submit", "@post('/form')", prevent=True)
        key = list(attrs.keys())[0]
        assert "prevent" in key

    def test_signals(self):
        attrs = data.signals({"count": 0, "name": "test"})
        assert "data-signals" in attrs
        value = attrs["data-signals"]
        parsed = json.loads(value)
        assert parsed["count"] == 0
        assert parsed["name"] == "test"

    def test_signals_ifmissing(self):
        attrs = data.signals({"new": 1}, ifmissing=True)
        assert "data-signals__ifmissing" in attrs

    def test_signals_from_dataclass(self):
        from dataclasses import dataclass

        @dataclass
        class FormState:
            count: int = 0
            name: str = ""

        attrs = data.signals(FormState())
        assert "data-signals" in attrs
        value = attrs["data-signals"]
        parsed = json.loads(value)
        assert parsed["count"] == 0
        assert parsed["name"] == ""

    def test_indicator(self):
        attrs = data.indicator("isLoading")
        assert attrs == {"data-indicator": "isLoading"}

    def test_ignore(self):
        attrs = data.ignore()
        assert attrs == {"data-ignore": True}

    def test_ignore_self_only(self):
        attrs = data.ignore(self_only=True)
        assert attrs == {"data-ignore__self": True}


class TestDatastarActions:
    """Test DatastarActions helper (at.*)."""

    def test_get_simple(self):
        action = at.get("/api/data")
        assert action == "@get('/api/data')"

    def test_get_with_query(self):
        action = at.get("/search", {"q": "test"})
        assert "@get" in action
        assert "/search" in action

    def test_post_simple(self):
        action = at.post("/api/submit")
        assert action == "@post('/api/submit')"

    def test_post_with_options(self):
        payload = {"extra": 123}

        action = at.post(
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
        action = at.put("/api/item/123")
        assert action == "@put('/api/item/123')"

    def test_patch(self):
        action = at.patch("/api/item/123")
        assert "@patch" in action

    def test_delete(self):
        action = at.delete("/api/item/123")
        assert action == "@delete('/api/item/123')"

    def test_peek(self):
        action = at.peek("$count")
        assert "@peek" in action
        assert "$count" in action

    def test_set_all(self):
        action = at.set_all("false")
        assert "@setAll" in action

    def test_toggle_all(self):
        action = at.toggle_all()
        assert "@toggleAll" in action


class TestDatastarIntegration:
    """Test using Datastar attributes in HTML elements."""

    def test_button_with_click_handler(self):
        from stario.html import Button

        btn = Button(
            data.on("click", at.get("/api/increment")),
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
            data.bind("username"),
        )
        html = render(inp)

        assert 'data-bind="username"' in html

    def test_div_with_signals(self):
        d = Div(
            data.signals({"count": 0}),
            {"id": "app"},
            Span(data.text("$count")),
        )
        html = render(d)

        assert "data-signals" in html
        assert "data-text" in html
