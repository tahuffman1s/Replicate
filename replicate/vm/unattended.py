"""Render Autounattend.xml and the Replicate setup CD contents."""

from __future__ import annotations

import json
from pathlib import Path

from replicate import paths
from replicate.util import xml_escape

# IANA time zone -> Windows time zone id, for the unattended install.
WINDOWS_TIME_ZONES = {
    "America/New_York": "Eastern Standard Time",
    "America/Detroit": "Eastern Standard Time",
    "America/Toronto": "Eastern Standard Time",
    "America/Chicago": "Central Standard Time",
    "America/Winnipeg": "Central Standard Time",
    "America/Mexico_City": "Central Standard Time (Mexico)",
    "America/Denver": "Mountain Standard Time",
    "America/Edmonton": "Mountain Standard Time",
    "America/Phoenix": "US Mountain Standard Time",
    "America/Los_Angeles": "Pacific Standard Time",
    "America/Vancouver": "Pacific Standard Time",
    "America/Anchorage": "Alaskan Standard Time",
    "Pacific/Honolulu": "Hawaiian Standard Time",
    "America/Sao_Paulo": "E. South America Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Dublin": "GMT Standard Time",
    "Europe/Lisbon": "GMT Standard Time",
    "Europe/Paris": "Romance Standard Time",
    "Europe/Madrid": "Romance Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "Europe/Amsterdam": "W. Europe Standard Time",
    "Europe/Rome": "W. Europe Standard Time",
    "Europe/Stockholm": "W. Europe Standard Time",
    "Europe/Zurich": "W. Europe Standard Time",
    "Europe/Warsaw": "Central European Standard Time",
    "Europe/Helsinki": "FLE Standard Time",
    "Europe/Kiev": "FLE Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "Asia/Kolkata": "India Standard Time",
    "Asia/Shanghai": "China Standard Time",
    "Asia/Tokyo": "Tokyo Standard Time",
    "Asia/Singapore": "Singapore Standard Time",
    "Australia/Sydney": "AUS Eastern Standard Time",
    "Australia/Perth": "W. Australia Standard Time",
    "Pacific/Auckland": "New Zealand Standard Time",
    "UTC": "UTC",
    "Etc/UTC": "UTC",
}


def windows_time_zone(iana: str) -> str:
    return WINDOWS_TIME_ZONES.get(iana, "UTC")


def render_autounattend(user: str, password: str, timezone_iana: str, template: Path | None = None) -> str:
    template = template or (paths.GUEST_DIR / "Autounattend.xml.in")
    text = template.read_text(encoding="utf-8")
    return (
        text.replace("@USER@", xml_escape(user))
        .replace("@PASSWORD@", xml_escape(password))
        .replace("@TIMEZONE@", xml_escape(windows_time_zone(timezone_iana)))
    )


def default_instance_port(index: int, instance: str) -> int:
    """Fixed TCP ports so the host can connect without SQL Browser: 1433 for
    the default instance, 14331, 14332, ... for named ones."""
    if instance.upper() == "MSSQLSERVER":
        return 1433
    return 14330 + index


def setup_manifest(instances: list[str], admin_login: str, admin_password: str, windows_user: str) -> dict:
    ports: dict[str, int] = {}
    named = 0
    for name in instances:
        if name.upper() == "MSSQLSERVER":
            ports[name] = 1433
        else:
            named += 1
            ports[name] = default_instance_port(named, name)
    return {
        "version": 1,
        "instances": [{"name": name, "port": ports[name]} for name in instances],
        "adminLogin": admin_login,
        "adminPassword": admin_password,
        "windowsUser": windows_user,
        "statePath": r"C:\ProgramData\Replicate\setup-state.json",
    }


def write_setup_tree(target: Path, manifest: dict) -> None:
    """Copy the guest scripts and the manifest into ``target`` (a CD staging dir)."""
    target.mkdir(parents=True, exist_ok=True)
    for script in paths.GUEST_DIR.iterdir():
        if script.suffix.lower() in (".ps1", ".cmd", ".txt", ".md"):
            (target / script.name).write_bytes(script.read_bytes())
    (target / "replicate.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
