"""Native desktop presentation for a searchable local LTK mod library."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, cast

from .catalog import CatalogError, CatalogSnapshot, ModRecord, load_catalog
from .controller import AppState

Action = Callable[[], object]
StartupGetter = Callable[[], bool]
StartupSetter = Callable[[bool], object]
PathOpener = Callable[[Path], object]
CatalogLoader = Callable[[Path], CatalogSnapshot]
PackageImporter = Callable[[tuple[Path, ...]], object]
PackageSelector = Callable[[], tuple[Path, ...]]
EnabledIdsGetter = Callable[[], Collection[str]]
ModToggle = Callable[[str], bool]
GameDirGetter = Callable[[], Path | None]
GameDirSetter = Callable[[Path], Path]
DirectorySelector = Callable[[], Path | None]


def format_package_size(value: int) -> str:
    if value < 0:
        raise ValueError("package size cannot be negative")
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


class DesktopApplication:
    """Tk/ttk window whose public methods are safe from worker threads."""

    ALL_CATEGORIES = "All categories"
    POLL_MILLISECONDS = 60
    FILTER_DEBOUNCE_MILLISECONDS = 120

    def __init__(
        self,
        *,
        catalog_path: Path,
        package_dir: Path,
        data_dir: Path,
        log_file: Path,
        on_refresh: Action,
        on_import: PackageImporter,
        on_start_manager: Action,
        on_exit: Action,
        startup_enabled: StartupGetter,
        set_startup_enabled: StartupSetter,
        path_opener: PathOpener,
        enabled_ids: EnabledIdsGetter | None = None,
        on_toggle_mod: ModToggle | None = None,
        on_start_patcher: Action | None = None,
        on_stop_patcher: Action | None = None,
        game_dir: GameDirGetter | None = None,
        set_game_dir: GameDirSetter | None = None,
        directory_selector: DirectorySelector | None = None,
        package_revealer: PathOpener | None = None,
        package_selector: PackageSelector | None = None,
        catalog_loader: CatalogLoader = load_catalog,
        logger: logging.Logger | None = None,
    ) -> None:
        self._catalog_path = catalog_path
        self._package_dir = package_dir
        self._data_dir = data_dir
        self._log_file = log_file
        self._on_refresh = on_refresh
        self._on_import = on_import
        self._on_start_manager = on_start_manager
        self._on_exit = on_exit
        self._startup_enabled = startup_enabled
        self._set_startup_enabled = set_startup_enabled
        self._path_opener = path_opener
        self._enabled_ids_getter = enabled_ids or (lambda: ())
        self._on_toggle_mod = on_toggle_mod or (lambda _content_id: False)
        self._on_start_patcher = on_start_patcher or (lambda: False)
        self._on_stop_patcher = on_stop_patcher or (lambda: False)
        self._game_dir_getter = game_dir or (lambda: None)
        self._set_game_dir = set_game_dir or (lambda selected: selected)
        self._directory_selector = directory_selector or self._select_game_directory
        self._package_revealer = package_revealer or path_opener
        self._package_selector = package_selector or self._select_packages
        self._catalog_loader = catalog_loader
        self._logger = logger or logging.getLogger(__name__)

        self._events: Queue[tuple[str, object | None]] = Queue()
        self._catalog = CatalogSnapshot(())
        self._root: Any | None = None
        self._tree: Any | None = None
        self._search_var: Any | None = None
        self._champion_var: Any | None = None
        self._result_var: Any | None = None
        self._status_var: Any | None = None
        self._stats_var: Any | None = None
        self._detail_var: Any | None = None
        self._startup_var: Any | None = None
        self._game_dir_var: Any | None = None
        self._champion_box: Any | None = None
        self._start_button: Any | None = None
        self._stop_button: Any | None = None
        self._toggle_button: Any | None = None
        self._game_dir_button: Any | None = None
        self._filter_after_id: str | None = None
        self._rows: dict[str, ModRecord] = {}
        self._enabled_ids: set[str] = set()
        self._game_dir_value: Path | None = None
        self._runtime_running = False
        self._runtime_pending: str | None = None
        self._sort_column = "name"
        self._sort_descending = False
        self._exit_pending = False
        self._exit_thread: Thread | None = None

    def run(self, *, show_on_start: bool = True) -> None:
        """Create the native window and enter its main loop on this thread."""

        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        self._root = root
        self._build_window(root, tk, ttk)
        self._load_catalog_now()
        if show_on_start:
            root.deiconify()
            root.lift()
        else:
            root.withdraw()
        root.after(self.POLL_MILLISECONDS, self._drain_events)
        root.mainloop()

    def show(self) -> None:
        self._events.put(("show", None))

    def hide(self) -> None:
        self._events.put(("hide", None))

    def stop(self) -> None:
        self._events.put(("stop", None))

    def refresh_catalog(self) -> None:
        self._events.put(("refresh", None))

    def update_status(self, state: AppState, detail: str) -> None:
        self._events.put(("status", (state, detail)))

    def update_runtime_status(self, detail: str, running: bool) -> None:
        """Queue a patcher lifecycle update for the Tk thread."""

        self._events.put(("runtime_status", (detail, running)))

    def _build_window(self, root: Any, tk: Any, ttk: Any) -> None:
        root.title("League Skin Manager LTK")
        root.geometry("1120x720")
        root.minsize(880, 560)
        root.configure(background="#0b1220")
        root.protocol("WM_DELETE_WINDOW", self._hide_now)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#0b1220")
        style.configure("Panel.TFrame", background="#111c2e")
        style.configure(
            "Title.TLabel",
            background="#0b1220",
            foreground="#f8fafc",
            font=("Segoe UI Semibold", 22),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#0b1220",
            foreground="#94a3b8",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Panel.TLabel",
            background="#111c2e",
            foreground="#dbeafe",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Status.TLabel",
            background="#111c2e",
            foreground="#60a5fa",
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "Accent.TButton",
            background="#2563eb",
            foreground="#ffffff",
            borderwidth=0,
            padding=(14, 9),
            font=("Segoe UI Semibold", 10),
        )
        style.map("Accent.TButton", background=[("active", "#3b82f6")])
        style.configure(
            "Secondary.TButton",
            background="#1e293b",
            foreground="#e2e8f0",
            borderwidth=0,
            padding=(12, 8),
            font=("Segoe UI", 9),
        )
        style.map("Secondary.TButton", background=[("active", "#334155")])
        style.configure(
            "Treeview",
            background="#111c2e",
            fieldbackground="#111c2e",
            foreground="#e2e8f0",
            rowheight=29,
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#1e293b",
            foreground="#cbd5e1",
            relief="flat",
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Treeview",
            background=[("selected", "#1d4ed8")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Dark.TEntry",
            fieldbackground="#0f172a",
            foreground="#f8fafc",
            insertcolor="#f8fafc",
            bordercolor="#334155",
            padding=9,
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#0f172a",
            foreground="#f8fafc",
            arrowcolor="#94a3b8",
            bordercolor="#334155",
            padding=7,
        )
        style.configure(
            "Dark.TCheckbutton",
            background="#111c2e",
            foreground="#cbd5e1",
            font=("Segoe UI", 9),
        )

        outer = ttk.Frame(root, style="App.TFrame", padding=24)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        title_group = ttk.Frame(header, style="App.TFrame")
        title_group.pack(side="left", fill="x", expand=True)
        ttk.Label(title_group, text="LTK Mod Library", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_group,
            text=(
                "Search authorized custom mods while the open engine stays independent of this UI."
            ),
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        actions = ttk.Frame(header, style="App.TFrame")
        actions.pack(side="right")
        ttk.Button(
            actions,
            text="Refresh library",
            style="Secondary.TButton",
            command=self._refresh_clicked,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions,
            text="Import mods…",
            style="Secondary.TButton",
            command=self._import_clicked,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions,
            text="Open LTK Manager",
            style="Accent.TButton",
            command=self._manager_clicked,
        ).pack(side="left")

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        panel.pack(fill="both", expand=True)

        self._status_var = tk.StringVar(value="Starting")
        self._stats_var = tk.StringVar(value="Loading local library…")
        status_row = ttk.Frame(panel, style="Panel.TFrame")
        status_row.pack(fill="x", pady=(0, 14))
        ttk.Label(status_row, textvariable=self._status_var, style="Status.TLabel").pack(
            side="left"
        )
        ttk.Label(status_row, textvariable=self._stats_var, style="Panel.TLabel").pack(side="right")

        runtime_actions = ttk.Frame(panel, style="Panel.TFrame")
        runtime_actions.pack(fill="x", pady=(0, 12))
        self._start_button = ttk.Button(
            runtime_actions,
            text="Start enabled mods",
            style="Accent.TButton",
            command=self._start_patcher_clicked,
        )
        self._start_button.pack(side="left", padx=(0, 8))
        self._stop_button = ttk.Button(
            runtime_actions,
            text="Stop",
            style="Secondary.TButton",
            command=self._stop_patcher_clicked,
        )
        self._stop_button.pack(side="left")
        ttk.Label(
            runtime_actions,
            text="The patcher uses only mods enabled in the default profile.",
            style="Panel.TLabel",
        ).pack(side="left", padx=(12, 0))

        filters = ttk.Frame(panel, style="Panel.TFrame")
        filters.pack(fill="x", pady=(0, 12))
        self._search_var = tk.StringVar()
        search = ttk.Entry(
            filters,
            textvariable=self._search_var,
            style="Dark.TEntry",
            font=("Segoe UI", 10),
        )
        search.pack(side="left", fill="x", expand=True, padx=(0, 10))
        search.insert(0, "")
        self._search_var.trace_add("write", self._filter_changed)
        self._champion_var = tk.StringVar(value=self.ALL_CATEGORIES)
        self._champion_box = ttk.Combobox(
            filters,
            textvariable=self._champion_var,
            state="readonly",
            width=25,
            style="Dark.TCombobox",
        )
        self._champion_box.pack(side="left")
        self._champion_box.bind("<<ComboboxSelected>>", self._filter_changed)

        table_frame = ttk.Frame(panel, style="Panel.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("enabled", "name", "author", "format", "size")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        self._tree = tree
        tree.heading("enabled", text="Enabled", command=lambda: self._sort_by("enabled"))
        tree.heading("name", text="Mod", command=lambda: self._sort_by("name"))
        tree.heading("author", text="Author", command=lambda: self._sort_by("author"))
        tree.heading("format", text="Format", command=lambda: self._sort_by("format"))
        tree.heading("size", text="Package size", command=lambda: self._sort_by("size"))
        tree.column("enabled", width=85, minwidth=75, anchor="center")
        tree.column("name", width=390, minwidth=230, anchor="w")
        tree.column("author", width=210, minwidth=130, anchor="w")
        tree.column("format", width=100, minwidth=80, anchor="center")
        tree.column("size", width=130, minwidth=100, anchor="e")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        tree.bind("<<TreeviewSelect>>", self._selection_changed)

        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x", pady=(13, 0))
        info = ttk.Frame(footer, style="Panel.TFrame")
        info.pack(side="left", fill="x", expand=True)
        self._result_var = tk.StringVar(value="0 results")
        self._detail_var = tk.StringVar(value="Select a mod to view its package.")
        ttk.Label(info, textvariable=self._result_var, style="Status.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self._detail_var, style="Panel.TLabel").pack(
            anchor="w", pady=(3, 0)
        )
        footer_actions = ttk.Frame(footer, style="Panel.TFrame")
        footer_actions.pack(side="right")
        self._toggle_button = ttk.Button(
            footer_actions,
            text="Toggle selected",
            style="Accent.TButton",
            command=self._toggle_selected,
        )
        self._toggle_button.pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="Show selected package",
            style="Secondary.TButton",
            command=self._open_selected,
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="App data",
            style="Secondary.TButton",
            command=lambda: self._open_path(self._data_dir),
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="Logs",
            style="Secondary.TButton",
            command=lambda: self._open_path(self._log_file),
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="Refresh",
            style="Secondary.TButton",
            command=self._load_catalog_now,
        ).pack(side="left")

        settings = ttk.Frame(outer, style="App.TFrame")
        settings.pack(fill="x", pady=(12, 0))
        game_settings = ttk.Frame(settings, style="App.TFrame")
        game_settings.pack(fill="x", pady=(0, 8))
        ttk.Label(
            game_settings,
            text="League Game directory:",
            style="Subtitle.TLabel",
        ).pack(side="left")
        self._game_dir_var = tk.StringVar(value="Not configured")
        ttk.Label(
            game_settings,
            textvariable=self._game_dir_var,
            style="Subtitle.TLabel",
        ).pack(side="left", fill="x", expand=True, padx=(8, 10))
        self._game_dir_button = ttk.Button(
            game_settings,
            text="Choose directory",
            style="Secondary.TButton",
            command=self._choose_game_dir,
        )
        self._game_dir_button.pack(side="right")
        startup_settings = ttk.Frame(settings, style="App.TFrame")
        startup_settings.pack(fill="x")
        try:
            startup_value = bool(self._startup_enabled())
        except Exception:
            self._logger.exception("Unable to read Start with Windows setting")
            startup_value = False
        self._startup_var = tk.BooleanVar(value=startup_value)
        ttk.Checkbutton(
            startup_settings,
            text="Start with Windows in the background",
            variable=self._startup_var,
            command=self._startup_clicked,
            style="Dark.TCheckbutton",
        ).pack(side="left")
        ttk.Button(
            startup_settings,
            text="Exit application",
            style="Secondary.TButton",
            command=self._exit_clicked,
        ).pack(side="right")
        self._load_game_dir_setting()
        self._update_runtime_controls()
        self._update_selection_controls()

    def _drain_events(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "show":
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                elif kind == "hide":
                    root.withdraw()
                elif kind == "stop":
                    root.destroy()
                    return
                elif kind == "refresh":
                    self._load_catalog_now()
                elif kind == "status":
                    state, detail = cast(tuple[AppState, str], payload)
                    if self._status_var is not None:
                        self._status_var.set(detail)
                    if state in (AppState.READY, AppState.OFFLINE_READY):
                        self._load_catalog_now()
                elif kind == "runtime_status":
                    detail, running = cast(tuple[str, bool], payload)
                    self._runtime_running = running
                    self._runtime_pending = None
                    if self._status_var is not None:
                        self._status_var.set(detail)
                    self._update_runtime_controls()
                elif kind == "exit_complete":
                    self._exit_pending = False
                    self._exit_thread = None
                    if payload is True:
                        root.destroy()
                        return
                    if self._status_var is not None:
                        self._status_var.set("Background work is still stopping; try Exit again")
                elif kind == "exit_error":
                    self._exit_pending = False
                    self._exit_thread = None
                    if self._status_var is not None:
                        self._status_var.set(f"Could not exit: {payload}")
        except Empty:
            pass
        try:
            root.after(self.POLL_MILLISECONDS, self._drain_events)
        except Exception:
            return

    def _load_catalog_now(self) -> None:
        try:
            catalog = self._catalog_loader(self._catalog_path)
        except CatalogError as exc:
            self._logger.warning("Could not load desktop catalog: %s", exc)
            if self._status_var is not None:
                self._status_var.set(str(exc))
            return
        self._catalog = catalog
        if self._champion_box is not None:
            values = (self.ALL_CATEGORIES, *catalog.categories)
            self._champion_box.configure(values=values)
            current = self._champion_var.get() if self._champion_var is not None else ""
            if current not in values and self._champion_var is not None:
                self._champion_var.set(self.ALL_CATEGORIES)
        self._refresh_enabled_ids()
        self._update_library_stats()
        self._apply_filter()
        self._update_runtime_controls()

    def _refresh_enabled_ids(self) -> bool:
        try:
            self._enabled_ids = set(self._enabled_ids_getter())
        except Exception as exc:
            self._logger.exception("Could not read enabled mod profile")
            if self._status_var is not None:
                self._status_var.set(f"Could not read enabled mods: {exc}")
            return False
        return True

    def _enabled_count(self) -> int:
        known_ids = {mod.id for mod in self._catalog.mods}
        return len(self._enabled_ids & known_ids)

    def _update_library_stats(self) -> None:
        if self._stats_var is not None:
            self._stats_var.set(
                f"{self._enabled_count():,} enabled  •  {len(self._catalog.mods):,} mods  •  "
                f"{len(self._catalog.categories):,} categories  •  "
                f"{format_package_size(self._catalog.total_bytes)} on disk"
            )

    def _filter_changed(self, *_args: object) -> None:
        root = self._root
        if root is None:
            return
        if self._filter_after_id is not None:
            root.after_cancel(self._filter_after_id)
        self._filter_after_id = root.after(
            self.FILTER_DEBOUNCE_MILLISECONDS,
            self._apply_filter,
        )

    def _apply_filter(self) -> None:
        self._filter_after_id = None
        tree = self._tree
        if tree is None:
            return
        query = self._search_var.get() if self._search_var is not None else ""
        selected_champion = (
            self._champion_var.get() if self._champion_var is not None else self.ALL_CATEGORIES
        )
        category = None if selected_champion == self.ALL_CATEGORIES else selected_champion
        records = list(self._catalog.filtered(query, category))
        sorters: dict[str, Callable[[ModRecord], Any]] = {
            "enabled": lambda mod: (mod.id not in self._enabled_ids, mod.name.casefold()),
            "name": lambda mod: (mod.name.casefold(), mod.author.casefold()),
            "author": lambda mod: (mod.author.casefold(), mod.name.casefold()),
            "format": lambda mod: (mod.format, mod.name.casefold()),
            "size": lambda mod: mod.size,
        }
        records.sort(key=sorters[self._sort_column], reverse=self._sort_descending)

        selected_ids = set(tree.selection())
        children = tree.get_children()
        if children:
            tree.delete(*children)
        self._rows.clear()
        for mod in records:
            item_id = mod.id
            self._rows[item_id] = mod
            tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    "On" if mod.id in self._enabled_ids else "Off",
                    mod.name,
                    mod.author,
                    mod.format.upper(),
                    format_package_size(mod.size),
                ),
            )
        retained_selection = tuple(mod.id for mod in records if mod.id in selected_ids)
        if retained_selection:
            tree.selection_set(*retained_selection)
        if self._result_var is not None:
            self._result_var.set(
                f"{len(records):,} result{'s' if len(records) != 1 else ''}  •  "
                f"{self._enabled_count():,} enabled"
            )
        if self._detail_var is not None:
            self._detail_var.set("Select a mod to view its package.")
        self._update_selection_controls()

    def _sort_by(self, column: str) -> None:
        if column == self._sort_column:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_column = column
            self._sort_descending = False
        self._apply_filter()

    def _selection_changed(self, _event: object | None = None) -> None:
        selected = self._selected_mods()
        self._update_selection_controls()
        if not selected or self._detail_var is None:
            return
        if len(selected) > 1:
            enabled = sum(mod.id in self._enabled_ids for mod in selected)
            self._detail_var.set(f"{len(selected):,} mods selected • {enabled:,} currently enabled")
            return
        mod = selected[0]
        path = self._package_dir / mod.file_name
        self._detail_var.set(
            f"{mod.name} • {mod.author} • {mod.version} • {format_package_size(mod.size)} • {path}"
        )

    def _selected_mod(self) -> ModRecord | None:
        selected = self._selected_mods()
        return selected[0] if selected else None

    def _selected_mods(self) -> tuple[ModRecord, ...]:
        tree = self._tree
        if tree is None:
            return ()
        return tuple(
            mod for item in tree.selection() if (mod := self._rows.get(str(item))) is not None
        )

    def _update_selection_controls(self) -> None:
        if self._toggle_button is not None:
            state = "normal" if self._selected_mods() else "disabled"
            self._toggle_button.configure(state=state)

    def _toggle_selected(self) -> None:
        selected = self._selected_mods()
        if not selected:
            if self._status_var is not None:
                self._status_var.set("Select one or more mods first")
            return
        changed = 0
        for mod in selected:
            try:
                is_enabled = self._on_toggle_mod(mod.id)
            except Exception as exc:
                self._logger.exception("Could not toggle mod %s", mod.id)
                if self._status_var is not None:
                    self._status_var.set(f"Could not toggle {mod.name}: {exc}")
                self._refresh_enabled_ids()
                self._update_library_stats()
                self._apply_filter()
                self._update_runtime_controls()
                return
            if is_enabled:
                self._enabled_ids.add(mod.id)
            else:
                self._enabled_ids.discard(mod.id)
            changed += 1
        self._update_library_stats()
        self._apply_filter()
        self._update_runtime_controls()
        if self._status_var is not None:
            self._status_var.set(
                f"Updated {changed:,} mod{'s' if changed != 1 else ''} in the default profile"
            )

    def _open_selected(self) -> None:
        mod = self._selected_mod()
        if mod is None:
            if self._status_var is not None:
                self._status_var.set("Select a mod first")
            return
        path = self._package_dir / mod.file_name
        try:
            self._package_revealer(path)
        except Exception as exc:
            self._logger.exception("Could not reveal %s", path)
            if self._status_var is not None:
                self._status_var.set(f"Could not show {path.name}: {exc}")

    def _open_path(self, path: Path) -> None:
        try:
            self._path_opener(path)
        except Exception as exc:
            self._logger.exception("Could not open %s", path)
            if self._status_var is not None:
                self._status_var.set(f"Could not open {path.name}: {exc}")

    def _refresh_clicked(self) -> None:
        try:
            if self._on_refresh() is False and self._status_var is not None:
                self._status_var.set("Library refresh was not started")
        except Exception as exc:
            self._logger.exception("Desktop library refresh failed")
            if self._status_var is not None:
                self._status_var.set(f"Could not refresh library: {exc}")

    def _import_clicked(self) -> None:
        try:
            paths = self._package_selector()
            if paths and self._on_import(paths) is False and self._status_var is not None:
                self._status_var.set("Mod import was not started")
        except Exception as exc:
            self._logger.exception("Desktop package import failed")
            if self._status_var is not None:
                self._status_var.set(f"Could not import mods: {exc}")

    def _manager_clicked(self) -> None:
        try:
            if self._on_start_manager() is False and self._status_var is not None:
                self._status_var.set("LTK Manager could not be started")
        except Exception as exc:
            self._logger.exception("Desktop manager action failed")
            if self._status_var is not None:
                self._status_var.set(f"Could not start manager: {exc}")

    def _start_patcher_clicked(self) -> None:
        if self._runtime_running or self._runtime_pending is not None:
            return
        if self._enabled_count() == 0:
            if self._status_var is not None:
                self._status_var.set("Enable at least one mod before starting")
            return
        if self._game_dir_value is None:
            if self._status_var is not None:
                self._status_var.set("Choose the League Game directory before starting")
            return
        try:
            if self._on_start_patcher() is False:
                raise RuntimeError("the start request was rejected")
        except Exception as exc:
            self._logger.exception("Could not start the LTK patcher")
            if self._status_var is not None:
                self._status_var.set(f"Could not start patcher: {exc}")
            return
        self._runtime_pending = "start"
        if self._status_var is not None:
            self._status_var.set("Starting enabled mods…")
        self._update_runtime_controls()

    def _stop_patcher_clicked(self) -> None:
        if not self._runtime_running or self._runtime_pending is not None:
            return
        try:
            if self._on_stop_patcher() is False:
                raise RuntimeError("the stop request was rejected")
        except Exception as exc:
            self._logger.exception("Could not stop the LTK patcher")
            if self._status_var is not None:
                self._status_var.set(f"Could not stop patcher: {exc}")
            return
        self._runtime_pending = "stop"
        if self._status_var is not None:
            self._status_var.set("Stopping the LTK patcher…")
        self._update_runtime_controls()

    def _update_runtime_controls(self) -> None:
        pending = self._runtime_pending is not None
        can_start = (
            not self._runtime_running
            and not pending
            and self._enabled_count() > 0
            and self._game_dir_value is not None
        )
        can_stop = self._runtime_running and not pending
        if self._start_button is not None:
            self._start_button.configure(state="normal" if can_start else "disabled")
        if self._stop_button is not None:
            self._stop_button.configure(state="normal" if can_stop else "disabled")
        if self._game_dir_button is not None:
            self._game_dir_button.configure(
                state="disabled" if self._runtime_running or pending else "normal"
            )

    def _load_game_dir_setting(self) -> None:
        try:
            configured = self._game_dir_getter()
            self._game_dir_value = Path(configured) if configured is not None else None
        except Exception as exc:
            self._logger.exception("Could not read League Game directory")
            self._game_dir_value = None
            if self._status_var is not None:
                self._status_var.set(f"Could not read League Game directory: {exc}")
        self._update_game_dir_display()

    def _choose_game_dir(self) -> None:
        try:
            selected = self._directory_selector()
            if selected is None:
                return
            self._game_dir_value = Path(self._set_game_dir(Path(selected)))
        except Exception as exc:
            self._logger.exception("Could not update League Game directory")
            if self._status_var is not None:
                self._status_var.set(f"Could not set League Game directory: {exc}")
            return
        self._update_game_dir_display()
        self._update_runtime_controls()
        if self._status_var is not None:
            self._status_var.set("League Game directory updated")

    def _update_game_dir_display(self) -> None:
        if self._game_dir_var is not None:
            value = (
                str(self._game_dir_value) if self._game_dir_value is not None else "Not configured"
            )
            self._game_dir_var.set(value)

    def _startup_clicked(self) -> None:
        if self._startup_var is None:
            return
        desired = bool(self._startup_var.get())
        try:
            if self._set_startup_enabled(desired) is False:
                raise RuntimeError("the setting was rejected")
        except Exception as exc:
            self._logger.exception("Unable to update Start with Windows setting")
            self._startup_var.set(not desired)
            if self._status_var is not None:
                self._status_var.set(f"Could not update startup setting: {exc}")

    def _exit_clicked(self) -> None:
        if self._exit_pending:
            return
        self._exit_pending = True
        if self._status_var is not None:
            self._status_var.set("Stopping application…")
        worker = Thread(
            target=self._run_exit_request,
            name="desktop-shutdown-request",
            daemon=False,
        )
        self._exit_thread = worker
        try:
            worker.start()
        except Exception as exc:
            self._exit_pending = False
            self._exit_thread = None
            self._logger.exception("Could not start desktop shutdown worker")
            if self._status_var is not None:
                self._status_var.set(f"Could not exit: {exc}")

    def _run_exit_request(self) -> None:
        try:
            result = self._on_exit()
        except Exception as exc:
            self._logger.exception("Desktop exit action failed")
            self._events.put(("exit_error", str(exc)))
            return
        self._events.put(("exit_complete", result is not False))

    def _hide_now(self) -> None:
        if self._root is not None:
            self._root.withdraw()

    @staticmethod
    def _select_game_directory() -> Path | None:
        from tkinter import filedialog

        value = filedialog.askdirectory(
            title="Choose League of Legends or Game directory",
            mustexist=True,
        )
        return Path(value) if value else None

    @staticmethod
    def _select_packages() -> tuple[Path, ...]:
        from tkinter import filedialog

        values = filedialog.askopenfilenames(
            title="Import LTK mod packages",
            filetypes=(
                ("LTK mod packages", "*.modpkg *.fantome"),
                ("Mod package", "*.modpkg"),
                ("Fantome package", "*.fantome"),
            ),
        )
        return tuple(Path(value) for value in values)


__all__ = [
    "Action",
    "CatalogLoader",
    "DirectorySelector",
    "DesktopApplication",
    "EnabledIdsGetter",
    "GameDirGetter",
    "GameDirSetter",
    "ModToggle",
    "PathOpener",
    "PackageImporter",
    "PackageSelector",
    "StartupGetter",
    "StartupSetter",
    "format_package_size",
]
