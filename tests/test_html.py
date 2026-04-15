"""Tests for stario.html module - HTML generation and rendering."""

import dataclasses
from typing import Any, cast

import pytest

from stario.exceptions import StarioError
from stario.html import (
    A,
    Body,
    Br,
    Button,
    Col,
    Comment,
    Div,
    Head,
    Html,
    HtmlDocument,
    Img,
    Input,
    Li,
    Meta,
    P,
    SafeString,
    Script,
    Span,
    Tag,
    Title,
    Ul,
    baked,
    render,
)
from stario.html.attributes import render_styles
from stario.html.baked import _BakeSlot
from stario.html.escape import (
    escape_attribute_key,
    escape_attribute_value,
    escape_text,
)


class TestEscapeAttributeValue:
    """Test attribute-value escaping helper."""

    def test_escape_ampersand(self):
        assert escape_attribute_value("a & b") == "a &amp; b"

    def test_escape_less_than(self):
        assert escape_attribute_value("a < b") == "a &lt; b"

    def test_escape_greater_than(self):
        assert escape_attribute_value("a > b") == "a &gt; b"

    def test_escape_double_quote(self):
        assert escape_attribute_value('say "hello"') == "say &quot;hello&quot;"

    def test_escape_single_quote(self):
        assert escape_attribute_value("say 'hello'") == "say &#x27;hello&#x27;"

    def test_escape_all_characters(self):
        result = escape_attribute_value("<script>alert('\"XSS\" & bad')</script>")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result
        assert "&quot;" in result
        assert "&#x27;" in result

    def test_no_escape_needed(self):
        assert escape_attribute_value("hello world") == "hello world"


class TestEscapeText:
    """Test text-node escaping helper."""

    def test_text_escapes_markup_chars(self):
        assert escape_text("a < b & c > d") == "a &lt; b &amp; c &gt; d"

    def test_text_leaves_quotes_literal(self):
        assert escape_text('say "hello" and \'bye\'') == 'say "hello" and \'bye\''


class TestEscapeAttributeKey:
    """Test attribute key escaping."""

    def test_simple_key(self):
        assert escape_attribute_key("class") == "class"

    def test_key_with_hyphen(self):
        assert escape_attribute_key("data-value") == "data-value"

    def test_key_with_equals(self):
        assert "&#x3D;" in escape_attribute_key("onclick=alert()")

    def test_key_with_space(self):
        assert "&nbsp;" in escape_attribute_key("class name")


class TestSvgNamespace:
    """SVG tags live on ``stario.html.svg`` (not package top level)."""

    def test_svg_submodule(self):
        from stario.html import svg

        assert (
            render(svg.Svg(svg.Circle()))
            == "<svg><circle/></svg>"
        )


class TestSafeString:
    """Test SafeString for unescaped content."""

    def test_safestring_is_frozen(self):
        s = SafeString("x")
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.safe_str = "y"  # type: ignore[misc]

    def test_safestring_not_escaped(self):
        safe = SafeString("<b>bold</b>")
        result = render(Div(safe))
        assert "<div><b>bold</b></div>" == result

    def test_regular_string_escaped(self):
        result = render(Div("<b>bold</b>"))
        assert "&lt;b&gt;" in result


class TestComment:
    """Test HTML comment node helper."""

    def test_comment_renders_in_tree(self):
        result = render(Div(Comment("server-note"), P("Hello")))
        assert result == "<div><!--server-note--><p>Hello</p></div>"

    def test_comment_escapes_dangerous_sequences(self):
        result = render(Comment("--><script>alert(1)</script>"))
        assert result == "<!----&gt;&lt;script&gt;alert(1)&lt;/script&gt;-->"

    def test_comment_rejects_boolean_content(self):
        with pytest.raises(StarioError, match="Invalid comment content type: bool"):
            Comment(True)


class TestTagCreation:
    """Test Tag class and element creation."""

    def test_create_simple_tag(self):
        my_div = Tag("div")
        result = render(my_div("hello"))
        assert result == "<div>hello</div>"

    def test_self_closing_tag(self):
        my_br = Tag("br", True)
        result = render(my_br())
        assert result == "<br/>"

    def test_raw_tag_requires_call(self):
        with pytest.raises(StarioError, match="Tag object directly"):
            render(cast(Any, Tag("div")))

    def test_tag_with_attributes(self):
        result = render(Div({"class": "test", "id": "main"}, "content"))
        assert 'class="test"' in result
        assert 'id="main"' in result
        assert ">content</div>" in result

    def test_tag_no_children(self):
        result = render(Div())
        assert result == "<div></div>"

    def test_tag_multiple_children(self):
        result = render(Div(P("one"), P("two")))
        assert result == "<div><p>one</p><p>two</p></div>"

    def test_tag_list_children(self):
        items = [Li("a"), Li("b"), Li("c")]
        result = render(Ul(*items))
        assert "<ul><li>a</li><li>b</li><li>c</li></ul>" == result


class TestAttributeTypes:
    """Test different attribute value types."""

    def test_string_attribute(self):
        result = render(Div({"class": "container"}))
        assert 'class="container"' in result

    def test_integer_attribute(self):
        result = render(Input({"tabindex": 0}))
        assert 'tabindex="0"' in result

    def test_float_attribute(self):
        result = render(Div({"data-opacity": 0.5}))
        assert 'data-opacity="0.5"' in result

    def test_true_boolean_attribute(self):
        result = render(Input({"disabled": True}))
        assert "disabled" in result
        assert "disabled=" not in result  # No value for true booleans

    def test_false_boolean_attribute(self):
        result = render(Input({"disabled": False}))
        assert "disabled" not in result

    def test_none_attribute(self):
        result = render(Input({"required": None}))
        assert "required" in result

    def test_list_attribute(self):
        result = render(Div({"class": ["btn", "primary", "large"]}))
        assert 'class="btn primary large"' in result

    def test_style_dict_attribute(self):
        result = render(Div({"style": {"color": "red", "font-size": "16px"}}))
        assert 'style="' in result
        assert "color:red;" in result
        assert "font-size:16px;" in result

    def test_style_dict_rejects_invalid_values(self):
        with pytest.raises(StarioError, match="Invalid CSS value type"):
            render(Div({"style": {"color": ["red", "blue"]}}))

    def test_style_dict_rejects_css_separators_in_keys(self):
        with pytest.raises(StarioError, match="Invalid CSS property name"):
            render(Div({"style": {"x;background:red": "1"}}))

    def test_style_dict_rejects_at_rules(self):
        with pytest.raises(StarioError, match="do not support at-rules"):
            render(Div({"style": {"@media": "screen"}}))

    def test_nested_data_attributes(self):
        result = render(Div({"data": {"user-id": "123", "role": "admin"}}))
        assert 'data-user-id="123"' in result
        assert 'data-role="admin"' in result

    def test_nested_attribute_key_uses_key_escaping(self):
        result = render(Div({"data": {"bad key=1": "x"}}))
        assert "data-bad&nbsp;key&#x3D;1" in result

    def test_nested_attribute_key_rejects_non_strings(self):
        with pytest.raises(StarioError, match="Invalid nested attribute name type"):
            render(Div(cast(Any, {"data": {object(): "x"}})))

    def test_attribute_list_rejects_invalid_items(self):
        with pytest.raises(StarioError, match="Invalid list item type for attribute"):
            render(Div(cast(Any, {"class": ["btn", object()]})))

    def test_nested_attribute_list_rejects_invalid_items(self):
        with pytest.raises(StarioError, match="Invalid list item type for nested attribute"):
            render(Div(cast(Any, {"data": {"ids": ["123", object()]}})))


class TestRender:
    """Test the render function."""

    def test_render_simple_element(self):
        result = render(P("Hello"))
        assert result == "<p>Hello</p>"

    def test_render_multiple_elements(self):
        result = render(P("one"), P("two"), P("three"))
        assert result == "<p>one</p><p>two</p><p>three</p>"

    def test_render_nested_elements(self):
        result = render(Div(Span(P("deep"))))
        assert result == "<div><span><p>deep</p></span></div>"

    def test_render_text_escaping(self):
        result = render(P("<script>alert('xss')</script>"))
        assert "&lt;script&gt;" in result
        assert "&lt;/script&gt;" in result

    def test_render_integer(self):
        result = render(Span(42))
        assert result == "<span>42</span>"

    def test_render_float(self):
        result = render(Span(3.14))
        assert result == "<span>3.14</span>"

    def test_render_rejects_boolean_child(self):
        with pytest.raises(
            StarioError, match="Boolean values are not valid HTML child content"
        ):
            render(Span(True))


class TestRenderStyles:
    """Test style dictionary rendering."""

    def test_simple_styles(self):
        result = render_styles({"color": "red"})
        assert result.safe_str == "color:red;"

    def test_multiple_styles(self):
        result = render_styles({"color": "red", "margin": "10px"})
        # Order may vary
        assert "color:red;" in result.safe_str
        assert "margin:10px;" in result.safe_str


class TestHtmlTags:
    """Test predefined HTML tags."""

    def test_html_document_has_doctype(self):
        result = render(HtmlDocument(Head(Title("Test")), Body(P("Hello"))))
        assert result.startswith("<!doctype html>")
        assert "<html>" in result

    def test_html_is_plain_html_tag(self):
        result = render(Html(Head(Title("Test")), Body(P("Hello"))))
        assert result == "<html><head><title>Test</title></head><body><p>Hello</p></body></html>"

    def test_self_closing_tags(self):
        assert render(Br()) == "<br/>"
        assert render(Col()) == "<col/>"
        assert 'src="test.png"' in render(Img({"src": "test.png"}))
        assert render(Input({"type": "text"})) == '<input type="text"/>'
        assert "<meta" in render(Meta({"charset": "utf-8"}))

    def test_void_elements_with_attributes(self):
        result = render(Img({"src": "img.png", "alt": "An image"}))
        assert 'src="img.png"' in result
        assert 'alt="An image"' in result
        assert result.endswith("/>")


class TestComplexElements:
    """Test complex HTML structures."""

    def test_navigation_menu(self):
        nav = Ul(
            {"class": "nav"},
            Li(A({"href": "/"}, "Home")),
            Li(A({"href": "/about"}, "About")),
            Li(A({"href": "/contact"}, "Contact")),
        )
        result = render(nav)
        assert '<ul class="nav">' in result
        assert '<a href="/">Home</a>' in result

    def test_form_elements(self):
        form = Div(
            {"class": "form"},
            Input({"type": "text", "name": "username", "placeholder": "Username"}),
            Input({"type": "password", "name": "password"}),
            Button({"type": "submit"}, "Login"),
        )
        result = render(form)
        assert 'type="text"' in result
        assert 'type="password"' in result
        assert ">Login</button>" in result

    def test_script_with_content(self):
        result = render(Script("console.log('hello');"))
        assert "<script>console.log('hello');</script>" == result

    def test_nested_conditional_content(self):
        show_extra = True
        result = render(
            Div(
                P("Always shown"),
                P("Extra content") if show_extra else None,
            )
        )
        assert "<p>Always shown</p>" in result
        assert "<p>Extra content</p>" in result


class TestBakedDecorator:
    """@baked: dynamic builders return segment lists; static ones return SafeString."""

    def test_positional_and_render(self):
        @baked
        def layout(title, body):
            return Div(Title(title), body)

        assert render(layout("Hi", P("w"))) == "<div><title>Hi</title><p>w</p></div>"

    def test_keyword_arguments(self):
        @baked
        def layout(title, body):
            return Div(Title(title), body)

        assert render(layout(body=P("x"), title="T")) == "<div><title>T</title><p>x</p></div>"

    def test_keyword_only_parameters(self):
        @baked
        def layout(*, title, body):
            return Div(Title(title), body)

        assert render(layout(title="A", body=P("b"))) == "<div><title>A</title><p>b</p></div>"

    def test_default_none_omits_child(self):
        @baked
        def block(inner=None):
            return Div(P("pre"), inner)

        assert render(block()) == "<div><p>pre</p></div>"
        assert render(block(P("mid"))) == "<div><p>pre</p><p>mid</p></div>"

    def test_static_only_no_slots(self):
        @baked
        def shell():
            return Div(P("fixed"))

        assert shell() == SafeString("<div><p>fixed</p></div>")
        assert render(shell()) == "<div><p>fixed</p></div>"

    def test_rejects_static_boolean_child(self):
        def bad():
            return Div(True)

        with pytest.raises(
            StarioError,
            match="@baked: boolean values are not valid HTML child content",
        ):
            baked(bad)

    def test_rejects_var_positional(self):

        def bad(*a):
            return Div()

        with pytest.raises(StarioError, match="\\*args"):
            baked(bad)

    def test_rejects_var_keyword(self):

        def bad(**kw):
            return Div()

        with pytest.raises(StarioError, match="\\*\\*kwargs"):
            baked(bad)

    def test_rejects_positional_only(self):

        def bad(x, /):
            return Div(str(x))

        with pytest.raises(StarioError, match="positional-only"):
            baked(bad)

    def test_bakeslot_left_in_tree_raises(self):
        with pytest.raises(StarioError, match="Unfilled @baked slot"):
            render(Div(cast(Any, _BakeSlot("t"))))


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_none_child_ignored(self):
        result = render(Div(None, "text", None))
        assert result == "<div>text</div>"

    def test_empty_attributes_dict(self):
        result = render(Div({}, "text"))
        assert result == "<div>text</div>"

    def test_mixed_attributes_and_children(self):
        with pytest.raises(StarioError, match="attributes must be passed before children"):
            render(Div({"id": "1"}, "text", {"class": "test"}))

    def test_attribute_value_with_quotes(self):
        result = render(Div({"data-json": '{"key":"value"}'}))
        assert "&quot;" in result  # Quotes should be escaped

    def test_text_content_keeps_quotes_literal(self):
        result = render(P('say "hello" and \'bye\''))
        assert result == '<p>say "hello" and \'bye\'</p>'

    def test_safestring_attribute(self):
        result = render(Div({"data-raw": SafeString("raw<>value")}))
        assert 'data-raw="raw<>value"' in result


def test_html_package_reexports_full_tag_catalog():
    """Guards ``from .tags import *`` in ``stario.html`` against a drifting ``tags`` catalog."""
    import stario.html as html
    from stario.html import tags as tags_mod

    for name in sorted(
        n
        for n in tags_mod.__dict__
        if not n.startswith("_") and n not in {"_Tag", "_SafeString"}
    ):
        assert getattr(html, name) is getattr(tags_mod, name), name
