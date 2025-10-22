from abc import ABC, abstractmethod
from inspect import Parameter as InspectParameter
from typing import Annotated, Any, cast, get_args

from pydantic import TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import Request as Request

from stario.exceptions import InvalidParameterError


class RequestParameter[T](ABC):
    """
    Abstract base class for extracting parameters from HTTP requests.
    Subclasses implement specific extraction logic for different parts of the request.
    """

    PARAMETER_LOCATION: str = "parameter"

    def __init__(self, name: str | None = None) -> None:
        self.name: str | None = name

    def __call__(self, param: InspectParameter) -> "_ParamExtractorSync[T]":
        return _ParamExtractorSync.get_or_create(self, param)

    @staticmethod
    @abstractmethod
    def extract(request: Request, name: str) -> Any:
        """
        Extract the raw value of the parameter from the request.
        This method must be implemented by subclasses.
        """
        raise NotImplementedError


class _ParamExtractorSync[T]:
    """
    Synchronous parameter extractor with singleton caching.

    Extractors with the same name, return type, and default value will be cached
    and reused to ensure singleton behavior.
    """

    # Class-level cache for singleton behavior
    # Uniqueness key: type, name, return_type, default
    # This is for request scope reusability for dependecy injection mechanism.
    _cache: dict[tuple, "_ParamExtractorSync[Any]"] = {}

    def __init__(self, rparam: RequestParameter[T], iparam: InspectParameter) -> None:
        self.request_param = rparam
        self.name = rparam.name or iparam.name
        self.return_type = get_args(iparam.annotation)[0]
        self.default: T | type[InspectParameter.empty] = iparam.default
        self.adapter = TypeAdapter[T](self.return_type)

    @classmethod
    def get_or_create(
        cls, rparam: RequestParameter[T], iparam: InspectParameter
    ) -> "_ParamExtractorSync[T]":
        """
        Get an existing extractor from cache or create a new one.

        This ensures singleton behavior for extractors with the same characteristics.
        Creates a cache key based on:
        - The request parameter class type
        - The parameter name
        - The return type
        - The default value (using id() for object identity)
        """
        name = rparam.name or iparam.name
        return_type: type = get_args(iparam.annotation)[0]
        default: T | type[InspectParameter.empty] = iparam.default

        # Uniqueness key:
        cache_key = (type(rparam), name, return_type, default)

        if cache_key not in cls._cache:
            cls._cache[cache_key] = cls(rparam, iparam)

        return cls._cache[cache_key]

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
            if self.default is not InspectParameter.empty and not isinstance(
                self.default, type
            ):
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


class ParseQueryParam[T](RequestParameter[T]):
    """
    Extracts a single query parameter from the request.

    Validates the parameter against the specified type using Pydantic.
    Raises HTTP 400 if the parameter is missing and no default is provided.
    Raises HTTP 422 if the parameter cannot be validated as the expected type.

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Query
        >>> from stario.requests import ParseQueryParam
        >>>
        >>> # Basic usage with int type
        >>> async def get_page(page: Annotated[int, ParseQueryParam()]):
        ...     return f"Page {page}"
        >>>
        >>> app = Stario(Query("/items", get_page))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/items?page=5")
        >>> assert resp.status_code == 200
        >>> assert resp.text == "Page 5"
        >>>
        >>> # With default value
        >>> async def get_items(limit: Annotated[int, ParseQueryParam()] = 10):
        ...     return f"Limit: {limit}"
        >>>
        >>> app = Stario(Query("/items", get_items))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/items")
        >>> assert resp.text == "Limit: 10"
        >>>
        >>> # Custom parameter name
        >>> async def search(q: Annotated[str, ParseQueryParam(name="query")]):
        ...     return f"Searching for: {q}"
        >>>
        >>> app = Stario(Query("/search", search))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/search?query=python")
        >>> assert resp.text == "Searching for: python"
    """

    PARAMETER_LOCATION = "query parameter"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.query_params[name]


class ParseQueryParams[T](RequestParameter[T]):
    """
    Extracts multiple query parameters with the same name from the request.

    Useful for handling query strings like "?tag=python&tag=web&tag=api".
    Returns a list of values validated against the specified type.
    Raises HTTP 400 if no parameters with the given name are found.

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Query
        >>> from stario.requests import ParseQueryParams
        >>>
        >>> # Extract multiple tags
        >>> async def filter_by_tags(tags: Annotated[list[str], ParseQueryParams()]):
        ...     return f"Tags: {', '.join(tags)}"
        >>>
        >>> app = Stario(Query("/items", filter_by_tags))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/items?tags=python&tags=web&tags=api")
        >>> assert resp.status_code == 200
        >>> assert resp.text == "Tags: python, web, api"
        >>>
        >>> # With type validation (list of ints)
        >>> async def get_ids(id: Annotated[list[int], ParseQueryParams()]):
        ...     return f"IDs: {id}"
        >>>
        >>> app = Stario(Query("/items", get_ids))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/items?id=1&id=2&id=3")
        >>> assert resp.text == "IDs: [1, 2, 3]"
    """

    PARAMETER_LOCATION = "query parameter"

    @staticmethod
    def extract(request: Request, name: str) -> list[str]:
        values = request.query_params.getlist(name)
        return values


class ParsePathParam[T](RequestParameter[T]):
    """
    Extracts a path parameter from the request.

    Path parameters are defined in the route pattern using curly braces, like "/users/{user_id}".
    Validates the parameter against the specified type using Pydantic.
    Raises HTTP 422 if the parameter cannot be validated as the expected type.

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Query
        >>> from stario.requests import ParsePathParam
        >>>
        >>> # Extract user ID from path
        >>> async def get_user(user_id: Annotated[int, ParsePathParam()]):
        ...     return f"User ID: {user_id}"
        >>>
        >>> app = Stario(Query("/users/{user_id}", get_user))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/users/42")
        >>> assert resp.status_code == 200
        >>> assert resp.text == "User ID: 42"
        >>>
        >>> # Multiple path parameters
        >>> async def get_item(category: Annotated[str, ParsePathParam()],
        ...                    item_id: Annotated[int, ParsePathParam()]):
        ...     return f"Category: {category}, Item: {item_id}"
        >>>
        >>> app = Stario(Query("/shop/{category}/{item_id}", get_item))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/shop/electronics/123")
        >>> assert resp.text == "Category: electronics, Item: 123"
    """

    PARAMETER_LOCATION = "path parameter"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.path_params[name]


class ParseHeader[T](RequestParameter[T]):
    """
    Extracts a single header from the request.

    Header names are case-insensitive. Validates the header value against the specified type.
    Raises HTTP 400 if the header is missing and no default is provided.
    Raises HTTP 422 if the header cannot be validated as the expected type.

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Query
        >>> from stario.requests import ParseHeader
        >>>
        >>> # Extract Authorization header
        >>> async def auth_endpoint(auth: Annotated[str, ParseHeader(name="Authorization")]):
        ...     return f"Token: {auth}"
        >>>
        >>> app = Stario(Query("/protected", auth_endpoint))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/protected", headers={"Authorization": "Bearer abc123"})
        >>> assert resp.status_code == 200
        >>> assert resp.text == "Token: Bearer abc123"
        >>>
        >>> # Extract custom header with validation
        >>> async def rate_limit(limit: Annotated[int, ParseHeader(name="X-Rate-Limit")] = 100):
        ...     return f"Rate limit: {limit}"
        >>>
        >>> app = Stario(Query("/api", rate_limit))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/api", headers={"X-Rate-Limit": "50"})
        >>> assert resp.text == "Rate limit: 50"
    """

    PARAMETER_LOCATION = "header"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.headers[name]


class ParseHeaders[T](RequestParameter[T]):
    """
    Extracts multiple headers with the same name from the request.

    Some HTTP headers can appear multiple times in a request (e.g., "Accept").
    Returns a list of values validated against the specified type.
    Raises HTTP 400 if no headers with the given name are found.

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Query
        >>> from stario.requests import ParseHeaders
        >>>
        >>> # Extract multiple Accept headers
        >>> async def content_negotiation(accept: Annotated[list[str], ParseHeaders(name="Accept")]):
        ...     return f"Accept types: {', '.join(accept)}"
        >>>
        >>> app = Stario(Query("/content", content_negotiation))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/content", headers=[
        ...         ("Accept", "application/json"),
        ...         ("Accept", "text/html")
        ...     ])
        >>> assert resp.status_code == 200
        >>> assert "application/json" in resp.text
    """

    PARAMETER_LOCATION = "header"

    @staticmethod
    def extract(request: Request, name: str) -> list[str]:
        values = request.headers.getlist(name)
        return values


class ParseCookie[T](RequestParameter[T]):
    """
    Extracts a cookie from the request.

    Validates the cookie value against the specified type using Pydantic.
    Raises HTTP 400 if the cookie is missing and no default is provided.
    Raises HTTP 422 if the cookie cannot be validated as the expected type.

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Query
        >>> from stario.requests import ParseCookie
        >>>
        >>> # Extract session ID from cookie
        >>> async def check_session(session_id: Annotated[str, ParseCookie(name="session")]):
        ...     return f"Session: {session_id}"
        >>>
        >>> app = Stario(Query("/dashboard", check_session))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/dashboard", cookies={"session": "abc123xyz"})
        >>> assert resp.status_code == 200
        >>> assert resp.text == "Session: abc123xyz"
        >>>
        >>> # With default value
        >>> async def get_theme(theme: Annotated[str, ParseCookie()] = "light"):
        ...     return f"Theme: {theme}"
        >>>
        >>> app = Stario(Query("/app", get_theme))
        >>> with TestClient(app) as client:
        ...     resp = client.get("/app")
        >>> assert resp.text == "Theme: light"
    """

    PARAMETER_LOCATION = "cookie"

    @staticmethod
    def extract(request: Request, name: str) -> str:
        return request.cookies[name]


class ParseRawBody:
    """
    Extracts the raw body of the request as bytes or str.

    Note: Request body can only be read once per request. Ensure only one body extractor is used per endpoint to avoid issues.

    The return type must be either `bytes` or `str`. If `str` is specified, the body will be decoded using the
    provided encoding (default: "utf-8").

    Examples:
        >>> from typing import Annotated
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Command
        >>> from stario.requests import ParseRawBody
        >>>
        >>> # Extract raw body as bytes
        >>> async def upload_binary(data: Annotated[bytes, ParseRawBody()]):
        ...     return f"Received {len(data)} bytes"
        >>>
        >>> app = Stario(Command("/upload", upload_binary))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/upload", content=b"binary data here")
        >>> assert resp.status_code == 200
        >>> assert resp.text == "Received 16 bytes"
        >>>
        >>> # Extract raw body as string
        >>> async def webhook(payload: Annotated[str, ParseRawBody()]):
        ...     return f"Webhook: {payload}"
        >>>
        >>> app = Stario(Command("/webhook", webhook))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/webhook", content=b"event data")
        >>> assert resp.text == "Webhook: event data"
        >>>
        >>> # Custom encoding
        >>> async def upload_text(text: Annotated[str, ParseRawBody(encoding="latin-1")]):
        ...     return f"Text: {text}"
        >>>
        >>> app = Stario(Command("/text", upload_text))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/text", content="café".encode("latin-1"))
        >>> assert "café" in resp.text
    """

    def __init__(self, encoding: str = "utf-8") -> None:
        self.encoding = encoding

    def __call__(self, param: InspectParameter) -> "_RawBodyExtractor":
        return_type = get_args(param.annotation)[0]
        if return_type not in [bytes, str]:
            raise InvalidParameterError(
                f"Invalid return type for RawBody parameter '{param.name}': {return_type.__name__}",
                context={
                    "parameter": param.name,
                    "requested_type": return_type.__name__,
                    "supported_types": ["bytes", "str"],
                },
                help_text="RawBody only supports bytes or str return types.",
                example="""from typing import Annotated
from stario import Command
from stario.requests import RawBody

# Correct usage:
def handler(body: Annotated[bytes, RawBody()]): ...  # ✅ bytes
def handler(body: Annotated[str, RawBody()]): ...   # ✅ str

# Incorrect:
# def handler(body: Annotated[dict, RawBody()]): ... # ❌ dict not supported
# def handler(body: Annotated[int, RawBody()]): ...  # ❌ int not supported""",
            )

        return _RawBodyExtractor(self, param)


class _RawBodyExtractor[T]:
    """
    Asynchronous extractor for raw request body.
    """

    def __init__(self, rparam: ParseRawBody, iparam: InspectParameter) -> None:
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

        # This should never happen if __call__ validation works correctly
        raise InvalidParameterError(
            f"Invalid return type for RawBody: {self.return_type.__name__}",
            context={
                "return_type": self.return_type.__name__,
                "supported_types": ["bytes", "str"],
            },
            help_text="This is likely a framework bug. Please report it.",
        )


class ParseJsonBody[T]:
    """
    Extracts and validates the JSON body of the request using Pydantic.

    Note: For performance, ensure body is read only once; this assumes single body param per endpoint.

    The body is validated against the specified Pydantic model or type.
    Raises HTTP 422 if the body is invalid JSON or doesn't match the expected schema.

    Examples:
        >>> from typing import Annotated
        >>> from pydantic import BaseModel
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Command
        >>> from stario.requests import ParseJsonBody
        >>>
        >>> # Define a Pydantic model
        >>> class User(BaseModel):
        ...     name: str
        ...     age: int
        >>>
        >>> # Extract and validate JSON body
        >>> async def create_user(user: Annotated[User, ParseJsonBody()]):
        ...     return f"Created user: {user.name}, age {user.age}"
        >>>
        >>> app = Stario(Command("/users", create_user))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/users", json={"name": "Alice", "age": 30})
        >>> assert resp.status_code == 200
        >>> assert resp.text == "Created user: Alice, age 30"
        >>>
        >>> # Simple dict validation
        >>> async def process_data(data: Annotated[dict[str, int], ParseJsonBody()]):
        ...     total = sum(data.values())
        ...     return f"Total: {total}"
        >>>
        >>> app = Stario(Command("/process", process_data))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/process", json={"a": 10, "b": 20, "c": 30})
        >>> assert resp.text == "Total: 60"
    """

    def __call__(self, param: InspectParameter) -> "_JsonBodyExtractor[T]":
        return _JsonBodyExtractor(param)


class _JsonBodyExtractor[T]:
    """
    Asynchronous extractor for JSON body with validation.
    """

    def __init__(self, iparam: InspectParameter) -> None:
        self.return_type = get_args(iparam.annotation)[0]
        self.default: T | object = iparam.default
        self.adapter: TypeAdapter[T] = TypeAdapter(self.return_type)

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


class ParseBody[T]:
    """
    Generic body extractor that handles bytes, str, or JSON based on type and content-type.

    Note: For optimal performance and correctness, use only one body extractor per endpoint as body can be read only once.

    - If the type is `bytes`, returns raw body as bytes
    - If the type is `str`, returns body decoded as UTF-8 string
    - For other types, checks Content-Type header and validates as JSON if "application/json"
    - Raises HTTP 415 (Unsupported Media Type) if Content-Type doesn't match expectations
    - Raises HTTP 422 if validation fails

    Examples:
        >>> from typing import Annotated
        >>> from pydantic import BaseModel
        >>> from starlette.testclient import TestClient
        >>> from stario import Stario
        >>> from stario.routes import Command
        >>> from stario.requests import ParseBody
        >>>
        >>> # Handle bytes
        >>> async def process_bytes(data: Annotated[bytes, ParseBody()]):
        ...     return f"Received {len(data)} bytes"
        >>>
        >>> app = Stario(Command("/bytes", process_bytes))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/bytes", content=b"hello")
        >>> assert resp.text == "Received 5 bytes"
        >>>
        >>> # Handle string
        >>> async def process_text(text: Annotated[str, ParseBody()]):
        ...     return f"Text: {text}"
        >>>
        >>> app = Stario(Command("/text", process_text))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/text", content=b"hello world")
        >>> assert resp.text == "Text: hello world"
        >>>
        >>> # Handle JSON with Pydantic model
        >>> class Item(BaseModel):
        ...     name: str
        ...     price: float
        >>>
        >>> async def create_item(item: Annotated[Item, ParseBody()]):
        ...     return f"Item: {item.name} - ${item.price}"
        >>>
        >>> app = Stario(Command("/items", create_item))
        >>> with TestClient(app) as client:
        ...     resp = client.post("/items", json={"name": "Widget", "price": 9.99})
        >>> assert resp.text == "Item: Widget - $9.99"
    """

    def __call__(self, param: InspectParameter) -> "_BodyExtractor[T]":
        return _BodyExtractor(param)


class _BodyExtractor[T]:
    """
    Asynchronous generic body extractor.
    """

    def __init__(self, iparam: InspectParameter) -> None:
        self.return_type = get_args(iparam.annotation)[0]
        self.default: T | object = iparam.default
        self.adapter: TypeAdapter[T] = TypeAdapter(self.return_type)

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


# Syntax sugar - Type aliases for cleaner parameter annotations

type QueryParam[T] = Annotated[T, ParseQueryParam()]
"""
Type alias for extracting a single query parameter.

Equivalent to `Annotated[T, ParseQueryParam()]`. Provides a cleaner syntax for route handlers.

Examples:
    >>> from stario.requests import QueryParam
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Query
    >>>
    >>> async def search(q: QueryParam[str], page: QueryParam[int] = 1):
    ...     return f"Search: {q}, Page: {page}"
    >>>
    >>> app = Stario(Query("/search", search))
    >>> with TestClient(app) as client:
    ...     resp = client.get("/search?q=python&page=2")
    >>> assert resp.text == "Search: python, Page: 2"
"""

type QueryParams[T] = Annotated[T, ParseQueryParams()]
"""
Type alias for extracting multiple query parameters with the same name.

Equivalent to `Annotated[T, ParseQueryParams()]`. Use for query strings like "?tag=a&tag=b".

Examples:
    >>> from stario.requests import QueryParams
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Query
    >>>
    >>> async def filter_items(tags: QueryParams[list[str]]):
    ...     return f"Tags: {', '.join(tags)}"
    >>>
    >>> app = Stario(Query("/items", filter_items))
    >>> with TestClient(app) as client:
    ...     resp = client.get("/items?tags=python&tags=web")
    >>> assert resp.text == "Tags: python, web"
"""

type PathParam[T] = Annotated[T, ParsePathParam()]
"""
Type alias for extracting a path parameter.

Equivalent to `Annotated[T, ParsePathParam()]`. Path parameters are defined in route patterns like "/users/{id}".

Examples:
    >>> from stario.requests import PathParam
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Query
    >>>
    >>> async def get_user(user_id: PathParam[int]):
    ...     return f"User ID: {user_id}"
    >>>
    >>> app = Stario(Query("/users/{user_id}", get_user))
    >>> with TestClient(app) as client:
    ...     resp = client.get("/users/123")
    >>> assert resp.text == "User ID: 123"
"""

type Header[T] = Annotated[T, ParseHeader()]
"""
Type alias for extracting a single header.

Equivalent to `Annotated[T, ParseHeader()]`. Header names are case-insensitive.

Examples:
    >>> from stario.requests import Header
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Query
    >>>
    >>> async def check_auth(authorization: Header[str]):
    ...     return f"Auth: {authorization}"
    >>>
    >>> app = Stario(Query("/api", check_auth))
    >>> with TestClient(app) as client:
    ...     resp = client.get("/api", headers={"Authorization": "Bearer token123"})
    >>> assert resp.text == "Auth: Bearer token123"
"""

type Headers[T] = Annotated[T, ParseHeaders()]
"""
Type alias for extracting multiple headers with the same name.

Equivalent to `Annotated[T, ParseHeaders()]`. Some HTTP headers can appear multiple times.

Examples:
    >>> from stario.requests import Headers
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Query
    >>>
    >>> async def get_accepts(accept: Headers[list[str]]):
    ...     return f"Accepts: {len(accept)} types"
    >>>
    >>> app = Stario(Query("/content", get_accepts))
    >>> with TestClient(app) as client:
    ...     resp = client.get("/content", headers=[("Accept", "text/html"), ("Accept", "application/json")])
    >>> assert "2 types" in resp.text
"""

type Cookie[T] = Annotated[T, ParseCookie()]
"""
Type alias for extracting a cookie.

Equivalent to `Annotated[T, ParseCookie()]`. Extracts and validates cookie values.

Examples:
    >>> from stario.requests import Cookie
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Query
    >>>
    >>> async def get_session(session_id: Cookie[str]):
    ...     return f"Session: {session_id}"
    >>>
    >>> app = Stario(Query("/app", get_session))
    >>> with TestClient(app) as client:
    ...     resp = client.get("/app", cookies={"session_id": "xyz789"})
    >>> assert resp.text == "Session: xyz789"
"""

type Body[T] = Annotated[T, ParseBody()]
"""
Type alias for extracting request body (auto-detects bytes, str, or JSON).

Equivalent to `Annotated[T, ParseBody()]`. Automatically handles different body types based on the return type annotation.

Examples:
    >>> from pydantic import BaseModel
    >>> from stario.requests import Body
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Command
    >>>
    >>> class Product(BaseModel):
    ...     name: str
    ...     price: float
    >>>
    >>> async def create_product(product: Body[Product]):
    ...     return f"Product: {product.name}"
    >>>
    >>> app = Stario(Command("/products", create_product))
    >>> with TestClient(app) as client:
    ...     resp = client.post("/products", json={"name": "Laptop", "price": 999.99})
    >>> assert resp.text == "Product: Laptop"
"""

type JsonBody[T] = Annotated[T, ParseJsonBody()]
"""
Type alias for extracting and validating JSON request body.

Equivalent to `Annotated[T, ParseJsonBody()]`. Always expects JSON content.

Examples:
    >>> from pydantic import BaseModel
    >>> from stario.requests import JsonBody
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Command
    >>>
    >>> class LoginData(BaseModel):
    ...     username: str
    ...     password: str
    >>>
    >>> async def login(data: JsonBody[LoginData]):
    ...     return f"Login: {data.username}"
    >>>
    >>> app = Stario(Command("/login", login))
    >>> with TestClient(app) as client:
    ...     resp = client.post("/login", json={"username": "alice", "password": "secret"})
    >>> assert resp.text == "Login: alice"
"""

type RawBody[T] = Annotated[T, ParseRawBody()]
"""
Type alias for extracting raw request body as bytes or str.

Equivalent to `Annotated[T, ParseRawBody()]`. Use when you need the raw body without JSON parsing.

Examples:
    >>> from stario.requests import RawBody
    >>> from starlette.testclient import TestClient
    >>> from stario import Stario, Command
    >>>
    >>> async def upload_file(data: RawBody[bytes]):
    ...     return f"Uploaded {len(data)} bytes"
    >>>
    >>> app = Stario(Command("/upload", upload_file))
    >>> with TestClient(app) as client:
    ...     resp = client.post("/upload", content=b"file content")
    >>> assert resp.text == "Uploaded 12 bytes"
"""


__all__ = [
    # Commonly used
    "QueryParam",
    "QueryParams",
    "PathParam",
    "Header",
    "Headers",
    "Cookie",
    "Body",
    "JsonBody",
    "RawBody",
    "Request",
    # Explicit parsers (prob less commonly used)
    "ParseQueryParam",
    "ParseQueryParams",
    "ParsePathParam",
    "ParseHeader",
    "ParseHeaders",
    "ParseCookie",
    "ParseBody",
    "ParseJsonBody",
    "ParseRawBody",
]
