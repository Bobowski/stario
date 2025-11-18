from collections import defaultdict
from datetime import datetime

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.traceback import Traceback

from stario.storyteller.types import LogRecord


def log_level_color(level: str) -> str:
    return {
        "debug": "cyan",
        "info": "green",
        "warning": "yellow",
        "error": "red",
        "critical": "magenta",
        "exception": "red",
    }.get(level, "white")


def http_status_color(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "green"
    if 300 <= status_code < 400:
        return "yellow"
    if 400 <= status_code < 500:
        return "red"
    return "bright_red"


def format_relative_time(relative_ms: float) -> str:
    """Format relative time in milliseconds as a human-readable string.

    Formats:
    - < 1s: +123 (milliseconds)
    - < 1m: +1.234 (seconds with ms)
    - < 1h: +m:ss.mmm (minutes:seconds.milliseconds)
    - >= 1h: +h:mm:ss.mmm (hours:minutes:seconds.milliseconds)
    """
    if relative_ms < 1000:
        return f"+{relative_ms:.0f}"

    if relative_ms < 60_000:
        return f"+{relative_ms / 1000:.3f}"

    total_seconds, milliseconds = divmod(int(relative_ms), 1000)

    if relative_ms < 3_600_000:
        minutes, seconds = divmod(total_seconds, 60)
        return f"+{minutes}:{seconds:02}.{milliseconds:03}"

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"+{hours}:{minutes:02}:{seconds:02}.{milliseconds:03}"


class RichListener:
    """Rich console output with request grouping and live updates."""

    def __init__(self, max_live_requests: int = 100):
        self.max_live_requests = max_live_requests
        self.open_traces = defaultdict[str, list[LogRecord]](list)
        self.live = Live(auto_refresh=False)

    def open(self) -> None:
        """Open the sink."""
        self.live.start()

    def close(self) -> None:
        """Close the sink."""
        if self.live:
            self.live.stop()

    def enqueue(self, record: LogRecord) -> None:
        self.open_traces[record["trace_id"]].append(record)
        self._redraw()

    def _redraw(self) -> None:
        """Create content for live display - sorted in-progress requests."""

        if not self.open_traces:
            self.live.update(Group(), refresh=True)
            return

        alive_traces = []
        to_print = []

        # Make a copy of items to allow safe mutation during iteration
        for trace_id, records in list(self.open_traces.items()):
            group = self._build_trace_group(records)
            if records[-1]["event"] == "request.end":
                # This group is done, print it and remove it from the open traces
                to_print.append(group)
                del self.open_traces[trace_id]

            else:
                # This group is still alive, add it to the alive traces
                alive_traces.append(group)

        self.live.update(Group(*alive_traces), refresh=True)
        for group in to_print:
            self.live.console.print(group)

    def _build_trace_group(self, records: list[LogRecord]) -> Text | Panel:
        default_events = {
            "request.start",
            "response.start",
            "response.end",
            "request.end",
        }

        if all(record["event"] in default_events for record in records):
            # We add a space to the left to align the line with the panel
            return Text("   ").append(self._build_line(records))

        return self._build_panel(records)

    def _build_line(self, records: list[LogRecord]) -> Text:
        """Build a single line for a trace."""
        line = Text()

        trace_id = records[0]["trace_id"]
        request_start = records[0]
        response_start = next(
            (r for r in records if r["event"] == "response.start"), None
        )
        response_end = next((r for r in records if r["event"] == "response.end"), None)
        request_end = records[-1] if records[-1]["event"] == "request.end" else None

        color = "white"
        if response_start is not None:
            color = http_status_color(response_start["status_code"])

        is_dimmed = ""
        if request_end is not None:
            is_dimmed = "dim "

        start_datetime = datetime.fromtimestamp(
            request_start["time_ns"] / 1_000_000_000
        )
        start_iso_str = start_datetime.strftime("%H:%M:%S.%f")[:-3]
        line.append(f"{start_iso_str} [{trace_id[-8:]}] ", style=is_dimmed + color)

        line.append(f"{request_start['method']:>7} ", style=is_dimmed + "white")

        line.append(f"{request_start['path']:<50} ", style=is_dimmed + "white")

        line.append(
            (
                f"[{response_start['status_code']}] "
                if response_start is not None
                else "[___] "
            ),
            style=is_dimmed + color,
        )

        if response_start is not None:
            duration_ns = response_start["time_ns"] - request_start["time_ns"]
            line.append(f"{duration_ns / 1_000_000:.1f}ms", style=is_dimmed + color)

        if response_end is not None and response_start is not None:
            duration_ns = response_end["time_ns"] - response_start["time_ns"]
            line.append(f" / {duration_ns / 1_000_000:.1f}ms", style=is_dimmed + color)

        if request_end is not None:
            if response_end is None:
                duration_ns = request_end["time_ns"] - request_start["time_ns"]
            else:
                duration_ns = request_end["time_ns"] - response_end["time_ns"]
            line.append(f" / {duration_ns / 1_000_000:.1f}ms", style=is_dimmed + color)

        return line

    def _build_panel(self, records: list[LogRecord]) -> Panel:
        """Build a Rich panel for a trace."""

        title = self._build_line(records)

        # trace_id = records[0]["trace_id"]
        # request_start = records[0]
        response_start = next(
            (r for r in records if r["event"] == "response.start"), None
        )
        request_end = records[-1] if records[-1]["event"] == "request.end" else None

        color = "white"
        if response_start is not None:
            color = http_status_color(response_start["status_code"])

        is_dimmed = ""
        if request_end is not None:
            is_dimmed = "dim "

        contents = []
        last_record = records[0]
        for record in records:
            if record["event"] in (
                "request.start",
                "response.start",
                "response.end",
                "request.end",
            ):
                last_record = record
                continue

            contents.append(
                self._build_route_line(record, last_record["time_ns"], is_dimmed)
            )
            last_record = record

        return Panel(
            Group(*contents),
            title=title,
            title_align="left",
            border_style=is_dimmed + color,
            padding=(0, 1),
        )

    def _build_route_line(
        self, record: LogRecord, start_timestamp_ns: int, is_dimmed: str
    ) -> Group:
        line = Text()

        relative_ms = (record["time_ns"] - start_timestamp_ns) / 1_000_000
        time_str = format_relative_time(relative_ms)

        # Rich supports many colors; instead of "white", we can use "grey70" for a softer neutral.
        line.append(f"{time_str:>13} ", style=is_dimmed + "grey70")

        event = record.get("event", "")
        event_color = "white"
        line.append(f"{event:18} ", style=is_dimmed + event_color)

        exceptions = []
        for key, value in record.items():
            if key in ("time_ns", "trace_id", "event"):
                continue

            if isinstance(value, BaseException):

                exc = value
                tb = getattr(exc, "__traceback__", None)

                # Suppress frames from this file and anything under the "stario" package
                trace = Traceback.from_exception(
                    type(exc),
                    exc,
                    tb,
                    suppress=[__file__],
                    width=self.live.console.width,
                    max_frames=2,
                )
                exceptions.append(trace)

                continue

            line.append(f" {key}={value} ", style=is_dimmed + "grey70")

        return Group(*exceptions, line)
