import asyncio
import functools
import inspect
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
    Literal,
    Protocol,
    Self,
    Sequence,
    TypeIs,
    TypeVar,
    cast,
    get_args,
    get_origin,
    overload,
    runtime_checkable,
)

from starlette.requests import Request

from stario.application import Stario

# from stario.application import Stario

type DependencyLifetime = Literal["transient", "request", "singleton", "lazy"]
"""
Specifies the lifetime of a dependency instance:

- "transient": Called every time the dependency is used. Could be called multiple times in the same request.
- "request": Called once and shared for the duration of a request. Reused for subdependencies within the same request.
- "singleton": Called once and shared for the entire lifetime of the application. Use this to avoid global variables.
- "lazy": Returns a callable that can be called to get the actual dependency instance.
          This is useful for dependencies that are expensive to create and you want to defer the creation until it's actually needed.

Use this type to control how dependencies are shared and reused within your application.
"""


@runtime_checkable
class Injectable(Protocol):

    def on_inspect(self, parameter: Parameter) -> Callable[..., Any]:
        """
        This is called when we're trying to build a dependency tree.
        Use this to build a dependency that requires some special handling based on the parameter it's used as.

        def foo(name: Annotated[int, MyInjectable()]) -> int:
            ...

        class MyInjectable:
            def on_inspect(self, parameter: Parameter) -> Callable[..., int]:
                if parameter.annotation == int:
                    return lambda: 42
                else:
                    raise ValueError(f"Unknown annotation: {parameter.annotation}")

        In this case MyInjectable will be provided a context it's being used in (parameter),
          and should return a dedicated callable to actually be used while requested.
        """
        ...


R = TypeVar("R")


class Dependency:

    __slots__ = ("name", "function", "is_async", "lifetime", "children")

    def __init__(
        self,
        name: str,
        function: Callable[..., Any] | type[Request] | type[Stario],
        lifetime: DependencyLifetime = "request",
        children: Sequence[Self] | None = None,
    ) -> None:

        self.name = name
        self.function = function
        self.is_async = is_async_callable(function)
        self.lifetime = lifetime
        self.children = list(children) if children is not None else []

    @classmethod
    def _build_node(cls, prm: Parameter) -> Self:

        if get_origin(prm.annotation) is Annotated:
            # name: Annotated[type, dependency[, lifetime]]

            try:
                _, arg, *modifiers = get_args(prm.annotation)

            except ValueError:
                raise ValueError(
                    f"Unknown annotation: {prm.name} must be Annotated with one argument"
                )

            lifetime = modifiers[0] if modifiers else "request"
            func = arg.on_inspect(prm) if isinstance(arg, Injectable) else arg
            return cls._build_tree(prm.name, func, lifetime)

        if isinstance(prm.annotation, type):
            # name: Request | Stario
            # will be replaced by actual Request or Stario instance on resolve

            if issubclass(prm.annotation, Request):
                return cls(prm.name, Request)

            elif issubclass(prm.annotation, Stario):
                return cls(prm.name, Stario)

            raise ValueError(f"Unknown annotation type: {prm.annotation}")

        if prm.default is not Parameter.empty:
            # name: Any = default

            return cls(prm.name, lambda: prm.default)

        raise ValueError(f"Unknown annotation: {prm.annotation}")

    @classmethod
    def _build_tree(
        cls,
        name: str,
        handler: Callable[..., Any],
        lifetime: DependencyLifetime = "request",
    ) -> Self:
        """
        Builds a tree of dependencies starting from a given function.
        """

        fn = get_callable(handler)
        signature = inspect.signature(fn)
        children = [
            cls._build_node(param)
            for param in signature.parameters.values()
            if param.name != "self"
        ]

        return cls(name, fn, lifetime, children)

    @classmethod
    def build(cls, handler: Callable[..., Any]) -> Self:
        """
        Builds a tree of dependencies starting from a given function.
        """

        return cls._build_tree(handler.__name__, handler)

    async def resolve(self, app: Stario, request: Request) -> Any:
        # Fast path for built-in types
        if isinstance(self.function, type) and issubclass(self.function, Request):
            return request

        if isinstance(self.function, type) and issubclass(self.function, Stario):
            return app

        # After checks, self.function must be callable
        self.function = cast(Callable[..., Any], self.function)

        # Get caches once
        singletons = app.state.cache

        # Handle singleton lifetime with early return
        if self.lifetime == "singleton":
            if self.function in singletons:
                return await singletons[self.function]
            # Create future for singleton
            fut = asyncio.Future()
            singletons[self.function] = fut

        # Handle request lifetime
        elif self.lifetime == "request":
            # Initialize request cache if not exists
            if not hasattr(request.state, "cache"):
                request.state.cache = {}

            futures = request.state.cache

            if self.function in futures:
                return await futures[self.function]

            # Create future for request scope
            fut = asyncio.Future()
            futures[self.function] = fut

        else:
            fut = None

        # TODO: lazy?

        # Resolve children efficiently
        try:
            if not self.children:
                arguments = {}

            elif len(self.children) == 1:
                # Single child - no need for TaskGroup
                child = self.children[0]
                arguments = {child.name: await child.resolve(app, request)}

            else:
                # Multiple children - use TaskGroup for parallel execution
                async with asyncio.TaskGroup() as tg:
                    tasks = [
                        tg.create_task(d.resolve(app, request)) for d in self.children
                    ]
                arguments = {
                    c.name: task.result() for c, task in zip(self.children, tasks)
                }

            # Execute function
            if self.is_async:
                result = await self.function(**arguments)
            else:
                result = self.function(**arguments)

            # Set result in future if we created one
            if fut is not None:
                fut.set_result(result)

            return result

        except Exception as e:
            if fut is not None and not fut.done():
                # Avoid un-retrieved exception warnings on cached futures
                fut.cancel()
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
