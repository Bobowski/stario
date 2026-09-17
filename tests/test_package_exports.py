"""Smoke tests for public import surface."""

import importlib

import pytest


def test_core_modules_import() -> None:
    import stario
    import stario.datastar
    import stario.filesystem
    import stario.http
    import stario.json
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
                "Assets",
                "Context",
                "Files",
                "Route",
                "StaticAssets",
                "UrlPath",
                "Writer",
            ],
        ),
        (
            "stario.filesystem",
            ["Assets", "Files"],
        ),
        (
            "stario.staticassets",
            ["AssetManifest", "StaticAssets", "fingerprint"],
        ),
        (
            "stario.routing",
            ["Route", "UrlPath", "Segment", "normalize_path", "append_query_fragment"],
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
            "stario.json",
            [
                "JsonCodec",
                "StdlibJsonCodec",
                "dumps",
                "dumps_bytes",
                "loads",
                "set_codec",
            ],
        ),
    ],
)
def test_public_exports(module_name: str, names: list[str]) -> None:
    module = importlib.import_module(module_name)
    for name in names:
        assert hasattr(module, name), f"{module_name} missing {name!r}"


def test_filesystem_all_is_exact() -> None:
    import stario.filesystem

    assert stario.filesystem.__all__ == ["Assets", "Files"]
    assert stario.Assets is stario.filesystem.Assets
    assert stario.Files is stario.filesystem.Files


def test_obsolete_staticassets_still_exported() -> None:
    import stario
    import stario.staticassets

    assert stario.StaticAssets is stario.staticassets.StaticAssets
    assert stario.AssetManifest is stario.staticassets.AssetManifest


def test_obsolete_staticassets_warn_on_construct(tmp_path) -> None:
    from stario.staticassets import AssetManifest, StaticAssets

    (tmp_path / "app.js").write_text("ok")
    with pytest.warns(DeprecationWarning, match="obsolete and will be removed"):
        manifest = AssetManifest(tmp_path)
    with pytest.warns(DeprecationWarning, match="obsolete and will be removed"):
        StaticAssets(manifest)


@pytest.mark.parametrize(
    "removed_module",
    [
        "stario.urls",
        "stario.files",
        "stario.filesystem.assets",
        "stario.filesystem.files",
        "stario.filesystem.live",
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
