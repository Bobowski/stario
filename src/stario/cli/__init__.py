"""
Command-line scaffolding (``init``) and dev servers (``serve`` / ``watch``) built on the same ``Server`` as library use.

Apps are referenced as ``module:callable`` so a checked-out tree runs without installing a console script first.

Telemetry is configured with ``--tracer`` (``auto``, ``tty``, ``json``, ``sqlite``, or ``module:callable``).
"""

import argparse
import itertools
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NamedTuple

from stario.http.writer import CompressionConfig

from . import term
from .errors import CliError
from .runtime import CliLoop, serve_once, watch_app

# Must match ``stario.http.request``; avoid importing ``request`` here (circular import risk).
_CLI_DEFAULT_MAX_HEADER_BYTES = 64 * 1024
_CLI_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024

_ROOT_EPILOG = """\
Examples:
  stario init
  stario serve main:bootstrap
  stario watch main:bootstrap
  stario watch main:bootstrap --tracer json
  stario watch main:bootstrap --watch app/
  stario watch main:bootstrap --watch '**/*.py' --watch-ignore '*.sqlite3'
"""

TEMPLATES_DIR = Path(__file__).parent / "templates"
GITHUB_API = "https://api.github.com/repos/Bobowski/stario/contents"
MANIFEST_URL = (
    "https://raw.githubusercontent.com/Bobowski/stario/main/examples/manifest.json"
)


class Template(NamedTuple):
    name: str
    description: str
    long_description: str = ""
    bundled: bool = True
    recommended: bool = False


BUNDLED_TEMPLATES: list[Template] = [
    Template(
        name="tiles",
        description="Collaborative painting board",
        long_description=(
            "The best way to start! A multiplayer canvas where users paint colored\n"
            "    tiles together in real-time. Experience Datastar's reactive signals,\n"
            "    SSE streaming, and see how Stario makes multiplayer trivial."
        ),
        bundled=True,
        recommended=True,
    ),
    Template(
        name="hello-world",
        description="Minimal counter app",
        long_description=(
            "A clean starting point with just the essentials. Simple counter\n"
            "    demonstrating Datastar signals and server interaction."
        ),
        bundled=True,
    ),
]


def _resolve_compression_config(
    *,
    min_size: int,
    zstd_level: int,
    zstd_window_log: int | None,
    brotli_level: int,
    brotli_window_log: int | None,
    gzip_level: int,
    gzip_window_bits: int | None,
) -> CompressionConfig:
    if min_size < 0:
        raise CliError("--compress-min-size must be 0 or greater.")
    if zstd_level >= 0 and not 1 <= zstd_level <= 22:
        raise CliError(
            "--compress-zstd-level must be negative or between 1 and 22."
        )
    if zstd_window_log is not None and not 10 <= zstd_window_log <= 31:
        raise CliError(
            "--compress-zstd-window-log must be between 10 and 31."
        )
    if brotli_level >= 0 and not 0 <= brotli_level <= 11:
        raise CliError(
            "--compress-brotli-level must be negative or between 0 and 11."
        )
    if brotli_window_log is not None and not 10 <= brotli_window_log <= 24:
        raise CliError(
            "--compress-brotli-window-log must be between 10 and 24."
        )
    if gzip_level >= 0 and not 1 <= gzip_level <= 9:
        raise CliError(
            "--compress-gzip-level must be negative or between 1 and 9."
        )
    if gzip_window_bits is not None and not 9 <= gzip_window_bits <= 15:
        raise CliError(
            "--compress-gzip-window-bits must be between 9 and 15."
        )
    return CompressionConfig(
        min_size=min_size,
        zstd_level=zstd_level,
        zstd_window_log=zstd_window_log,
        brotli_level=brotli_level,
        brotli_window_log=brotli_window_log,
        gzip_level=gzip_level,
        gzip_window_bits=gzip_window_bits,
    )


def _build_parser() -> argparse.ArgumentParser:
    try:
        pkg_ver = version("stario")
    except PackageNotFoundError:
        pkg_ver = "unknown"

    parser = argparse.ArgumentParser(
        prog="stario",
        description="Create, serve, and watch App apps.",
        epilog=_ROOT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {pkg_ver}",
    )

    # Shared ``serve`` / ``watch`` flags (parent parser, no -h).
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--compress-gzip-window-bits",
        dest="gzip_window_bits",
        default=None,
        type=int,
        metavar="N",
        help="Gzip window bits; unset keeps the codec default",
    )
    shared.add_argument(
        "--compress-gzip-level",
        dest="gzip_level",
        default=6,
        type=int,
        help="Gzip compression level; any negative value disables (default: %(default)s)",
    )
    shared.add_argument(
        "--compress-brotli-window-log",
        dest="brotli_window_log",
        default=None,
        type=int,
        metavar="N",
        help="Brotli window log; unset keeps the codec default",
    )
    shared.add_argument(
        "--compress-brotli-level",
        dest="brotli_level",
        default=4,
        type=int,
        help="Brotli compression level; any negative value disables (default: %(default)s)",
    )
    shared.add_argument(
        "--compress-zstd-window-log",
        dest="zstd_window_log",
        default=None,
        type=int,
        metavar="N",
        help="Zstd window log; unset keeps the codec default",
    )
    shared.add_argument(
        "--compress-zstd-level",
        dest="zstd_level",
        default=3,
        type=int,
        help="Zstd compression level; any negative value disables (default: %(default)s)",
    )
    shared.add_argument(
        "--compress-min-size",
        dest="compress_min_size",
        default=512,
        type=int,
        help="Minimum response size in bytes before compression applies (default: %(default)s)",
    )
    shared.add_argument(
        "--max-request-body-bytes",
        dest="max_request_body_bytes",
        default=_CLI_DEFAULT_MAX_BODY_BYTES,
        type=int,
        help="Maximum request body bytes per message (413 when exceeded) (default: %(default)s)",
    )
    shared.add_argument(
        "--max-request-header-bytes",
        dest="max_request_header_bytes",
        default=_CLI_DEFAULT_MAX_HEADER_BYTES,
        type=int,
        help="Maximum request line + headers size in bytes (431 when exceeded) (default: %(default)s)",
    )
    shared.add_argument(
        "--host",
        default="127.0.0.1",
        help="TCP host to bind when not using a Unix socket (default: %(default)s)",
    )
    shared.add_argument(
        "--port",
        default=8000,
        type=int,
        help="TCP port to bind when not using a Unix socket (default: %(default)s)",
    )
    shared.add_argument(
        "--unix-socket",
        dest="unix_socket",
        default=None,
        metavar="PATH",
        help="Listen on a Unix domain socket instead of TCP",
    )
    shared.add_argument(
        "--loop",
        choices=("asyncio", "uvloop"),
        default="asyncio",
        type=str.lower,
        help="Event loop backend for the server process (default: %(default)s)",
    )
    shared.add_argument(
        "--tracer",
        dest="tracer_spec",
        default=None,
        metavar="SPEC",
        help=(
            "Telemetry sink: auto (TTY span tree if stdout is a TTY, else NDJSON), "
            "or tty, json, sqlite, or custom <module>:<callable>"
        ),
    )

    sub = parser.add_subparsers(dest="command", required=True)

    serve_p = sub.add_parser(
        "serve",
        parents=[shared],
        help="Start a App app once.",
        description="Start a App app once.",
        epilog=(
            "Examples:\n"
            "  stario serve main:bootstrap\n"
            "  stario serve main:bootstrap --tracer json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    serve_p.add_argument(
        "app",
        metavar="MODULE:CALLABLE",
        help="Import path to the app bootstrap callable",
    )

    watch_epilog = """\
Examples:
  stario watch main:bootstrap
  stario watch main:bootstrap --tracer json
  stario watch main:bootstrap --watch app/
  stario watch main:bootstrap --watch main.py
  stario watch main:bootstrap --watch '**/*.py' --watch-ignore 'data/'
  stario watch main:bootstrap --watch-ignore '*.sqlite3'
"""
    watch_p = sub.add_parser(
        "watch",
        parents=[shared],
        help="Start a App app and restart it on changes.",
        description="Start a App app and restart it on changes.",
        epilog=watch_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    watch_p.add_argument(
        "--watch",
        dest="watch_specs",
        action="append",
        default=[],
        metavar="SPEC",
        help="File, directory, or glob to watch; end directories with / when they do not exist yet",
    )
    watch_p.add_argument(
        "--watch-ignore",
        dest="watch_ignore_specs",
        action="append",
        default=[],
        metavar="SPEC",
        help="File, directory, or glob to ignore; sqlite files are ignored by default",
    )
    watch_p.add_argument(
        "app",
        metavar="MODULE:CALLABLE",
        help="Import path to the app bootstrap callable",
    )

    init_p = sub.add_parser(
        "init",
        help="Create a new App project from a template.",
        description="Create a new App project from a template. NAME sets the project directory; omit it to be prompted.",
    )
    init_p.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Project directory name",
    )
    init_p.add_argument(
        "-t",
        "--template",
        dest="template_name",
        default=None,
        metavar="NAME",
        help="Template to use (skip interactive selection)",
    )

    return parser


def _fetch_remote_example(example_name: str, dest: Path) -> None:
    """Clone one GitHub examples/ tree via the REST API (recursive directory walk)."""

    def walk_github_contents(items: list) -> list:
        out: list = []
        for item in items:
            if item["type"] == "file":
                out.append(item)
            elif item["type"] == "dir":
                req = urllib.request.Request(
                    item["url"], headers={"User-Agent": "stario-cli"}
                )
                with urllib.request.urlopen(req, timeout=10) as response:
                    subdir = json.loads(response.read())
                out.extend(walk_github_contents(subdir))
        return out

    api_url = f"{GITHUB_API}/examples/{example_name}"
    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "stario-cli"})
        with urllib.request.urlopen(req, timeout=10) as response:
            contents = json.loads(response.read())

        for item in walk_github_contents(contents):
            if item["type"] == "file":
                rel_path = item["path"].split(f"examples/{example_name}/", 1)[1]
                file_path = dest / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with urllib.request.urlopen(item["download_url"]) as f:
                    file_path.write_bytes(f.read())

    except urllib.error.URLError as e:
        raise CliError(
            f"Failed to fetch example '{example_name}': {e}\n"
            "Check your internet connection or try a bundled template."
        )


def _stario_dep_for_init() -> str:
    """Constraint matching the running CLI major: ``stario>=N,<N+1``."""
    try:
        v = version("stario")
    except PackageNotFoundError:
        return "stario"
    # Drop PEP 440 epoch (e.g. ``1!3.0.0`` → ``3.0.0``).
    rel = v.split("!", 1)[-1]
    first = rel.split(".", 1)[0]
    digits = "".join(itertools.takewhile(str.isdigit, first))
    if not digits:
        return "stario"
    major = int(digits)
    return f"stario>={major},<{major + 1}"


def _init_project(args: argparse.Namespace) -> None:
    name: str | None = args.name
    template_name: str | None = args.template_name

    term.echo()
    term.echo(term.style("⭐ App", fg="yellow", bold=True))
    term.echo()

    # Bundled templates plus optional remote manifest (best-effort; offline keeps bundled only).
    templates: list[Template] = list(BUNDLED_TEMPLATES)
    remote_catalog_unavailable = False
    try:
        req = urllib.request.Request(
            MANIFEST_URL,
            headers={"User-Agent": "stario-cli"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read())
        templates.extend(
            Template(
                name=ex["name"],
                description=ex["description"],
                long_description=ex.get("long_description", ""),
                bundled=False,
            )
            for ex in data.get("examples", [])
        )
    except Exception:
        remote_catalog_unavailable = True

    has_remote = any(not t.bundled for t in templates)
    template_map = {t.name: t for t in templates}
    num_map = {str(i + 1): t for i, t in enumerate(templates)}

    if template_name is None:
        default_idx = next((i for i, t in enumerate(templates) if t.recommended), 0)

        term.echo(term.style("? Choose a template:", fg="cyan", bold=True))
        term.echo()
        if remote_catalog_unavailable:
            term.echo(
                term.style(
                    "  Remote template catalog unavailable — only bundled templates are listed.",
                    dim=True,
                )
            )
            term.echo()

        for i, t in enumerate(templates):
            num = term.style(f"  [{i + 1}]", fg="cyan")
            name_style = term.style(t.name, bold=True)
            suffix = ""
            if t.recommended:
                suffix = term.style("  ★ great starting point", fg="yellow")
            elif not t.bundled:
                suffix = term.style("  ↓", fg="blue")
            term.echo(f"{num} {name_style} - {t.description}{suffix}")
            if t.long_description:
                term.echo(term.style(f"    {t.long_description}", dim=True))
            term.echo()

        if has_remote:
            term.echo(
                term.style("  ↓", fg="blue")
                + term.style(" = downloaded from GitHub", dim=True)
            )
            term.echo()

        choice = term.prompt(
            term.style("? Enter number or name", fg="cyan", bold=True),
            default=str(default_idx + 1),
        )
        if choice in num_map:
            template_name = num_map[choice].name
        elif choice in template_map:
            template_name = choice
        else:
            raise CliError(
                f"Unknown template '{choice}'. Use a number (1-{len(templates)}) or template name."
            )

    if template_name not in template_map:
        raise CliError(
            f"Unknown template '{template_name}'. "
            f"Available: {', '.join(template_map.keys())}"
        )

    chosen = template_map[template_name]

    if name is None:
        name = str(
            term.prompt(
                term.style("? Project name", fg="cyan", bold=True),
                default="stario-app",
            )
        )

    project_dir = Path.cwd() / name
    if project_dir.exists():
        raise CliError(f"Directory '{name}' already exists")

    term.echo()
    term.echo(
        term.style("  Creating ", dim=True)
        + term.style(name, fg="green", bold=True)
        + term.style(f" with {template_name} template...", dim=True)
    )
    term.echo()

    term.echo(term.style("  ◐ ", fg="yellow") + "Setting up project with uv...")
    result = subprocess.run(
        ["uv", "init", "--app", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CliError(f"uv init failed: {result.stderr}")

    stario_dep = _stario_dep_for_init()
    term.echo(term.style("  ◐ ", fg="yellow") + "Adding stario dependency...")
    add = subprocess.run(
        ["uv", "add", stario_dep],
        cwd=project_dir,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise CliError(
            f"uv add {stario_dep!r} failed: {add.stderr or add.stdout or 'unknown error'}"
        )

    for file in ("hello.py", "README.md"):
        p = project_dir / file
        if p.exists():
            p.unlink()

    if chosen.bundled:
        term.echo(term.style("  ◐ ", fg="yellow") + "Copying template files...")
        template_dir = TEMPLATES_DIR / template_name
        if not template_dir.exists():
            raise CliError(
                f"Template directory not found: {template_dir}\n"
                "This is a bug in stario - please report it."
            )
        for item in template_dir.iterdir():
            dst = project_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dst)
            else:
                shutil.copy2(item, dst)
    else:
        term.echo(
            term.style("  ◐ ", fg="yellow") + "Downloading template from GitHub..."
        )
        _fetch_remote_example(template_name, project_dir)

    term.echo()
    term.echo(
        term.style("  ✓ ", fg="green")
        + term.style("Project created successfully!", fg="green", bold=True)
    )
    term.echo()
    term.echo(term.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
    term.echo()

    if term.confirm(
        term.style("  ? Start the app now?", fg="cyan", bold=True),
        default=True,
    ):
        term.echo()
        term.echo(
            term.style("  🚀 Starting server at ", fg="white")
            + term.style("http://localhost:8000", fg="cyan", underline=True)
        )
        term.echo(term.style("     Press Ctrl+C to stop", dim=True))
        term.echo()
        os.chdir(project_dir)
        subprocess.run(["uv", "run", "stario", "watch", "main:bootstrap"])
        term.echo()
        term.echo(term.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
        term.echo()
        term.echo(
            term.style("  To continue working on your project:", fg="white", bold=True)
        )
        term.echo()
        term.echo(term.style(f"     cd {name}", fg="cyan"))
        term.echo(term.style("     uv run stario watch main:bootstrap", fg="cyan"))
        term.echo()
        term.echo(
            term.style("     # Or start it once without file watching", dim=True)
        )
        term.echo(term.style("     uv run stario serve main:bootstrap", fg="cyan"))
        term.echo()
    else:
        term.echo()
        term.echo(
            term.style("  🚀 Ready to go! Run these commands:", fg="white", bold=True)
        )
        term.echo()
        term.echo(term.style(f"     cd {name}", fg="cyan"))
        term.echo(term.style("     uv run stario watch main:bootstrap", fg="cyan"))
        term.echo()
        term.echo(
            term.style("     # Or start it once without file watching", dim=True)
        )
        term.echo(term.style("     uv run stario serve main:bootstrap", fg="cyan"))
        term.echo()
        term.echo(term.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
        term.echo()
        term.echo(
            term.style("  Open ", dim=True)
            + term.style("http://localhost:8000", fg="cyan", underline=True)
            + term.style(" and have fun! ⭐", dim=True)
        )
        term.echo()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        code = e.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1

    try:
        if args.command == "init":
            _init_project(args)
            return 0

        # ``serve`` and ``watch``: same request limits + compression, then runtime splits.
        if args.max_request_header_bytes < 256:
            raise CliError(
                "--max-request-header-bytes must be at least 256 (or use the default)."
            )
        if args.max_request_body_bytes < 1:
            raise CliError(
                "--max-request-body-bytes must be at least 1 (or use the default)."
            )
        compression = _resolve_compression_config(
            min_size=args.compress_min_size,
            zstd_level=args.zstd_level,
            zstd_window_log=args.zstd_window_log,
            brotli_level=args.brotli_level,
            brotli_window_log=args.brotli_window_log,
            gzip_level=args.gzip_level,
            gzip_window_bits=args.gzip_window_bits,
        )
        loop: CliLoop = args.loop
        if args.command == "serve":
            serve_once(
                args.app,
                loop=loop,
                tracer_spec=args.tracer_spec,
                host=args.host,
                port=args.port,
                unix_socket=args.unix_socket,
                compression=compression,
                max_request_header_bytes=args.max_request_header_bytes,
                max_request_body_bytes=args.max_request_body_bytes,
            )
        else:
            watch_app(
                args.app,
                loop=loop,
                tracer_spec=args.tracer_spec,
                host=args.host,
                port=args.port,
                unix_socket=args.unix_socket,
                compression=compression,
                watch_specs=tuple(args.watch_specs),
                watch_ignore_specs=tuple(args.watch_ignore_specs),
                max_request_header_bytes=args.max_request_header_bytes,
                max_request_body_bytes=args.max_request_body_bytes,
            )
    except CliError as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        term.echo()
        term.echo(term.style("Interrupted.", dim=True))
        return 130
    return 0
