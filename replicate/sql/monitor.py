"""Live activity of a SQL Server: counters, requests and waits, sampled on a timer.

Cumulative counters from ``sys.dm_os_performance_counters`` are turned into
per-second rates between samples; CPU comes from the scheduler-monitor ring
buffer; waits from ``sys.dm_os_wait_stats`` as deltas, grouped. Everything is
kept in fixed-length series the Activity page draws.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from replicate.sql.connection import SqlConnection, SqlError

log = logging.getLogger(__name__)

RATE_COUNTERS = {
    "Batch Requests/sec": "batches",
    "SQL Compilations/sec": "compilations",
    "SQL Re-Compilations/sec": "recompilations",
    "Transactions/sec": "transactions",
    "Page reads/sec": "page_reads",
    "Page writes/sec": "page_writes",
    "Lock Waits/sec": "lock_waits",
    "Number of Deadlocks/sec": "deadlocks",
    "Logins/sec": "logins",
}
GAUGE_COUNTERS = {
    "User Connections": "connections",
    "Page life expectancy": "ple",
    "Processes blocked": "blocked",
    "Memory Grants Pending": "grants_pending",
    "Target Server Memory (KB)": "target_memory_kb",
    "Total Server Memory (KB)": "total_memory_kb",
}

# Ignore the idle/background waits the usual wait-stats scripts skip.
IGNORED_WAITS = {
    "BROKER_EVENTHANDLER", "BROKER_RECEIVE_WAITFOR", "BROKER_TASK_STOP", "BROKER_TO_FLUSH", "BROKER_TRANSMITTER",
    "CHECKPOINT_QUEUE", "CHKPT", "CLR_AUTO_EVENT", "CLR_MANUAL_EVENT", "CLR_SEMAPHORE", "DBMIRROR_DBM_EVENT",
    "DBMIRROR_EVENTS_QUEUE", "DBMIRROR_WORKER_QUEUE", "DBMIRRORING_CMD", "DIRTY_PAGE_POLL", "DISPATCHER_QUEUE_SEMAPHORE",
    "EXECSYNC", "FSAGENT", "FT_IFTS_SCHEDULER_IDLE_WAIT", "FT_IFTSHC_MUTEX", "HADR_CLUSAPI_CALL", "HADR_FILESTREAM_IOMGR_IOCOMPLETION",
    "HADR_LOGCAPTURE_WAIT", "HADR_NOTIFICATION_DEQUEUE", "HADR_TIMER_TASK", "HADR_WORK_QUEUE", "KSOURCE_WAKEUP",
    "LAZYWRITER_SLEEP", "LOGMGR_QUEUE", "MEMORY_ALLOCATION_EXT", "ONDEMAND_TASK_QUEUE", "PARALLEL_REDO_DRAIN_WORKER",
    "PARALLEL_REDO_LOG_CACHE", "PARALLEL_REDO_TRAN_LIST", "PARALLEL_REDO_WORKER_SYNC", "PARALLEL_REDO_WORKER_WAIT_WORK",
    "PREEMPTIVE_OS_FLUSHFILEBUFFERS", "PREEMPTIVE_XE_GETTARGETSTATE", "PVS_PREALLOCATE", "PWAIT_ALL_COMPONENTS_INITIALIZED",
    "PWAIT_DIRECTLOGCONSUMER_GETNEXT", "PWAIT_EXTENSIBILITY_CLEANUP_TASK", "QDS_PERSIST_TASK_MAIN_LOOP_SLEEP",
    "QDS_ASYNC_QUEUE", "QDS_CLEANUP_STALE_QUERIES_TASK_MAIN_LOOP_SLEEP", "QDS_SHUTDOWN_QUEUE", "REDO_THREAD_PENDING_WORK",
    "REQUEST_FOR_DEADLOCK_SEARCH", "RESOURCE_QUEUE", "SERVER_IDLE_CHECK", "SLEEP_BPOOL_FLUSH", "SLEEP_DBSTARTUP",
    "SLEEP_DCOMSTARTUP", "SLEEP_MASTERDBREADY", "SLEEP_MASTERMDREADY", "SLEEP_MASTERUPGRADED", "SLEEP_MSDBSTARTUP",
    "SLEEP_SYSTEMTASK", "SLEEP_TASK", "SLEEP_TEMPDBSTARTUP", "SNI_HTTP_ACCEPT", "SOS_WORK_DISPATCHER", "SP_SERVER_DIAGNOSTICS_SLEEP",
    "SQLTRACE_BUFFER_FLUSH", "SQLTRACE_INCREMENTAL_FLUSH_SLEEP", "SQLTRACE_WAIT_ENTRIES", "VDI_CLIENT_OTHER", "WAIT_FOR_RESULTS",
    "WAITFOR", "WAITFOR_TASKSHUTDOWN", "WAIT_XTP_RECOVERY", "WAIT_XTP_HOST_WAIT", "WAIT_XTP_OFFLINE_CKPT_NEW_LOG",
    "WAIT_XTP_CKPT_CLOSE", "XE_DISPATCHER_JOIN", "XE_DISPATCHER_WAIT", "XE_TIMER_EVENT", "XE_LIVE_TARGET_TVF",
}


def wait_category(wait_type: str) -> str:
    w = wait_type.upper()
    if w.startswith(("LCK_",)):
        return "Lock"
    if w.startswith(("PAGEIOLATCH_", "IO_COMPLETION", "WRITELOG", "LOGBUFFER", "ASYNC_IO_COMPLETION", "BACKUPIO")):
        return "I/O"
    if w.startswith(("PAGELATCH_", "LATCH_")):
        return "Latch"
    if w.startswith(("CXPACKET", "CXCONSUMER", "CXSYNC", "EXCHANGE")):
        return "Parallelism"
    if w.startswith(("SOS_SCHEDULER_YIELD", "THREADPOOL")):
        return "CPU"
    if w.startswith(("RESOURCE_SEMAPHORE", "CMEMTHREAD", "MEMORY")):
        return "Memory"
    if w.startswith(("ASYNC_NETWORK_IO", "NETWORK", "OLEDB")):
        return "Network"
    if w.startswith(("PREEMPTIVE_",)):
        return "Preemptive"
    return "Other"


WAIT_CATEGORIES = ["CPU", "I/O", "Lock", "Latch", "Memory", "Network", "Parallelism", "Preemptive", "Other"]


@dataclass
class Sample:
    at: float
    rates: dict[str, float] = field(default_factory=dict)
    gauges: dict[str, float] = field(default_factory=dict)
    cpu_sql: float = 0.0
    cpu_other: float = 0.0
    active_requests: int = 0
    sessions: int = 0
    waits: dict[str, float] = field(default_factory=dict)  # category -> ms waited per second


@dataclass
class ActiveRequest:
    session_id: int
    status: str
    command: str
    database: str
    wait_type: str
    elapsed_ms: int
    cpu_ms: int
    reads: int
    login: str
    program: str
    host: str
    text: str
    blocked_by: int


class Series:
    def __init__(self, maxlen: int):
        self.times: deque[float] = deque(maxlen=maxlen)
        self.values: dict[str, deque[float]] = {}
        self.maxlen = maxlen

    def append(self, at: float, **values: float) -> None:
        self.times.append(at)
        for key, value in values.items():
            self.values.setdefault(key, deque([0.0] * (len(self.times) - 1), maxlen=self.maxlen)).append(value)
        for key, series in self.values.items():
            if key not in values:
                series.append(series[-1] if series else 0.0)

    def latest(self, key: str, default: float = 0.0) -> float:
        series = self.values.get(key)
        return series[-1] if series else default

    def clear(self) -> None:
        self.times.clear()
        self.values.clear()


class Monitor:
    """Samples one server on a background thread while ``running``."""

    def __init__(self, connect: Callable[[], SqlConnection], interval: float, history: int):
        self._connect = connect
        self.interval = interval
        self.series = Series(history)
        self.requests: list[ActiveRequest] = []
        self.top_waits: list[tuple[str, float]] = []
        self.last_sample: Sample | None = None
        self.error: str | None = None
        self.listeners: list[Callable[[Sample | None, str | None], None]] = []
        self._prev_counters: dict[str, float] | None = None
        self._prev_waits: dict[str, float] | None = None
        self._prev_time: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="replicate-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _notify(self, sample: Sample | None, error: str | None) -> None:
        from replicate.tasks import on_main

        for listener in list(self.listeners):
            on_main(listener, sample, error)

    def _loop(self) -> None:
        conn: SqlConnection | None = None
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                if conn is None:
                    conn = self._connect()
                    self._prev_counters = None
                    self._prev_waits = None
                sample = self.sample_once(conn)
                self.error = None
                self._notify(sample, None)
            except SqlError as exc:
                self.error = str(exc)
                if conn is not None:
                    conn.close()
                conn = None
                self._notify(None, self.error)
            except Exception as exc:  # noqa: BLE001
                log.exception("monitor sample failed")
                self.error = str(exc)
                self._notify(None, self.error)
            remaining = self.interval - (time.monotonic() - started)
            if self._stop.wait(max(0.5, remaining)):
                break
        if conn is not None:
            conn.close()

    # -- one sample ---------------------------------------------------------
    def sample_once(self, conn: SqlConnection) -> Sample:
        now = time.time()
        names = ", ".join(f"N'{n}'" for n in list(RATE_COUNTERS) + list(GAUGE_COUNTERS))
        rows = conn.query(
            f"SELECT RTRIM(counter_name) AS name, RTRIM(object_name) AS obj, RTRIM(instance_name) AS inst, cntr_value AS value"
            f" FROM sys.dm_os_performance_counters WHERE RTRIM(counter_name) IN ({names})"
            f" AND (instance_name = '' OR instance_name = '_Total')"
        )
        counters: dict[str, float] = {}
        for row in rows:
            name = row["name"]
            if name in counters and row["inst"] != "_Total":
                continue
            counters[name] = float(row["value"])

        sample = Sample(at=now)
        elapsed = now - self._prev_time if self._prev_time else 0.0
        for counter, key in RATE_COUNTERS.items():
            value = counters.get(counter, 0.0)
            if self._prev_counters is not None and elapsed > 0:
                sample.rates[key] = max(0.0, (value - self._prev_counters.get(counter, value)) / elapsed)
            else:
                sample.rates[key] = 0.0
        for counter, key in GAUGE_COUNTERS.items():
            sample.gauges[key] = counters.get(counter, 0.0)
        self._prev_counters = counters

        cpu = conn.query(
            "SELECT TOP 1 record.value('(./Record/SchedulerMonitorEvent/SystemHealth/ProcessUtilization)[1]', 'int') AS sql_cpu,"
            " record.value('(./Record/SchedulerMonitorEvent/SystemHealth/SystemIdle)[1]', 'int') AS idle"
            " FROM (SELECT CONVERT(xml, record) AS record FROM sys.dm_os_ring_buffers"
            " WHERE ring_buffer_type = N'RING_BUFFER_SCHEDULER_MONITOR' AND record LIKE '%<SystemHealth>%') AS x"
            " ORDER BY record.value('(./Record/@time)[1]', 'bigint') DESC"
        )
        if cpu:
            sample.cpu_sql = float(cpu[0]["sql_cpu"] or 0)
            sample.cpu_other = max(0.0, 100.0 - sample.cpu_sql - float(cpu[0]["idle"] or 0))

        reqs = conn.query(
            "SELECT r.session_id, r.status, r.command, DB_NAME(r.database_id) AS db, ISNULL(r.wait_type, '') AS wait_type,"
            " r.total_elapsed_time, r.cpu_time, r.logical_reads, s.login_name, ISNULL(s.program_name, '') AS program,"
            " ISNULL(s.host_name, '') AS host, r.blocking_session_id,"
            " SUBSTRING(t.text, (r.statement_start_offset / 2) + 1,"
            "   ((CASE r.statement_end_offset WHEN -1 THEN DATALENGTH(t.text) ELSE r.statement_end_offset END"
            "     - r.statement_start_offset) / 2) + 1) AS stmt"
            " FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id"
            " OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t"
            " WHERE s.is_user_process = 1 AND r.session_id <> @@SPID ORDER BY r.total_elapsed_time DESC"
        )
        self.requests = [
            ActiveRequest(
                session_id=int(r["session_id"]),
                status=r["status"] or "",
                command=r["command"] or "",
                database=r["db"] or "",
                wait_type=r["wait_type"] or "",
                elapsed_ms=int(r["total_elapsed_time"] or 0),
                cpu_ms=int(r["cpu_time"] or 0),
                reads=int(r["logical_reads"] or 0),
                login=r["login_name"] or "",
                program=r["program"] or "",
                host=r["host"] or "",
                text=(r["stmt"] or "").strip(),
                blocked_by=int(r["blocking_session_id"] or 0),
            )
            for r in reqs
        ]
        sample.active_requests = len(self.requests)
        sample.sessions = int(conn.scalar("SELECT COUNT(*) FROM sys.dm_exec_sessions WHERE is_user_process = 1") or 0)

        waits_rows = conn.query("SELECT wait_type, wait_time_ms FROM sys.dm_os_wait_stats WHERE waiting_tasks_count > 0")
        waits = {r["wait_type"]: float(r["wait_time_ms"]) for r in waits_rows if r["wait_type"] not in IGNORED_WAITS}
        categories = {c: 0.0 for c in WAIT_CATEGORIES}
        top: dict[str, float] = {}
        if self._prev_waits is not None and elapsed > 0:
            for wait_type, value in waits.items():
                delta = value - self._prev_waits.get(wait_type, value)
                if delta <= 0:
                    continue
                per_second = delta / elapsed
                categories[wait_category(wait_type)] += per_second
                top[wait_type] = per_second
        self._prev_waits = waits
        self._prev_time = now
        sample.waits = categories
        self.top_waits = sorted(top.items(), key=lambda kv: kv[1], reverse=True)[:8]

        self.series.append(
            now,
            cpu_sql=sample.cpu_sql,
            cpu_other=sample.cpu_other,
            active_requests=float(sample.active_requests),
            sessions=float(sample.sessions),
            **{f"rate_{k}": v for k, v in sample.rates.items()},
            **{f"gauge_{k}": v for k, v in sample.gauges.items()},
            **{f"wait_{k}": v for k, v in categories.items()},
        )
        self.last_sample = sample
        return sample
