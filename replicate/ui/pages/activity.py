"""Activity: live counters, waits, requests and a statement trace, drawn as charts."""

from __future__ import annotations

import logging
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402

from replicate.config import SqlServer  # noqa: E402
from replicate.sql.monitor import WAIT_CATEGORIES, Monitor, Sample  # noqa: E402
from replicate.sql.profiler import AVAILABLE_EVENTS, Profiler, TraceEvent  # noqa: E402
from replicate.ui.context import SERVERS_CHANGED, AppContext  # noqa: E402
from replicate.ui.widgets.charts import BarChart, LineChart, SeriesSpec, StatTile  # noqa: E402
from replicate.ui.widgets.common import Table, heading, text_button  # noqa: E402

log = logging.getLogger(__name__)


def _card(child: Gtk.Widget) -> Gtk.Box:
    box = Gtk.Box()
    box.add_css_class("card")
    box.add_css_class("chart-card")
    box.append(child)
    child.set_hexpand(True)
    return box


class ActivityPage(Gtk.ScrolledWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.monitor: Monitor | None = None
        self.profiler: Profiler | None = None
        self.current: SqlServer | None = None
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        clamp = Adw.Clamp(maximum_size=1100, tightening_threshold=900)
        self.set_child(clamp)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.add_css_class("page-content")
        clamp.set_child(box)

        # Toolbar
        bar = Gtk.Box(spacing=10)
        bar.append(Gtk.Label(label="Server"))
        self.server_model = Gtk.StringList()
        self.server_drop = Gtk.DropDown(model=self.server_model)
        self.server_drop.connect("notify::selected", self._on_server_changed)
        bar.append(self.server_drop)
        self.monitor_button = Gtk.ToggleButton(label="Monitor")
        self.monitor_button.add_css_class("suggested-action")
        self.monitor_button.connect("toggled", self._on_monitor_toggled)
        bar.append(self.monitor_button)
        self.window_drop = Gtk.DropDown(model=Gtk.StringList.new(["Last 5 min", "Last 15 min", "Last 60 min"]))
        self.window_drop.connect("notify::selected", self._on_window_changed)
        bar.append(self.window_drop)
        self.status = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("dim-label")
        bar.append(self.status)
        box.append(bar)

        # Tiles
        tiles = Gtk.Box(spacing=10, homogeneous=True)
        self.tile_cpu = StatTile("SQL Server CPU", unit="%", color_index=0)
        self.tile_batches = StatTile("Batches / s", color_index=1)
        self.tile_requests = StatTile("Active requests", color_index=2)
        self.tile_sessions = StatTile("User sessions")
        self.tile_ple = StatTile("Page life expectancy")
        self.tile_blocked = StatTile("Blocked")
        for t in (self.tile_cpu, self.tile_batches, self.tile_requests, self.tile_sessions, self.tile_ple, self.tile_blocked):
            tiles.append(t)
        box.append(tiles)

        # Charts
        grid = Gtk.Grid(column_spacing=10, row_spacing=10, column_homogeneous=True)
        self.chart_cpu = LineChart([SeriesSpec("cpu_sql", "SQL Server", 0), SeriesSpec("cpu_other", "Other processes", 1)], unit="%", y_max=100, stacked=True, title="CPU")
        self.chart_rates = LineChart([SeriesSpec("rate_batches", "Batches/s", 0), SeriesSpec("rate_transactions", "Transactions/s", 1), SeriesSpec("rate_compilations", "Compilations/s", 2)], title="Throughput")
        self.chart_waits = LineChart([SeriesSpec(f"wait_{c}", c, i) for i, c in enumerate(WAIT_CATEGORIES)], unit="ms/s", stacked=True, title="Wait time by category", height=190)
        self.chart_io = LineChart([SeriesSpec("rate_page_reads", "Page reads/s", 0), SeriesSpec("rate_page_writes", "Page writes/s", 1), SeriesSpec("rate_lock_waits", "Lock waits/s", 7)], title="I/O and locking")
        grid.attach(_card(self.chart_cpu), 0, 0, 1, 1)
        grid.attach(_card(self.chart_rates), 1, 0, 1, 1)
        grid.attach(_card(self.chart_waits), 0, 1, 1, 1)
        grid.attach(_card(self.chart_io), 1, 1, 1, 1)
        box.append(grid)

        second = Gtk.Box(spacing=10, homogeneous=True)
        self.chart_top_waits = BarChart(unit="ms/s", title="Top wait types", color_index=6)
        second.append(_card(self.chart_top_waits))
        self.chart_memory = LineChart([SeriesSpec("gauge_connections", "User connections", 0), SeriesSpec("active_requests", "Active requests", 2)], title="Connections and requests")
        second.append(_card(self.chart_memory))
        box.append(second)

        # Requests
        box.append(heading("Running now", "Requests executing on the server (sys.dm_exec_requests)."))
        self.requests_table = Table(
            [("SPID", "session_id", 60, "end"), ("Status", "status", 90, "start"), ("Database", "database", 110, "start"),
             ("Elapsed ms", "elapsed_ms", 90, "end"), ("CPU ms", "cpu_ms", 80, "end"), ("Wait", "wait_type", 130, "start"),
             ("Blocked by", "blocked_by", 80, "end"), ("Login", "login", 110, "start"), ("Program", "program", 140, "start"), ("Statement", "text", 0, "start")],
            formatters={"blocked_by": lambda v: "" if not v else str(v)},
            height=150,
            on_activate=self._show_text,
        )
        box.append(self.requests_table)

        # Trace
        trace_head = Gtk.Box(spacing=10)
        trace_head.append(heading("Statement trace", "Extended Events session on the server, like a Profiler trace: batches and procedure calls as they complete."))
        trace_head.get_first_child().set_hexpand(True)
        self.trace_button = Gtk.ToggleButton(label="Start trace")
        self.trace_button.add_css_class("suggested-action")
        self.trace_button.connect("toggled", self._on_trace_toggled)
        self.pause_button = Gtk.ToggleButton(label="Pause")
        self.pause_button.connect("toggled", lambda b: self.profiler and self.profiler.pause(b.get_active()))
        self.clear_button = text_button("Clear", self._clear_trace)
        self.filter_button = Gtk.MenuButton(label="Events", valign=Gtk.Align.CENTER)
        self.filter_button.set_popover(self._build_filter_popover())
        for w in (self.filter_button, self.pause_button, self.clear_button, self.trace_button):
            w.set_valign(Gtk.Align.CENTER)
            trace_head.append(w)
        box.append(trace_head)

        trace_tiles = Gtk.Box(spacing=10, homogeneous=True)
        self.tile_events = StatTile("Events / s", color_index=0)
        self.tile_avg = StatTile("Avg duration", unit="ms", color_index=1)
        self.tile_max = StatTile("Max duration", unit="ms", color_index=7)
        self.tile_count = StatTile("Captured")
        for t in (self.tile_events, self.tile_avg, self.tile_max, self.tile_count):
            trace_tiles.append(t)
        box.append(trace_tiles)

        trace_charts = Gtk.Box(spacing=10, homogeneous=True)
        self.chart_events = LineChart([SeriesSpec("events", "Events/s", 0)], title="Event rate")
        self.chart_hist = BarChart(title="Duration distribution", color_index=1)
        self.chart_top = BarChart(unit="ms", title="Top statements by total time", color_index=6)
        trace_charts.append(_card(self.chart_events))
        trace_charts.append(_card(self.chart_hist))
        trace_charts.append(_card(self.chart_top))
        box.append(trace_charts)

        self.trace_table = Table(
            [("Time", "time", 90, "start"), ("Event", "name", 140, "start"), ("Duration ms", "duration_ms", 95, "end"), ("CPU ms", "cpu_ms", 75, "end"),
             ("Reads", "reads", 80, "end"), ("Writes", "writes", 70, "end"), ("Rows", "rows", 70, "end"), ("Database", "database", 110, "start"),
             ("Login", "login", 100, "start"), ("App", "app", 130, "start"), ("SPID", "session_id", 60, "end"), ("Text", "text", 0, "start")],
            formatters={"duration_ms": lambda v: f"{float(v):,.1f}", "cpu_ms": lambda v: f"{float(v):,.0f}", "reads": lambda v: f"{int(v):,}", "writes": lambda v: f"{int(v):,}", "rows": lambda v: f"{int(v):,}"},
            height=260,
            on_activate=self._show_text,
        )
        box.append(self.trace_table)

        self.detail = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.detail.add_css_class("card")
        self.detail.set_top_margin(8)
        self.detail.set_bottom_margin(8)
        self.detail.set_left_margin(8)
        self.detail.set_right_margin(8)
        detail_scroll = Gtk.ScrolledWindow(min_content_height=90, max_content_height=220, propagate_natural_height=True)
        detail_scroll.set_child(self.detail)
        box.append(heading("Selected statement", "Double-click a row above to see the full text here."))
        box.append(detail_scroll)

        ctx.subscribe(SERVERS_CHANGED, self._reload_servers)
        self._reload_servers()

    # -- servers ------------------------------------------------------------
    def _reload_servers(self) -> None:
        current_id = self.current.id if self.current else None
        self.server_model.splice(0, self.server_model.get_n_items(), [s.name for s in self.ctx.registry.servers] or ["No servers"])
        servers = self.ctx.registry.servers
        if servers:
            index = next((i for i, s in enumerate(servers) if s.id == current_id), 0)
            self.server_drop.set_selected(index)
            self.current = servers[index]
        else:
            self.current = None
        self.server_drop.set_sensitive(bool(servers))
        self.monitor_button.set_sensitive(bool(servers))
        self.trace_button.set_sensitive(bool(servers))

    def select_server(self, server: SqlServer) -> None:
        servers = self.ctx.registry.servers
        if server in servers:
            self.server_drop.set_selected(servers.index(server))
            if not self.monitor_button.get_active():
                self.monitor_button.set_active(True)

    def _on_server_changed(self, *_args) -> None:
        servers = self.ctx.registry.servers
        index = self.server_drop.get_selected()
        new = servers[index] if 0 <= index < len(servers) else None
        if new is self.current:
            return
        was_monitoring = self.monitor_button.get_active()
        was_tracing = self.trace_button.get_active()
        self.monitor_button.set_active(False)
        self.trace_button.set_active(False)
        self.current = new
        self._reset_views()
        if was_monitoring:
            self.monitor_button.set_active(True)
        if was_tracing:
            self.trace_button.set_active(True)

    def _on_window_changed(self, *_args) -> None:
        seconds = [300, 900, 3600][self.window_drop.get_selected()]
        for chart in (self.chart_cpu, self.chart_rates, self.chart_waits, self.chart_io, self.chart_memory, self.chart_events):
            chart.window_seconds = seconds
            chart.queue_draw()

    def _reset_views(self) -> None:
        for chart in (self.chart_cpu, self.chart_rates, self.chart_waits, self.chart_io, self.chart_memory):
            chart.clear()
        for tile in (self.tile_cpu, self.tile_batches, self.tile_requests, self.tile_sessions, self.tile_ple, self.tile_blocked):
            tile.set_value(None)
        self.requests_table.clear()
        self.chart_top_waits.set_items([])
        self.status.set_label("")

    # -- monitor ------------------------------------------------------------
    def _on_monitor_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            if self.current is None:
                button.set_active(False)
                return
            server = self.current
            settings = self.ctx.config.monitor
            self.monitor = Monitor(lambda: self.ctx.registry.connect(server), settings.interval_seconds, settings.history_points)
            self.monitor.listeners.append(self._on_sample)
            self.monitor.start()
            button.set_label("Stop monitoring")
            self.status.set_label(f"Connecting to {server.name}…")
        else:
            if self.monitor:
                self.monitor.stop()
                self.monitor = None
            button.set_label("Monitor")
            self.status.set_label("Monitoring stopped")

    def _on_sample(self, sample: Sample | None, error: str | None) -> None:
        if self.monitor is None:
            return
        if error:
            self.status.set_label(f"Error: {error}")
            return
        if sample is None:
            return
        series = self.monitor.series
        self.status.set_label(f"{self.current.name if self.current else ''} · sampled {time.strftime('%H:%M:%S')} every {self.monitor.interval:g}s")
        self.tile_cpu.set_value(sample.cpu_sql, f"{sample.cpu_other:.0f}% other processes")
        self.tile_batches.set_value(sample.rates.get("batches", 0), f"{sample.rates.get('transactions', 0):.0f} transactions/s")
        self.tile_requests.set_value(float(sample.active_requests), f"{sample.rates.get('compilations', 0):.1f} compilations/s")
        self.tile_sessions.set_value(float(sample.sessions), f"{sample.gauges.get('connections', 0):.0f} connections")
        ple = sample.gauges.get("ple", 0)
        self.tile_ple.set_value(f"{int(ple // 60)}m {int(ple % 60)}s", f"{sample.gauges.get('total_memory_kb', 0) / 1024 / 1024:.1f} GiB in use")
        blocked = sample.gauges.get("blocked", 0)
        self.tile_blocked.set_value(float(blocked), "processes waiting on locks" if blocked else "no blocking")
        times = list(series.times)
        values = {k: list(v) for k, v in series.values.items()}
        for chart in (self.chart_cpu, self.chart_rates, self.chart_waits, self.chart_io, self.chart_memory):
            chart.set_data(times, values)
        self.chart_top_waits.set_items(self.monitor.top_waits)
        self.requests_table.set_rows([r.__dict__ for r in self.monitor.requests])

    # -- trace --------------------------------------------------------------
    def _build_filter_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=8, margin_bottom=8, margin_start=10, margin_end=10)
        box.append(Gtk.Label(label="Events to capture", xalign=0, css_classes=["heading"]))
        self.event_checks: dict[str, Gtk.CheckButton] = {}
        selected = set(self.ctx.config.monitor.trace_events)
        for key, label in AVAILABLE_EVENTS.items():
            check = Gtk.CheckButton(label=label, active=key in selected)
            check.connect("toggled", self._on_filters_changed)
            self.event_checks[key] = check
            box.append(check)
        row = Gtk.Box(spacing=8, margin_top=6)
        row.append(Gtk.Label(label="Minimum duration (ms)"))
        self.min_duration = Gtk.SpinButton.new_with_range(0, 600000, 10)
        self.min_duration.set_value(self.ctx.config.monitor.trace_min_duration_ms)
        self.min_duration.connect("value-changed", self._on_filters_changed)
        row.append(self.min_duration)
        box.append(row)
        box.append(Gtk.Label(label="Changes apply when the trace is next started.", xalign=0, css_classes=["dim-label", "caption"]))
        popover.set_child(box)
        return popover

    def _on_filters_changed(self, *_args) -> None:
        self.ctx.config.monitor.trace_events = [k for k, c in self.event_checks.items() if c.get_active()]
        self.ctx.config.monitor.trace_min_duration_ms = int(self.min_duration.get_value())
        self.ctx.save_config()

    def _on_trace_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active():
            if self.current is None:
                button.set_active(False)
                return
            server = self.current
            m = self.ctx.config.monitor
            self.profiler = Profiler(
                lambda: self.ctx.registry.connect(server),
                events=list(m.trace_events) or ["sql_batch_completed", "rpc_completed"],
                min_duration_ms=m.trace_min_duration_ms,
                exclude_apps=list(m.trace_exclude_apps),
                max_rows=m.trace_max_rows,
            )
            self.profiler.listeners.append(self._on_trace_events)
            self.profiler.start()
            button.set_label("Stop trace")
            self.tile_count.set_value(0.0, "starting the event session…")
        else:
            if self.profiler:
                self.profiler.stop()
                self.profiler = None
            button.set_label("Start trace")
            self.pause_button.set_active(False)

    def _clear_trace(self) -> None:
        if self.profiler:
            self.profiler.clear()
        self.trace_table.clear()
        self.chart_events.clear()
        self.chart_hist.set_items([])
        self.chart_top.set_items([])
        for tile in (self.tile_events, self.tile_avg, self.tile_max, self.tile_count):
            tile.set_value(None)

    def _on_trace_events(self, fresh: list[TraceEvent]) -> None:
        if self.profiler is None:
            return
        if self.profiler.error:
            self.tile_count.set_value(float(len(self.profiler.rows)), f"error: {self.profiler.error}"[:120])
        rows = [
            {
                "time": ev.timestamp.astimezone().strftime("%H:%M:%S"),
                "name": ev.name,
                "duration_ms": ev.duration_ms,
                "cpu_ms": ev.cpu_ms,
                "reads": ev.reads,
                "writes": ev.writes,
                "rows": ev.rows,
                "database": ev.database,
                "login": ev.login,
                "app": ev.app,
                "session_id": ev.session_id,
                "text": ev.text or ev.message or ev.object_name,
            }
            for ev in fresh
        ]
        if rows:
            self.trace_table.append_rows(rows, max_rows=self.ctx.config.monitor.trace_max_rows)
        summary = self.profiler.summary()
        self.tile_events.set_value(summary.events_per_second, f"{len(summary.by_app)} application{'s' if len(summary.by_app) != 1 else ''}")
        self.tile_avg.set_value(summary.avg_duration_ms)
        self.tile_max.set_value(summary.max_duration_ms)
        self.tile_count.set_value(float(summary.total), "paused" if self.profiler.paused else "")
        rate = list(self.profiler.rate_series)
        self.chart_events.set_data([t for t, _ in rate], {"events": [v for _, v in rate]})
        self.chart_hist.set_items([(label, float(count)) for label, count in self.profiler.duration_histogram()])
        self.chart_top.set_items([(text, total) for text, total, _count in summary.top_statements])

    def _show_text(self, row: dict) -> None:
        self.detail.get_buffer().set_text(str(row.get("text", "")))

    def shutdown(self) -> None:
        if self.monitor:
            self.monitor.stop()
        if self.profiler:
            self.profiler.stop()
