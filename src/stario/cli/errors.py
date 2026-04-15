"""CLI errors (non-zero exit, message on stderr)."""


class CliError(Exception):
    """User-facing CLI failure (contrast with unexpected tracebacks)."""
