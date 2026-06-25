"""Route pattern segments — the shared edge alphabet for format and trie walks.

Each URL piece becomes a ``Segment`` with one of three kinds:

- ``exact``    — literal text (`users`)
- ``wildcard`` — one segment, no slash (`{user_id}`)
- ``catchall`` — rest of path or host (`{path...}`), only in terminal position

``Segment`` objects are shared by ``UrlPath`` (link building) and the HTTP trie
(matching). Use ``Segment.parse`` for pattern text; host/path parsers below apply
placement rules.
"""

import keyword
from dataclasses import dataclass
from typing import Literal, Self

from stario.exceptions import StarioError

type SegmentKind = Literal["exact", "wildcard", "catchall"]


@dataclass(frozen=True, slots=True)
class Segment:
    """One parsed route pattern segment.

    ``name`` is the trie key / ``href()`` kwarg. For ``exact`` segments it is the
    literal value; ``pattern`` is the authored segment text.
    """

    kind: SegmentKind
    name: str
    pattern: str

    def __post_init__(self) -> None:
        if not self.name:
            raise StarioError(
                "route segment name must not be empty",
                help_text="Use whole-segment placeholders like '{name}'.",
            )

    @classmethod
    def parse(cls, route_pattern: str, raw: str) -> Self:
        # Placeholders must occupy the whole segment — no partial `{id}-edit`.
        if raw.startswith("{") and raw.endswith("...}"):
            kind: SegmentKind = "catchall"
            name = raw[1:-4]
        elif raw.startswith("{") and raw.endswith("}"):
            kind = "wildcard"
            name = raw[1:-1]
        else:
            if "{" in raw or "}" in raw:
                raise StarioError(
                    "Invalid route parameter",
                    context={
                        "pattern": route_pattern,
                        "segment": raw,
                        "reason": "placeholder must fill the segment",
                    },
                    help_text=(
                        "Use whole-segment placeholders like '{name}' or terminal "
                        "catchalls like '{path...}'."
                    ),
                )
            return cls("exact", raw, raw)

        # Shared name rules for wildcards and catchalls.
        if not name:
            raise StarioError(
                "Invalid route parameter",
                context={
                    "pattern": route_pattern,
                    "segment": raw,
                    "reason": "parameter name is empty",
                },
                help_text=(
                    "Use whole-segment placeholders like '{name}' or terminal "
                    "catchalls like '{path...}'."
                ),
            )
        if not (name[0].isalpha() or name[0] == "_"):
            raise StarioError(
                "Invalid route parameter",
                context={
                    "pattern": route_pattern,
                    "segment": raw,
                    "reason": "parameter name must start with a letter or underscore",
                },
                help_text=(
                    "Use whole-segment placeholders like '{name}' or terminal "
                    "catchalls like '{path...}'."
                ),
            )
        if not all(ch.isalnum() or ch == "_" for ch in name):
            raise StarioError(
                "Invalid route parameter",
                context={
                    "pattern": route_pattern,
                    "segment": raw,
                    "reason": (
                        "parameter name may contain only letters, numbers, "
                        "and underscores"
                    ),
                },
                help_text=(
                    "Use whole-segment placeholders like '{name}' or terminal "
                    "catchalls like '{path...}'."
                ),
            )
        if keyword.iskeyword(name):
            raise StarioError(
                "Invalid route parameter",
                context={
                    "pattern": route_pattern,
                    "segment": raw,
                    "reason": f"parameter name {name!r} is a Python keyword",
                },
                help_text=(
                    "Choose a non-reserved placeholder name such as "
                    f"'{name}_id' or '{name}_name'."
                ),
            )
        if kind == "wildcard":
            return cls(kind, name, f"{{{name}}}")
        return cls(kind, name, f"{{{name}...}}")


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
                help_text="Remove repeated dots from the host pattern.",
            )
        segment = Segment.parse(host, raw)
        # Host matching is case-insensitive; store exact labels lowercased.
        if segment.kind == "exact":
            segment = Segment("exact", raw.lower(), raw.lower())
        segments.append(segment)
    # Catchall is only valid on the leftmost label ({tenant...}.example.com).
    for segment in segments[1:]:
        if segment.kind == "catchall":
            raise StarioError(
                "Catchall host param in invalid position",
                context={"host": host, "segment": segment.pattern},
                help_text="Catchall is only allowed on the first host segment.",
            )
    return tuple(segments)


def parse_path_segments(path: str) -> tuple[Segment, ...]:
    """Parse a canonical path (from ``normalize_path``) into segments."""
    path_body = path.strip("/")
    if not path_body:
        return ()
    segments: list[Segment] = []
    for raw in path_body.split("/"):
        if not raw:
            raise StarioError(
                "Route pattern contains empty path segment",
                context={"path": path},
                help_text="Remove repeated slashes from the route pattern.",
            )
        segments.append(Segment.parse(path, raw))
    # Catchall is only valid on the final path segment (/files/{path...}).
    for segment in segments[:-1]:
        if segment.kind == "catchall":
            raise StarioError(
                "Catchall path param in invalid position",
                context={"path": path, "segment": segment.pattern},
                help_text="Catchall is only allowed on the last path segment.",
            )
    return tuple(segments)
