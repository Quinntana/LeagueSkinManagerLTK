from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from league_skin_manager.config import APP_NAME
from league_skin_manager.installation import (
    UNINSTALL_REGISTRY_KEY,
    AppsAndFeaturesRegistration,
    InstallationError,
    InstallLayout,
    apps_entry_values,
    create_start_menu_shortcut,
    installed_size_kib,
    quote_command,
    remove_start_menu_shortcut,
    start_menu_shortcut_path,
)


def test_layout_is_exactly_under_local_programs(tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path / "Local AppData")

    assert layout.install_dir == (tmp_path / "Local AppData").resolve() / "Programs" / APP_NAME
    assert layout.validated_install_dir() == layout.install_dir

    unsafe = InstallLayout(
        local_appdata=layout.local_appdata,
        install_dir=layout.local_appdata / APP_NAME,
        executable=layout.executable,
        uninstaller=layout.uninstaller,
    )
    with pytest.raises(InstallationError, match="outside"):
        unsafe.validated_install_dir()


def test_apps_entry_has_professional_per_user_values(tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path)

    values = apps_entry_values(
        layout,
        estimated_size_kib=12345,
        install_date=date(2026, 7, 13),
    )

    assert values["DisplayName"] == ("League Skin Manager LTK", "str")
    assert values["DisplayVersion"] == ("0.1.0", "str")
    assert values["Publisher"] == ("Quinntana", "str")
    assert values["InstallLocation"] == (str(layout.install_dir), "str")
    assert values["UninstallString"] == (f'"{layout.uninstaller.resolve()}"', "str")
    assert values["EstimatedSize"] == (12345, "dword")
    assert values["InstallDate"] == ("20260713", "str")
    assert values["NoModify"] == (1, "dword")
    assert values["NoRepair"] == (1, "dword")


def test_quote_command_rejects_injectable_arguments(tmp_path: Path) -> None:
    assert quote_command(tmp_path / "app.exe", "--background").endswith('app.exe" --background')
    with pytest.raises(ValueError, match="quotes"):
        quote_command(tmp_path / "app.exe", 'bad"argument')


class FakeKey:
    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 1
    KEY_SET_VALUE = 2
    REG_SZ = "REG_SZ"
    REG_DWORD = "REG_DWORD"

    def __init__(
        self,
        *,
        initial: dict[str, tuple[object, object]] | None = None,
        key_exists: bool = False,
        fail_once_on: str | None = None,
    ) -> None:
        self.created: list[tuple[object, str, int, int]] = []
        self.values = dict(initial or {})
        self.key_exists = key_exists or initial is not None
        self.fail_once_on = fail_once_on
        self.deleted: list[tuple[object, str]] = []

    def CreateKeyEx(self, *args: Any) -> FakeKey:
        self.created.append(args)
        self.key_exists = True
        return FakeKey()

    def OpenKey(self, *_args: Any) -> FakeKey:
        if not self.key_exists:
            raise FileNotFoundError
        return FakeKey()

    def EnumValue(self, _key: FakeKey, index: int) -> tuple[str, object, object]:
        try:
            name, (kind, value) = tuple(self.values.items())[index]
        except IndexError as exc:
            raise OSError(259, "No more data") from exc
        return name, value, kind

    def SetValueEx(
        self,
        _key: FakeKey,
        name: str,
        _reserved: int,
        kind: object,
        value: object,
    ) -> None:
        if name == self.fail_once_on:
            self.fail_once_on = None
            raise PermissionError("registry denied")
        self.values[name] = (kind, value)

    def DeleteValue(self, _key: FakeKey, name: str) -> None:
        try:
            del self.values[name]
        except KeyError as exc:
            raise FileNotFoundError from exc

    def DeleteKey(self, root: object, path: str) -> None:
        if not self.key_exists:
            raise FileNotFoundError
        self.deleted.append((root, path))
        self.key_exists = False
        self.values.clear()


def test_registration_writes_and_removes_only_hkcu(tmp_path: Path) -> None:
    registry = FakeRegistry()
    registration = AppsAndFeaturesRegistration(registry)
    layout = InstallLayout.discover(tmp_path)

    registration.register(layout, estimated_size_kib=20)
    assert registry.created == [(registry.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY, 0, 3)]
    assert registry.values["DisplayName"] == (registry.REG_SZ, "League Skin Manager LTK")
    assert registry.values["EstimatedSize"] == (registry.REG_DWORD, 20)

    assert registration.unregister()
    assert registry.deleted == [(registry.HKEY_CURRENT_USER, UNINSTALL_REGISTRY_KEY)]


def test_registration_failure_deletes_new_partial_key(tmp_path: Path) -> None:
    registry = FakeRegistry(fail_once_on="Publisher")
    registration = AppsAndFeaturesRegistration(registry)

    with pytest.raises(PermissionError, match="registry denied"):
        registration.register(InstallLayout.discover(tmp_path), estimated_size_kib=20)

    assert not registry.key_exists
    assert registry.values == {}


def test_registration_failure_restores_existing_values(tmp_path: Path) -> None:
    existing = {
        "DisplayName": (FakeRegistry.REG_SZ, "Previous name"),
        "CustomValue": (FakeRegistry.REG_DWORD, 7),
    }
    registry = FakeRegistry(initial=existing, fail_once_on="Publisher")
    registration = AppsAndFeaturesRegistration(registry)

    with pytest.raises(PermissionError, match="registry denied"):
        registration.register(InstallLayout.discover(tmp_path), estimated_size_kib=20)

    assert registry.key_exists
    assert registry.values == existing


def test_layout_rejects_executable_outside_exact_install_children(tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path)
    unsafe = InstallLayout(
        local_appdata=layout.local_appdata,
        install_dir=layout.install_dir,
        executable=tmp_path / f"{APP_NAME}.exe",
        uninstaller=layout.uninstaller,
    )

    with pytest.raises(InstallationError, match="executable is outside"):
        unsafe.validated_install_dir()


def test_installed_size_rounds_up_and_rejects_missing_payload(tmp_path: Path) -> None:
    first = tmp_path / "first.exe"
    second = tmp_path / "second.exe"
    first.write_bytes(b"a" * 1024)
    second.write_bytes(b"b")

    assert installed_size_kib((first, second)) == 2
    with pytest.raises(InstallationError, match="measure"):
        installed_size_kib((tmp_path / "missing.exe",))


def test_start_menu_shortcut_is_exact_and_removable(monkeypatch: Any, tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"app")
    layout.uninstaller.write_bytes(b"uninstall")
    appdata = tmp_path / "Roaming"
    system_root = tmp_path / "Windows"
    powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.write_bytes(b"powershell")
    monkeypatch.setenv("SYSTEMROOT", str(system_root))
    calls: list[dict[str, object]] = []

    def create_fixture(_command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        Path(str(environment["LSMLTK_SHORTCUT_PATH"])).write_bytes(b"shell-link")
        return SimpleNamespace(returncode=0, stderr="")

    shortcut = create_start_menu_shortcut(
        layout,
        appdata=appdata,
        runner=create_fixture,
    )

    assert shortcut == start_menu_shortcut_path(appdata)
    environment = calls[0]["env"]
    assert isinstance(environment, dict)
    assert environment["LSMLTK_SHORTCUT_TARGET"] == str(layout.executable.resolve())
    assert remove_start_menu_shortcut(appdata) is True
    assert remove_start_menu_shortcut(appdata) is False
