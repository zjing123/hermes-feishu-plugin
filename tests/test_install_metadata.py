"""Tests for install helpers and packaging metadata."""

from __future__ import annotations

from pathlib import Path
import tomllib

import hermes_feishu_plugin.install as install_module


def _seed_directory_link(path: Path, target: Path) -> None:
    try:
        path.symlink_to(target, target_is_directory=True)
    except OSError:
        path.mkdir()


def test_sync_profile_plugin_links_creates_root_and_profile_symlinks(tmp_path, monkeypatch) -> None:
    """Directory-plugin installs should link root and profile plugin folders."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "plugin.yaml").write_text("name: hermes_feishu_plugin\n", encoding="utf-8")
    (repo_root / "__init__.py").write_text("", encoding="utf-8")

    home_root = tmp_path / "home"
    hermes_root = home_root / ".hermes"
    root_plugins = hermes_root / "plugins"
    default_plugins = hermes_root / "profiles" / "default" / "plugins"
    hermes_site_packages = hermes_root / "hermes-agent" / "venv" / "lib" / "python3.11" / "site-packages"
    root_plugins.mkdir(parents=True)
    default_plugins.mkdir(parents=True)
    hermes_site_packages.mkdir(parents=True)

    legacy_link = root_plugins / "hermes-feishu-plugin"
    _seed_directory_link(legacy_link, repo_root)
    legacy_runtime_plugin = default_plugins / "runtime_patches"
    legacy_runtime_plugin.mkdir()
    (legacy_runtime_plugin / "plugin.yaml").write_text("name: runtime_patches\n", encoding="utf-8")

    monkeypatch.setattr(install_module, "_resolve_plugin_root", lambda: repo_root)
    monkeypatch.setattr(install_module.Path, "home", lambda: home_root)
    monkeypatch.setattr(install_module.site, "getsitepackages", lambda: [str(tmp_path / "site-packages")])
    (tmp_path / "site-packages").mkdir()

    synced_scopes = install_module.sync_profile_plugin_links()

    assert set(synced_scopes) == {"root", "default"}
    root_link = root_plugins / "hermes_feishu_plugin"
    default_link = default_plugins / "hermes_feishu_plugin"
    assert root_link.exists()
    assert default_link.exists()
    if root_link.is_symlink():
        assert root_link.resolve() == repo_root
    else:
        assert (root_link / "plugin.yaml").read_text(encoding="utf-8") == "name: hermes_feishu_plugin\n"
    if default_link.is_symlink():
        assert default_link.resolve() == repo_root
    else:
        assert (default_link / "plugin.yaml").read_text(encoding="utf-8") == "name: hermes_feishu_plugin\n"
    assert not legacy_link.exists()
    assert not legacy_runtime_plugin.exists()
    # Behaviour, not a frozen literal: the loader must point sys.path at this
    # checkout's src dir before importing the early startup module.
    loader = (root_plugins / "sitecustomize.py").read_text(encoding="utf-8")
    assert "hermes_feishu_plugin.startup" in loader
    assert str((repo_root / "src").resolve()) in loader
    assert (tmp_path / "site-packages" / "hermes_feishu_plugin_startup.pth").read_text(
        encoding="utf-8") == loader
    assert (hermes_site_packages / "hermes_feishu_plugin_startup.pth").read_text(
        encoding="utf-8") == loader


def test_startup_loader_skips_and_cleans_non_gateway_envs(tmp_path, monkeypatch) -> None:
    """The early loader belongs in the gateway venv only.

    Writing it into the bundled toolchain / PM environments executed plugin code
    inside interpreters that never serve Feishu, one ModuleNotFoundError per
    start — those files must be skipped and any stale one removed.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "plugin.yaml").write_text("name: hermes_feishu_plugin\n", encoding="utf-8")
    (repo_root / "__init__.py").write_text("", encoding="utf-8")

    home_root = tmp_path / "home"
    hermes_root = home_root / ".hermes"
    root_plugins = hermes_root / "plugins"
    root_plugins.mkdir(parents=True)
    hermes_site_packages = hermes_root / "hermes-agent" / "venv" / "lib" / "python3.11" / "site-packages"
    hermes_site_packages.mkdir(parents=True)
    tools_site_packages = hermes_root / "tools" / "python-3.14" / "lib" / "python3.14" / "site-packages"
    tools_site_packages.mkdir(parents=True)
    pm_site_packages = (hermes_root / "installs" / "abc123" / "environments" / "def456"
                        / "venv" / "lib" / "python3.14" / "site-packages")
    pm_site_packages.mkdir(parents=True)

    stale = "import hermes_feishu_plugin.startup\n"
    for site_dir in (tools_site_packages, pm_site_packages):
        (site_dir / install_module.STARTUP_PTH_NAME).write_text(stale, encoding="utf-8")

    monkeypatch.setattr(install_module, "_resolve_plugin_root", lambda: repo_root)
    monkeypatch.setattr(install_module.Path, "home", lambda: home_root)
    monkeypatch.setattr(install_module.site, "getsitepackages", lambda: [str(tools_site_packages)])

    install_module.sync_profile_plugin_links()

    assert (hermes_site_packages / install_module.STARTUP_PTH_NAME).is_file()
    assert not (tools_site_packages / install_module.STARTUP_PTH_NAME).exists()
    assert not (pm_site_packages / install_module.STARTUP_PTH_NAME).exists()


def test_project_metadata_declares_directory_plugin_and_entrypoint_support() -> None:
    """Repository metadata should advertise Hermes-supported plugin loading modes."""
    repo_root = Path(__file__).resolve().parents[1]
    pyproject_data = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    entrypoints = pyproject_data["project"]["entry-points"]["hermes_agent.plugins"]

    assert entrypoints["hermes_feishu_plugin"] == "hermes_feishu_plugin.plugin"
    assert "pytest>=8.0" in pyproject_data["project"]["optional-dependencies"]["test"]

    plugin_yaml = (repo_root / "plugin.yaml").read_text(encoding="utf-8")
    assert "provides_hooks:" in plugin_yaml
    for hook_name in ("pre_llm_call", "pre_tool_call", "post_tool_call"):
        assert f"  - {hook_name}" in plugin_yaml
