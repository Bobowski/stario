"""
Stario CLI - Initialize Stario projects.

Usage:
    stario init            # Interactive project setup
    stario init myproject  # Create project with name
    stario init -t hello-world  # Specify template
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import click

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


@click.group()
@click.version_option(package_name="stario")
def main() -> None:
    """Stario - High-performance Python web framework."""
    pass


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
    Create a new Stario project.

    NAME: Project directory name (prompted if not provided)
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
        click.style("  ? Start the server now?", fg="cyan", bold=True),
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
        subprocess.run(["uv", "run", "main.py"])

        # After server stops, show how to get back
        click.echo()
        click.echo(click.style("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", dim=True))
        click.echo()
        click.echo(
            click.style("  To continue working on your project:", fg="white", bold=True)
        )
        click.echo()
        click.echo(click.style(f"     cd {name}", fg="cyan"))
        click.echo(click.style("     uv run main.py", fg="cyan"))
        click.echo()
        click.echo(click.style("     # Or with auto-reload", dim=True))
        click.echo(click.style('     uv add --dev watchfiles', fg="cyan"))
        click.echo(click.style('     uv run watchfiles "python main.py" .', fg="cyan"))
        click.echo()
    else:
        # Show manual instructions
        click.echo()
        click.echo(
            click.style("  🚀 Ready to go! Run these commands:", fg="white", bold=True)
        )
        click.echo()
        click.echo(click.style(f"     cd {name}", fg="cyan"))
        click.echo(click.style("     uv run main.py", fg="cyan"))
        click.echo()
        click.echo(click.style("     # Or with auto-reload", dim=True))
        click.echo(click.style('     uv add --dev watchfiles', fg="cyan"))
        click.echo(click.style('     uv run watchfiles "python main.py" .', fg="cyan"))
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
