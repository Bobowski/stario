"""
Params Demo - URL Parameter Routing with Stario

Run with: uv run stario watch main:bootstrap
      or: uv run stario serve main:bootstrap

This example demonstrates:
1. URL path parameters like /items/{id}
2. Nested parameters like /categories/{catId}/items/{itemId}
3. Using c.req.params to access parameter values
4. Using c.url_for() with params dict to generate URLs
"""

from pathlib import Path

from stario import Stario, Span
from stario.http.types import Context
from stario.http.writer import Writer
from stario.html import (
    A,
    Body,
    Code,
    Div,
    H1,
    H2,
    H3,
    Head,
    Html,
    Li,
    Link,
    Meta,
    P,
    Pre,
    Script,
    Span,
    Title,
    Ul,
)


def page(url_for, *children):
    """Base HTML shell with Datastar."""
    return Html(
        {"lang": "en"},
        Head(
            Meta({"charset": "UTF-8"}),
            Meta(
                {"name": "viewport", "content": "width=device-width, initial-scale=1"}
            ),
            Title("Params Demo - Stario"),
            Link({"rel": "stylesheet", "href": url_for("static", "css/style.css")}),
            Script(
                {
                    "type": "module",
                    "src": url_for("static", "js/datastar.js"),
                }
            ),
        ),
        Body(
            Div({"class": "container"}, *children),
        ),
    )


def code_block(text):
    """Code block."""
    return Pre({"class": "code-block"}, Code(text))


async def home(c: Context, w: Writer) -> None:
    """Home page explaining URL parameters."""
    w.html(
        page(
            c.url_for,
            H1("URL Parameter Routing Demo"),
            P("This demo shows how to use URL parameters in Stario routes."),
            H2("What are URL Parameters?"),
            P(
                "URL parameters allow you to capture parts of the URL path as variables."
            ),
            P(
                "Instead of creating separate routes for each item, you can use one route pattern."
            ),
            H2("Route Patterns"),
            Div(
                {"class": "route-demo"},
                H3("Single Parameter"),
                code_block("/items/{itemId}"),
                P("Matches: /items/123, /items/abc, /items/hello"),
                P("Access with: c.req.params['itemId']"),
                H3("Multiple Parameters"),
                code_block("/categories/{catId}/items/{itemId}"),
                P("Matches: /categories/electronics/items/456"),
                P("Access with: c.req.params['catId'] and c.req.params['itemId']"),
                H3("Static Segments Mixed"),
                code_block("/users/{userId}/posts/{postId}/comments/{commentId}"),
                P("Three parameters with static segments between them."),
            ),
            H2("Try It Out"),
            P("Click these links to see parameter routing in action:"),
            Ul(
                Li(
                    A(
                        {"href": c.url_for("item", {"itemId": "42"})},
                        "Single param: /items/42",
                    )
                ),
                Li(
                    A(
                        {"href": c.url_for("item", {"itemId": "hello"})},
                        "Single param: /items/hello",
                    )
                ),
                Li(
                    A(
                        {
                            "href": c.url_for(
                                "category_item",
                                {"catId": "electronics", "itemId": "99"},
                            )
                        },
                        "Two params: /categories/electronics/items/99",
                    )
                ),
                Li(
                    A(
                        {
                            "href": c.url_for(
                                "comment",
                                {
                                    "userId": "alice",
                                    "postId": "123",
                                    "commentId": "456",
                                },
                            )
                        },
                        "Three params: /users/alice/posts/123/comments/456",
                    )
                ),
            ),
            H2("Backend Handler Example"),
            code_block("""async def item_handler(c: Context, w: Writer) -> None:
    item_id = c.req.params['itemId']  # Get the param value
    w.json({'item_id': item_id})"""),
            H2("Generating URLs with url_for"),
            code_block("""# Single param
c.url_for('item', {'itemId': '42'})
# → '/items/42'

# Multiple params
c.url_for('category_item', {'catId': 'books', 'itemId': '77'})
# → '/categories/books/items/77'"""),
        )
    )


async def item(c: Context, w: Writer) -> None:
    """Display a single item - single param demo."""
    item_id = c.req.params.get("itemId", "unknown")
    w.html(
        page(
            c.url_for,
            H1("Item Detail Page"),
            Div(
                {"class": "route-demo item"},
                H2(f"Viewing Item: {item_id}"),
                P(f"This page was rendered by the /items/{item_id} route."),
                P(f"The parameter 'itemId' has value: {item_id}"),
                H3("What happened?"),
                Ul(
                    Li(f"URL: /items/{item_id}"),
                    Li("Route pattern: /items/{itemId}"),
                    Li("Handler accessed: c.req.params['itemId']"),
                ),
            ),
            H2("Code"),
            code_block(f"""async def item(c: Context, w: Writer) -> None:
    item_id = c.req.params['itemId']  # = "{item_id}"
    # ... render page ..."""),
            H2("Navigate"),
            Div(
                {"class": "nav-links"},
                A({"href": c.url_for("home")}, "Back to Home"),
                A({"href": c.url_for("item", {"itemId": "123"})}, "Another item"),
                A(
                    {
                        "href": c.url_for(
                            "category_item", {"catId": "electronics", "itemId": item_id}
                        )
                    },
                    "Category view",
                ),
            ),
        )
    )


async def category_item(c: Context, w: Writer) -> None:
    """Display item in category - two param demo."""
    cat_id = c.req.params.get("catId", "unknown")
    item_id = c.req.params.get("itemId", "unknown")
    w.html(
        page(
            c.url_for,
            H1("Category + Item Page"),
            Div(
                {"class": "route-demo category"},
                H2(f"Category: {cat_id}"),
                H3(f"Item: {item_id}"),
                P("This page uses two URL parameters!"),
                H3("Parameters captured:"),
                Ul(
                    Li(f"catId = {cat_id}"),
                    Li(f"itemId = {item_id}"),
                ),
            ),
            H2("Route Pattern"),
            code_block("/categories/{catId}/items/{itemId}"),
            H2("Navigate"),
            Div(
                {"class": "nav-links"},
                A({"href": c.url_for("home")}, "Back to Home"),
                A({"href": c.url_for("item", {"itemId": item_id})}, "View item only"),
                A(
                    {
                        "href": c.url_for(
                            "comment",
                            {"userId": "bob", "postId": "1", "commentId": "1"},
                        )
                    },
                    "Three params",
                ),
            ),
        )
    )


async def comment(c: Context, w: Writer) -> None:
    """Display comment - three param demo."""
    user_id = c.req.params.get("userId", "unknown")
    post_id = c.req.params.get("postId", "unknown")
    comment_id = c.req.params.get("commentId", "unknown")
    w.html(
        page(
            c.url_for,
            H1("Comment Thread"),
            Div(
                {"class": "route-demo comment"},
                H2(f"User: {user_id}"),
                H3(f"Post: {post_id}"),
                H3(f"Comment: {comment_id}"),
                P("Three parameters captured from the URL path!"),
            ),
            H2("All Parameters"),
            Div(
                {"class": "params-box"},
                code_block(f"""c.req.params = {{
    'userId': '{user_id}',
    'postId': '{post_id}',
    'commentId': '{comment_id}'
}}"""),
            ),
            H2("Navigate"),
            Div(
                {"class": "nav-links"},
                A({"href": c.url_for("home")}, "Back to Home"),
                A(
                    {"href": c.url_for("item", {"itemId": post_id})},
                    "View post as item",
                ),
                A(
                    {
                        "href": c.url_for(
                            "category_item", {"catId": "general", "itemId": post_id}
                        )
                    },
                    "View in category",
                ),
            ),
        )
    )


async def bootstrap(app: Stario, span) -> None:
    """Register routes with parameter patterns."""
    static_dir = Path(__file__).parent / "app" / "static"
    static_dir_display = (
        static_dir.relative_to(Path.cwd())
        if static_dir.is_relative_to(Path.cwd())
        else static_dir
    )
    app.assets("/static", static_dir, name="static")

    app.get("/", home, name="home")
    app.get("/items/{itemId}", item, name="item")
    app.get("/categories/{catId}/items/{itemId}", category_item, name="category_item")
    app.get(
        "/users/{userId}/posts/{postId}/comments/{commentId}", comment, name="comment"
    )
