from starlette.exceptions import HTTPException as HTTPException


class StarioException(Exception):
    """Base exception for all errors in the Stario framework.

    This exception provides rich context and helpful guidance to developers when things go wrong.

    Attributes:
        message (str): A clear, human-readable error message.
        code (str): A unique error code for logging and tracking (e.g., 'DEPENDENCY_RESOLUTION_ERROR').
        context (dict): Additional debugging info (e.g., {'parameter': 'user_id', 'annotation': 'int'}).
        help_text (str): Suggested fix or next steps for resolution.
        example (str | None): Code example showing the correct usage.
    """

    code: str = "STARIO_GENERIC_ERROR"

    def __init__(
        self,
        message: str,
        code: str | None = None,
        context: dict | None = None,
        help_text: str | None = None,
        example: str | None = None,
    ):
        self.message = message
        # Prefer instance code if provided, else class code
        self.code = code or self.__class__.code
        self.context = context or {}
        self.example = example
        self.help_text = (
            help_text
            or f"Check the documentation at https://stario.dev/reference/errors#{self.code.lower()} for more details."
        )
        super().__init__(message)

    def __str__(self):
        parts = ["\n╭─ Stario Error ─────────────────────────────────────────────"]
        parts.append(f"│ {self.message}")
        parts.append("│")

        if self.code:
            parts.append(f"│ Error Code: {self.code}")

        if self.context:
            parts.append("│ Context:")
            for key, value in self.context.items():
                parts.append(f"│   • {key}: {value}")

        if self.example:
            parts.append("│")
            parts.append("│ Example (correct usage):")
            for line in self.example.strip().split("\n"):
                parts.append(f"│   {line}")

        if self.help_text:
            parts.append("│")
            parts.append(f"│ 💡 Help: {self.help_text}")

        parts.append("╰────────────────────────────────────────────────────────────")
        return "\n".join(parts)


class HtmlRenderError(StarioException):
    """Error raised when there is an error rendering HTML."""

    code: str = "HTML_RENDER_ERROR"


class HtmlBuildError(StarioException):
    """Error raised when there is an error while building HTML tree."""

    code: str = "HTML_BUILD_ERROR"


class InvalidAttributeValueError(StarioException):
    """Error raised when an HTML attribute has an invalid value type."""

    code: str = "INVALID_ATTRIBUTE_VALUE"


class InvalidStyleValueError(StarioException):
    """Error raised when a CSS style property has an invalid value type."""

    code: str = "INVALID_STYLE_VALUE"


class DependencyResolutionError(StarioException):
    """Error raised when there is an error while resolving dependencies."""

    code: str = "DEPENDENCY_RESOLUTION_ERROR"


class DependencyBuildError(StarioException):
    """Error raised when there is an error while building dependencies."""

    code: str = "DEPENDENCY_BUILD_ERROR"


class InvalidAnnotationError(StarioException):
    """Error raised when a parameter has an invalid or unsupported type annotation."""

    code: str = "INVALID_ANNOTATION"


class InvalidCallableError(StarioException):
    """Error raised when a non-callable object is used where a callable is expected."""

    code: str = "INVALID_CALLABLE"


class InvalidParameterError(StarioException):
    """Error raised when a parameter configuration is invalid."""

    code: str = "INVALID_PARAMETER"


class DatastarConfigError(StarioException):
    """Error raised when Datastar configuration (debounce, throttle, etc.) is invalid."""

    code: str = "DATASTAR_CONFIG_ERROR"


class MiddlewareError(StarioException):
    """Error raised when there's an issue with middleware configuration."""

    code: str = "MIDDLEWARE_ERROR"
