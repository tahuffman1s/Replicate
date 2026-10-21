import json
import os
import subprocess
import sys

import pytest

from replicate.config import AppConfig, Share, load_config, save_config
from replicate.vhd import shares as sh


def test_parse_share_urls():
    for text in ("smb://prodfs/sqlcloneimages$/DEV/ACCT/img_001", r"\\prodfs\sqlcloneimages$\DEV\ACCT\img_001", "//prodfs/sqlcloneimages$/DEV/ACCT/img_001"):
        parts = sh.parse_share_url(text)
        assert (parts.server, parts.share, parts.subpath) == ("prodfs", "sqlcloneimages$", "DEV/ACCT/img_001")
        assert parts.unc == "//prodfs/sqlcloneimages$"
        assert parts.url == "smb://prodfs/sqlcloneimages$/DEV/ACCT/img_001"
        assert parts.windows == r"\\prodfs\sqlcloneimages$\DEV\ACCT\img_001"
    assert sh.parse_share_url("smb://server/share").subpath == ""
    assert sh.parse_share_url("smb://server/share/a%20b").subpath == "a b"
    for bad in ("", "smb://server", "server", "smb://server/share/../x"):
        with pytest.raises(sh.ShareError):
            sh.parse_share_url(bad)
    assert sh.is_smb_uri("smb://x/y") and sh.is_smb_uri(r"\\x\y") and not sh.is_smb_uri("/mnt/x")


def test_mount_options_per_auth():
    share = Share(url="smb://s/sh/d", auth="kerberos")
    opts = sh.mount_options(share, uid=1000, gid=1000)
    assert "sec=krb5" in opts and "cruid=1000" in opts and "ro" in opts.split(",") and "file_mode=0644" in opts
    share = Share(url="smb://s/sh", auth="password", readonly=False)
    opts = sh.mount_options(share, uid=1000, gid=1000)
    assert "sec=krb5" not in opts and "ro" not in opts.split(",") and "credentials=" not in opts
    assert "guest" in sh.mount_options(Share(url="smb://s/sh", auth="guest"), uid=1, gid=1)


def test_paths_and_display():
    share = Share(id="abc123", url="smb://prodfs/sqlcloneimages$/DEV/ACCT")
    assert sh.mountpoint_for(share) == "/run/replicate/shares/abc123"
    assert sh.folder_path(share) == "/run/replicate/shares/abc123/DEV/ACCT"
    assert sh.display_name(share) == r"\\prodfs\sqlcloneimages$\DEV\ACCT"
    made = sh.share_from_uri("smb://prodfs/sqlcloneimages$/DEV")
    assert made.url == "smb://prodfs/sqlcloneimages$/DEV" and made.label
    assert not sh.is_mounted(share)


def test_shares_round_trip_in_config(tmp_path):
    cfg = AppConfig()
    cfg.vhd.shares.append(Share(url="smb://s/sh/d", auth="password", domain="CORP", username="svc"))
    path = tmp_path / "c.json"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.vhd.shares[0].username == "svc" and loaded.vhd.shares[0].secret_key.startswith("share:")


def test_helper_refuses_bad_share_arguments(tmp_path):
    helper = os.path.join(os.path.dirname(sh.__file__), "mount_helper.py")
    if os.geteuid() != 0:
        pytest.skip("argument checks run after the root check")
    for args in (["mount-share", "notaunc", "/run/replicate/shares/x", "--options", "ro"],
                 ["mount-share", "//s/sh", "/tmp/elsewhere", "--options", "ro"],
                 ["mount-share", "//s/sh", "/run/replicate/shares/x", "--options", "ro,credentials=/etc/shadow"],
                 ["umount-share", "/etc"]):
        proc = subprocess.run([sys.executable, helper, *args], capture_output=True, text=True)
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        assert data["ok"] is False, args


PROC_MOUNTS = """proc /proc proc rw 0 0
//prodfs/sqlcloneimages$ /mnt/sqlcloneimages cifs rw,relatime,vers=3.1.1,file_mode=0640,dir_mode=0750,uid=1000,gid=100 0 0
//prodfs/sqlcloneimages$ /run/replicate/shares/other cifs ro,noperm,file_mode=0644,dir_mode=0755 0 0
\\\\fileserver\\backups /mnt/back\\040ups cifs rw,noperm 0 0
systemd-1 /mnt/auto autofs rw 0 0
"""


def test_existing_mounts_are_found_and_judged():
    mounts = sh._parse_mounts(PROC_MOUNTS)
    assert [(m.mountpoint, m.source) for m in mounts] == [
        ("/mnt/sqlcloneimages", "//prodfs/sqlcloneimages$"),
        ("/run/replicate/shares/other", "//prodfs/sqlcloneimages$"),
        ("/mnt/back ups", "\\\\fileserver\\backups"),
    ]
    system, other, backups = mounts
    assert not system.readable_by_others and not system.ours
    assert other.readable_by_others and other.ours and other.readonly
    assert backups.readable_by_others  # noperm: the server decides
    share = Share(id="abc", url="smb://PRODFS/SqlCloneImages$/DEV/ACCT")
    # our own mount wins, then another entry's, then the system's
    own = sh.ExistingMount("/run/replicate/shares/abc", "//prodfs/sqlcloneimages$", options="ro,noperm")
    assert sh.find_mount(share, [system, own, other]).mountpoint == "/run/replicate/shares/abc"
    assert sh.find_mount(share, [system, other]).mountpoint == "/run/replicate/shares/other"
    found = sh.find_mount(share, [system])
    assert found is system and sh.folder_path(share, found) == "/mnt/sqlcloneimages/DEV/ACCT"
    assert "by the system at /mnt/sqlcloneimages" in system.describe(share) and "only you can read" in system.describe(share)
    assert other.describe(share).startswith("mounted (shared")
    assert own.describe(share) == "mounted"
    assert sh.find_mount(Share(url="smb://elsewhere/x"), mounts) is None
    assert sh.find_mount(Share(url="smb://fileserver/backups/sql"), mounts) is backups


def test_fstab_entries_and_reachability(tmp_path):
    entries = sh._parse_fstab("# drives\n//prodfs/sqlcloneimages$ /mnt/sqlcloneimages cifs credentials=/var/lib/x,uid=travis,x-systemd.automount,noauto 0 0\n/dev/sda1 / ext4 defaults 0 1\n")
    assert len(entries) == 1 and entries[0].mountpoint == "/mnt/sqlcloneimages" and sh._unc_key(entries[0].source) == "prodfs/sqlcloneimages$"
    assert sh.reachable(str(tmp_path)) is None
    assert "does not exist" in sh.reachable(str(tmp_path / "missing"))


def test_mount_share_uses_a_readable_system_mount(tmp_path, monkeypatch):
    share = Share(id="abc", url="smb://prodfs/sqlcloneimages$/DEV")
    (tmp_path / "DEV").mkdir()
    system = sh.ExistingMount(str(tmp_path), "//prodfs/sqlcloneimages$", options="rw,noperm")
    monkeypatch.setattr(sh, "current_mounts", lambda: [system])
    calls = []
    from replicate.vhd import nbd
    monkeypatch.setattr(nbd, "_privileged", lambda args, input_data=None: calls.append(args))
    assert sh.mount_share(share, None, uid=1, gid=1) == str(tmp_path / "DEV")
    assert calls == []  # nothing mounted: the system's mount will do
    assert not sh.can_unmount(share)
    sh.unmount_share(share)
    assert calls == []  # and it is not Replicate's to unmount


def test_mount_share_mounts_again_over_an_owner_only_system_mount(tmp_path, monkeypatch):
    share = Share(id="abc", url="smb://prodfs/sqlcloneimages$", auth="guest")
    system = sh.ExistingMount(str(tmp_path), "//prodfs/sqlcloneimages$", options="rw,file_mode=0640,dir_mode=0750")
    monkeypatch.setattr(sh, "current_mounts", lambda: [system])
    monkeypatch.setattr(sh, "which", lambda *names: "/sbin/mount.cifs")
    monkeypatch.setattr(sh, "mountpoint_for", lambda s: str(tmp_path / "own"))
    calls = []
    from replicate.vhd import nbd

    def fake_privileged(args, input_data=None):
        calls.append(args)
        (tmp_path / "own").mkdir(exist_ok=True)

    monkeypatch.setattr(nbd, "_privileged", fake_privileged)
    assert sh.mount_share(share, None, uid=1, gid=1) == str(tmp_path / "own")
    assert calls and calls[0][0] == "mount-share" and calls[0][2] == str(tmp_path / "own")


def test_mount_share_remounts_a_stale_own_mount(tmp_path, monkeypatch):
    share = Share(id="abc", url="smb://prodfs/sqlcloneimages$", auth="guest")
    own = str(tmp_path / "own")
    monkeypatch.setattr(sh, "mountpoint_for", lambda s: own)
    monkeypatch.setattr(sh, "current_mounts", lambda: [sh.ExistingMount(own, "//prodfs/sqlcloneimages$", options="ro")])
    monkeypatch.setattr(sh, "which", lambda *names: "/sbin/mount.cifs")
    calls = []
    from replicate.vhd import nbd

    def fake_privileged(args, input_data=None):
        calls.append(args[0])
        if args[0] == "mount-share":
            (tmp_path / "own").mkdir(exist_ok=True)

    monkeypatch.setattr(nbd, "_privileged", fake_privileged)
    assert sh.mount_share(share, None, uid=1, gid=1) == own  # the mount point did not exist: stale
    assert calls == ["umount-share", "mount-share"]


def test_mount_share_refuses_a_dead_system_mount(tmp_path, monkeypatch):
    share = Share(id="abc", url="smb://prodfs/sqlcloneimages$")
    monkeypatch.setattr(sh, "current_mounts", lambda: [sh.ExistingMount(str(tmp_path / "gone"), "//prodfs/sqlcloneimages$", options="rw,noperm")])
    with pytest.raises(sh.ShareError, match="mounted by the system"):
        sh.mount_share(share, None, uid=1, gid=1)
