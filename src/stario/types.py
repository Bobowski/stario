"""
Type definitions and aliases for Stario.

This module contains shared type definitions that are used across multiple modules.
By centralizing these types here, we avoid circular import issues.
"""

from typing import Awaitable, Callable, Mapping, Sequence

from starlette.types import Receive, Scope, Send

# Headers constraint type - used for route header matching
type HeadersConstraint = Mapping[str, str | None] | Sequence[
    str | tuple[str, str | None]
]

# Endpoint function type
type EndpointFunction[T] = Callable[..., T]

# Request handler type
type RequestHandler = Callable[[Scope, Receive, Send], Awaitable[None]]

# Adapter function type
type AdapterFunction[T] = Callable[[EndpointFunction[T]], RequestHandler]
