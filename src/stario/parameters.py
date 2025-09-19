from abc import ABC, abstractmethod
from inspect import Parameter as InspectParameter
from typing import Annotated, Any, cast, get_args

from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request


class RequestParameter[T](ABC):
    """
    Abstract base class for extracting parameters from HTTP requests.
    Subclasses implement specific extraction logic for different parts of the request.
    """

    PARAMETER_LOCATION: str = "parameter"

    def __init__(self, name: str | None = None) -> None:
        self.name: str | None = name

    def on_inspect(self, param: InspectParameter) -> "_ParamExtractorSync[T]":
        return _ParamExtractorSync(self, param)

    @staticmethod
    @abstractmethod
    def extract(request: Request, name: str) -> Any:
        """
        Extract the raw value of the parameter from the request.
        This method must be implemented by subclasses.
        """
        raise NotImplementedError


class _ParamExtractorSync[T]:

    def __init__(self, rparam: RequestParameter[T], iparam: InspectParameter) -> None:
        """
        Initialize the synchronous parameter extractor with validation and default handling.
        """

        self.request_param = rparam
        self.name = rparam.name or iparam.name
        self.return_type = get_args(iparam.annotation)[0]
        self.default: T | object = iparam.default
        self.adapter: TypeAdapter[Any] = TypeAdapter(self.return_type)

    @property
    def parameter_location(self) -> str:
        return self.request_param.PARAMETER_LOCATION

    def __call__(self, request: Request) -> T:
        """
        Extract and validate the parameter value from the request.
        Handles defaults and raises appropriate HTTP exceptions on errors.
        """
        try:
            raw = self.request_param.extract(request, self.name)
            return self.adapter.validate_python(raw)
        except KeyError:
            if self.default is not InspectParameter.empty:
                return cast(T, self.default)

            raise HTTPException(
                status_code=400,
                detail=f"Missing required {self.parameter_location} '{self.name}'. Provide it in the request.",
            )
        except ValidationError as e:
            expected = getattr(self.return_type, "__name__", str(self.return_type))
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {self.parameter_location} '{self.name}'. Expected {expected}. {e}",
            )


class QueryParam[T](RequestParameter[T]):
    """
    Extracts a single query parameter from the request.
    """

    PARAMETER_LOCATION = "query parameter"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.query_params[name]


class QueryParams[T](RequestParameter[T]):
    """
    Extracts multiple query parameters with the same name from the request.
    """

    PARAMETER_LOCATION = "query parameter"

    @staticmethod
    def extract(request: Request, name: str) -> list[str]:
        values = request.query_params.getlist(name)
        if not values:
            raise KeyError(name)
        return values


class PathParam[T](RequestParameter[T]):
    """
    Extracts a path parameter from the request.
    """

    PARAMETER_LOCATION = "path parameter"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.path_params[name]


class Header[T](RequestParameter[T]):
    """
    Extracts a single header from the request.
    """

    PARAMETER_LOCATION = "header"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.headers[name]


class Headers[T](RequestParameter[T]):
    """
    Extracts multiple headers with the same name from the request.
    """

    PARAMETER_LOCATION = "header"

    @staticmethod
    def extract(request: Request, name: str) -> list[str]:
        values = request.headers.getlist(name)
        if not values:
            raise KeyError(name)
        return values


class Cookie[T](RequestParameter[T]):
    """
    Extracts a cookie from the request.
    """

    PARAMETER_LOCATION = "cookie"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.cookies[name]


class RawBody:
    """
    Extracts the raw body of the request as bytes or str.
    Note: Request body can only be read once per request. Ensure only one body extractor is used per endpoint to avoid issues.
    """

    def __init__(self, encoding: str = "utf-8") -> None:
        self.encoding = encoding

    def on_inspect(self, param: InspectParameter) -> "_RawBodyExtractor":
        return_type = get_args(param.annotation)[0]
        if return_type not in [bytes, str]:
            raise ValueError(
                f"Invalid return type for RawBody: {return_type}, must be bytes or str"
            )

        return _RawBodyExtractor(self, param)


class _RawBodyExtractor[T]:
    """
    Asynchronous extractor for raw request body.
    """

    def __init__(self, rparam: RawBody, iparam: InspectParameter) -> None:
        self.encoding = rparam.encoding
        self.return_type: type[T] = get_args(iparam.annotation)[0]

    async def __call__(self, request: Request) -> bytes | str:
        """
        Asynchronously extract the raw body.
        Decodes to str if specified, otherwise returns bytes.
        """
        raw = await request.body()
        if self.return_type is bytes:
            return raw

        if self.return_type is str:
            return raw.decode(self.encoding)

        raise ValueError(
            f"Invalid return type for RawBody: {self.return_type}, must be bytes or str"
        )


class JsonBody[T]:
    """
    Extracts and validates the JSON body of the request using Pydantic.
    Note: For performance, ensure body is read only once; this assumes single body param per endpoint.
    """

    def on_inspect(self, param: InspectParameter) -> "_JsonBodyExtractor[T]":
        return _JsonBodyExtractor(param)


class _JsonBodyExtractor[T]:
    """
    Asynchronous extractor for JSON body with validation.
    """

    def __init__(self, iparam: InspectParameter) -> None:
        self.return_type = get_args(iparam.annotation)[0]
        self.default: T | object = iparam.default
        self.adapter: TypeAdapter[Any] = TypeAdapter(self.return_type)

    async def __call__(self, request: Request) -> T:
        """
        Asynchronously extract and validate the JSON body.
        """
        try:
            raw = await request.body()
            return self.adapter.validate_json(raw)

        except ValidationError as e:
            expected = getattr(self.return_type, "__name__", str(self.return_type))
            raise HTTPException(
                status_code=422,
                detail=f"Invalid request body. Expected {expected}. {e}",
            )


class Body[T]:
    """
    Generic body extractor that handles bytes, str, or JSON based on type and content-type.
    Note: For optimal performance and correctness, use only one body extractor per endpoint as body can be read only once.
    """

    def on_inspect(self, param: InspectParameter) -> "_BodyExtractor[T]":
        return _BodyExtractor(param)


class _BodyExtractor[T]:
    """
    Asynchronous generic body extractor.
    """

    def __init__(self, iparam: InspectParameter) -> None:
        self.return_type = get_args(iparam.annotation)[0]
        self.default: T | object = iparam.default
        self.adapter: TypeAdapter[Any] = TypeAdapter(self.return_type)

    async def __call__(self, request: Request) -> T:
        """
        Asynchronously extract the body based on expected type and content-type.
        """

        if self.return_type is bytes:
            return cast(T, await request.body())

        try:
            if self.return_type is str:
                raw = await request.body()
                return cast(T, raw.decode(encoding="utf-8"))

            if request.headers.get("Content-Type") == "application/json":
                raw = await request.body()
                return self.adapter.validate_json(raw)

        except ValidationError as e:
            expected = getattr(self.return_type, "__name__", str(self.return_type))
            raise HTTPException(
                status_code=422,
                detail=f"Invalid request body. Expected {expected}. {e}",
            )

        raise HTTPException(
            status_code=415,
            detail=f"Unsupported media type header. Expected {self.return_type}. Received {request.headers.get('Content-Type')}",
        )


# Shorthand aliases for type annotations - syntax sugar
"""
These type aliases provide shorthand for annotating parameters with extractors.
Example usage: def handler(param: aQueryParam[int]) -> ...
"""

type aCookie[T] = Annotated[T, Cookie[T]()]
type aHeader[T] = Annotated[T, Header[T]()]
type aHeaders[T] = Annotated[list[T], Headers[T]()]
type aPathParam[T] = Annotated[T, PathParam[T]()]
type aQueryParam[T] = Annotated[T, QueryParam[T]()]
type aQueryParams[T] = Annotated[list[T], QueryParams[T]()]
type aBody[T] = Annotated[T, Body[T]()]
type aJsonBody[T] = Annotated[T, JsonBody[T]()]
type aRawBody[T] = Annotated[T, RawBody()]
