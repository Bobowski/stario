"""Shared type aliases for the test client."""

from collections.abc import Mapping, Sequence

from stario.http.headers import Headers

type QueryScalar = str | int | float | bool
type QueryValue = QueryScalar | Sequence[QueryScalar]
type QueryParamInput = Mapping[str, QueryValue] | Sequence[tuple[str, QueryScalar]]
type HeaderMap = Mapping[str, str] | Headers
type CookieMap = Mapping[str, str]
type FormValue = str | int | float | bool
type FormData = (
    Mapping[str, FormValue | Sequence[FormValue]] | Sequence[tuple[str, FormValue]]
)
type FileValue = bytes | str | tuple[str, bytes | str] | tuple[str, bytes | str, str]
type FileData = Mapping[str, FileValue] | Sequence[tuple[str, FileValue]]
