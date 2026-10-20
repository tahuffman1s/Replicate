"""Turn the files on a mounted disk into databases on a SQL Server.

Two ways in, both plain T-SQL run from the host:

* **Attach** (``CREATE DATABASE ... FOR ATTACH``): the disk holds the data and
  log files themselves (a SQL Clone image, a copied data directory, a disk
  from another server). Nothing is copied; the database lives on the clone.
* **Restore** (``RESTORE DATABASE ... FROM DISK``): the disk holds ``.bak``
  files. The data files are written to the instance's default directories,
  or onto the clone disk itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PureWindowsPath

from replicate.sql.connection import SqlConnection
from replicate.util import quote_sql_identifier as qi
from replicate.util import quote_sql_string as qs


@dataclass
class AttachPlan:
    database: str
    files: list[str]  # primary .mdf first
    source: str = "attach"


@dataclass
class BackupPlan:
    database: str
    backup_file: str
    logical_files: list[dict] = field(default_factory=list)  # from RESTORE FILELISTONLY
    source: str = "restore"


def _stem(path: str) -> str:
    name = PureWindowsPath(path).stem
    # X_log, X_Log, X_1, X_Data -> X
    return re.sub(r"[_-](log|data|primary|\d+)$", "", name, flags=re.IGNORECASE)


def plan_attachments(files: list[dict]) -> list[AttachPlan]:
    """Group .mdf/.ndf/.ldf files that belong together, by their directory and stem."""
    groups: dict[tuple[str, str], list[str]] = {}
    for f in files:
        kind = f.get("kind", "").lower()
        if kind not in ("mdf", "ndf", "ldf"):
            continue
        path = f["path"]
        key = (str(PureWindowsPath(path).parent).lower(), _stem(path).lower())
        groups.setdefault(key, []).append(path)
    plans = []
    for (_, stem), paths in sorted(groups.items()):
        mdfs = [p for p in paths if p.lower().endswith(".mdf")]
        if not mdfs:
            continue
        ordered = mdfs + [p for p in paths if p.lower().endswith(".ndf")] + [p for p in paths if p.lower().endswith(".ldf")]
        plans.append(AttachPlan(database=PureWindowsPath(mdfs[0]).stem, files=ordered))
    return plans


def plan_backups(files: list[dict]) -> list[BackupPlan]:
    return [
        BackupPlan(database=PureWindowsPath(f["path"]).stem, backup_file=f["path"])
        for f in files
        if f.get("kind", "").lower() == "bak"
    ]


def existing_databases(conn: SqlConnection) -> list[str]:
    return [r["name"] for r in conn.query("SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name")]


def default_paths(conn: SqlConnection) -> tuple[str, str]:
    row = conn.query(
        "SELECT CAST(SERVERPROPERTY('InstanceDefaultDataPath') AS nvarchar(512)) AS data,"
        " CAST(SERVERPROPERTY('InstanceDefaultLogPath') AS nvarchar(512)) AS log"
    )[0]
    return row["data"] or "", row["log"] or row["data"] or ""


def attach_database(conn: SqlConnection, plan: AttachPlan, name: str | None = None, *, replace: bool = False) -> str:
    name = name or plan.database
    if replace and name in existing_databases(conn):
        drop_database(conn, name)
    files = ",\n  ".join(f"(FILENAME = {qs(p)})" for p in plan.files)
    conn.execute(f"CREATE DATABASE {qi(name)} ON\n  {files}\nFOR ATTACH;")
    return name


def backup_file_list(conn: SqlConnection, backup_file: str) -> list[dict]:
    rows = conn.query(f"RESTORE FILELISTONLY FROM DISK = {qs(backup_file)};")
    return [{"logical": r["LogicalName"], "physical": r["PhysicalName"], "type": r["Type"]} for r in rows]


def restore_database(
    conn: SqlConnection,
    plan: BackupPlan,
    name: str | None = None,
    *,
    target_dir: str | None = None,
    replace: bool = False,
) -> str:
    """Restore a full backup, moving each file into ``target_dir`` (or the defaults)."""
    name = name or plan.database
    data_dir, log_dir = default_paths(conn)
    if target_dir:
        data_dir = log_dir = target_dir.rstrip("\\") + "\\"
    files = plan.logical_files or backup_file_list(conn, plan.backup_file)
    moves = []
    used: set[str] = set()
    for f in files:
        ext = PureWindowsPath(f["physical"]).suffix or (".ldf" if f["type"] == "L" else ".mdf")
        base = re.sub(r"[^A-Za-z0-9_.-]", "_", f["logical"])
        candidate = f"{name}_{base}{ext}"
        counter = 1
        while candidate.lower() in used:
            counter += 1
            candidate = f"{name}_{base}_{counter}{ext}"
        used.add(candidate.lower())
        directory = log_dir if f["type"] == "L" else data_dir
        moves.append(f"MOVE {qs(f['logical'])} TO {qs(directory.rstrip(chr(92)) + chr(92) + candidate)}")
    options = ",\n  ".join(moves + ["RECOVERY", "STATS = 10"] + (["REPLACE"] if replace else []))
    conn.execute(f"RESTORE DATABASE {qi(name)} FROM DISK = {qs(plan.backup_file)} WITH\n  {options};", timeout=6 * 3600)
    return name


def detach_database(conn: SqlConnection, name: str) -> None:
    conn.execute(f"ALTER DATABASE {qi(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;")
    conn.execute(f"EXEC sp_detach_db @dbname = {qs(name)}, @skipchecks = 'true';")


def drop_database(conn: SqlConnection, name: str) -> None:
    conn.execute(f"ALTER DATABASE {qi(name)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;")
    conn.execute(f"DROP DATABASE {qi(name)};")


def database_files(conn: SqlConnection, name: str) -> list[str]:
    rows = conn.query(f"SELECT physical_name FROM sys.master_files WHERE database_id = DB_ID({qs(name)})")
    return [r["physical_name"] for r in rows]
