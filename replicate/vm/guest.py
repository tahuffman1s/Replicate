"""Run PowerShell inside the Windows guest through the QEMU guest agent.

The agent runs as SYSTEM, so this is how Replicate starts and stops SQL
Server services, brings hot-plugged disks online and reads files from the
guest, with no network and no credentials involved.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

from replicate.vm.virsh import Virsh, VirshError

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"


class GuestError(RuntimeError):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str

    def json(self) -> Any:
        text = self.stdout.strip()
        # PowerShell may print warnings before the JSON; keep the last JSON document.
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        if start < 0:
            raise GuestError(f"Expected JSON from the guest, got: {text[:300]}")
        return json.loads(text[start:])


class Guest:
    def __init__(self, virsh: Virsh, vm_name: str):
        self.virsh = virsh
        self.vm = vm_name

    def ping(self) -> bool:
        return self.virsh.agent_ping(self.vm)

    def wait(self, timeout: float, interval: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.ping():
                return True
            time.sleep(interval)
        return False

    def info(self) -> dict[str, Any]:
        return self.virsh.agent_command(self.vm, {"execute": "guest-get-osinfo"}).get("return", {})

    def exec(self, path: str, args: list[str], timeout: float = 120, input_data: bytes | None = None) -> ExecResult:
        command: dict[str, Any] = {
            "execute": "guest-exec",
            "arguments": {"path": path, "arg": args, "capture-output": True},
        }
        if input_data is not None:
            command["arguments"]["input-data"] = base64.b64encode(input_data).decode("ascii")
        try:
            pid = self.virsh.agent_command(self.vm, command)["return"]["pid"]
        except (KeyError, TypeError) as exc:
            raise GuestError("The guest agent did not start the command") from exc
        except VirshError as exc:
            raise GuestError(str(exc)) from exc

        deadline = time.monotonic() + timeout
        delay = 0.3
        while True:
            status = self.virsh.agent_command(self.vm, {"execute": "guest-exec-status", "arguments": {"pid": pid}})["return"]
            if status.get("exited"):
                break
            if time.monotonic() > deadline:
                raise GuestError(f"Command in the guest did not finish within {int(timeout)} s")
            time.sleep(delay)
            delay = min(delay * 1.5, 3.0)

        def decode(key: str) -> str:
            data = status.get(key)
            return base64.b64decode(data).decode("utf-8", errors="replace") if data else ""

        return ExecResult(int(status.get("exitcode", -1)), decode("out-data"), decode("err-data"))

    def powershell(self, script: str, timeout: float = 120) -> ExecResult:
        """Run a PowerShell script (as SYSTEM) and return its output."""
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        args = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-OutputFormat", "Text", "-EncodedCommand", encoded]
        return self.exec(POWERSHELL, args, timeout=timeout)

    def powershell_json(self, script: str, timeout: float = 120) -> Any:
        result = self.powershell(script, timeout=timeout)
        if result.exit_code != 0 and not result.stdout.strip():
            raise GuestError(result.stderr.strip() or f"PowerShell exited with {result.exit_code}")
        return result.json()

    def read_file(self, path: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
        try:
            handle = self.virsh.agent_command(
                self.vm, {"execute": "guest-file-open", "arguments": {"path": path, "mode": "rb"}}
            )["return"]
        except VirshError as exc:
            raise GuestError(f"Cannot open {path} in the guest: {exc}") from exc
        chunks: list[bytes] = []
        total = 0
        try:
            while total < max_bytes:
                ret = self.virsh.agent_command(
                    self.vm, {"execute": "guest-file-read", "arguments": {"handle": handle, "count": 1 << 20}}
                )["return"]
                data = base64.b64decode(ret.get("buf-b64", ""))
                chunks.append(data)
                total += len(data)
                if ret.get("eof") or not data:
                    break
        finally:
            self.virsh.agent_command(self.vm, {"execute": "guest-file-close", "arguments": {"handle": handle}}, timeout=10)
        return b"".join(chunks)

    def write_file(self, path: str, data: bytes) -> None:
        handle = self.virsh.agent_command(
            self.vm, {"execute": "guest-file-open", "arguments": {"path": path, "mode": "wb"}}
        )["return"]
        try:
            for offset in range(0, len(data), 1 << 20):
                chunk = data[offset : offset + (1 << 20)]
                self.virsh.agent_command(
                    self.vm,
                    {"execute": "guest-file-write", "arguments": {"handle": handle, "buf-b64": base64.b64encode(chunk).decode("ascii")}},
                )
        finally:
            self.virsh.agent_command(self.vm, {"execute": "guest-file-close", "arguments": {"handle": handle}}, timeout=10)

    def shutdown(self) -> None:
        self.virsh.agent_command(self.vm, {"execute": "guest-shutdown", "arguments": {"mode": "powerdown"}}, timeout=10)
