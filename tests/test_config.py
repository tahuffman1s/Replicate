import json

from replicate.config import AppConfig, AppState, Mount, RestoredDatabase, SqlServer, load_config, load_state, save_config, save_state


def test_config_roundtrip(tmp_path):
    cfg = AppConfig()
    cfg.vm.name = "sqlhost"
    cfg.vm.sql_instances = ["MSSQLSERVER", "ACCT"]
    s = SqlServer(name="Main", host="vm", port=1433, service_name="MSSQLSERVER")
    t = SqlServer(name="Acct", host="vm", port=14331, instance="ACCT", service_name="MSSQL$ACCT")
    s.linked_to.append(t.id)
    cfg.servers += [s, t]
    path = tmp_path / "config.json"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.vm.name == "sqlhost"
    assert loaded.vm.disk_path.endswith("sqlhost.qcow2")
    assert [x.name for x in loaded.servers] == ["Main", "Acct"]
    assert loaded.servers[0].linked_to == [t.id]
    assert loaded.server(t.id).display_instance == ".\\ACCT"
    assert loaded.vm_servers() == loaded.servers


def test_unknown_keys_and_missing_sections_are_tolerated(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "vm": {"name": "x", "future_option": 1}, "servers": [{"name": "A", "bogus": True}]}))
    cfg = load_config(path)
    assert cfg.vm.name == "x"
    assert cfg.servers[0].name == "A"
    assert cfg.monitor.interval_seconds == 2.0


def test_state_roundtrip(tmp_path):
    state = AppState()
    m = Mount(image_path="/img/a.vhdx", clone_path="/clones/a.qcow2", vm_name="v", target_dev="vdb", serial="rp-1")
    m.databases.append(RestoredDatabase(server_id="s1", name="Sales", mode="attach", files=["E:\\Sales.mdf"]))
    state.mounts.append(m)
    state.vm_install = {"phase": "sql", "message": "Installing"}
    path = tmp_path / "state.json"
    save_state(state, path)
    loaded = load_state(path)
    assert loaded.mounts[0].databases[0].name == "Sales"
    assert not loaded.mounts[0].is_direct
    assert loaded.vm_install["phase"] == "sql"


def test_secrets_file_backend(tmp_path):
    from replicate.secrets import SecretStore

    store = SecretStore(tmp_path / "secrets.json", use_keyring=False)
    assert store.backend == "file"
    store.set("sql:1", "hunter2")
    assert store.get("sql:1") == "hunter2"
    assert oct((tmp_path / "secrets.json").stat().st_mode & 0o777) == "0o600"
    store.delete("sql:1")
    assert store.get("sql:1") is None
