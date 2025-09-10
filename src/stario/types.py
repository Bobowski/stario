from typing import Literal

type CacheScope = Literal["none", "request", "app"] | tuple[Literal["ttl"], int]
type RunMode = Literal["auto", "sync", "thread"]
