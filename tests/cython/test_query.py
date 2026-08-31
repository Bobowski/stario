"""Cython query-byte parse."""

from stario_cython.exchange import ParsedQuery as CyQuery


CASES = [
    b"",
    b"a=1&a=2",
    b"q=100%25",
    b"flag=",
    b"enabled",
    b"a+b=c",
    b"q=%C3%A9",
    b"q=%A9",
    b"a=1=2",
    b"&&a=1",
    b"q=%zz",
    b"q=%",
    b"q=%2",
    b"q=%2G",
    b"=x",
    b"a=&b=2",
    b"q=%C3%A9&raw=\xe9",
    b"a=1&b=2&a=3",
    b"hello+world=x+y",
    b"x=%20%2B%20",
]


def test_cython_query_cases() -> None:
    for raw in CASES:
        cy = CyQuery(raw)
        assert cy.get("missing", "d") == "d"
        items = cy.items()
        assert len(cy) == len({key for key, _value in items})
        assert bool(cy) == bool(items)
        for key, _value in items:
            assert key in cy
            assert cy.get(key) == next(v for k, v in items if k == key)
            assert cy.getlist(key) == [v for k, v in items if k == key]
