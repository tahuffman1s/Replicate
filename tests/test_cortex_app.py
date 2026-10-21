import json

import pytest

from replicate.config import Share
from replicate.vhd import cortex_app
from replicate.vhd import shares as sh


@pytest.fixture
def cortex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("REPLICATE_NO_KEYRING", "1")
    monkeypatch.setenv("PATH", str(tmp_path / "nobin"))
    d = tmp_path / "config" / "cortex"
    d.mkdir(parents=True)
    return d


def test_reads_drives_and_account_the_way_cortex_writes_them(cortex_home):
    (cortex_home / "drives.json").write_text(json.dumps([
        {"name": "DEV images", "path": r"\\prodfs\sqlcloneimages$", "login": True, "sidebar": True},
        {"name": "Backups", "path": "smb://fileserver/backups/sql", "login": False},
        {"name": "broken"},
        {"name": "no share", "path": r"\\server"},
    ]))
    (cortex_home / "account.json").write_text(json.dumps({"user": "travis", "domain": "CORP"}))
    drives = cortex_app.drives()
    assert [d.name for d in drives] == ["DEV images", "Backups"]
    assert drives[0].path == r"\\prodfs\sqlcloneimages$" and drives[0].url == "smb://prodfs/sqlcloneimages$" and drives[0].login
    assert drives[1].path == r"\\fileserver\backups\sql" and drives[1].server == "fileserver"
    acct = cortex_app.account()
    assert acct.is_set and acct.display() == r"CORP\travis" and acct.attributes() == {"user": "travis", "domain": "CORP"}
    assert cortex_app.installed()
    assert cortex_app.password(acct) is None  # no keyring in tests
    assert "2 drives" in cortex_app.describe() and r"CORP\travis" in cortex_app.describe()


def test_without_cortex(cortex_home):
    cortex_home.rmdir()
    assert cortex_app.drives() == [] and not cortex_app.account().is_set
    assert not cortex_app.installed() and cortex_app.describe() == "not installed"
    assert sh.default_auth() == "kerberos"


def test_share_paths_match_case_blind():
    assert cortex_app.same_share(r"\\PRODFS\SqlCloneImages$\DEV", "smb://prodfs/sqlcloneimages$/DEV/ACCT")
    assert not cortex_app.same_share(r"\\prodfs\other$", "smb://prodfs/sqlcloneimages$")
    assert cortex_app.is_within("smb://prodfs/sqlcloneimages$/DEV/ACCT", r"\\prodfs\sqlcloneimages$\DEV")
    assert not cortex_app.is_within("smb://prodfs/sqlcloneimages$/QA", r"\\prodfs\sqlcloneimages$\DEV")
    assert cortex_app.smb_uri(r"\\prodfs\sqlcloneimages$\DEV ACCT") == "smb://prodfs/sqlcloneimages%24/DEV%20ACCT/"
    assert cortex_app.is_gvfs_path("/run/user/1000/gvfs/smb-share:server=prodfs,share=sqlcloneimages$/DEV/vhd")
    assert not cortex_app.is_gvfs_path("/run/replicate/shares/abc/DEV/vhd")


def test_share_from_drive_signs_in_through_cortex(cortex_home):
    (cortex_home / "account.json").write_text(json.dumps({"user": "travis", "domain": "CORP"}))
    drive = cortex_app.Drive("DEV images", r"\\prodfs\sqlcloneimages$")
    share = sh.share_from_drive(drive, r"DEV\ACCT")
    assert (share.url, share.auth, share.label) == ("smb://prodfs/sqlcloneimages$/DEV/ACCT", "cortex", "DEV images")
    assert sh.share_from_drive(drive).url == "smb://prodfs/sqlcloneimages$"
    assert sh.default_auth() == "cortex" and sh.share_from_uri("smb://x/y").auth == "cortex"
    opts = sh.mount_options(share, uid=1000, gid=1000).split(",")
    assert "sec=krb5" not in opts and "guest" not in opts and "uid=1000" in opts
    assert cortex_app.drive_for_share("smb://PRODFS/sqlcloneimages$/x", [drive]) is drive
    assert cortex_app.drive_for_share("smb://prodfs/other", [drive]) is None


def test_cortex_credentials_need_account_and_keyring(cortex_home):
    share = Share(url="smb://prodfs/sqlcloneimages$", auth="cortex")
    with pytest.raises(sh.ShareError, match="no sign-in yet"):
        sh.credentials_for(share, None)
    (cortex_home / "account.json").write_text(json.dumps({"user": "travis", "domain": "CORP"}))
    with pytest.raises(sh.ShareError, match="keyring"):
        sh.credentials_for(share, None)
    assert sh.credentials_for(Share(url="smb://s/x", auth="password", username="u", domain="D"), "pw") == {"username": "u", "password": "pw", "domain": "D"}
    assert sh.credentials_for(Share(url="smb://s/x", auth="kerberos"), None) is None
    assert sh.credentials_for(Share(url="smb://s/x", auth="guest"), None) is None


def test_cortex_password_is_what_the_helper_signs_in_with(cortex_home, monkeypatch):
    (cortex_home / "account.json").write_text(json.dumps({"user": "travis", "domain": "CORP"}))
    monkeypatch.setattr(cortex_app, "password", lambda acct=None: "s3cret")
    assert sh.credentials_for(Share(url="smb://s/x", auth="cortex"), None) == {"username": "travis", "password": "s3cret", "domain": "CORP"}


def test_drive_for_gvfs_path():
    drive = cortex_app.Drive("DEV", r"\\prodfs\sqlcloneimages$")
    connected = [(drive, "/run/user/1000/gvfs/smb-share:server=prodfs,share=sqlcloneimages$")]
    assert cortex_app.drive_for_path("/run/user/1000/gvfs/smb-share:server=prodfs,share=sqlcloneimages$/DEV/ACCT/vhd", connected) is drive
    assert cortex_app.drive_for_path("/run/user/1000/gvfs/smb-share:server=other,share=x/vhd", connected) is None


def test_find_cortex_from_desktop_entry(cortex_home, tmp_path):
    apps = tmp_path / "data" / "applications"
    apps.mkdir(parents=True)
    exe = tmp_path / "Cortex-0.1.2-x86_64.AppImage"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (apps / "io.github.tahuffman1s.Cortex.desktop").write_text(f"[Desktop Entry]\nExec={exe} %U\n")
    found = cortex_app.find_cortex()
    assert found.available and found.command == [str(exe)] and found.source == "desktop"
    (cortex_home.parent / "autostart").mkdir()
    (cortex_home.parent / "autostart" / "io.github.tahuffman1s.Cortex.desktop").write_text(f"[Desktop Entry]\nExec={exe} --connect\n")
    assert cortex_app.find_cortex().source == "autostart" and cortex_app.find_cortex().command == [str(exe)]
