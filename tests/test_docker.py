import json
import os
import stat

from replicate.config import AppConfig, Mount, SqlServer
from replicate.docker.engine import CONTAINER_MOUNTS, ContainerSpec, Docker, _status_word, free_port, generate_sa_password
from replicate.docker.manager import ContainerManager
from replicate.secrets import SecretStore
from replicate.sql.servers import ServerRegistry
from replicate.vhd import nbd
from replicate.vhd import restore as rs


def test_create_args_cover_what_sql_server_needs(tmp_path):
    spec = ContainerSpec(name="sales", port=14331, sa_password="Secret1!x", memory_mib=2048,
                         data_dir=str(tmp_path / "data"), mounts_dir=str(tmp_path / "mounts"))
    args = Docker("docker").create_args(spec)
    text = " ".join(args)
    assert args[0] == "create" and args[-1] == "mcr.microsoft.com/mssql/server:2022-latest"
    assert "--name sales" in text and "-p 127.0.0.1:14331:1433" in text
    assert "ACCEPT_EULA=Y" in text and "MSSQL_PID=Developer" in text and "MSSQL_SA_PASSWORD=Secret1!x" in text
    assert "--memory 2048m" in text and "MSSQL_MEMORY_LIMIT_MB=1638" in text
    assert f"target=/var/opt/mssql" in text
    assert f"target={CONTAINER_MOUNTS},bind-propagation=rslave" in text
    assert "io.github.tahuffman1s.replicate=1" in text and "io.github.tahuffman1s.replicate.port=14331" in text
    assert "host.docker.internal:host-gateway" in text


def test_status_words_and_helpers():
    assert _status_word("Up 3 minutes") == "running"
    assert _status_word("running") == "running"
    assert _status_word("Exited (0) 2 hours ago") == "exited"
    assert _status_word("") == "unknown"
    pw = generate_sa_password()
    assert len(pw) >= 12 and any(c.isdigit() for c in pw) and any(c.isupper() for c in pw)
    port = free_port(14330, 14340)
    assert 14330 <= port < 14340


def test_container_path_mapping(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.docker.data_root = str(tmp_path / "docker")
    manager = ContainerManager(cfg, __import__("replicate.config", fromlist=["AppState"]).AppState(), SecretStore(tmp_path / "s.json", use_keyring=False))
    mounts = manager.mounts_dir("sales")
    mounts.mkdir(parents=True)
    inner = mounts / "abc" / "p1"
    inner.mkdir(parents=True)
    assert manager.container_path("sales", str(inner / "Sales.mdf")) == "/mnt/replicate/abc/p1/Sales.mdf"
    try:
        manager.container_path("sales", str(tmp_path / "elsewhere.mdf"))
    except Exception as exc:  # noqa: BLE001
        assert "cannot see" in str(exc)
    else:
        raise AssertionError("paths outside the mounts dir must be refused")


def test_server_where_and_docker_servers():
    cfg = AppConfig()
    vm = SqlServer(name="vm", vm_managed=True)
    legacy_remote = SqlServer(name="r", vm_managed=False)
    docker = SqlServer(name="d", vm_managed=False, backend="docker", container="sales", host="127.0.0.1", port=14331)
    cfg.servers += [vm, legacy_remote, docker]
    assert (vm.where, legacy_remote.where, docker.where) == ("vm", "remote", "docker")
    assert cfg.vm_servers() == [vm] and cfg.docker_servers() == [docker]
    assert docker.in_docker and not docker.in_vm


def test_link_data_sources_between_backends(tmp_path):
    cfg = AppConfig()
    secrets = SecretStore(tmp_path / "s.json", use_keyring=False)
    registry = ServerRegistry(cfg, secrets, vm=None)  # type: ignore[arg-type]
    a = SqlServer(name="a", backend="docker", container="sales", host="127.0.0.1", port=14331, vm_managed=False)
    b = SqlServer(name="b", backend="docker", container="acct", host="127.0.0.1", port=14332, vm_managed=False)
    remote = SqlServer(name="r", vm_managed=False, host="sql01.example.net", port=1433)
    local = SqlServer(name="l", vm_managed=False, host="localhost", port=1444)
    assert registry.data_source(b, source=a) == "acct,1433"
    assert registry.data_source(remote, source=a) == "sql01.example.net,1433"
    assert registry.data_source(local, source=a) == "host.docker.internal,1444"
    assert registry.data_source(a, source=remote) == "127.0.0.1,14331"


def test_scan_files_finds_database_files(tmp_path):
    root = tmp_path / "p1"
    (root / "Data").mkdir(parents=True)
    (root / "Data" / "Sales.mdf").write_bytes(b"x" * 10)
    (root / "Data" / "Sales_log.ldf").write_bytes(b"x")
    (root / "Backups").mkdir()
    (root / "Backups" / "Sales.bak").write_bytes(b"x" * 5)
    (root / "notes.txt").write_text("no")
    (root / "$RECYCLE.BIN").mkdir()
    (root / "$RECYCLE.BIN" / "old.mdf").write_bytes(b"x")
    files = nbd.scan_files([str(root)])
    kinds = sorted(f["kind"] for f in files)
    assert kinds == ["bak", "ldf", "mdf"]
    assert all("$RECYCLE" not in f["path"] for f in files)
    plans = rs.plan_attachments(files)
    assert [p.database for p in plans] == ["Sales"]
    assert rs.plan_backups(files)[0].backup_file.endswith("Sales.bak")


def test_copy_into_container_uses_docker_cp():
    calls = []

    class FakeDocker:
        def exec(self, name, *cmd, user=None, timeout=120):
            calls.append(("exec", name, cmd, user))
            return ""

        def copy_in(self, name, host_path, container_path):
            calls.append(("cp", name, host_path, container_path))

    inside = rs.copy_into_container(FakeDocker(), "sales", ["/mnt/x/p1/Sales.mdf", "/mnt/x/p1/Sales_log.ldf"], "Sales Copy")
    assert inside == ["/var/opt/mssql/data/replicate/Sales_Copy/Sales.mdf", "/var/opt/mssql/data/replicate/Sales_Copy/Sales_log.ldf"]
    assert calls[0] == ("exec", "sales", ("mkdir", "-p", "/var/opt/mssql/data/replicate/Sales_Copy"), "root")
    assert calls[-1][2][0] == "chown"
    assert rs.looks_like_io_refusal("Unable to open the physical file \"/mnt/replicate/x\". Operating system error 22")
    assert not rs.looks_like_io_refusal("Database 'Sales' already exists")


def test_mount_record_round_trip(tmp_path):
    from replicate.config import AppState, load_state, save_state

    state = AppState()
    state.mounts.append(Mount(image_path="/i.vhdx", clone_path="/c.qcow2", backend="docker", container="sales", host_dir="/d/m/abc", mountpoints=["/d/m/abc/p1"], drive_letters=["/mnt/replicate/abc/p1"]))
    save_state(state, tmp_path / "state.json")
    loaded = load_state(tmp_path / "state.json")
    m = loaded.mounts[0]
    assert m.backend == "docker" and m.container == "sales" and m.mountpoints == ["/d/m/abc/p1"]


def test_mount_helper_refuses_without_root_and_parses_args(tmp_path):
    import subprocess
    import sys

    helper = os.path.join(os.path.dirname(rs.__file__), "mount_helper.py")
    proc = subprocess.run([sys.executable, helper, "mount", "/nope", str(tmp_path)], capture_output=True, text=True)
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    assert data["ok"] is False
    assert "root" in data["error"] or "not a file" in data["error"]
    assert stat.S_IMODE(os.stat(helper).st_mode) & stat.S_IXUSR
