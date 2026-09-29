"""Install helpers for the Hermes Feishu plugin."""

from __future__ import annotations

from pathlib import Path
import logging
import shutil
import site

logger = logging.getLogger(__name__)

PLUGIN_LINK_NAME = "hermes_feishu_plugin"
LEGACY_LINK_NAMES = ("hermes-feishu-plugin",)
LEGACY_PLUGIN_DIR_NAMES = ("runtime_patches",)
STARTUP_PTH_NAME = "hermes_feishu_plugin_startup.pth"
SITECUSTOMIZE_NAME = "sitecustomize.py"
# The loader may execute in an env where this package is not importable (the
# .pth lands in a site-packages, the package lives in the plugin checkout), so
# the src dir is injected into sys.path before the import. It is resolved from
# the plugin root at write time — a hardcoded ~/.hermes/plugins/<name> breaks
# for a renamed link, a profile-scoped install, or a checkout kept elsewhere.
# Environments that never serve the gateway: a loader file there only executed
# plugin code inside unrelated interpreters (one ModuleNotFoundError per start).
_NON_GATEWAY_ENV_FRAGMENTS = (".hermes/tools", ".hermes/installs")


def _hermes_venv_lib() -> Path:
    """The venv that runs the gateway. Resolved per call: HOME is patchable."""
    return Path.home() / ".hermes" / "hermes-agent" / "venv" / "lib"


def _startup_import_line(plugin_root: Path) -> str:
    """`.pth` / sitecustomize body that bootstraps the early patch loader."""
    src_dir = (plugin_root / "src").resolve()
    return (
        f"import sys; sys.path.insert(0, {str(src_dir)!r}); "
        "import hermes_feishu_plugin.startup\n"
    )
INSTALL_IGNORE_PATTERNS = (
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".pytest_tmp",
    "dist",
    "build",
    "*.egg-info",
    "docs",
)


def _resolve_plugin_root() -> Path:
    """Return the repository root for directory-plugin symlink installs."""
    return Path(__file__).resolve().parents[2]


def _iter_plugin_dirs(root: Path) -> list[tuple[str, Path]]:
    plugin_dirs: list[tuple[str, Path]] = [("root", root / "plugins")]
    profiles_root = root / "profiles"
    if not profiles_root.exists():
        return plugin_dirs

    for profile_dir in sorted(path for path in profiles_root.iterdir() if path.is_dir()):
        plugin_dirs.append((profile_dir.name, profile_dir / "plugins"))
    return plugin_dirs


def _remove_legacy_links(plugins_dir: Path, plugin_dir: Path, plugin_name: str) -> None:
    for legacy_name in LEGACY_LINK_NAMES:
        if legacy_name == plugin_name:
            continue
        legacy_path = plugins_dir / legacy_name
        if not legacy_path.exists():
            continue
        if legacy_path.is_symlink():
            try:
                if legacy_path.resolve() == plugin_dir:
                    legacy_path.unlink()
                    continue
            except OSError:
                legacy_path.unlink()
                continue
        if legacy_path.is_dir():
            shutil.rmtree(legacy_path)
            continue
        legacy_path.unlink()


def _remove_legacy_plugin_dirs(plugins_dir: Path) -> None:
    """Remove superseded local plugin directories from the plugin root."""
    for legacy_name in LEGACY_PLUGIN_DIR_NAMES:
        legacy_path = plugins_dir / legacy_name
        if legacy_path.is_symlink():
            legacy_path.unlink()
            continue
        if legacy_path.is_dir():
            shutil.rmtree(legacy_path)


def _iter_site_package_dirs() -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen or not path.exists():
            return
        seen.add(resolved)
        paths.append(path)

    for raw_path in site.getsitepackages():
        path = Path(raw_path)
        if not any(fragment in path.as_posix() for fragment in _NON_GATEWAY_ENV_FRAGMENTS):
            add(path)

    hermes_venv_lib = _hermes_venv_lib()
    if hermes_venv_lib.exists():
        for path in sorted(hermes_venv_lib.glob("python*/site-packages")):
            add(path)

    return paths


def _remove_stale_startup_loaders(keep: list[Path]) -> list[str]:
    """Delete our loader from envs that no longer get one.

    Only a file recognisably ours (it imports the startup module) is touched, so
    an unrelated file of the same name is never removed.
    """
    keep_keys = {path.resolve() for path in keep}
    home = Path.home() / ".hermes"
    candidates: list[Path] = []
    for root, patterns in (
        (home / "tools", ("*/lib/python*/site-packages",)),
        (home / "installs", ("*/environments/*/venv/lib/python*/site-packages",)),
    ):
        if not root.exists():
            continue
        for pattern in patterns:
            candidates.extend(root.glob(pattern))

    removed: list[str] = []
    for site_dir in candidates:
        pth_path = site_dir / STARTUP_PTH_NAME
        try:
            if not pth_path.is_file() or pth_path.parent.resolve() in keep_keys:
                continue
            if "hermes_feishu_plugin.startup" in pth_path.read_text(encoding="utf-8", errors="replace"):
                pth_path.unlink()
                removed.append(str(pth_path))
        except OSError:  # an unreadable env is not fatal
            logger.debug("hermes_feishu_plugin: could not clean up %s", pth_path)
    return removed


def _write_startup_loader(plugins_root: Path, plugin_root: Path) -> list[str]:
    import_line = _startup_import_line(plugin_root)
    synced: list[str] = []
    sitecustomize_path = plugins_root / SITECUSTOMIZE_NAME
    sitecustomize_path.write_text(import_line, encoding="utf-8")
    synced.append(str(sitecustomize_path))

    keep = _iter_site_package_dirs()
    for site_dir in keep:
        pth_path = site_dir / STARTUP_PTH_NAME
        pth_path.write_text(import_line, encoding="utf-8")
        synced.append(str(pth_path))

    synced.extend(_remove_stale_startup_loaders(keep))
    return synced


def _copy_plugin_dir(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*INSTALL_IGNORE_PATTERNS),
    )


def _create_plugin_link(plugins_dir: Path, plugin_dir: Path, plugin_name: str) -> Path:
    link_path = plugins_dir / plugin_name
    try:
        link_path.symlink_to(plugin_dir, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 1314:
            raise
        _copy_plugin_dir(plugin_dir, link_path)
    return link_path


def _same_location(a: Path, b: Path) -> bool:
    """True when both paths denote the same place (symlink-loop safe)."""
    try:
        return a.resolve() == b.resolve()
    except OSError:  # ELOOP from an existing self-referential link
        return False


def sync_profile_plugin_links(*, plugin_name: str = PLUGIN_LINK_NAME) -> list[str]:
    """Ensure the plugin is linked into root and profile plugin directories.

    Nothing here may delete a real plugin directory. ``hermes plugins install``
    puts a git clone AT ``<plugins>/<name>`` and imports the plugin from it, so
    ``plugin_dir`` IS ``link_path``; the earlier unconditional
    ``shutil.rmtree(link_path)`` then deleted the install and replaced it with a
    symlink pointing at itself (an ELOOP that breaks every later access).
    """
    plugin_dir = _resolve_plugin_root()
    root = Path.home() / ".hermes"
    synced: list[str] = []

    for scope, plugins_dir in _iter_plugin_dirs(root):
        plugins_dir.mkdir(parents=True, exist_ok=True)
        _remove_legacy_links(plugins_dir, plugin_dir, plugin_name)
        _remove_legacy_plugin_dirs(plugins_dir)

        link_path = plugins_dir / plugin_name

        # Already in place — either the directory plugin itself (plugin_dir IS
        # link_path) or the symlink this function created on an earlier run.
        if _same_location(link_path, plugin_dir):
            synced.append(scope)
            continue

        if link_path.is_symlink():
            link_path.unlink()

        if link_path.exists():
            # A real directory under someone else's name is not ours to delete:
            # Hermes's installer owns this path. Refuse loudly instead of
            # destroying an install.
            logger.warning(
                "hermes_feishu_plugin: %s exists as a real directory; leaving it "
                "untouched (remove it manually to let the plugin link %s)",
                link_path, plugin_dir,
            )
            continue

        _create_plugin_link(plugins_dir, plugin_dir, plugin_name)
        synced.append(scope)

    _write_startup_loader(root / "plugins", plugin_dir)
    return synced


def main() -> None:
    """Link the plugin into all Hermes profile plugin directories."""
    for scope in sync_profile_plugin_links():
        print(scope)
