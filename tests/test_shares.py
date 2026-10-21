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
