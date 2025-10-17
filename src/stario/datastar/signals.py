import inspect
from typing import Annotated, Any, get_args

from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request

from .events import SignalsDict


class ParseSignals:
    def __init__(self, name: str = "datastar"):
        self.name = name
        self.adapter = TypeAdapter(SignalsDict)

    async def __call__(self, request: Request) -> SignalsDict:
        if request.method == "GET":
            raw = request.query_params.get(self.name)
        else:
            raw = await request.body()

        if raw is None:
            return {}

        try:
            return self.adapter.validate_json(raw)
        except ValidationError as exc:
            # Pydantic validation failed (malformed/invalid JSON, model error)
            raise HTTPException(
                status_code=422, detail=f"Format of signals payload is invalid. {exc}"
            )
        except Exception as exc:
            # Invalid JSON, parsing errors, etc.
            raise HTTPException(
                status_code=400, detail=f"Malformed signals payload. {exc}"
            )


type Signals = Annotated[SignalsDict, ParseSignals()]
"""
Dependency that reads all Datastar signals from the incoming request and
provides them as a JSON/dict.

This can be used in your endpoint or handler to access all signals sent by
the client, parsed and validated as a dictionary. For GET requests, signals
are read from the query parameters; for other methods, they are read from
the request body.

Example usage:
    async def my_handler(signals: Signals):
        # signals is a dict containing all Datastar signals
        ...

The signals are validated using Pydantic and provided as a standard Python
dictionary.
"""


class _ParseSignal[T]:

    # Class-level cache for singleton behavior
    # Uniqueness key: type, name, return_type, default
    # This is for request scope reusability for dependecy injection mechanism.
    _cache: dict[tuple, "_ParseSignal[Any]"] = {}

    def __init__(
        self, name: str, return_type: type, default: T | type[inspect.Parameter.empty]
    ):
        self.name = name
        self.parts = self.name.split(".")
        self.return_type = return_type
        self.default = default
        self.adapter = TypeAdapter[T](self.return_type)

    @classmethod
    def get_or_create(
        cls, signal_name: str | None, param: inspect.Parameter
    ) -> "_ParseSignal[T]":
        """
        Get an existing parser from cache or create a new one.
        """

        name = signal_name or param.name.replace("__", ".")
        return_type: type = get_args(param.annotation)[0]
        default: T | type[inspect.Parameter.empty] = param.default

        # Uniqueness key:
        cache_key = (type(param), name, return_type, default)
        if cache_key not in cls._cache:
            cls._cache[cache_key] = cls(name, return_type, default)

        return cls._cache[cache_key]

    async def __call__(self, signals: Signals) -> T:
        try:
            signal_dict = signals
            for part in self.parts:

                if isinstance(signal_dict, dict):
                    signal_dict = signal_dict[part]
                    continue

                raise KeyError(self.name)

            return self.adapter.validate_python(signal_dict)

        except KeyError:

            if self.default is not inspect.Parameter.empty and not isinstance(
                self.default, type
            ):
                return self.default

            raise HTTPException(
                status_code=400,
                detail=f"Missing required signal '{self.name}'. Provide it in the request.",
            )
        except ValidationError as e:
            expected = getattr(self.return_type, "__name__", str(self.return_type))
            raise HTTPException(
                status_code=422,
                detail=f"Invalid type of signal '{self.name}'. Expected {expected}. {e}",
            )


class ParseSignal[T]:
    def __init__(self, name: str | None = None):
        """
        Dependency for extracting a signal value from the incoming request.

        Use this as a dependency to read and validate a signal from the request's
        signals dictionary. If a `name` is provided, the signal with that name will
        be extracted; otherwise, the name of the function parameter will be used as
        the signal key.

        NOTE: if name is not provided directly it will be derived from the parameter
        name by replacing double underscores with dots.
        eg. "online__counter" will be "online.counter"
        async def handler(online__counter: Signal[int]):
            # Reads the 'online.counter' signal and validates it as int

        This is typically used with `Signal[T]` or
        `Annotated[T, ParseSignal(...)]` to declare that a handler parameter should
        be populated from a signal and validated as type `T`.

        Exceptions:
            - Raises `HTTPException` with status code 400 if the required signal is
              missing and no default is provided.
            - Raises `HTTPException` with status code 422 if the signal is present
              but cannot be validated as type `T`.

        Example:
            async def handler(online__counter: Signal[int]):
                # Reads the 'online.counter' signal and validates it as int
                # "__" is replaced with "." in the parameter name

            async def handler(counter: Annotated[int, ParseSignal("online")]):
                # Reads the 'online' signal and validates it as int, assigns to 'counter'
        """
        self.name = name

    def __call__(self, param: inspect.Parameter) -> _ParseSignal[T]:
        return _ParseSignal.get_or_create(self.name, param)


type Signal[T] = Annotated[T, ParseSignal()]
"""
A type annotation for extracting a signal value from the incoming request.

Use `Signal[T]` as a parameter type in your handler to declare that you expect
a signal named after the parameter and that it should be parsed and validated
as type `T`. If the signal is missing, a 400 error is raised unless a default
is provided. If the signal is present but cannot be validated as type `T`,
a 422 error is raised.

Example:
    async def handler(online: Signal[int]):
        # 'online' will be parsed from the signals dict and validated as int
        ...

To specify a custom signal name, use `Annotated` directly:

    from typing import Annotated

    async def handler(counter: Annotated[int, ParseSignal("online")]):
        # 'counter' will be parsed from the 'online' signal
        ...
"""
