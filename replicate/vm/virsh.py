"""A thin wrapper around the ``virsh`` command.

Replicate drives libvirt through ``virsh`` rather than the libvirt Python
bindings: it is on every machine that has libvirt, needs no compiled
extension in the AppImage, and everything the app does maps onto one
command. ``LIBVIRT_DEFAULT_URI`` is ignored; the URI always comes from the
configuration.
"""

from __future__ import annotations

import json
import re
import tempfile
import os
from pathlib import Path
from typing import Any

from replicate.util import CommandError, run, which


class VirshError(RuntimeError):
    pass


class LibvirtUnavailable(VirshError):
    pass


class Virsh:
    def __init__(self, uri: str = "qemu:///system"):
        self.uri = uri
        self.binary = which("virsh")

    # -- plumbing ----------------------------------------------------------
    def available(self) -> bool:
        return self.binary is not None

    def _call(self, *args: str, check: bool = True, timeout: float | None = 120) -> str:
        if not self.binary:
            raise LibvirtUnavailable("virsh was not found; install libvirt's client tools")
        cmd = [self.binary, "--connect", self.uri, "--quiet", *args]
        try:
            return run(cmd, check=check, timeout=timeout)
        except CommandError as exc:
            message = exc.stderr.strip() or exc.stdout.strip()
            if "Failed to connect socket" in message or "authentication failed" in message.lower():
                raise LibvirtUnavailable(
                    f"Cannot reach libvirt at {self.uri}: {message}\n"
                    "Is libvirtd running, and is your user in the libvirt group?"
                ) from exc
            raise VirshError(message) from exc

    def connection_ok(self) -> tuple[bool, str]:
        try:
            out = self._call("version", timeout=15)
            return True, out.strip().splitlines()[0] if out.strip() else "connected"
        except VirshError as exc:
            return False, str(exc)

    # -- domains -----------------------------------------------------------
    def exists(self, name: str) -> bool:
        try:
            self._call("dominfo", name, timeout=15)
            return True
        except VirshError:
            return False

    def state(self, name: str) -> str:
        """libvirt state string: running, paused, shut off, ... or 'undefined'."""
        try:
            return self._call("domstate", name, timeout=15).strip()
        except VirshError:
            return "undefined"

    def define(self, xml: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as fh:
            fh.write(xml)
            path = fh.name
        try:
            self._call("define", path)
        finally:
            os.unlink(path)

    def undefine(self, name: str) -> None:
        try:
            self._call("undefine", name, "--nvram", "--tpm")
        except VirshError:
            self._call("undefine", name, "--nvram")

    def start(self, name: str) -> None:
        self._call("start", name)

    def shutdown(self, name: str) -> None:
        # The guest agent gives Windows a clean shutdown; ACPI is the fallback.
        try:
            self._call("shutdown", name, "--mode", "agent", timeout=30)
        except VirshError:
            self._call("shutdown", name, "--mode", "acpi")

    def reboot(self, name: str) -> None:
        self._call("reboot", name)

    def destroy(self, name: str) -> None:
        self._call("destroy", name)

    def suspend(self, name: str) -> None:
        self._call("suspend", name)

    def resume(self, name: str) -> None:
        self._call("resume", name)

    def send_key(self, name: str, key: str) -> None:
        self._call("send-key", name, key, check=False, timeout=10)

    def dumpxml(self, name: str) -> str:
        return self._call("dumpxml", name)

    def dominfo(self, name: str) -> dict[str, str]:
        info: dict[str, str] = {}
        for line in self._call("dominfo", name, timeout=15).splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
        return info

    # -- devices -----------------------------------------------------------
    def block_devices(self, name: str) -> dict[str, str]:
        """Map target dev -> source path for a domain (from its persistent config)."""
        out = self._call("domblklist", name, "--details", timeout=15)
        devices: dict[str, str] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] in ("file", "block", "network", "-"):
                # Type Device Target Source
                target = parts[2]
                source = parts[3] if len(parts) > 3 else "-"
                devices[target] = source
            elif len(parts) == 2:
                devices[parts[0]] = parts[1]
        return devices

    def free_virtio_target(self, name: str) -> str:
        used = set(self.block_devices(name))
        for letter in "bcdefghijklmnopqrstuvwxyz":
            dev = f"vd{letter}"
            if dev not in used:
                return dev
        raise VirshError("No free virtio disk slot left on the VM")

    def attach_device(self, name: str, xml: str, live: bool) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as fh:
            fh.write(xml)
            path = fh.name
        try:
            args = ["attach-device", name, path, "--persistent"] if live else ["attach-device", name, path, "--config"]
            self._call(*args)
        finally:
            os.unlink(path)

    def detach_disk(self, name: str, target: str, live: bool) -> None:
        args = ["detach-disk", name, target, "--persistent"] if live else ["detach-disk", name, target, "--config"]
        self._call(*args)

    def change_media(self, name: str, target: str, path: str | None, live: bool) -> None:
        args = ["change-media", name, target]
        args += [path, "--insert"] if path else ["--eject", "--force"]
        args += ["--live", "--config"] if live else ["--config"]
        self._call(*args, check=False)

    # -- network -----------------------------------------------------------
    def ensure_network(self, network: str = "default") -> None:
        try:
            self._call("net-info", network, timeout=15)
        except VirshError:
            if network != "default":
                raise
            candidates = [
                Path(p)
                for p in (
                    "/usr/share/libvirt/networks/default.xml",
                    "/var/lib/libvirt/qemu/networks/default.xml",
                    "/run/current-system/sw/var/lib/libvirt/qemu/networks/default.xml",
                )
            ]
            xml_path = next((p for p in candidates if p.exists()), None)
            if xml_path is None:
                xml = (
                    "<network><name>default</name><forward mode='nat'/>"
                    "<bridge name='virbr0' stp='on' delay='0'/>"
                    "<ip address='192.168.122.1' netmask='255.255.255.0'>"
                    "<dhcp><range start='192.168.122.2' end='192.168.122.254'/></dhcp></ip></network>"
                )
                with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8") as fh:
                    fh.write(xml)
                    xml_path = Path(fh.name)
            self._call("net-define", str(xml_path))
        self._call("net-autostart", network, check=False)
        self._call("net-start", network, check=False)

    def ip_address(self, name: str) -> str | None:
        """The guest's IPv4 address, from the guest agent first, then DHCP leases."""
        for source in ("agent", "lease"):
            try:
                out = self._call("domifaddr", name, "--source", source, "--full", timeout=20)
            except VirshError:
                continue
            for match in re.finditer(r"ipv4\s+(\d+\.\d+\.\d+\.\d+)/\d+", out):
                ip = match.group(1)
                if not ip.startswith("127.") and not ip.startswith("169.254."):
                    return ip
        return None

    # -- guest agent -------------------------------------------------------
    def agent_command(self, name: str, command: dict[str, Any], timeout: float = 30) -> dict[str, Any]:
        out = self._call("qemu-agent-command", name, "--timeout", str(int(timeout)), json.dumps(command), timeout=timeout + 10)
        try:
            return json.loads(out) if out.strip() else {}
        except json.JSONDecodeError as exc:
            raise VirshError(f"Guest agent returned something that is not JSON: {out[:200]}") from exc

    def agent_ping(self, name: str) -> bool:
        try:
            self.agent_command(name, {"execute": "guest-ping"}, timeout=5)
            return True
        except VirshError:
            return False
