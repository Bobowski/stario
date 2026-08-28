"""Cython query-byte parse matches the Python ParsedQuery helper."""

from stario.http.query import ParsedQuery as PyQuery
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


def test_cython_query_matches_python() -> None:
    for raw in CASES:
        py = PyQuery(raw)
        cy = CyQuery(raw)
        assert cy.as_dict() == py.as_dict(), raw
        assert cy.as_dict(last=True) == py.as_dict(last=True), raw
        assert cy.as_lists() == py.as_lists(), raw
        assert cy.items() == py.items(), raw
        assert len(cy) == len(py)
        assert bool(cy) == bool(py)
        for key, _value in py.items():
            assert key in cy
            assert cy.get(key) == py.get(key)
            assert cy.getlist(key) == py.getlist(key)
        assert cy.get("missing", "d") == "d"
