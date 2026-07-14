from __future__ import annotations

import logging
from pathlib import Path

import pytest

from league_skin_manager.atomic import read_json
from league_skin_manager.config import APP_NAME, AppPaths, RuntimeConfig
from league_skin_manager.logging_setup import configure_logging


def test_paths_are_side_effect_free_until_ensured(tmp_path: Path) -> None:
    local_appdata = tmp_path / "local"
    project = tmp_path / "project"
    paths = AppPaths.discover(local_appdata=local_appdata, project_root=project)

    assert paths.data_dir == local_appdata.resolve() / APP_NAME
    assert paths.package_dir == paths.library_dir / "packages"
    assert paths.default_profile_file == paths.profile_dir / "default.json"
    assert paths.library_manifest_file == paths.library_dir / "library.json"
    assert not paths.data_dir.exists()

    paths.ensure()
    assert paths.package_dir.is_dir()
    assert paths.profile_dir.is_dir()
    assert paths.engine_state_dir.is_dir()
    assert paths.overlay_dir.is_dir()
    assert paths.cache_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_runtime_config_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        RuntimeConfig(process_poll_seconds=0)
    with pytest.raises(ValueError, match="positive"):
        RuntimeConfig(engine_timeout_seconds=0)
    with pytest.raises(ValueError, match="positive"):
        RuntimeConfig(overlay_timeout_seconds=0)


def test_read_json_uses_fallback_for_malformed_content(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")

    assert read_json(path, {"fallback": True}) == {"fallback": True}


def test_logging_configuration_is_idempotent(tmp_path: Path) -> None:
    logger = logging.getLogger("league_skin_manager")
    logger.handlers.clear()
    first = configure_logging(tmp_path)
    second = configure_logging(tmp_path)
    assert first is second
    assert len(first.handlers) == 2
