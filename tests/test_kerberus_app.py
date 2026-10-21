import json
import stat

from replicate.sql import kerberus_app as ka

LIST_JSON = json.dumps([
    {
        "principal": "travis@CORP.EXAMPLE.COM", "cache": "KCM:1000:12345", "default": True, "state": "valid", "renewable": True,
        "tickets": [
            {"service": "krbtgt/CORP.EXAMPLE.COM@CORP.EXAMPLE.COM", "starts": "2026-10-20T08:00:00", "expires": "2099-10-20T18:00:00",
             "renew_until": "2099-10-27T08:00:00", "flags": "FRIA"},
            {"service": "MSSQLSvc/sql01.corp.example.com:1433@CORP.EXAMPLE.COM", "starts": "2026-10-20T09:00:00", "expires": "2099-10-20T18:00:00",
             "renew_until": None, "flags": "FRA"},
        ],
    },
    {"principal": "admin@LAB.LOCAL", "cache": "KCM:1000:12346", "default": False, "state": "expired", "renewable": False,
     "tickets": [{"service": "krbtgt/LAB.LOCAL@LAB.LOCAL", "starts": "2026-10-19T08:00:00", "expires": "2026-10-19T18:00:00", "renew_until": None, "flags": "FIA"}]},
])


def test_parse_list_json_picks_the_tgt():
    caches = ka.parse_list_json(LIST_JSON)
    assert [c.principal for c in caches] == ["travis@CORP.EXAMPLE.COM", "admin@LAB.LOCAL"]
    first = caches[0]
    assert first.default and first.renewable and first.state == "valid"
    assert first.expires is not None and first.expires.year == 2099 and first.expires.tzinfo is not None
    assert first.renew_until is not None
    assert "renewed by Kerberus" in first.describe()
    assert "expired" in caches[1].describe()


def test_cache_for_matches_principal_or_user():
    caches = ka.parse_list_json(LIST_JSON)
    assert ka.cache_for(caches, "travis@CORP.EXAMPLE.COM").cache == "KCM:1000:12345"
    assert ka.cache_for(caches, "TRAVIS@corp.example.com").principal == "travis@CORP.EXAMPLE.COM"
    assert ka.cache_for(caches, "admin").principal == "admin@LAB.LOCAL"
    assert ka.cache_for(caches, "").default
    assert ka.cache_for(caches, "nobody@CORP.EXAMPLE.COM") is None


def test_find_kerberus_through_autostart_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert not ka.find_kerberus().available
    appimage = tmp_path / "Applications" / "Kerberus-1.4.2-x86_64.AppImage"
    appimage.parent.mkdir()
    appimage.write_text("#!/bin/sh\n")
    appimage.chmod(appimage.stat().st_mode | stat.S_IXUSR)
    autostart = tmp_path / "config" / "autostart"
    autostart.mkdir(parents=True)
    (autostart / f"{ka.KERBERUS_APP_ID}.desktop").write_text(
        f"[Desktop Entry]\nType=Application\nName=Kerberus\nExec={appimage} --background\n"
    )
    found = ka.find_kerberus()
    assert found.available and found.source == "autostart"
    assert found.command == [str(appimage)]
    # An explicit setting wins.
    explicit = ka.find_kerberus(str(appimage))
    assert explicit.source == "setting"


def test_list_caches_runs_the_command(tmp_path, monkeypatch):
    script = tmp_path / "kerberus"
    script.write_text("#!/bin/sh\n[ \"$1\" = list ] && cat <<'EOF'\n" + LIST_JSON + "\nEOF\n")
    script.chmod(0o755)
    caches = ka.list_caches(ka.Kerberus([str(script)], "path"))
    assert len(caches) == 2
    assert ka.list_caches(ka.Kerberus()) == []


def test_switch_and_maintain_run_kerberus(monkeypatch, tmp_path):
    import subprocess

    from replicate.sql import kerberus_app

    runs = []

    class Done:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: (runs.append(argv), Done())[1])
    k = kerberus_app.Kerberus(["/opt/kerberus"], "path")
    assert kerberus_app.switch(k, "travis@CORP.EXAMPLE.COM") and runs[-1] == ["/opt/kerberus", "switch", "travis@CORP.EXAMPLE.COM"]
    assert kerberus_app.maintain_once(k) and runs[-1] == ["/opt/kerberus", "watch", "--once"]
    assert not kerberus_app.maintain_once(kerberus_app.Kerberus())
    monkeypatch.setattr(kerberus_app, "which", lambda name: None)
    assert not kerberus_app.switch(kerberus_app.Kerberus(), "x@Y")


def test_usable_and_wait_for_ticket(monkeypatch):
    import datetime as dt

    from replicate.sql import kerberus_app

    live = kerberus_app.CacheInfo("a@R", "c", True, "valid", True, dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    dead = kerberus_app.CacheInfo("a@R", "c", True, "expired", True, dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
    assert kerberus_app.usable(live) and not kerberus_app.usable(dead) and not kerberus_app.usable(None)
    rounds = [[], [dead], [live]]
    monkeypatch.setattr(kerberus_app, "list_caches", lambda k, timeout=20: rounds.pop(0) if rounds else [live])
    found = kerberus_app.wait_for_ticket(kerberus_app.Kerberus(["k"]), "a@R", seconds=5, poll=0.01)
    assert found is live
    monkeypatch.setattr(kerberus_app, "list_caches", lambda k, timeout=20: [])
    assert kerberus_app.wait_for_ticket(kerberus_app.Kerberus(["k"]), "", seconds=0.05, poll=0.01) is None
