"""HTTP testing: in-process client with no open port.

Public surface is mainly `TestClient`, `TestResponse`, `TestStreamResponse`,
`TestTracer`, and `aload_app` (bootstrap loading for integration tests).
Tests need an async runner (for example pytest-asyncio).
"""

from stario.testing.client import TestClient
from stario.testing.load import aload_app
from stario.testing.response import TestResponse, TestStreamResponse
from stario.testing.tracer import TestTracer

__all__ = [
    "TestClient",
    "TestResponse",
    "TestStreamResponse",
    "TestTracer",
    "aload_app",
]
