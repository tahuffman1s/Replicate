"""Charts drawn with cairo: line/area over time, horizontal bars, stat tiles.

Colours follow the reference data-viz palette (validated for colour-vision
deficiency in both modes); text and grid come from the widget's own style
so the charts sit naturally on light and dark libadwaita surfaces.
"""

from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gtk, Pango, PangoCairo  # noqa: E402

LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


def _hex(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def palette() -> list[str]:
    return DARK if Adw.StyleManager.get_default().get_dark() else LIGHT


def series_color(index: int) -> tuple[float, float, float]:
    colors = palette()
    return _hex(colors[index % len(colors)])


def _fmt(value: float, unit: str = "") -> str:
    if unit == "%":
        return f"{value:.0f}%"
    if abs(value) >= 1_000_000:
        text = f"{value / 1_000_000:.1f}M"
    elif abs(value) >= 10_000:
        text = f"{value / 1000:.0f}k"
    elif abs(value) >= 1000:
        text = f"{value / 1000:.1f}k"
    elif abs(value) >= 100 or value == int(value):
        text = f"{value:.0f}"
    else:
        text = f"{value:.1f}"
    return f"{text}{(' ' + unit) if unit and unit != '%' else ''}"


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = value / (10**exp)
    for step in (1, 2, 2.5, 5, 10):
        if base <= step:
            return step * (10**exp)
    return 10 ** (exp + 1)


@dataclass
class SeriesSpec:
    key: str
    label: str
    color_index: int = 0


@dataclass
class ChartData:
    times: Sequence[float] = field(default_factory=list)
    values: dict[str, Sequence[float]] = field(default_factory=dict)


class LineChart(Gtk.DrawingArea):
    """Multi-series line (or stacked area) chart over time with hover readout."""

    def __init__(self, series: list[SeriesSpec], *, unit: str = "", stacked: bool = False, y_max: float | None = None,
                 title: str = "", height: int = 160):
        super().__init__()
        self.series = series
        self.unit = unit
        self.stacked = stacked
        self.fixed_y_max = y_max
        self.title = title
        self.data = ChartData()
        self.window_seconds = 300.0
        self._hover_x: float | None = None
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.add_css_class("chart")
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())

    def set_data(self, times: Sequence[float], values: dict[str, Sequence[float]]) -> None:
        self.data = ChartData(times, values)
        self.queue_draw()

    def clear(self) -> None:
        self.set_data([], {})

    def _on_motion(self, _ctrl: Gtk.EventControllerMotion, x: float, _y: float) -> None:
        self._hover_x = x
        self.queue_draw()

    def _on_leave(self, _ctrl: Gtk.EventControllerMotion) -> None:
        self._hover_x = None
        self.queue_draw()

    # -- drawing -----------------------------------------------------------
    def _colors(self) -> tuple[Gdk.RGBA, Gdk.RGBA]:
        fg = self.get_color()
        grid = Gdk.RGBA()
        grid.red, grid.green, grid.blue, grid.alpha = fg.red, fg.green, fg.blue, 0.12
        return fg, grid

    def _text(self, cr, text: str, x: float, y: float, *, size: float = 10, align: str = "left", alpha: float = 0.7,
              weight: str = "normal") -> tuple[float, float]:
        fg = self.get_color()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(f"Sans {'Bold ' if weight == 'bold' else ''}{size}"))
        layout.set_text(text, -1)
        _ink, logical = layout.get_pixel_extents()
        w, h = logical.width, logical.height
        if align == "right":
            x -= w
        elif align == "center":
            x -= w / 2
        cr.set_source_rgba(fg.red, fg.green, fg.blue, alpha)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)
        return w, h

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        fg, grid = self._colors()
        pad_l, pad_r, pad_t, pad_b = 44, 12, 8, 20
        y_max_probe = self.fixed_y_max if self.fixed_y_max is not None else self._peak_estimate()
        pad_l = max(36, int(6.4 * max(len(_fmt(y_max_probe * f, self.unit)) for f in (0.0, 0.5, 1.0))) + 12)
        if self.title:
            self._text(cr, self.title, pad_l, 0, size=10, alpha=0.85, weight="bold")
            pad_t = 24
        plot_w = max(1, width - pad_l - pad_r)
        legend_rows = self._legend_rows(cr, plot_w) if len(self.series) >= 2 else []
        legend_h = 16 * len(legend_rows) + (4 if legend_rows else 0)
        plot_h = max(1, height - pad_t - pad_b - legend_h)
        x0, y0 = pad_l, pad_t

        times = list(self.data.times)
        now = times[-1] if times else time.time()
        t_min = now - self.window_seconds
        visible_idx = [i for i, t in enumerate(times) if t >= t_min]
        series_vals = {s.key: list(self.data.values.get(s.key, [])) for s in self.series}

        # y scale
        if self.fixed_y_max is not None:
            y_max = self.fixed_y_max
        else:
            peak = 0.0
            for i in visible_idx:
                total = 0.0
                for s in self.series:
                    vals = series_vals[s.key]
                    v = vals[i] if i < len(vals) else 0.0
                    if self.stacked:
                        total += v
                    else:
                        peak = max(peak, v)
                if self.stacked:
                    peak = max(peak, total)
            y_max = _nice_max(peak * 1.1 if peak else 1.0)

        # grid + y labels
        cr.set_line_width(1)
        for frac in (0.0, 0.5, 1.0):
            y = y0 + plot_h - frac * plot_h
            cr.set_source_rgba(grid.red, grid.green, grid.blue, grid.alpha)
            cr.move_to(x0, round(y) + 0.5)
            cr.line_to(x0 + plot_w, round(y) + 0.5)
            cr.stroke()
            self._text(cr, _fmt(y_max * frac, self.unit), x0 - 6, y - 7, size=9, align="right", alpha=0.6)
        # x labels: minutes ago
        for frac, label in ((0.0, f"-{int(self.window_seconds // 60)}m"), (0.5, f"-{int(self.window_seconds // 120)}m"), (1.0, "now")):
            self._text(cr, label, x0 + frac * plot_w, y0 + plot_h + 4, size=9, align="center" if 0 < frac < 1 else ("right" if frac == 1 else "left"), alpha=0.55)

        def px(t: float) -> float:
            return x0 + (t - t_min) / self.window_seconds * plot_w

        def py(v: float) -> float:
            return y0 + plot_h - min(max(v / y_max, 0.0), 1.0) * plot_h

        if not visible_idx:
            self._text(cr, "Waiting for data", x0 + plot_w / 2, y0 + plot_h / 2 - 8, size=10, align="center", alpha=0.5)
        else:
            base = [0.0] * len(times)
            cr.save()
            cr.rectangle(x0, y0, plot_w, plot_h)
            cr.clip()
            for s in self.series:
                vals = series_vals[s.key]
                r, g, b = series_color(s.color_index)
                points = []
                for i in visible_idx:
                    v = vals[i] if i < len(vals) else 0.0
                    top = base[i] + v if self.stacked else v
                    points.append((px(times[i]), py(top), py(base[i]) if self.stacked else py(0)))
                    if self.stacked:
                        base[i] += v
                if self.stacked:
                    cr.set_source_rgba(r, g, b, 0.75)
                    cr.move_to(points[0][0], points[0][2])
                    for x, y, _ in points:
                        cr.line_to(x, y)
                    for x, _, yb in reversed(points):
                        cr.line_to(x, yb)
                    cr.close_path()
                    cr.fill()
                else:
                    cr.set_source_rgba(r, g, b, 0.12)
                    cr.move_to(points[0][0], py(0))
                    for x, y, _ in points:
                        cr.line_to(x, y)
                    cr.line_to(points[-1][0], py(0))
                    cr.close_path()
                    cr.fill()
                    cr.set_source_rgb(r, g, b)
                    cr.set_line_width(2)
                    cr.set_line_join(1)
                    cr.move_to(points[0][0], points[0][1])
                    for x, y, _ in points[1:]:
                        cr.line_to(x, y)
                    cr.stroke()
            cr.restore()

            # hover crosshair + readout
            if self._hover_x is not None and x0 <= self._hover_x <= x0 + plot_w:
                t_hover = t_min + (self._hover_x - x0) / plot_w * self.window_seconds
                nearest = min(visible_idx, key=lambda i: abs(times[i] - t_hover))
                hx = px(times[nearest])
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.35)
                cr.set_line_width(1)
                cr.move_to(round(hx) + 0.5, y0)
                cr.line_to(round(hx) + 0.5, y0 + plot_h)
                cr.stroke()
                lines = [time.strftime("%H:%M:%S", time.localtime(times[nearest]))]
                for s in self.series:
                    vals = series_vals[s.key]
                    v = vals[nearest] if nearest < len(vals) else 0.0
                    lines.append(f"{s.label}: {_fmt(v, self.unit)}")
                box_w = 10 + max(len(line) for line in lines) * 6.2
                box_h = 6 + len(lines) * 14
                bx = hx + 10 if hx + 10 + box_w < x0 + plot_w else hx - 10 - box_w
                by = y0 + 4
                bg = self._surface_color()
                cr.set_source_rgba(bg[0], bg[1], bg[2], 0.95)
                self._rounded_rect(cr, bx, by, box_w, box_h, 6)
                cr.fill_preserve()
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.2)
                cr.stroke()
                for n, line in enumerate(lines):
                    if n > 0:
                        r, g, b = series_color(self.series[n - 1].color_index)
                        cr.set_source_rgb(r, g, b)
                        cr.arc(bx + 9, by + 4 + n * 14 + 7, 3.5, 0, 2 * math.pi)
                        cr.fill()
                    self._text(cr, line, bx + (18 if n > 0 else 6), by + 3 + n * 14, size=9, alpha=0.9 if n == 0 else 0.8)

        # legend (wraps onto as many rows as the width needs)
        if legend_rows:
            ly = height - legend_h + 2
            for row in legend_rows:
                lx = x0
                for s, w in row:
                    r, g, b = series_color(s.color_index)
                    cr.set_source_rgb(r, g, b)
                    self._rounded_rect(cr, lx, ly + 4, 10, 10, 3)
                    cr.fill()
                    self._text(cr, s.label, lx + 14, ly, size=9, alpha=0.75)
                    lx += 14 + w + 14
                ly += 16

    def _legend_rows(self, cr, plot_w: float) -> list[list[tuple[SeriesSpec, float]]]:
        rows: list[list[tuple[SeriesSpec, float]]] = [[]]
        used = 0.0
        for s in self.series:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 9"))
            layout.set_text(s.label, -1)
            w = layout.get_pixel_extents()[1].width
            item_w = 14 + w + 14
            if rows[-1] and used + item_w > plot_w:
                rows.append([])
                used = 0.0
            rows[-1].append((s, w))
            used += item_w
        return rows

    def _peak_estimate(self) -> float:
        peak = 0.0
        times = list(self.data.times)
        if not times:
            return 1.0
        t_min = times[-1] - self.window_seconds
        for i, t in enumerate(times):
            if t < t_min:
                continue
            total = 0.0
            for s in self.series:
                vals = self.data.values.get(s.key, [])
                v = vals[i] if i < len(vals) else 0.0
                total += v
                if not self.stacked:
                    peak = max(peak, v)
            if self.stacked:
                peak = max(peak, total)
        return _nice_max(peak * 1.1 if peak else 1.0)

    def _surface_color(self) -> tuple[float, float, float]:
        return (0.11, 0.11, 0.11) if Adw.StyleManager.get_default().get_dark() else (1.0, 1.0, 1.0)

    @staticmethod
    def _rounded_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


class BarChart(Gtk.DrawingArea):
    """Horizontal bars with a label on the left and the value on the right."""

    def __init__(self, *, unit: str = "", title: str = "", color_index: int = 0, max_bars: int = 8, row_height: int = 22):
        super().__init__()
        self.unit = unit
        self.title = title
        self.color_index = color_index
        self.max_bars = max_bars
        self.row_height = row_height
        self.items: list[tuple[str, float]] = []
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.add_css_class("chart")
        self._update_height()
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.queue_draw())

    def _update_height(self) -> None:
        rows = max(1, min(len(self.items), self.max_bars))
        self.set_content_height((24 if self.title else 4) + rows * self.row_height + 6)

    def set_items(self, items: list[tuple[str, float]]) -> None:
        self.items = items[: self.max_bars]
        self._update_height()
        self.queue_draw()

    def _draw(self, _area, cr, width: int, height: int) -> None:
        fg = self.get_color()
        y = 2
        if self.title:
            LineChart._text(self, cr, self.title, 0, 0, size=10, alpha=0.85, weight="bold")  # type: ignore[arg-type]
            y = 24
        if not self.items:
            LineChart._text(self, cr, "No data yet", 0, y + 2, size=10, alpha=0.5)  # type: ignore[arg-type]
            return
        label_w = min(int(width * 0.42), 260)
        value_w = 64
        bar_x = label_w + 8
        bar_w = max(10, width - bar_x - value_w)
        peak = max((v for _, v in self.items), default=0) or 1.0
        r, g, b = series_color(self.color_index)
        for label, value in self.items:
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("Sans 9"))
            layout.set_width(label_w * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_text(label, -1)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.8)
            cr.move_to(0, y + 3)
            PangoCairo.show_layout(cr, layout)
            w = max(2.0, bar_w * value / peak)
            cr.set_source_rgba(r, g, b, 0.9)
            LineChart._rounded_rect(cr, bar_x, y + 4, w, self.row_height - 9, 3)
            cr.fill()
            LineChart._text(self, cr, _fmt(value, self.unit), width, y + 3, size=9, align="right", alpha=0.7)  # type: ignore[arg-type]
            y += self.row_height


class StatTile(Gtk.Box):
    """A headline number with a label and an optional note underneath."""

    def __init__(self, label: str, *, unit: str = "", color_index: int | None = None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.unit = unit
        self.add_css_class("card")
        self.add_css_class("stat-tile")
        self.set_hexpand(True)
        top = Gtk.Box(spacing=6)
        if color_index is not None:
            dot = Gtk.DrawingArea()
            dot.set_size_request(10, 10)
            dot.set_valign(Gtk.Align.CENTER)

            def draw_dot(_a, cr, w, h):
                r, g, b = series_color(color_index)
                cr.set_source_rgb(r, g, b)
                cr.arc(w / 2, h / 2, 5, 0, 2 * math.pi)
                cr.fill()

            dot.set_draw_func(draw_dot)
            top.append(dot)
        self.label = Gtk.Label(label=label, xalign=0)
        self.label.add_css_class("caption-heading")
        self.label.add_css_class("dim-label")
        top.append(self.label)
        self.append(top)
        self.value = Gtk.Label(label="–", xalign=0)
        self.value.add_css_class("title-1")
        self.append(self.value)
        self.note = Gtk.Label(label="", xalign=0)
        self.note.add_css_class("caption")
        self.note.add_css_class("dim-label")
        self.note.set_ellipsize(Pango.EllipsizeMode.END)
        self.append(self.note)

    def set_value(self, value: float | str | None, note: str = "") -> None:
        if value is None:
            self.value.set_label("–")
        elif isinstance(value, str):
            self.value.set_label(value)
        else:
            self.value.set_label(_fmt(value, self.unit))
        self.note.set_label(note)
        self.note.set_visible(bool(note))
