from __future__ import annotations

import logging
import sys
from pathlib import Path
from threading import Event
from typing import Any, cast

import league_skin_manager.main as app_main
from league_skin_manager.config import AppPaths
from league_skin_manager.controller import AppController


class NoopActivationEvent:
    def create(self) -> bool:
        return True

    def signal(self) -> bool:
        return True

    def wait(self, _timeout_milliseconds: int) -> bool:
        return False

    def close(self) -> None:
        return None


def test_wait_for_workers_retries_without_releasing_ownership() -> None:
    class Controller:
        def __init__(self) -> None:
            self.results = iter((False, False, True))
            self.calls: list[float] = []

        def shutdown(self, timeout_seconds: float) -> bool:
            self.calls.append(timeout_seconds)
            return next(self.results)

    controller = Controller()

    app_main._wait_for_workers(
        cast(AppController, controller),
        0.25,
        logging.getLogger("test.main"),
    )

    assert controller.calls == [0.25, 0.25, 0.25]


def test_second_instance_signals_existing_desktop_without_launching_manager(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Paths:
        log_dir = tmp_path / "logs"
        provider_dir = tmp_path / "LTK Manager"

        @staticmethod
        def ensure() -> None:
            return None

    class Mutex:
        def acquire(self) -> bool:
            return False

    launches: list[Path] = []
    discoveries: list[bool] = []
    activations: list[str] = []

    class ActivationEvent(NoopActivationEvent):
        def create(self) -> bool:
            activations.append("create")
            return True

        def signal(self) -> bool:
            activations.append("signal")
            return True

        def close(self) -> None:
            activations.append("close")

    class Launcher:
        def __init__(self, _logger: logging.Logger) -> None:
            return None

        def launch(self, executable: Path) -> bool:
            launches.append(executable)
            return True

    def discover() -> Paths:
        discoveries.append(True)
        return Paths()

    monkeypatch.setattr(AppPaths, "discover", staticmethod(discover))
    monkeypatch.setattr(app_main, "configure_logging", lambda _path: logging.getLogger("test"))
    monkeypatch.setattr(app_main, "SingleInstanceMutex", Mutex)
    monkeypatch.setattr(app_main, "InstanceActivationEvent", ActivationEvent)
    monkeypatch.setattr(app_main, "ProcessLauncher", Launcher)
    monkeypatch.setattr(sys, "platform", "win32")

    assert app_main.run() == 2
    assert launches == []
    assert discoveries == []
    assert activations == ["create", "signal", "close"]


def test_activation_listener_forwards_signal_and_stops() -> None:
    stop = Event()
    waits: list[int] = []
    activations: list[bool] = []

    class ActivationEvent(NoopActivationEvent):
        def wait(self, timeout_milliseconds: int) -> bool:
            waits.append(timeout_milliseconds)
            return True

    def activate() -> None:
        activations.append(True)
        stop.set()

    app_main._listen_for_activation(
        cast(Any, ActivationEvent()),
        stop,
        activate,
        logging.getLogger("test.activation"),
    )

    assert waits == [250]
    assert activations == [True]


def test_activation_listener_logs_callback_and_wait_failures(caplog: Any) -> None:
    waits = 0

    class ActivationEvent(NoopActivationEvent):
        def wait(self, _timeout_milliseconds: int) -> bool:
            nonlocal waits
            waits += 1
            if waits == 1:
                return True
            raise OSError("event unavailable")

    def fail_activation() -> None:
        raise RuntimeError("window unavailable")

    with caplog.at_level(logging.ERROR):
        app_main._listen_for_activation(
            cast(Any, ActivationEvent()),
            Event(),
            fail_activation,
            logging.getLogger("test.activation.failure"),
        )

    assert waits == 2
    assert "Could not show the desktop" in caplog.text
    assert "activation listener failed" in caplog.text


def test_composition_failure_closes_created_resources_and_releases_mutex(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Paths:
        project_root = tmp_path
        data_dir = tmp_path / "data"
        log_dir = tmp_path / "logs"
        package_dir = tmp_path / "library" / "packages"
        library_manifest_file = tmp_path / "library" / "library.json"

        @staticmethod
        def ensure() -> None:
            return None

    released: list[bool] = []
    visible_errors: list[tuple[str, Path | None]] = []

    class Mutex:
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            released.append(True)

    class Launcher:
        def __init__(self, _logger: logging.Logger) -> None:
            return None

    monkeypatch.setattr(AppPaths, "discover", staticmethod(lambda: Paths()))
    monkeypatch.setattr(app_main, "configure_logging", lambda _path: logging.getLogger("test"))
    monkeypatch.setattr(app_main, "SingleInstanceMutex", Mutex)
    monkeypatch.setattr(app_main, "InstanceActivationEvent", NoopActivationEvent)
    monkeypatch.setattr(app_main, "ProcessLauncher", Launcher)
    monkeypatch.setattr(
        app_main,
        "_show_startup_error",
        lambda error, log_file: visible_errors.append((str(error), log_file)),
    )
    monkeypatch.setattr(
        app_main,
        "LocalModLibrary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("composition failed")),
    )
    monkeypatch.setattr(sys, "platform", "win32")

    assert app_main.run() == 1
    assert released == [True]
    assert visible_errors == [
        ("composition failed", tmp_path / "logs" / "LeagueSkinManagerLTK.log")
    ]
