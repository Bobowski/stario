import asyncio
import functools
import inspect
from dataclasses import dataclass
from inspect import (
    Parameter,
    isasyncgenfunction,
    isgeneratorfunction,
)
from typing import (
    Annotated,
    Any,
    Awaitable,
    Callable,
    Self,
    TypeIs,
    TypeVar,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

# from stario.application import Stario
from stario.parameters import RequestParam
from stario.types import CacheScope, RunMode


@dataclass
class Inject:
    func: Callable
    cache: CacheScope = "request"
    mode: RunMode = "auto"


R = TypeVar("R")


class Dependency:

    def __init__(
        self,
        func: Callable[..., Any],
        cache: CacheScope = "request",
        mode: RunMode = "auto",
        return_type: type[Any] | None = None,
        default: Any = Parameter.empty,
    ) -> None:
        # This is what we use to resolve the dependency
        self.func = func
        self.cache = cache
        self.mode = mode
        # Useful info for the tree
        self.is_async = is_async_callable(self.func)
        self.is_gen = is_generator_callable(self.func)
        self.return_type = return_type or get_type_hints(self.func).get("return")
        self.default = default
        self.children: dict[str, Self] = {}

    async def call(self, *args: Any, **kwargs: Any) -> Any:
        if self.mode == "auto":
            # Auto: async if async
            if self.is_async:
                return await self.func(*args, **kwargs)
            elif self.is_gen:
                # This is for a StreamingResponse so we compute it sync
                #   and then it's handled by the StreamingResponse threded / async like it wants
                return self.func(*args, **kwargs)

            # run on threadpool if sync
            return await run_in_threadpool(self.func, *args, **kwargs)

        if self.mode == "sync":
            if self.is_async:
                # We can't run this on threadpool
                return await self.func(*args, **kwargs)

            return self.func(*args, **kwargs)

        if self.mode == "thread":
            return await run_in_threadpool(self.func, *args, **kwargs)

        raise ValueError(f"Unknown run mode: {self.mode}")

    @classmethod
    def _build_function_parameter(cls, param: Parameter) -> Self:
        """
        Based on the function inspect parameter, build a dependency for it.
        """

        if get_origin(param.annotation) is Annotated:

            try:
                # We're interested in the second argument
                # The first is the annotated type, so we don't care
                _, arg, *_ = get_args(param.annotation)

            except ValueError:
                raise ValueError(
                    f"Unknown annotation: {param.name} must be Annotated with one argument"
                )

            if isinstance(arg, RequestParam):
                # Updates url param based on inspect info like defaults etc.

                # Getting those params should be "quick" so we run it sync
                #  rather than delegating to threadpool
                return cls.build(
                    function=arg.digest_parameter(param),
                    mode="sync",
                    default=param.default,
                )

            elif isinstance(arg, Inject):
                return cls.build(
                    function=arg.func,
                    cache=arg.cache,
                    mode=arg.mode,
                    default=param.default,
                )

            return cls.build(function=arg, default=param.default)

        elif isinstance(param.annotation, type) and issubclass(
            param.annotation, Request
        ):
            # This is something of a special case
            return cls(Request)

        elif param.default is not Parameter.empty:
            return cls(
                func=lambda: param.default,
                return_type=param.annotation,  # type: ignore[assignment]
                mode="sync",
                default=param.default,
            )

        else:
            # We cannot really build dependencies for this so we give up
            raise ValueError(
                f"Unknown annotation: {param.name}: {param.annotation} must be Annotated or Request"
            )

    @classmethod
    def build(
        cls,
        function: Callable[..., Any],
        cache: CacheScope = "request",
        mode: RunMode = "auto",
        default: Any = Parameter.empty,
    ) -> Self:
        """
        Builds a tree of dependencies starting from a given function.
        """

        func = get_callable(function)
        signature = inspect.signature(func)
        info = cls(func=func, cache=cache, mode=mode, default=default)

        info.children = {
            param_name: cls._build_function_parameter(param)
            for param_name, param in signature.parameters.items()
            if param_name != "self"
        }

        return info

    async def resolve(
        self,
        request: Request,
        app: Any,
        futures: dict[Callable, Awaitable[Any]],
    ) -> Any:

        if self.func is Request:
            return request

        if self.cache == "app" and self.func in app.cache:
            return app.cache[self.func]

        if self.cache == "request" and self.func in futures:
            return await futures[self.func]

        fut = asyncio.Future()
        if self.cache == "request":
            futures[self.func] = fut

        try:
            if len(self.children) == 1:
                name, child = list(self.children.items())[0]
                arguments = {name: await child.resolve(request, app, futures)}

            elif self.children:
                dep_results = await asyncio.gather(
                    *[d.resolve(request, app, futures) for d in self.children.values()]
                )
                arguments = {k: v for k, v in zip(self.children.keys(), dep_results)}

            else:
                arguments = {}

            # Execute this node's function
            result = await self.call(**arguments)

            if self.cache == "app":
                app.cache[self.func] = result

            elif self.cache == "request":
                fut.set_result(result)

            else:
                # For 'none' scope, return result directly without setting future
                return result  # type: ignore

            return result

        except Exception as e:
            if self.cache in ("request", "app"):
                fut.set_exception(e)
            raise e


T = TypeVar("T")
AwaitableCallable = Callable[..., Awaitable[T]]


@overload
def is_async_callable(obj: AwaitableCallable[T]) -> TypeIs[AwaitableCallable[T]]: ...


@overload
def is_async_callable(obj: Any) -> TypeIs[AwaitableCallable[Any]]: ...


def is_async_callable(obj: Any) -> Any:
    while isinstance(obj, functools.partial):
        obj = obj.func

    return inspect.iscoroutinefunction(obj) or (
        callable(obj) and inspect.iscoroutinefunction(obj.__call__)
    )


def is_generator_callable(obj: Any) -> bool:
    while isinstance(obj, functools.partial):
        obj = obj.func

    return isgeneratorfunction(obj) or isasyncgenfunction(obj)


def get_callable(obj: Any) -> Callable:
    # If it's the function use it, if it's an object with a __call__ use that

    if inspect.isroutine(obj) or inspect.iscoroutinefunction(obj):
        return obj

    return obj.__call__
