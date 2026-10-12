"""Full-page render benchmark: build vs render vs encode at 10/100/500 rows.

Run from the project root:

    uv run benchmarks/html/render_page.py
"""

import time

from stario import classes, h
from stario.markup import render


def row(i):
    return h.Tr(
        h.Td(classes("px-3", "py-2", "font-mono"), f"user-{i}"),
        h.Td(classes("px-3", "py-2"), f"Name {i} & co <b>"),
        h.Td(classes("px-3", "py-2", "text-right"), i * 7),
        h.Td(h.A({"href": f"/users/{i}"}, classes("link"), "open")),
    )


def page(n):
    return h.Html(
        h.Head(h.Title("Users"), h.Link({"rel": "stylesheet", "href": "/static/app.css"})),
        h.Body(
            classes("min-h-screen", "bg-base-100"),
            h.Nav(classes("navbar"), h.A({"href": "/"}, "Home"), h.A({"href": "/users"}, "Users")),
            h.Main(
                classes("container", "mx-auto", "p-6"),
                h.H1(classes("text-2xl", "font-semibold"), "Users"),
                h.Table(
                    classes("table"),
                    h.Thead(h.Tr(*[h.Th(c) for c in ("id", "name", "score", "")])),
                    h.Tbody(*[row(i) for i in range(n)]),
                ),
            ),
            h.Footer(classes("footer"), h.P("Made with Stario")),
        ),
    )


def best(fn, n, repeat=7):
    for _ in range(50):
        fn()
    b = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter_ns()
        for _ in range(n):
            fn()
        b = min(b, (time.perf_counter_ns() - t0) / n)
    return b / 1000


for rows in (10, 100, 500):
    tree = page(rows)
    out = render(tree)
    n = 2000 if rows <= 100 else 400
    build = best(lambda rows=rows: page(rows), n)
    rp = best(lambda tree=tree: render(tree), n)
    enc = best(lambda out=out: out.encode("utf-8"), n)
    print(
        f"rows={rows:>3} bytes={len(out):>6}  build {build:8.1f}us  render {rp:8.1f}us"
        f"  encode {enc:6.1f}us  render share of build+render: {rp / (build + rp):.0%}"
    )
