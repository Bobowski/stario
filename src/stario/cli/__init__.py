"""
Stario CLI.

Usage:
    stario init
    stario serve main:bootstrap
    stario watch main:bootstrap
"""

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import click

from stario.http.writer import CompressionConfig

from .runtime import serve_once, watch_app

# Path to bundled templates (relative to this file)
TEMPLATES_DIR = Path(__file__).parent / "templates"

# GitHub URLs for fetching remote examples
GITHUB_API = "https://api.github.com/repos/Bobowski/stario/contents"
MANIFEST_URL = (
    "https://raw.githubusercontent.com/Bobowski/stario/main/examples/manifest.json"
)


@dataclass
class Template:
    """Represents a project template."""

    name: str
    description: str
    long_description: str = ""
    bundled: bool = True
    recommended: bool = False


# Bundled templates (always available)
BUNDLED_TEMPLATES = [
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


def _fetch_remote_templates() -> list[Template]:
    """Fetch remote examples manifest. Returns empty list on any failure."""
    try:
        req = urllib.request.Request(
            MANIFEST_URL,
            headers={"User-Agent": "stario-cli"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read())
            return [
                Template(
                    name=ex["name"],
                    description=ex["description"],
                    long_description=ex.get("long_description", ""),
                    bundled=False,
                )
                for ex in data.get("examples", [])
            ]
    except Exception:
        return []  # Silent failure - just show bundled templates


def _get_all_templates() -> tuple[list[Template], bool]:
    """Get all available templates. Returns (templates, has_remote)."""
    templates = list(BUNDLED_TEMPLATES)
    remote = _fetch_remote_templates()
    templates.extend(remote)
    return templates, len(remote) > 0


def _fetch_remote_example(example_name: str, dest: Path) -> None:
    """Fetch example directory from GitHub."""
    api_url = f"{GITHUB_API}/examples/{example_name}"

    try:
        req = urllib.request.Request(api_url, headers={"User-Agent": "stario-cli"})
        with urllib.request.urlopen(req, timeout=10) as response:
            contents = json.loads(response.read())

        for item in _walk_github_contents(contents):
            if item["type"] == "file":
                # Strip the example prefix from path
                rel_path = item["path"].split(f"examples/{example_name}/", 1)[1]
                file_path = dest / rel_path
                file_path.parent.mkdir(parents=True, exist_ok=True)

                with urllib.request.urlopen(item["download_url"]) as f:
                    file_path.write_bytes(f.read())

    except urllib.error.URLError as e:
        raise click.ClickException(
            f"Failed to fetch example '{example_name}': {e}\n"
            "Check your internet connection or try a bundled template."
        )


def _walk_github_contents(items: list) -> list:
    """Recursively walk directory contents from GitHub API."""
    result = []
    for item in items:
        if item["type"] == "file":
            result.append(item)
        elif item["type"] == "dir":
            req = urllib.request.Request(
                item["url"], headers={"User-Agent": "stario-cli"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                subdir = json.loads(response.read())
            result.extend(_walk_github_contents(subdir))
    return result


@click.group(
    epilog=(
        "\b\n"
        "Examples:\n"
        "  stario init\n"
        "  stario serve main:bootstrap\n"
        "  stario watch main:bootstrap\n"
        "  stario watch main:bootstrap --watch-path src"
    )
)
@click.version_option(package_name="stario")
def main() -> None:
    """Create, serve, and watch Stario apps."""
    pass


def _resolve_compression_config(
    *,
    min_size: int,
    zstd_level: int,
    brotli_level: int,
    gzip_level: int,
) -> CompressionConfig:
    if min_size < 0:
        raise click.ClickException("--compress-min-size must be 0 or greater.")
    if zstd_level >= 0 and not 1 <= zstd_level <= 22:
        raise click.ClickException(
            "--compress-zstd-level must be negative or between 1 and 22."
        )
    if brotli_level >= 0 and not 0 <= brotli_level <= 11:
        raise click.ClickException(
            "--compress-brotli-level must be negative or between 0 and 11."
        )
    if gzip_level >= 0 and not 1 <= gzip_level <= 9:
        raise click.ClickException(
            "--compress-gzip-level must be negative or between 1 and 9."
        )
    return CompressionConfig(
        min_size=min_size,
        zstd_level=zstd_level,
        brotli_level=brotli_level,
        gzip_level=gzip_level,
    )


@main.command(
    name="serve",
    epilog="\b\nExample:\n  stario serve main:bootstrap",
)
@click.argument("app", metavar="MODULE:CALLABLE")
@click.option(
    "--tracer",
    "tracer_spec",
    help="Telemetry output: auto, rich, json, or custom <module>:<callable>",
)
@click.option(
    "--unix-socket",
    help="Listen on a Unix domain socket instead of TCP",
)
@click.option(
    "--port",
    default=8000,
    show_default=True,
    type=int,
    help="TCP port to bind when not using a Unix socket",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="TCP host to bind when not using a Unix socket",
)
@click.option(
    "--compress-min-size",
    default=512,
    show_default=True,
    type=int,
    help="Minimum response size in bytes before compression applies",
)
@click.option(
    "--compress-zstd-level",
    "zstd_level",
    default=3,
    show_default=True,
    type=int,
    help="Zstd compression level; any negative value disables",
)
@click.option(
    "--compress-brotli-level",
    "brotli_level",
    default=4,
    show_default=True,
    type=int,
    help="Brotli compression level; any negative value disables",
)
@click.option(
    "--compress-gzip-level",
    "gzip_level",
    default=6,
    show_default=True,
    type=int,
    help="Gzip compression level; any negative value disables",
)
def serve_command(
    app: str,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compress_min_size: int,
    zstd_level: int,
    brotli_level: int,
    gzip_level: int,
) -> None:
    """Start a Stario app once."""
    compression = _resolve_compression_config(
        min_size=compress_min_size,
        zstd_level=zstd_level,
        brotli_level=brotli_level,
        gzip_level=gzip_level,
    )
    serve_once(
        app,
        tracer_spec=tracer_spec,
        host=host,
        port=port,
        unix_socket=unix_socket,
        compression=compression,
    )


@main.command(
    name="watch",
    epilog=(
        "\b\n"
        "Examples:\n"
        "  stario watch main:bootstrap\n"
        "  stario watch main:bootstrap --watch-path src"
    ),
)
@click.option(
    "--watch-path",
    "watch_paths",
    multiple=True,
    help="Path to watch for changes; can be passed multiple times",
)
@click.argument("app", metavar="MODULE:CALLABLE")
@click.option(
    "--tracer",
    "tracer_spec",
    help="Telemetry output: auto, rich, json, or custom <module>:<callable>",
)
@click.option(
    "--unix-socket",
    help="Listen on a Unix domain socket instead of TCP",
)
@click.option(
    "--port",
    default=8000,
    show_default=True,
    type=int,
    help="TCP port to bind when not using a Unix socket",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="TCP host to bind when not using a Unix socket",
)
@click.option(
    "--compress-min-size",
    default=512,
    show_default=True,
    type=int,
    help="Minimum response size in bytes before compression applies",
)
@click.option(
    "--compress-zstd-level",
    "zstd_level",
    default=3,
    show_default=True,
    type=int,
    help="Zstd compression level; any negative value disables",
)
@click.option(
    "--compress-brotli-level",
    "brotli_level",
    default=4,
    show_default=True,
    type=int,
    help="Brotli compression level; any negative value disables",
)
@click.option(
    "--compress-gzip-level",
    "gzip_level",
    default=6,
    show_default=True,
    type=int,
    help="Gzip compression level; any negative value disables",
)
def watch_command(
    app: str,
    tracer_spec: str | None,
    host: str,
    port: int,
    unix_socket: str | None,
    compress_min_size: int,
    zstd_level: int,
    brotli_level: int,
    gzip_level: int,
    watch_paths: tuple[str, ...],
) -> None:
    """Start a Stario app and restart it on changes."""
    compression = _resolve_compression_config(
        min_size=compress_min_size,
        zstd_level=zstd_level,
        brotli_level=brotli_level,
        gzip_level=gzip_level,
    )
    watch_app(
        app,
        tracer_spec=tracer_spec,
        host=host,
        port=port,
        unix_socket=unix_socket,
        compression=compression,
        watch_paths=watch_paths or (".",),
    )


@main.command()
@click.argument("name", required=False)
@click.option(
    "--template",
    "-t",
    "template_name",
    help="Template to use (skip interactive selection)",
)
def init(name: str | None, template_name: str | None) -> None:
    """
    Create a new Stario project from a template.

    NAME sets the project directory. If omitted, Stario prompts for it.
    """
    click.echo()
    click.echo(click.style("⭐ Stario", fg="yellow", bold=True))
    click.echo()

    # Get available templates
    templates, has_remote = _get_all_templates()
    template_map = {t.name: t for t in templates}
    # Also map by number
    num_map = {str(i + 1): t for i, t in enumerate(templates)}

    # Template selection first
    if template_name is None:
        default_idx = next((i for i, t in enumerate(templates) if t.recommended), 0)

        click.echo(click.style("? Choose a template:", fg="cyan", bold=True))
        click.echo()

        for i, t in enumerate(templates):
            num = click.style(f"  [{i + 1}]", fg="cyan")
            name_style = click.style(t.name, bold=True)

            # Build suffix
            suffix = ""
            if t.recommended:
                suffix = click.style("  ★ great starting point", fg="yellow")
            elif not t.bundled:
                suffix = click.style("  ↓", fg="blue")

            click.echo(f"{num} {name_style} - {t.description}{suffix}")

            # Show long description if available
            if t.long_description:
                click.echo(click.style(f"    {t.long_description}", dim=True))
            click.echo()

        # Show legend only if we have remote templates
        if has_remote:
            click.echo(
                click.style("  ↓", fg="blue")
                + click.style(" = downloaded from GitHub", dim=True)
            )
            click.echo()

        # Prompt with number as default
        choice = click.prompt(
            click.style("? Enter number or name", fg="cyan", bold=True),
            default=str(default_idx + 1),
        )

        # Resolve choice (number or name)
        if choice in num_map:
            template_name = num_map[choice].name
        elif choice in template_map:
            template_name = choice
        else:
            raise click.ClickException(
                f"Unknown template '{choice}'. Use a number (1-{len(templates)}) or template name."
            )

    # Validate template
    if template_name not in template_map:
        raise click.ClickException(
            f"Unknown template '{template_name}'. "
            f"Available: {', '.join(template_map.keys())}"
        )

    template = template_map[template_name]

    # Project name (after template selection)
    if name is None:
        prompt_name = click.prompt(
            click.style("? Project name", fg="cyan", bold=True),
            default="stario-app",
        )
        name = str(prompt_name)

    project_dir = Path.cwd() / name

    if project_dir.exists():
        raise click.ClickException(f"Directory '{name}' already exists")

    click.echo()
    click.echo(
        click.style("  Creating ", dim=True)
        + click.style(name, fg="green", bold=True)
        + click.style(f" with {template_name} template...", dim=True)
    )
    click.echo()

    # 1. Initialize with uv
    click.echo(click.style("  ◐ ", fg="yellow") + "Setting up project with uv...")
    result = subprocess.run(
        ["uv", "init", "--app", name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(f"uv init failed: {result.stderr}")

    # 2. Add stario dependency
    click.echo(click.style("  ◐ ", fg="yellow") + "Adding stario dependency...")
    subprocess.run(
        ["uv", "add", "stario"],
        cwd=project_dir,
        capture_output=True,
    )

    # 3. Remove default files created by uv
    for file in ["hello.py", "README.md"]:
        default_file = project_dir / file
        if default_file.exists():
            default_file.unlink()

    # 4. Copy/fetch template files
    if template.bundled:
        click.echo(click.style("  ◐ ", fg="yellow") + "Copying template files...")
        _copy_bundled_template(project_dir, template_name)
    else:
        click.echo(
            click.style("  ◐ ", fg="yellow") + "Downloading template from GitHub..."
        )
        _fetch_remote_example(template_name, project_dir)

    # Success!
    click.echo()
    click.echo(
        click.style("  ✓ ", fg="green")
        + click.style("Project created successfully!", fg="green", bold=True)
    )
    click.echo()

    # Ask if they want to start the server
    click.echo(click.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
    click.echo()

    if click.confirm(
        click.style("  ? Start the app now?", fg="cyan", bold=True),
        default=True,
    ):
        click.echo()
        click.echo(
            click.style("  🚀 Starting server at ", fg="white")
            + click.style("http://localhost:8000", fg="cyan", underline=True)
        )
        click.echo(click.style("     Press Ctrl+C to stop", dim=True))
        click.echo()

        # Run the server in the project directory
        os.chdir(project_dir)
        subprocess.run(["uv", "run", "stario", "watch", "main:bootstrap"])

        # After server stops, show how to get back
        click.echo()
        click.echo(click.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
        click.echo()
        click.echo(
            click.style("  To continue working on your project:", fg="white", bold=True)
        )
        click.echo()
        click.echo(click.style(f"     cd {name}", fg="cyan"))
        click.echo(click.style("     uv run stario watch main:bootstrap", fg="cyan"))
        click.echo()
        click.echo(click.style("     # Or start it once without file watching", dim=True))
        click.echo(click.style("     uv run stario serve main:bootstrap", fg="cyan"))
        click.echo()
    else:
        # Show manual instructions
        click.echo()
        click.echo(
            click.style("  🚀 Ready to go! Run these commands:", fg="white", bold=True)
        )
        click.echo()
        click.echo(click.style(f"     cd {name}", fg="cyan"))
        click.echo(click.style("     uv run stario watch main:bootstrap", fg="cyan"))
        click.echo()
        click.echo(click.style("     # Or start it once without file watching", dim=True))
        click.echo(click.style("     uv run stario serve main:bootstrap", fg="cyan"))
        click.echo()
        click.echo(click.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
        click.echo()
        click.echo(
            click.style("  Open ", dim=True)
            + click.style("http://localhost:8000", fg="cyan", underline=True)
            + click.style(" and have fun! ⭐", dim=True)
        )
        click.echo()


def _copy_bundled_template(project_dir: Path, template_name: str) -> None:
    """Copy bundled template files to project directory."""
    template_dir = TEMPLATES_DIR / template_name

    if not template_dir.exists():
        raise click.ClickException(
            f"Template directory not found: {template_dir}\n"
            "This is a bug in stario - please report it."
        )

    for item in template_dir.iterdir():
        src = item
        dst = project_dir / item.name

        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


if __name__ == "__main__":
    main()
