"""Create a SQL Server container, and look at a container's log."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from replicate.docker.engine import free_port  # noqa: E402
from replicate.tasks import Progress, run_in_thread  # noqa: E402
from replicate.ui.context import CONTAINERS_CHANGED, SERVERS_CHANGED, AppContext  # noqa: E402
from replicate.ui.widgets.common import dialog_header, error_dialog  # noqa: E402


class AddContainerDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext):
        super().__init__(title="New SQL Server container", content_width=520)
        self.ctx = ctx
        view = Adw.ToolbarView()
        header, self.create_button = dialog_header(self, "Create", self._on_create)
        view.add_top_bar(header)
        page = Adw.PreferencesPage()
        view.set_content(page)
        self.set_child(view)

        group = Adw.PreferencesGroup(title="Container")
        group.set_description(
            "SQL Server 2022 for Linux (Developer edition) in Docker: no Windows and no VM. "
            "Databases live under the data directory in Settings and survive the container being recreated. "
            "The sa password is generated and kept in your keyring."
        )
        existing = set(ctx.containers.status_map())  # container name -> status
        n = 1
        while f"sql{n}" in existing:
            n += 1
        self.name = Adw.EntryRow(title="Name", text=f"sql{n}")
        group.add(self.name)
        self.port = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self.port.set_title("Port on this computer")
        self.port.set_subtitle("Published on 127.0.0.1 only")
        try:
            self.port.set_value(free_port())
        except Exception:  # noqa: BLE001
            self.port.set_value(14331)
        group.add(self.port)
        self.memory = Adw.SpinRow.new_with_range(1024, 262144, 512)
        self.memory.set_title("Memory limit (MiB)")
        self.memory.set_value(ctx.config.docker.memory_mib)
        group.add(self.memory)
        self.image = Adw.EntryRow(title="Image", text=ctx.config.docker.image)
        group.add(self.image)
        page.add(group)

        self.progress_group = Adw.PreferencesGroup()
        self.progress_row = Adw.ActionRow(use_markup=False, title="Creating…", subtitle="")
        self.progress_bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True)
        self.progress_row.add_suffix(self.progress_bar)
        self.progress_group.add(self.progress_row)
        self.progress_group.set_visible(False)
        page.add(self.progress_group)

    def _on_create(self) -> None:
        name = self.name.get_text().strip()
        if not name:
            error_dialog(self, "Name the container", "Letters, digits, dots, dashes and underscores.")
            return
        self.create_button.set_sensitive(False)
        self.progress_group.set_visible(True)

        def update(message: str, fraction: float) -> None:
            self.progress_row.set_subtitle(message)
            if fraction >= 0:
                self.progress_bar.set_fraction(fraction)
            else:
                self.progress_bar.pulse()

        progress = Progress(update)

        def done(server) -> None:
            self.ctx.toast(f"{server.name} is running on port {server.port}")
            self.ctx.emit(CONTAINERS_CHANGED)
            self.ctx.emit(SERVERS_CHANGED)
            self.close()

        def failed(exc: BaseException) -> None:
            self.create_button.set_sensitive(True)
            self.progress_row.set_title("Failed")
            self.progress_row.set_subtitle(str(exc)[:300])
            error_dialog(self, "Container not created", str(exc))

        run_in_thread(
            self.ctx.containers.create, name,
            port=int(self.port.get_value()), memory_mib=int(self.memory.get_value()), image=self.image.get_text().strip(), progress=progress,
            on_done=done, on_error=failed, name="docker-create",
        )


class ContainerLogDialog(Adw.Dialog):
    def __init__(self, ctx: AppContext, name: str):
        super().__init__(title=f"Log of {name}", content_width=720, content_height=520)
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        self.text = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.text.set_left_margin(8)
        self.text.set_right_margin(8)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self.text)
        view.set_content(scroll)
        self.set_child(view)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh")
        refresh.connect("clicked", lambda *_: self._load())
        header.pack_end(refresh)
        self.ctx, self.name = ctx, name
        self._load()

    def _load(self) -> None:
        def done(text: str) -> None:
            self.text.get_buffer().set_text(text or "(empty)")
            mark = self.text.get_buffer().get_insert()
            self.text.get_buffer().place_cursor(self.text.get_buffer().get_end_iter())
            self.text.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)

        run_in_thread(self.ctx.containers.logs, self.name, 400, on_done=done, on_error=lambda e: done(str(e)), name="docker-logs")
