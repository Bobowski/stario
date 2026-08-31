"""Unit tests for query string parsing."""

from stario.http.query import ParsedQuery


def test_empty_query() -> None:
    assert ParsedQuery(b"").as_dict() == {}


def test_repeated_keys() -> None:
    qp = ParsedQuery(b"a=1&a=2")
    assert qp.getlist("a") == ["1", "2"]
    assert qp.get("a") == "1"
    assert qp.as_dict() == {"a": "1"}
    assert qp.as_dict(last=True) == {"a": "2"}


def test_percent_decoding() -> None:
    assert ParsedQuery(b"q=100%25").get("q") == "100%"


def test_blank_and_bare_keys() -> None:
    assert ParsedQuery(b"flag=").get("flag") == ""
    assert ParsedQuery(b"enabled").get("enabled") == ""


def test_plus_decoding() -> None:
    assert ParsedQuery(b"a+b=c").get("a b") == "c"


def test_items_preserve_wire_order_and_duplicates() -> None:
    qp = ParsedQuery(b"b=2&a=1&b=3")
    assert qp.items() == [("b", "2"), ("a", "1"), ("b", "3")]
    assert qp.get("b") == "2"
    assert qp.getlist("b") == ["2", "3"]
    assert len(qp) == 2
    assert qp.as_dict() == {"b": "2", "a": "1"}
    assert qp.as_lists() == {"b": ["2", "3"], "a": ["1"]}


def test_get_scans_without_items() -> None:
    qp = ParsedQuery(b"unused=1&target=ok&other=2&target=later")
    assert qp.get("target") == "ok"
    assert qp.get("missing") is None
    assert qp.getlist("target") == ["ok", "later"]
    assert "target" in qp
    assert "unused" in qp
    assert "missing" not in qp
    assert bool(qp) is True
    assert bool(ParsedQuery(b"")) is False
    assert bool(ParsedQuery(b"&&")) is False


def test_first_get_then_as_dict() -> None:
    qp = ParsedQuery(b"a=1&a=2")
    assert qp.get("a") == "1"
    assert qp.as_dict() == {"a": "1"}
    assert qp.as_lists() == {"a": ["1", "2"]}
