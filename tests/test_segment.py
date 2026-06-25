"""Tests for route pattern segment parsing."""

import pytest

from stario.exceptions import StarioError
from stario.routing.segment import (
    Segment,
    host_pattern_labels,
    parse_host_segments,
    parse_path_segments,
)


class TestSegmentParse:
    def test_exact_segment(self):
        segment = Segment.parse("/users", "users")

        assert segment.kind == "exact"
        assert segment.name == "users"
        assert segment.pattern == "users"

    def test_wildcard_segment(self):
        segment = Segment.parse("/users/{id}", "{id}")

        assert segment.kind == "wildcard"
        assert segment.name == "id"
        assert segment.pattern == "{id}"

    def test_catchall_segment(self):
        segment = Segment.parse("/files/{path...}", "{path...}")

        assert segment.kind == "catchall"
        assert segment.name == "path"
        assert segment.pattern == "{path...}"

    def test_rejects_partial_placeholder(self):
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/items", "{id}-edit")

    def test_rejects_unclosed_placeholder(self):
        with pytest.raises(StarioError, match="placeholder must fill the segment"):
            Segment.parse("/items", "{broken")

    def test_rejects_empty_parameter_name(self):
        with pytest.raises(StarioError, match="parameter name is empty"):
            Segment.parse("/items", "{}")

    def test_is_frozen(self):
        segment = Segment.parse("/users", "users")

        with pytest.raises(AttributeError):
            segment.name = "other"  # type: ignore[misc]


class TestHostPatternLabels:
    def test_splits_simple_host(self):
        assert host_pattern_labels("api.example.com") == [
            "api",
            "example",
            "com",
        ]

    def test_preserves_catchall_label(self):
        assert host_pattern_labels("{tenant...}.example.com") == [
            "{tenant...}",
            "example",
            "com",
        ]


class TestParseHostSegments:
    def test_lowercases_exact_labels(self):
        segments = parse_host_segments("API.Example.COM")

        assert [segment.name for segment in segments] == ["api", "example", "com"]

    def test_rejects_empty_label(self):
        with pytest.raises(StarioError, match="empty host label"):
            parse_host_segments("api..example.com")

    def test_rejects_catchall_after_first_label(self):
        with pytest.raises(StarioError, match="Catchall host param in invalid position"):
            parse_host_segments("example.{tenant...}.com")


class TestParsePathSegments:
    def test_parses_canonical_path(self):
        segments = parse_path_segments("/users/{user_id}")

        assert len(segments) == 2
        assert segments[0].kind == "exact"
        assert segments[1].name == "user_id"

    def test_root_has_no_segments(self):
        assert parse_path_segments("/") == ()

    def test_rejects_empty_segment(self):
        with pytest.raises(StarioError, match="empty path segment"):
            parse_path_segments("/users//profile")

    def test_rejects_catchall_before_last_segment(self):
        with pytest.raises(StarioError, match="Catchall path param in invalid position"):
            parse_path_segments("/files/{path...}/download")
