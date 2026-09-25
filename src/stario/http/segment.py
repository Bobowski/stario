"""Route pattern segments — the trie edge alphabet.

Each `/` or `.` piece becomes a `Segment` with one of three kinds:

- `exact`    — literal text (`users`)
- `wildcard` — one segment, no slash (`{user_id}`)
- `catchall` — rest of path or host (`{path...}`), only in terminal position

`{name}` and `{name...}` must be the whole path segment or host label.
`Route` parses host and path once via `parse_route`. `add()` reuses those tuples.
"""

import keyword
from typing import Literal, Self

from stario.exceptions import StarioError

type SegmentKind = Literal["exact", "wildcard", "catchall"]


class Segment:
    """One parsed route pattern segment.

    `name` is the trie key / `href()` kwarg. For `exact` segments it is the
    literal value. `pattern` is derived (`users`, `{id}`, `{path...}`).
    """

    __slots__ = ("kind", "name")

    kind: SegmentKind
    name: str

    def __init__(self, kind: SegmentKind, name: str) -> None:
        if not name:
            raise StarioError("route segment name must not be empty")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "name", name)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Segment is immutable")

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Segment)
            and self.kind == other.kind
            and self.name == other.name
        )

    def __hash__(self) -> int:
        return hash((self.kind, self.name))

    def __repr__(self) -> str:
        return f"Segment({self.kind!r}, {self.name!r})"

    @property
    def pattern(self) -> str:
        if self.kind == "exact":
            return self.name
        if self.kind == "catchall":
            return f"{{{self.name}...}}"
        return f"{{{self.name}}}"

    @classmethod
    def parse(cls, route_pattern: str, raw: str) -> Self:
        if "{{" in raw or "}}" in raw:
            return cls("exact", raw.replace("{{", "{").replace("}}", "}"))
        if raw.startswith("{") and raw.endswith("...}"):
            kind: SegmentKind = "catchall"
            name = raw[1:-4]
        elif raw.startswith("{") and raw.endswith("}"):
            kind = "wildcard"
            name = raw[1:-1]
        else:
            if "{" in raw or "}" in raw:
                raise StarioError(
                    "Invalid route parameter: placeholder must fill the segment",
                    context={"pattern": route_pattern, "segment": raw},
                )
            return cls("exact", raw)

        if not name:
            raise StarioError(
                "Invalid route parameter: parameter name is empty",
                context={"pattern": route_pattern, "segment": raw},
            )
        if "{" in name or "}" in name:
            raise StarioError(
                "Invalid route parameter: placeholder must fill the segment",
                context={"pattern": route_pattern, "segment": raw},
            )
        if ":" in name or "!" in name:
            raise StarioError(
                "Invalid route parameter: format specs or conversions are not allowed",
                context={"pattern": route_pattern, "segment": raw, "parameter": name},
            )
        if not (name[0].isalpha() or name[0] == "_"):
            raise StarioError(
                "Invalid route parameter: parameter name must start with a letter or underscore",
                context={"pattern": route_pattern, "segment": raw, "parameter": name},
            )
        if not all(ch.isalnum() or ch == "_" for ch in name):
            raise StarioError(
                "Invalid route parameter: parameter name may contain only letters, numbers, and underscores",
                context={"pattern": route_pattern, "segment": raw, "parameter": name},
            )
        if name in {"query", "fragment"}:
            raise StarioError(
                f"Invalid route parameter: parameter name {name!r} is reserved",
                context={"pattern": route_pattern, "segment": raw, "parameter": name},
            )
        if keyword.iskeyword(name):
            raise StarioError(
                f"Invalid route parameter: parameter name {name!r} is a Python keyword",
                context={"pattern": route_pattern, "segment": raw, "parameter": name},
            )
        return cls(kind, name)


def host_pattern_labels(host: str) -> list[str]:
    if "..." in host:
        # Hide "..." inside catchall placeholders before split(".") so
        # "{tenant...}.example.com" stays one label, not three fragments.
        return [
            seg.replace("\x00", "...") for seg in host.replace("...", "\x00").split(".")
        ]
    return host.split(".")


def parse_host_segments(host: str) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    for raw in host_pattern_labels(host):
        if not raw:
            raise StarioError(
                "Host pattern contains empty host label",
                context={"host": host},
            )
        segment = Segment.parse(host, raw)
        if segment.kind == "exact":
            segment = Segment("exact", segment.name.lower())
        segments.append(segment)
    for segment in segments[1:]:
        if segment.kind == "catchall":
            raise StarioError(
                "Catchall host param in invalid position",
                context={"host": host, "segment": segment.pattern},
            )
    return tuple(segments)


def parse_path_segments(path: str) -> tuple[Segment, ...]:
    path_body = path.strip("/")
    if not path_body:
        return ()
    segments: list[Segment] = []
    for raw in path_body.split("/"):
        if not raw:
            raise StarioError(
                "Route pattern contains empty path segment",
                context={"path": path},
            )
        segments.append(Segment.parse(path, raw))
    for segment in segments[:-1]:
        if segment.kind == "catchall":
            raise StarioError(
                "Catchall path param in invalid position",
                context={"path": path, "segment": segment.pattern},
            )
    return tuple(segments)


def parse_route(
    host: str | None, path: str
) -> tuple[tuple[Segment, ...], tuple[Segment, ...]]:
    """One parse for `Route` and `Router`. Host labels, path pieces, unique names."""
    host_segments = parse_host_segments(host) if host else ()
    path_segments = parse_path_segments(path)
    seen: set[str] = set()
    for segment in (*host_segments, *path_segments):
        if segment.kind == "exact":
            continue
        if segment.name in seen:
            raise StarioError(
                "Duplicate route parameter",
                context={"parameter": segment.name},
            )
        seen.add(segment.name)
    return host_segments, path_segments
