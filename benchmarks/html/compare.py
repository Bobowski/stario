"""
Compare stario HTML generation against jinja2, htpy, dominate, and tdom.

Every contender renders the same 50-row product page (autoescaping on
everywhere). Run from the project root:

    uv run --with dominate --with htpy --with jinja2 --with tdom benchmarks/html/compare.py

Times are best-of-N microseconds per full page render; lower is better.
"""

import time
from string.templatelib import Template

import htpy as hp
import jinja2
from dominate import tags as dt
from tdom import html

from stario.markup import baked, classes, data, render
from stario.markup import html as h

ROW_COUNT = 50
ROWS = [
    {"id": i, "name": f"Item {i}", "active": i % 3 == 0, "price": f"{i}.99"}
    for i in range(ROW_COUNT)
]


def bench(fn, n: int = 2000, repeat: int = 7) -> float:
    for _ in range(200):
        fn()
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        best = min(best, (time.perf_counter_ns() - t0) / n)
    return best / 1000  # us


# --- stario: naive (tree built per call) ---------------------------------------


def stario_naive() -> str:
    return render(
        h.Html(
            {"lang": "en"},
            h.Body(
                classes("bg", "min-h-screen"),
                h.Main(
                    classes("mx-auto", "px-4"),
                    h.H1({"class": "title"}, "Products"),
                    h.Ul(
                        {"class": "product-list"},
                        *[
                            h.Li(
                                classes("card", "active" if r["active"] else None),
                                data({"id": r["id"]}),
                                h.Span({"class": "name"}, r["name"]),
                                h.Span({"class": "price"}, r["price"]),
                            )
                            for r in ROWS
                        ],
                    ),
                ),
            ),
        )
    )


# --- stario: @baked row + layout ------------------------------------------------


@baked
def _row(cls, rid, name, price):
    return h.Li(
        {"class": cls, "data-id": rid},
        h.Span({"class": "name"}, name),
        h.Span({"class": "price"}, price),
    )


@baked
def _page(rows):
    return h.Html(
        {"lang": "en"},
        h.Body(
            classes("bg", "min-h-screen"),
            h.Main(
                classes("mx-auto", "px-4"),
                h.H1({"class": "title"}, "Products"),
                h.Ul({"class": "product-list"}, rows),
            ),
        ),
    )


def stario_baked() -> str:
    return render(
        _page(
            [
                _row(
                    "card active" if r["active"] else "card",
                    r["id"],
                    r["name"],
                    r["price"],
                )
                for r in ROWS
            ]
        )
    )


# --- htpy -----------------------------------------------------------------------


def htpy_page() -> str:
    return str(
        hp.html(lang="en")[
            hp.body(class_=["bg", "min-h-screen"])[
                hp.main(class_=["mx-auto", "px-4"])[
                    hp.h1(class_="title")["Products"],
                    hp.ul(class_="product-list")[
                        (
                            hp.li(
                                class_=["card", "active" if r["active"] else None],
                                data_id=r["id"],
                            )[
                                hp.span(class_="name")[r["name"]],
                                hp.span(class_="price")[r["price"]],
                            ]
                            for r in ROWS
                        )
                    ],
                ]
            ]
        ]
    )


# --- dominate ---------------------------------------------------------------------


def dominate_page() -> str:
    doc = dt.html(lang="en")
    with doc:
        with dt.body(cls="bg min-h-screen"):
            with dt.main(cls="mx-auto px-4"):
                dt.h1("Products", cls="title")
                with dt.ul(cls="product-list"):
                    for r in ROWS:
                        cls = "card active" if r["active"] else "card"
                        with dt.li(cls=cls, data_id=r["id"]):
                            dt.span(r["name"], cls="name")
                            dt.span(r["price"], cls="price")
    return doc.render(pretty=False)


# --- jinja2 ------------------------------------------------------------------------

_env = jinja2.Environment(autoescape=True)
_tmpl = _env.from_string(
    '<html lang="en"><body class="bg min-h-screen"><main class="mx-auto px-4">'
    '<h1 class="title">Products</h1><ul class="product-list">{% for r in rows %}'
    '<li class="card{% if r.active %} active{% endif %}" data-id="{{ r.id }}">'
    '<span class="name">{{ r.name }}</span><span class="price">{{ r.price }}</span>'
    "</li>{% endfor %}</ul></main></body></html>"
)


def jinja_page() -> str:
    return _tmpl.render(rows=ROWS)


# --- tdom (Python 3.14 t-strings) ------------------------------------------------


def _tdom_row(*, cls: str, rid: int, name: str, price: str) -> Template:
    return t'<li class={cls} data-id={rid}><span class="name">{name}</span><span class="price">{price}</span></li>'


def _tdom_page(children: Template) -> Template:
    return t'<html lang="en"><body class={["bg", "min-h-screen"]}><main class={["mx-auto", "px-4"]}><h1 class="title">Products</h1><ul class="product-list">{children}</ul></main></body></html>'


def tdom_naive() -> str:
    """Inline t-string with a list comprehension for rows."""
    return html(t"""
<html lang="en">
<body class={["bg", "min-h-screen"]}>
<main class={["mx-auto", "px-4"]}>
<h1 class="title">Products</h1>
<ul class="product-list">
{[
    t'<li class={["card", "active" if r["active"] else None]} data-id={r["id"]}><span class="name">{r["name"]}</span><span class="price">{r["price"]}</span></li>'
    for r in ROWS
]}
</ul>
</main>
</body>
</html>
""")


def tdom_components() -> str:
    """Reusable Row + Page component functions (fastest tdom shape for this page)."""
    rows = [
        _tdom_row(
            cls="card active" if r["active"] else "card",
            rid=r["id"],
            name=r["name"],
            price=r["price"],
        )
        for r in ROWS
    ]
    return html(_tdom_page(rows))


# --- run ---------------------------------------------------------------------------


def main() -> None:
    contenders = [
        ("stario @baked", stario_baked),
        ("jinja2 (compiled, autoescape)", jinja_page),
        ("stario naive (tree per call)", stario_naive),
        ("htpy", htpy_page),
        ("dominate", dominate_page),
        ("tdom (components)", tdom_components),
        ("tdom (naive t-string)", tdom_naive),
    ]

    results = [(name, bench(fn)) for name, fn in contenders]
    fastest = min(t for _, t in results)

    print(f"\n{ROW_COUNT}-row product page, per render (best of 7 x 2000):\n")
    for name, t in sorted(results, key=lambda r: r[1]):
        print(f"  {name:<34} {t:9.1f} us   {t / fastest:5.1f}x")
    print()


if __name__ == "__main__":
    main()
