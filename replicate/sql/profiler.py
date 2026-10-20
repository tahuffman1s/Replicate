"""A statement trace, the way SQL Server Profiler showed one, on Extended Events.

Replicate creates an event session with a ring-buffer target, polls the
target's XML and turns new events into rows. Nothing is written on the
server beyond the session itself, which is dropped when tracing stops.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from replicate.sql.connection import SqlConnection, SqlError
from replicate.util import quote_sql_string as qs

log = logging.getLogger(__name__)

SESSION_NAME = "replicate_trace"

AVAILABLE_EVENTS = {
    "sql_batch_completed": "T-SQL batches",
    "rpc_completed": "Stored procedure calls (RPC)",
    "sql_statement_completed": "Individual statements",
    "sp_statement_completed": "Statements inside procedures",
    "error_reported": "Errors",
    "attention": "Attentions (cancelled queries)",
    "login": "Logins",
    "logout": "Logouts",
}
ACTIONS = ["client_app_name", "client_hostname", "database_name", "username", "session_id", "sql_text"]


@dataclass
class TraceEvent:
    timestamp: dt.datetime
    name: str
    duration_ms: float = 0.0
    cpu_ms: float = 0.0
    reads: int = 0
    writes: int = 0
    rows: int = 0
    text: str = ""
    object_name: str = ""
    database: str = ""
    login: str = ""
    app: str = ""
    host: str = ""
    session_id: int = 0
    result: str = ""
    message: str = ""
    seq: int = 0

    @property
    def key(self) -> tuple:
        return (self.timestamp.isoformat(), self.name, self.session_id, self.duration_ms, self.text[:80])


@dataclass
class TraceSummary:
    total: int = 0
    events_per_second: float = 0.0
    avg_duration_ms: float = 0.0
    max_duration_ms: float = 0.0
    by_app: dict[str, int] = field(default_factory=dict)
    top_statements: list[tuple[str, float, int]] = field(default_factory=list)  # text, total ms, count


def _normalise(text: str) -> str:
    """Collapse whitespace and literals so similar statements group together."""
    import re

    text = re.sub(r"\s+", " ", text.strip())
    text = re.sub(r"'(?:[^']|'')*'", "'?'", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "?", text)
    return text[:160]


def build_session_sql(events: list[str], min_duration_ms: int, exclude_apps: list[str]) -> str:
    """CREATE EVENT SESSION for the chosen events, with a duration floor on the
    *_completed events and the excluded applications filtered on every event."""
    app_preds = [f"sqlserver.client_app_name <> N'{app.replace(chr(39), chr(39) * 2)}'" for app in exclude_apps]
    actions = ", ".join(f"sqlserver.{a}" for a in ACTIONS)
    parts = []
    for name in events:
        if name not in AVAILABLE_EVENTS:
            continue
        preds = list(app_preds)
        if min_duration_ms > 0 and name.endswith("_completed"):
            preds.insert(0, f"duration >= {int(min_duration_ms) * 1000}")
        where = f" WHERE ({' AND '.join(preds)})" if preds else ""
        parts.append(f"ADD EVENT sqlserver.{name}(ACTION({actions}){where})")
    body = ",\n".join(parts)
    return (
        f"CREATE EVENT SESSION [{SESSION_NAME}] ON SERVER\n{body}\n"
        "ADD TARGET package0.ring_buffer(SET max_memory = 8192, max_events_limit = 5000)\n"
        "WITH (MAX_MEMORY = 16384 KB, EVENT_RETENTION_MODE = ALLOW_SINGLE_EVENT_LOSS,"
        " MAX_DISPATCH_LATENCY = 1 SECONDS, STARTUP_STATE = OFF);"
    )


def parse_ring_buffer(xml_text: str) -> list[TraceEvent]:
    if not xml_text:
        return []
    root = ET.fromstring(xml_text)
    events: list[TraceEvent] = []
    for idx, ev in enumerate(root.iter("event")):
        name = ev.get("name", "")
        stamp = ev.get("timestamp", "")
        try:
            when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            when = dt.datetime.now(dt.timezone.utc)
        values: dict[str, str] = {}
        for child in ev:
            key = child.get("name", "")
            value_el = child.find("value")
            text_el = child.find("text")
            value = (value_el.text if value_el is not None else "") or ""
            if text_el is not None and text_el.text:
                values[key + "@text"] = text_el.text
            values[key] = value
        te = TraceEvent(timestamp=when, name=name, seq=idx)

        def num(key: str) -> float:
            try:
                return float(values.get(key, "") or 0)
            except ValueError:
                return 0.0

        te.duration_ms = num("duration") / 1000.0
        te.cpu_ms = num("cpu_time") / 1000.0
        te.reads = int(num("logical_reads"))
        te.writes = int(num("writes"))
        te.rows = int(num("row_count"))
        te.text = values.get("batch_text") or values.get("statement") or values.get("sql_text") or ""
        te.object_name = values.get("object_name", "")
        te.database = values.get("database_name", "")
        te.login = values.get("username", "")
        te.app = values.get("client_app_name", "")
        te.host = values.get("client_hostname", "")
        te.session_id = int(num("session_id"))
        te.result = values.get("result@text") or values.get("result", "")
        te.message = values.get("message", "")
        events.append(te)
    return events


class Profiler:
    def __init__(self, connect: Callable[[], SqlConnection], *, events: list[str], min_duration_ms: int,
                 exclude_apps: list[str], max_rows: int = 2000, poll_interval: float = 1.5):
        self._connect = connect
        self.events = events
        self.min_duration_ms = min_duration_ms
        self.exclude_apps = exclude_apps
        self.poll_interval = poll_interval
        self.rows: deque[TraceEvent] = deque(maxlen=max_rows)
        self.error: str | None = None
        self.listeners: list[Callable[[list[TraceEvent]], None]] = []
        self.rate_series: deque[tuple[float, float]] = deque(maxlen=300)  # (time, events/s)
        self._seen: deque[tuple] = deque(maxlen=20000)
        self._seen_set: set[tuple] = set()
        self._last_ts: dt.datetime | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self, value: bool) -> None:
        self._paused = value

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="replicate-profiler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def clear(self) -> None:
        self.rows.clear()
        self.rate_series.clear()

    # -- server side -------------------------------------------------------
    def ensure_session(self, conn: SqlConnection) -> None:
        conn.execute(
            f"IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = {qs(SESSION_NAME)})"
            f" DROP EVENT SESSION [{SESSION_NAME}] ON SERVER;"
        )
        conn.execute(build_session_sql(self.events, self.min_duration_ms, self.exclude_apps))
        conn.execute(f"ALTER EVENT SESSION [{SESSION_NAME}] ON SERVER STATE = START;")

    @staticmethod
    def drop_session(conn: SqlConnection) -> None:
        conn.execute(
            f"IF EXISTS (SELECT 1 FROM sys.server_event_sessions WHERE name = {qs(SESSION_NAME)})"
            f" DROP EVENT SESSION [{SESSION_NAME}] ON SERVER;"
        )

    def poll(self, conn: SqlConnection) -> list[TraceEvent]:
        xml_text = conn.scalar(
            "SELECT CAST(t.target_data AS nvarchar(max)) FROM sys.dm_xe_session_targets t"
            " JOIN sys.dm_xe_sessions s ON s.address = t.event_session_address"
            f" WHERE s.name = {qs(SESSION_NAME)} AND t.target_name = N'ring_buffer'"
        )
        if xml_text is None:
            raise SqlError("The trace session is gone from the server")
        events = parse_ring_buffer(xml_text)
        fresh = []
        for ev in events:
            if self._last_ts and ev.timestamp < self._last_ts - dt.timedelta(seconds=5):
                continue
            key = ev.key
            if key in self._seen_set:
                continue
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(key)
            self._seen_set.add(key)
            fresh.append(ev)
        if fresh:
            self._last_ts = max(ev.timestamp for ev in fresh)
        return fresh

    def _loop(self) -> None:
        from replicate.tasks import on_main

        conn: SqlConnection | None = None
        try:
            conn = self._connect()
            self.ensure_session(conn)
            self.error = None
        except SqlError as exc:
            self.error = str(exc)
            for listener in list(self.listeners):
                on_main(listener, [])
            return
        last = time.monotonic()
        while not self._stop.is_set():
            try:
                fresh = self.poll(conn)
                now = time.monotonic()
                if not self._paused:
                    for ev in fresh:
                        self.rows.append(ev)
                    self.rate_series.append((time.time(), len(fresh) / max(now - last, 0.1)))
                last = now
                self.error = None
                for listener in list(self.listeners):
                    on_main(listener, fresh if not self._paused else [])
            except SqlError as exc:
                self.error = str(exc)
                for listener in list(self.listeners):
                    on_main(listener, [])
                try:
                    conn.close()
                    conn = self._connect()
                    self.ensure_session(conn)
                except SqlError:
                    pass
            if self._stop.wait(self.poll_interval):
                break
        try:
            self.drop_session(conn)
        except SqlError:
            pass
        finally:
            conn.close()

    # -- summaries for the charts -----------------------------------------
    def summary(self, window_seconds: float = 300) -> TraceSummary:
        rows = list(self.rows)
        summary = TraceSummary(total=len(rows))
        if not rows:
            return summary
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=window_seconds)
        recent = [r for r in rows if r.timestamp >= cutoff] or rows
        durations = [r.duration_ms for r in recent if r.duration_ms]
        if durations:
            summary.avg_duration_ms = sum(durations) / len(durations)
            summary.max_duration_ms = max(durations)
        span = max((recent[-1].timestamp - recent[0].timestamp).total_seconds(), 1.0)
        summary.events_per_second = len(recent) / span
        for r in recent:
            summary.by_app[r.app or "(unknown)"] = summary.by_app.get(r.app or "(unknown)", 0) + 1
        totals: dict[str, list[float]] = {}
        for r in recent:
            if not r.text:
                continue
            key = _normalise(r.text)
            entry = totals.setdefault(key, [0.0, 0])
            entry[0] += r.duration_ms
            entry[1] += 1
        summary.top_statements = sorted(((k, v[0], v[1]) for k, v in totals.items()), key=lambda t: t[1], reverse=True)[:8]
        return summary

    def duration_histogram(self, bins: tuple[float, ...] = (1, 10, 50, 100, 500, 1000, 5000)) -> list[tuple[str, int]]:
        counts = [0] * (len(bins) + 1)
        for r in self.rows:
            d = r.duration_ms
            idx = next((i for i, edge in enumerate(bins) if d < edge), len(bins))
            counts[idx] += 1
        labels = []
        prev = 0.0
        for edge in bins:
            labels.append(f"{prev:g}-{edge:g} ms")
            prev = edge
        labels.append(f">{bins[-1]:g} ms")
        return list(zip(labels, counts))
