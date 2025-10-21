import asyncio
import functools
import inspect
import types
from typing import (
    Annotated,
    Any,
    Awaitable,
    Callable,
    Literal,
    Sequence,
    TypeAliasType,
    TypeVar,
    cast,
    get_args,
    get_origin,
    overload,
)

from starlette.requests import Request

from stario.application import Stario
from stario.exceptions import (
    DependencyBuildError,
    InvalidAnnotationError,
    InvalidCallableError,
)

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


def resolve_request(request: Request) -> Request:
    return request


def resolve_stario(request: Request) -> Stario:
    return request.app


# TypeVar for methods that can return different generic variants
U = TypeVar("U")


class Dependency[T]:

    __slots__ = (
        "name",
        "function",
        "is_async",
        "lifetime",
        "children",
    )

    def __init__(
        self,
        name: str,
        function: Callable[..., T],
        lifetime: DependencyLifetime = "request",
        children: Sequence["Dependency[Any]"] | None = None,
    ) -> None:

        self.name = name
        self.function = function
        self.is_async = is_async_callable(function)
        self.lifetime = lifetime
        self.children = list(children) if children is not None else []

    @classmethod
    def _build_node(cls, p: inspect.Parameter) -> "Dependency[Any]":

        p = _unwrap_type_alias(p)

        if get_origin(p.annotation) is Annotated:
            # name: Annotated[type, dependency[, lifetime]]

            try:
                _, arg, *modifiers = get_args(p.annotation)

            except ValueError as e:
                raise InvalidAnnotationError(
                    f"Invalid Annotated type for parameter '{p.name}'",
                    context={
                        "parameter": p.name,
                        "annotation": str(p.annotation),
                        "error": str(e),
                    },
                    help_text="Annotated types to be considered for dependency injection must have at least one type argument and one dependency callable.",
                    example="""from typing import Annotated
from stario.requests import ParseQueryParam

# Correct usage:
def handler(user_id: Annotated[int, ParseQueryParam()]): ...
def handler(data: Annotated[dict, my_function]): ...

# With lifetime:
def handler(db: Annotated[Database, get_db, "singleton"]): ...""",
                ) from e

            lifetime = modifiers[0] if modifiers else "request"
            func = _try_apply_parameter_decorator(arg, p)

            return cls._build_tree(p.name, func, lifetime)

        if isinstance(p.annotation, type):
            # name: Request | Stario
            # will be replaced by actual Request or Stario instance on resolve

            if issubclass(p.annotation, Request):
                return Dependency(p.name, resolve_request)

            elif issubclass(p.annotation, Stario):
                return Dependency(p.name, resolve_stario)

            raise InvalidAnnotationError(
                f"Unsupported annotation type for parameter '{p.name}'",
                context={
                    "parameter": p.name,
                    "annotation": str(p.annotation),
                    "annotation_type": type(p.annotation).__name__,
                },
                help_text="Only Request, Stario, or another Annotated dependency are supported for dependency injection.",
                example="""from typing import Annotated
from starlette.requests import Request, ParseQueryParam
from stario import Stario

# Supported patterns:
def handler(request: Request): ...                       # Request (can access scope, headers, etc.)
def handler(app: Stario): ...                            # Stario app instance
def handler(user_id: Annotated[int, ParseQueryParam()]): # Annotated with parameter extractor
def handler(db: Annotated[DB, get_db]): ...              # Annotated with dependency function""",
            )

        if p.default is not inspect.Parameter.empty:
            # name: Any = default

            return Dependency[Any](p.name, lambda: p.default)

        raise InvalidAnnotationError(
            f"Cannot resolve dependency for parameter '{p.name}'",
            context={
                "parameter": p.name,
                "annotation": str(p.annotation),
                "has_default": p.default is not inspect.Parameter.empty,
            },
            help_text="Parameters must have a type annotation (Request, Stario, Annotated) or a default value.",
            example="""from typing import Annotated
from starlette.requests import Request, QueryParam
from stario import Stario

# Correct approaches:
def handler(request: Request): ...          # Type annotation
def handler(user_id: QueryParam[int]): ...  # Annotated type
def handler(page: int = 1): ...             # Default value
def handler(debug: bool = False): ...       # Default value""",
        )

    @classmethod
    def _build_tree[U](
        cls,
        name: str,
        handler: Callable[..., U],
        lifetime: DependencyLifetime = "request",
    ) -> "Dependency[U]":
        """
        Builds a tree of dependencies starting from a given function.
        """

        parameters, creation_func = _inspect_callable(handler)
        children = [cls._build_node(param) for param in parameters]

        return Dependency(name, creation_func, lifetime, children)

    @classmethod
    def build[U](
        cls,
        handler: Callable[..., U],
        lifetime: DependencyLifetime = "request",
    ) -> "Dependency[U]":
        """
        Builds a tree of dependencies starting from a given function.
        """

        return Dependency._build_tree(handler.__name__, handler, lifetime)

    async def resolve(self, request: Request) -> T | Awaitable[T]:

        # Fast path for built-in types
        if self.function is resolve_request or self.function is resolve_stario:
            return self.function(request)

        # Check for override - most-performant short-circuit
        if (override_func := request.app._mocks.get(self.function)) is not None:
            # Recursively resolve the override function
            return await Dependency._build_tree(
                self.name,
                override_func,
                cast(DependencyLifetime, self.lifetime),
            ).resolve(request)

        # Get caches once
        singletons = request.app.state.cache

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

        # Handle lazy lifetime - return an awaitable that resolves on demand
        elif self.lifetime == "lazy":

            async def lazy_resolver() -> T:
                # Resolve children efficiently
                if not self.children:
                    arguments = {}

                elif len(self.children) == 1:
                    # Single child - no need for gather
                    child = self.children[0]
                    arguments = {child.name: await child.resolve(request)}

                else:
                    # Multiple children - use gather for parallel execution
                    results = await asyncio.gather(
                        *[d.resolve(request) for d in self.children],
                        return_exceptions=True,
                    )
                    # Check for exceptions and raise the first one found
                    for result in results:
                        if isinstance(result, Exception):
                            raise result
                    arguments = {
                        c.name: result for c, result in zip(self.children, results)
                    }

                # Execute function
                if self.is_async:
                    result = await cast(Awaitable[T], self.function(**arguments))
                else:
                    result = self.function(**arguments)

                return result

            return lazy_resolver()

        else:
            fut = None

        # Resolve children efficiently
        try:
            if not self.children:
                arguments = {}

            elif len(self.children) == 1:
                # Single child - no need for TaskGroup
                child = self.children[0]
                arguments = {child.name: await child.resolve(request)}

            else:
                # Multiple children - use gather for parallel execution
                results = await asyncio.gather(
                    *[d.resolve(request) for d in self.children],
                    return_exceptions=True,
                )
                # Check for exceptions and raise the first one found
                for result in results:
                    if isinstance(result, Exception):
                        raise result
                arguments = {
                    c.name: result for c, result in zip(self.children, results)
                }

            # Execute function
            if self.is_async:
                result = await cast(Awaitable[T], self.function(**arguments))
            else:
                result = self.function(**arguments)

            # It's possible that the function returns a context manager instance
            # Combine async/sync context manager detection and avoid duplicate hasattr
            cleanup = None
            if async_cm := is_async_context_manager(result):
                result = await async_cm[0]()
                cleanup = (True, async_cm[1])  # (is_async: True, cleanup)
            elif sync_cm := is_sync_context_manager(result):
                result = sync_cm[0]()
                cleanup = (False, sync_cm[1])  # (is_async: False, cleanup)

            if cleanup is not None:
                # Use try/except to optimize for the common case (cleanups already exists)
                try:
                    request.state.cleanups.append(cleanup)
                except AttributeError:
                    request.state.cleanups = [cleanup]

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
def is_async_callable(obj: AwaitableCallable[T]) -> bool: ...


@overload
def is_async_callable(obj: Any) -> bool: ...


def is_async_callable(obj: Any) -> bool:
    while isinstance(obj, functools.partial):
        obj = obj.func

    return inspect.iscoroutinefunction(obj) or (
        callable(obj)
        and hasattr(obj, "__call__")
        and inspect.iscoroutinefunction(obj.__call__)
    )


def is_sync_context_manager(obj: Any) -> tuple[Callable, Callable] | None:
    """
    Check if an object is a synchronous context manager.

    Returns a tuple of the __enter__ and __exit__ methods if the object has both.
    This includes instances of classes implementing the context manager protocol.
    """
    # Use __slots__-safe presence and callable test, avoid type() or attribute search if possible
    # Try to access the dunder directly for maximum speed (avoids MRO lookup unless necessary)
    try:
        enter = obj.__enter__
        exit_ = obj.__exit__
        if callable(enter) and callable(exit_):
            return (enter, exit_)
        return None
    except AttributeError:
        return None


def is_async_context_manager(obj: Any) -> tuple[Callable, Callable] | None:
    """
    Check if an object is an asynchronous context manager.

    Returns a tuple of the __aenter__ and __aexit__ methods if the object has both.
    This includes instances of classes implementing the async context manager protocol.
    """
    try:
        aenter = obj.__aenter__
        aexit = obj.__aexit__
        if callable(aenter) and callable(aexit):
            return (aenter, aexit)
        return None
    except AttributeError:
        return None


def _inspect_callable(callable_obj: Any) -> tuple[list[inspect.Parameter], Callable]:
    """
    Inspects a callable to return its expected arguments, annotations, and a creation function.

    Returns:
        tuple: (parameters, creation_func)
        - parameters: List of parameters (excluding self/cls for bound methods).
        - creation_func: Function that can be called with the expected arguments.
    """
    if not callable(callable_obj):
        raise InvalidCallableError(
            f"Expected a callable object but got {type(callable_obj).__name__}",
            context={
                "object": str(callable_obj),
                "type": type(callable_obj).__name__,
            },
            help_text="Dependencies must be callable functions, methods, or classes.",
            example="""# Correct dependency patterns:
def get_database() -> Database:
    return Database()

# Then use in route:
def handler(db: Annotated[Database, get_database]): ...""",
        )

    # Get the signature of the callable
    try:
        sig = inspect.signature(callable_obj)
    except ValueError as e:
        # Handle built-in callables with no signature (e.g., len, print)
        raise DependencyBuildError(
            f"Cannot inspect signature of built-in callable: {callable_obj}",
            context={
                "callable": str(callable_obj),
                "type": type(callable_obj).__name__,
            },
            help_text="Built-in functions cannot be used as dependencies.",
        ) from e

    # Extract argument names and annotations
    parameters = [
        param
        for param_name, param in sig.parameters.items()
        if not (
            param_name in ("self", "cls")
            and param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
    ]

    # Create the creation function
    if isinstance(callable_obj, types.MethodType):
        # Bound method - need to call with self as first argument
        def creation_func(*args, **kwargs):
            return callable_obj.__func__(callable_obj.__self__, *args, **kwargs)

    else:
        # Everything else: functions, callable objects, classes, etc.
        # inspect.signature() already handles __call__ correctly
        creation_func = callable_obj

    return parameters, creation_func


def _unwrap_type_alias(p: inspect.Parameter) -> inspect.Parameter:
    """Unwrap TypeAliasType like QueryParam[T] to Annotated[T, ...].

    Returns the unwrapped parameter.
    """

    nested = getattr(p.annotation, "__value__", None)
    if nested is None:
        # not a type alias or generic alias
        return p

    # if it's TypeAliasType, unwrap it
    if isinstance(p.annotation, TypeAliasType):
        return inspect.Parameter(
            p.name,
            p.kind,
            default=p.default,
            annotation=nested,
        )

    # if it's GenericAlias, unwrap it
    if isinstance(p.annotation, types.GenericAlias):
        return_type = get_args(p.annotation)[0]
        _, *meta = get_args(nested)

        return inspect.Parameter(
            p.name,
            p.kind,
            default=p.default,
            annotation=Annotated[return_type, *meta],
        )

    raise ValueError(f"Unsupported annotation type: {type(p.annotation).__name__}")


def _try_apply_parameter_decorator(obj: Any, param: inspect.Parameter) -> Any:
    """
    Try to apply the object as a parameter decorator to the given parameter.

    If the object is a callable that expects exactly one parameter and can be
    called with an inspect.Parameter, it applies the decorator and returns the result.
    Otherwise, returns the original object unchanged.

    Args:
        obj: The potential decorator object
        param: The inspect.Parameter to pass to the decorator

    Returns:
        Either the result of obj(param) if obj is a parameter decorator,
        or the original obj if it's not.
    """
    if not callable(obj):
        return obj

    if is_async_callable(obj):
        return obj

    try:
        sig = inspect.signature(obj)
    except (ValueError, TypeError):
        return obj

    # Filter out self/cls parameters for methods
    parameters = [
        param_obj
        for param_name, param_obj in sig.parameters.items()
        if not (
            param_name in ("self", "cls")
            and param_obj.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
    ]

    # Must have exactly one parameter
    if len(parameters) != 1 or parameters[0].annotation != inspect.Parameter:
        return obj

    # Try applying as parameter decorator
    try:
        result = obj(param)
        # If it returns a callable, it's likely a successful parameter decorator application
        return result if callable(result) else obj
    except Exception:
        # If calling with Parameter fails, return original object
        return obj
