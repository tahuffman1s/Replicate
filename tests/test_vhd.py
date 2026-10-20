import shutil
import subprocess

import pytest

from replicate.vhd import restore as rs
from replicate.vhd.clones import create_clone, delete_clone, list_clones
from replicate.vhd.images import inspect, scan

qemu_img = shutil.which("qemu-img")


def test_plan_attachments_groups_files_by_stem():
    files = [
        {"path": r"E:\Data\Sales.mdf", "kind": "mdf"},
        {"path": r"E:\Data\Sales_log.ldf", "kind": "ldf"},
        {"path": r"E:\Data\Sales_1.ndf", "kind": "ndf"},
        {"path": r"E:\Data\HR.mdf", "kind": "mdf"},
        {"path": r"E:\Data\HR_Log.ldf", "kind": "ldf"},
        {"path": r"E:\Other\orphan_log.ldf", "kind": "ldf"},
        {"path": r"E:\Backups\Sales.bak", "kind": "bak"},
    ]
    plans = rs.plan_attachments(files)
    assert [p.database for p in plans] == ["HR", "Sales"]
    sales = plans[1]
    assert sales.files[0].endswith("Sales.mdf")
    assert sales.files[1].endswith("Sales_1.ndf")
    assert sales.files[2].endswith("Sales_log.ldf")
    backups = rs.plan_backups(files)
    assert [b.database for b in backups] == ["Sales"]


def test_attach_sql_quotes_identifiers_and_paths():
    class Fake:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None, timeout=None):
            self.sql.append(sql)

        def query(self, sql, params=None, timeout=None):
            return []

    conn = Fake()
    plan = rs.AttachPlan("We're]Odd", [r"E:\x'y.mdf", r"E:\x'y_log.ldf"])
    rs.attach_database(conn, plan)
    assert conn.sql[0].startswith("CREATE DATABASE [We're]]Odd] ON")
    assert "N'E:\\x''y.mdf'" in conn.sql[0]
    assert conn.sql[0].rstrip().endswith("FOR ATTACH;")


def test_restore_sql_moves_each_logical_file():
    class Fake:
        def __init__(self):
            self.sql = []

        def execute(self, sql, params=None, timeout=None):
            self.sql.append(sql)

        def query(self, sql, params=None, timeout=None):
            if "FILELISTONLY" in sql:
                return [
                    {"LogicalName": "Sales", "PhysicalName": r"C:\old\Sales.mdf", "Type": "D"},
                    {"LogicalName": "Sales_log", "PhysicalName": r"C:\old\Sales_log.ldf", "Type": "L"},
                ]
            if "InstanceDefaultDataPath" in sql:
                return [{"data": r"C:\Data\\", "log": r"C:\Log\\"}]
            return []

    conn = Fake()
    rs.restore_database(conn, rs.BackupPlan("Sales", r"E:\Sales.bak"), "Sales2", replace=True)
    restore = conn.sql[-1]
    assert "RESTORE DATABASE [Sales2] FROM DISK = N'E:\\Sales.bak'" in restore
    assert "MOVE N'Sales' TO N'C:\\Data\\Sales2_Sales.mdf'" in restore
    assert "MOVE N'Sales_log' TO N'C:\\Log\\Sales2_Sales_log.ldf'" in restore
    assert "REPLACE" in restore and "RECOVERY" in restore


@pytest.mark.skipif(qemu_img is None, reason="qemu-img not installed")
def test_inspect_scan_and_clone_real_vhdx(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    subprocess.run([qemu_img, "create", "-q", "-f", "vhdx", str(images / "Sales.vhdx"), "64M"], check=True)
    subprocess.run([qemu_img, "create", "-q", "-f", "vpc", str(images / "Legacy.vhd"), "64M"], check=True)
    (images / "notes.txt").write_text("ignored")
    found = scan([str(images)])
    assert sorted(i.name for i in found) == ["Legacy.vhd", "Sales.vhdx"]
    vhdx = next(i for i in found if i.name == "Sales.vhdx")
    assert vhdx.format == "vhdx" and vhdx.format_label == "VHDX"
    assert vhdx.virtual_size == 64 * 1024 * 1024
    assert not vhdx.is_differencing

    clones = tmp_path / "clones"
    clone = create_clone(vhdx, clones, "Sales test")
    info = inspect(clone)
    assert info.format == "qcow2"
    assert info.is_clone and info.backing_file == str(images / "Sales.vhdx")
    listed = list_clones(clones)
    assert len(listed) == 1 and listed[0].image_path.endswith("Sales.vhdx")
    # Clones are hidden from the image library unless asked for.
    assert all(not i.is_clone for i in scan([str(images), str(clones)]))
    assert any(i.is_clone for i in scan([str(clones)], include_clones=True))
    delete_clone(clone)
    assert list_clones(clones) == []
