import datetime as dt

from replicate.sql.monitor import Series, wait_category
from replicate.sql.profiler import SESSION_NAME, build_session_sql, parse_ring_buffer

RING = """<RingBufferTarget truncated="0" processingTime="0" totalEventsProcessed="2" eventCount="2" droppedCount="0" memoryUsed="1024">
  <event name="sql_batch_completed" package="sqlserver" timestamp="2026-10-20T14:03:01.123Z">
    <data name="cpu_time"><value>15000</value></data>
    <data name="duration"><value>42000</value></data>
    <data name="physical_reads"><value>0</value></data>
    <data name="logical_reads"><value>128</value></data>
    <data name="writes"><value>2</value></data>
    <data name="row_count"><value>7</value></data>
    <data name="result"><value>0</value><text>OK</text></data>
    <data name="batch_text"><value>SELECT * FROM Sales WHERE Id = 7</value></data>
    <action name="username" package="sqlserver"><value>sa</value></action>
    <action name="database_name" package="sqlserver"><value>Sales</value></action>
    <action name="client_app_name" package="sqlserver"><value>SSMS</value></action>
    <action name="client_hostname" package="sqlserver"><value>DEV</value></action>
    <action name="session_id" package="sqlserver"><value>55</value></action>
  </event>
  <event name="rpc_completed" package="sqlserver" timestamp="2026-10-20T14:03:02.000Z">
    <data name="cpu_time"><value>0</value></data>
    <data name="duration"><value>1000</value></data>
    <data name="object_name"><value>usp_GetThing</value></data>
    <data name="statement"><value>exec usp_GetThing 3</value></data>
    <action name="session_id" package="sqlserver"><value>56</value></action>
  </event>
</RingBufferTarget>"""


def test_parse_ring_buffer():
    events = parse_ring_buffer(RING)
    assert len(events) == 2
    first = events[0]
    assert first.name == "sql_batch_completed"
    assert first.duration_ms == 42.0 and first.cpu_ms == 15.0
    assert first.reads == 128 and first.writes == 2 and first.rows == 7
    assert first.text.startswith("SELECT * FROM Sales")
    assert first.login == "sa" and first.database == "Sales" and first.app == "SSMS" and first.session_id == 55
    assert first.result == "OK"
    assert first.timestamp.tzinfo is not None
    assert events[1].object_name == "usp_GetThing" and events[1].text == "exec usp_GetThing 3"


def test_build_session_sql_filters_and_events():
    sql = build_session_sql(["sql_batch_completed", "rpc_completed", "not_an_event"], 50, ["Replicate", "O'Brien"])
    assert sql.startswith(f"CREATE EVENT SESSION [{SESSION_NAME}] ON SERVER")
    assert "sqlserver.sql_batch_completed" in sql and "sqlserver.rpc_completed" in sql
    assert "not_an_event" not in sql
    assert "duration >= 50000" in sql
    assert "client_app_name <> N'Replicate'" in sql and "N'O''Brien'" in sql
    assert "package0.ring_buffer" in sql


def test_wait_categories():
    assert wait_category("LCK_M_X") == "Lock"
    assert wait_category("PAGEIOLATCH_SH") == "I/O"
    assert wait_category("PAGELATCH_EX") == "Latch"
    assert wait_category("CXPACKET") == "Parallelism"
    assert wait_category("SOS_SCHEDULER_YIELD") == "CPU"
    assert wait_category("ASYNC_NETWORK_IO") == "Network"
    assert wait_category("SOMETHING_NEW") == "Other"


def test_series_keeps_columns_aligned():
    s = Series(maxlen=3)
    s.append(1.0, a=1.0)
    s.append(2.0, a=2.0, b=5.0)
    s.append(3.0, b=6.0)
    s.append(4.0, a=4.0, b=7.0)
    assert list(s.times) == [2.0, 3.0, 4.0]
    assert list(s.values["a"]) == [2.0, 2.0, 4.0]
    assert list(s.values["b"]) == [5.0, 6.0, 7.0]
    assert s.latest("a") == 4.0


def test_profiler_summary_groups_similar_statements():
    from replicate.sql.profiler import Profiler, TraceEvent

    p = Profiler(lambda: None, events=["sql_batch_completed"], min_duration_ms=0, exclude_apps=[])
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(5):
        p.rows.append(TraceEvent(timestamp=now, name="sql_batch_completed", duration_ms=10 * (i + 1), text=f"SELECT * FROM T WHERE Id = {i}", app="A"))
    p.rows.append(TraceEvent(timestamp=now, name="sql_batch_completed", duration_ms=500, text="EXEC Big", app="B"))
    summary = p.summary()
    assert summary.total == 6
    assert summary.top_statements[0][0] == "EXEC Big"
    assert summary.top_statements[1] == ("SELECT * FROM T WHERE Id = ?", 150.0, 5)
    assert summary.by_app == {"A": 5, "B": 1}
    hist = dict(p.duration_histogram())
    assert hist[">5000 ms"] == 0 and sum(hist.values()) == 6
