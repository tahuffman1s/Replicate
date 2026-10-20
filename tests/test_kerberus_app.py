import json
import os
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
