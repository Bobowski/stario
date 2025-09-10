from inspect import Parameter
from types import UnionType
from typing import Any, Self, cast, get_args, get_origin

from pydantic import TypeAdapter
from starlette.requests import Request


class RequestParam[T]:

    def __init__(self, name: str | None = None) -> None:
        """ """

        self.name: str | None = name
        self.return_type: type[T] | None = None
        self.default: T | object = Parameter.empty
        self.adapter: TypeAdapter[Any] | None = None

    @classmethod
    def full_build(
        cls,
        name: str | None = None,
        return_type: type[T] | None = None,
        default: T | object = Parameter.empty,
    ) -> Self:
        p = cls(name)
        p.return_type = return_type
        p.default = default
        p.adapter = TypeAdapter(return_type) if return_type else None
        return p

    def digest_parameter(self, param: Parameter) -> "RequestParam[T]":
        """
        We assume this will be called after the parameter has been digested.
        So we can use the return type to get the adapter.
        """

        target_type, *_ = get_args(param.annotation)

        self.name = self.name or param.name
        self.default = self.default or param.default
        self.return_type = self.return_type or target_type
        self.adapter = TypeAdapter(self.return_type) if self.return_type else None

        return self

    def extract(self, request: Request) -> Any:
        raise NotImplementedError

    def __call__(self, request: Request) -> T:
        try:
            raw = self.extract(request)
            assert self.adapter is not None
            return self.adapter.validate_python(raw)  # type: ignore[no-any-return]

        except KeyError:
            if self.default is not Parameter.empty:
                return cast(T, self.default)
            raise


class QuerySingle[T](RequestParam[T]):

    def extract(self, request: Request) -> str:
        assert self.name is not None
        return request.query_params[self.name]


class QueryList[T](RequestParam[T]):

    def extract(self, request: Request) -> list[str]:
        assert self.name is not None
        return request.query_params.getlist(self.name)


class QueryParam[T](RequestParam[T]):

    def digest_parameter(self, param: Parameter) -> "RequestParam[T]":

        p = super().digest_parameter(param)

        # we need to decide if user wants a single query param or a list of query params

        if p.return_type is not None and get_origin(p.return_type) is UnionType:

            for union_type in get_args(p.return_type):
                if get_origin(union_type) is list:
                    return QueryList[T].full_build(p.name, union_type, p.default)

        else:
            if p.return_type is not None and get_origin(p.return_type) is list:
                return QueryList[T].full_build(p.name, p.return_type, p.default)

        return QuerySingle[T].full_build(p.name, p.return_type, p.default)


class Body[T]:
    pass


class PathParam[T](RequestParam[T]):

    def extract(self, request: Request) -> str:
        assert self.name is not None
        return request.path_params[self.name]


class Header[T](RequestParam[T]):

    def extract(self, request: Request) -> str:
        assert self.name is not None
        return request.headers[self.name]


class Cookie[T](RequestParam[T]):

    def extract(self, request: Request) -> str:
        assert self.name is not None
        return request.cookies[self.name]
