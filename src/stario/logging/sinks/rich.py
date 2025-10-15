from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from rich.console import Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich.traceback import Traceback

from stario.logging.types import (
    LogLevel,
    Record,
    RequestLogRecord,
    ResponseLogRecord,
    RouteLogRecord,
)


@dataclass
class RequestGroup:
    """Tracks all logs for a single request by request_id."""

    request_id: str
    start_timestamp: float
    request_log: Optional[RequestLogRecord] = None
    route_logs: List[RouteLogRecord] = field(default_factory=list)
    response_log: Optional[ResponseLogRecord] = None

    @property
    def duration_ms(self) -> float:
        """Calculate total request duration in milliseconds."""
        if not self.response_log:
            return 0.0
        return (self.response_log.timestamp - self.start_timestamp) * 1000


class RichConsoleSink:
    """Rich console output with request grouping and live updates."""

    # Color schemes
    LOG_LEVEL_COLORS = {
        LogLevel.DEBUG: "cyan",
        LogLevel.INFO: "green",
        LogLevel.WARNING: "yellow",
        LogLevel.ERROR: "red",
        LogLevel.CRITICAL: "magenta",
    }

    HTTP_METHOD_COLORS = {
        "GET": "blue",
        "POST": "green",
        "PUT": "yellow",
        "DELETE": "red",
        "PATCH": "magenta",
        "HEAD": "cyan",
        "OPTIONS": "white",
    }

    STATUS_CODE_COLORS = {
        "2xx": "green",
        "3xx": "yellow",
        "4xx": "red",
        "5xx": "bright_red",
    }

    def __init__(self, max_live_requests: int = 100):
        # self.console = Console()
        self.max_live_requests = max_live_requests
        self.request_groups: Dict[str, RequestGroup] = {}  # In-progress requests only
        self.live = Live(auto_refresh=False)
        self._started = False

    def open(self) -> None:
        """Open the sink."""
        self.live.start()
        self._started = True

    def close(self) -> None:
        """Close the sink."""
        if self._started:
            if self.live:
                self.live.stop()
            self._started = False

    async def write(self, records: list[Record]) -> None:
        """Write formatted log records with request grouping."""
        # Process all records
        for record in records:
            self._process_record(record)

        # Update live display only if there are in-progress requests
        # if self.request_groups:
        content = self._create_live_content()
        self.live.update(content, refresh=True)

    def _process_record(self, record: Record) -> None:
        """Process a single record and update request groups."""
        request_id = record.request_id

        # Get or create request group
        if request_id not in self.request_groups:
            self.request_groups[request_id] = RequestGroup(
                request_id=request_id, start_timestamp=record.timestamp
            )

        group = self.request_groups[request_id]
        if isinstance(record, RequestLogRecord):
            group.request_log = record
        elif isinstance(record, RouteLogRecord):
            group.route_logs.append(record)
        elif isinstance(record, ResponseLogRecord):
            group.response_log = record
            # Render completed request statically
            self._render_completed_request(group)
            # Remove from in-progress tracking
            self.request_groups.pop(request_id)

    def _render_completed_request(self, group: RequestGroup) -> None:
        """Render a completed request statically to console."""
        if group.route_logs or (group.response_log and group.response_log.exc_info):
            panel = self._create_request_panel(group)
            self.live.console.print(panel)
        else:
            line = self._create_single_line(group)
            self.live.console.print(line)

    def _create_live_content(self) -> Group:
        """Create content for live display - sorted in-progress requests."""

        if not self.request_groups:
            return Group()

        content_parts = []

        # Sort in-progress by start timestamp and limit to max_live_requests
        in_progress_groups = sorted(
            self.request_groups.values(), key=lambda g: g.start_timestamp
        )

        # Limit to max_live_requests (show oldest ones)
        if len(in_progress_groups) > self.max_live_requests:
            in_progress_groups = in_progress_groups[: self.max_live_requests]

        for group in in_progress_groups:
            if group.route_logs or (group.response_log and group.response_log.exc_info):
                panel = self._create_request_panel(group)
                content_parts.append(panel)
            else:
                line = self._create_single_line(group)
                content_parts.append(line)

        # Return empty Group if no content to avoid rendering empty Text
        return Group(*content_parts)

    def _create_request_panel(self, group: RequestGroup) -> Panel:
        """Create a Rich panel for a request group."""
        # Create panel title
        title = self._create_panel_title(group)

        # Create panel content (route logs only)
        content = self._create_panel_content(group)

        # Determine panel style based on completion status
        if group.response_log is not None:
            border_style = self._get_status_color(group.response_log.status_code)
        else:
            border_style = "dim white"

        return Panel(
            content,
            title=title,
            title_align="left",
            border_style=border_style,
            padding=(0, 1),
        )

    def _create_panel_title(self, group: RequestGroup) -> Text:
        """Create the panel title matching single-line format."""
        title = Text()

        # Timestamp
        dt = datetime.fromtimestamp(group.start_timestamp, tz=timezone.utc)
        time_str = dt.strftime("%H:%M:%S.%f")[:-3]
        title.append(f"{time_str} | ", style="dim")

        # Request ID (first 8 chars)
        title.append(f"{group.request_id[:8]} | ", style="dim")

        if group.request_log:
            # HTTP method (padded to 7 chars)
            method_color = self.HTTP_METHOD_COLORS.get(
                group.request_log.method, "white"
            )
            title.append(
                f"{group.request_log.method:<7} ", style=f"bold {method_color}"
            )

            # Path (padded to 28 characters for alignment)
            title.append(f"{group.request_log.path:<50} ", style="white")

        if group.response_log is not None:
            # Status code and duration
            status_color = self._get_status_color(group.response_log.status_code)
            title.append(f"[{group.response_log.status_code}] ", style=status_color)
            # Format duration: show as seconds if >= 1000ms, otherwise ms
            if group.duration_ms >= 1000:
                title.append(f"{group.duration_ms / 1000:.1f}s", style="dim")
            else:
                title.append(f"{group.duration_ms:.1f}ms", style="dim")
        else:
            # Use dim white to indicate ongoing status
            title.append("ongoing", style="dim white")

        return title

    def _create_panel_content(self, group: RequestGroup) -> Group:
        """Create the panel content with route logs."""
        content_parts: List[RenderableType] = []

        # Separate background and regular logs
        regular_logs = [log for log in group.route_logs if not log.background]
        background_logs = [log for log in group.route_logs if log.background]

        # Add regular logs first
        for i, route_log in enumerate(regular_logs):
            route_line = self._format_route_line(route_log, group.start_timestamp)
            content_parts.append(route_line)

            if route_log.exc_info:
                exc_renderable = self._format_exception(route_log.exc_info)
                if exc_renderable is not None:
                    content_parts.append(Text("\n"))
                    content_parts.append(exc_renderable)

        # Add background task divider and logs if any
        if background_logs:
            content_parts.append(
                Rule(
                    "[dim blue]── Background[/dim blue]",
                    style="dim blue",
                    align="left",
                )
            )

            for i, route_log in enumerate(background_logs):
                route_line = self._format_route_line(route_log, group.start_timestamp)
                content_parts.append(route_line)

                if route_log.exc_info:
                    exc_renderable = self._format_exception(route_log.exc_info)
                    if exc_renderable is not None:
                        content_parts.append(Text("\n"))
                        content_parts.append(exc_renderable)

        # Add response-level exception if present
        if group.response_log and group.response_log.exc_info:
            exc_renderable = self._format_exception(group.response_log.exc_info)
            if exc_renderable is not None:
                # if content_parts:
                #     content_parts.append(Text("\n"))
                content_parts.append(exc_renderable)

        return Group(*content_parts)

    def _create_single_line(self, group: RequestGroup) -> Text:
        """Create a single-line format for requests without route logs."""
        line = Text()

        # Leading spaces for alignment with panel (3 spaces to match panel indentation)
        line.append("   ")

        # Determine base styling - use dim + status color for completed requests, dim for ongoing
        if group.response_log is not None:
            base_style = f"dim {self._get_status_color(group.response_log.status_code)}"
        else:
            base_style = "dim white"

        # Timestamp (dimmed with status color for completed requests)
        dt = datetime.fromtimestamp(group.start_timestamp, tz=timezone.utc)
        time_str = dt.strftime("%H:%M:%S.%f")[:-3]
        line.append(f"{time_str} | ", style=base_style)

        # Request ID (dimmed with status color for completed requests)
        line.append(f"{group.request_id[:8]} | ", style=base_style)

        if group.request_log:
            # HTTP method (padded to 7 chars)
            method_color = self.HTTP_METHOD_COLORS.get(
                group.request_log.method, "white"
            )
            line.append(f"{group.request_log.method:<7} ", style=f"bold {method_color}")

            # Path (padded to 28 characters for alignment)
            line.append(f"{group.request_log.path:<50} ", style="white")

        if group.response_log is not None:
            # Status code and duration
            status_color = self._get_status_color(group.response_log.status_code)
            line.append(f"[{group.response_log.status_code}] ", style=status_color)
            # Format duration: show as seconds if >= 1000ms, otherwise ms (dimmed with status color)
            if group.duration_ms >= 1000:
                line.append(f"{group.duration_ms / 1000:.1f}s", style=base_style)
            else:
                line.append(f"{group.duration_ms:.1f}ms", style=base_style)
        else:
            # Indicate ongoing status
            line.append("ongoing", style=base_style)

        return line

    def _format_route_line(
        self, record: RouteLogRecord, start_timestamp: float
    ) -> Text:
        """Format a route log line with relative timestamp."""
        line = Text()

        # Relative timestamp (adaptive format, right-aligned in 12 chars)
        relative_ms = (record.timestamp - start_timestamp) * 1000
        if relative_ms >= 1000:
            time_str = f"+{relative_ms / 1000:.1f}s"
        else:
            time_str = f"+{relative_ms:.0f}ms"
        line.append(f"{time_str:>13} ", style="dim")

        # Log level with buffered indicator (padded to 8 chars)
        level_color = self.LOG_LEVEL_COLORS.get(record.level, "white")
        level_text = record.level.name
        if record.buffered:
            level_text = f"*{level_text}"  # Asterisk indicates buffered logs
        line.append(f"| {level_text:<8} | ", style=level_color)

        # Message
        line.append(f"{record.message:18}", style="white")

        # Context parameters (excluding special keys)
        if record.context:
            ctx_items = [
                f"{key}={value}"
                for key, value in record.context.items()
                if key not in ("route", "handler")
            ]
            if ctx_items:
                line.append(f" [{', '.join(ctx_items)}]", style="dim")

        return line

    def _format_exception(self, exc_info: tuple) -> Optional[Traceback]:
        """Format exception traceback using Rich's pretty printing."""
        if not exc_info:
            return None

        return Traceback.from_exception(
            exc_info[0],
            exc_info[1],
            exc_info[2],
            # show_locals=True,
            # max_frames=2,
            suppress=[__file__],  # Suppress frames from this file
            width=self.live.console.width,
        )

    def _get_status_color(self, status_code: int) -> str:
        """Get color for HTTP status code."""
        if 200 <= status_code < 300:
            return self.STATUS_CODE_COLORS["2xx"]
        elif 300 <= status_code < 400:
            return self.STATUS_CODE_COLORS["3xx"]
        elif 400 <= status_code < 500:
            return self.STATUS_CODE_COLORS["4xx"]
        else:
            return self.STATUS_CODE_COLORS["5xx"]
