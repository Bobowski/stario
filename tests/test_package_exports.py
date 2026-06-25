"""Smoke tests for public import surface."""

import importlib

import pytest


def test_core_modules_import() -> None:
    import stario
    import stario.datastar
    import stario.http
    import stario.markup
    import stario.routing
    import stario.staticassets

    assert isinstance(stario.__version__, str)
    assert stario.__version__
    assert hasattr(stario, "App")
    assert hasattr(stario.http, "Router")
    assert hasattr(stario.datastar, "data")
    assert hasattr(stario.markup, "render")


@pytest.mark.parametrize(
    ("module_name", "names"),
    [
        (
            "stario",
            [
                "App",
                "AssetManifest",
                "Context",
                "StaticAssets",
                "UrlPath",
                "Writer",
            ],
        ),
        (
            "stario.routing",
            ["UrlPath", "Segment", "normalize_path", "append_query_fragment"],
        ),
        (
            "stario.http",
            [
                "App",
                "Router",
                "Request",
                "Writer",
                "RouteMatch",
                "normalized_location",
                "default_not_found",
            ],
        ),
        (
            "stario.staticassets",
            ["AssetManifest", "StaticAssets", "fingerprint"],
        ),
    ],
)
def test_public_exports(module_name: str, names: list[str]) -> None:
    module = importlib.import_module(module_name)
    for name in names:
        assert hasattr(module, name), f"{module_name} missing {name!r}"


@pytest.mark.parametrize(
    "removed_module",
    [
        "stario.urls",
        "stario.http.router",
        "stario.http.staticassets",
        "stario.routing.trie",
        "stario.routing.pattern",
    ],
)
def test_removed_shim_modules(removed_module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(removed_module)


def test_stario_html_module_removed() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("stario.html")
