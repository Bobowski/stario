"""Datastar helpers for attributes, actions, tags, signals, and SSE.

Import the default singletons and the classes that build them:

```python
from stario.datastar import (
    DatastarActions,
    DatastarAttributes,
    ModuleScript,
    SSE,
    at,
    data,
    read_signals,
)
```

`at` and `data` are the stock `DatastarActions` / `DatastarAttributes`
instances. Import the classes when you need a separate namespace (tests or
multiple configured builders).
"""

from .actions import DatastarActions, at
from .attributes import DatastarAttributes, data
from .signals import FileSignal, read_signals
from .sse import SSE
from .tags import DATASTAR_CDN_URL, ModuleScript

__all__ = [
    "DATASTAR_CDN_URL",
    "SSE",
    "DatastarActions",
    "DatastarAttributes",
    "FileSignal",
    "ModuleScript",
    "at",
    "data",
    "read_signals",
]
