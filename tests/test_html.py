"""Tests for stario.html module - HTML generation and rendering."""


from stario.html import (
    A,
    Body,
    Br,
    Button,
    Div,
    Head,
    Html,
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
    render,
)
from stario.html.core import escape_attribute_key, faster_escape, render_styles


class TestFasterEscape:
    """Test HTML escaping function."""

    def test_escape_ampersand(self):
        assert faster_escape("a & b") == "a &amp; b"

    def test_escape_less_than(self):
        assert faster_escape("a < b") == "a &lt; b"

    def test_escape_greater_than(self):
        assert faster_escape("a > b") == "a &gt; b"

    def test_escape_double_quote(self):
        assert faster_escape('say "hello"') == "say &quot;hello&quot;"

    def test_escape_single_quote(self):
        assert faster_escape("say 'hello'") == "say &#x27;hello&#x27;"

    def test_escape_all_characters(self):
        result = faster_escape("<script>alert('\"XSS\" & bad')</script>")
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result
        assert "&quot;" in result
        assert "&#x27;" in result

    def test_no_escape_needed(self):
        assert faster_escape("hello world") == "hello world"


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


class TestSafeString:
    """Test SafeString for unescaped content."""

    def test_safestring_not_escaped(self):
        safe = SafeString("<b>bold</b>")
        result = render(Div(safe))
        assert "<div><b>bold</b></div>" == result

    def test_regular_string_escaped(self):
        result = render(Div("<b>bold</b>"))
        assert "&lt;b&gt;" in result


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

    def test_nested_data_attributes(self):
        result = render(Div({"data": {"user-id": "123", "role": "admin"}}))
        assert 'data-user-id="123"' in result
        assert 'data-role="admin"' in result


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

    def test_html_has_doctype(self):
        result = render(Html(Head(Title("Test")), Body(P("Hello"))))
        assert result.startswith("<!doctype html>")
        assert "<html>" in result

    def test_self_closing_tags(self):
        assert render(Br()) == "<br/>"
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
        assert "<script>console.log(&#x27;hello&#x27;);</script>" == result

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


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_none_child_ignored(self):
        result = render(Div(None, "text", None))
        assert result == "<div>text</div>"

    def test_empty_attributes_dict(self):
        result = render(Div({}, "text"))
        assert result == "<div>text</div>"

    def test_mixed_attributes_and_children(self):
        result = render(Div({"id": "1"}, "text", {"class": "test"}))
        # Both attribute dicts should be processed
        assert 'id="1"' in result
        assert 'class="test"' in result
        assert ">text</div>" in result

    def test_attribute_value_with_quotes(self):
        result = render(Div({"data-json": '{"key":"value"}'}))
        assert "&quot;" in result  # Quotes should be escaped

    def test_safestring_attribute(self):
        result = render(Div({"data-raw": SafeString("raw<>value")}))
        assert 'data-raw="raw<>value"' in result
