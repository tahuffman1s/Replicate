"""Lifecycle of the SQL host VM: create in the background, start, stop, talk to the guest."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets as pysecrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from replicate import host
from replicate.config import AppConfig, AppState, SqlServer, VmSettings, save_config, save_state
from replicate.secrets import SecretStore
from replicate.tasks import Progress
from replicate.util import local_timezone, run, which
from replicate.vm.domain_xml import FIRMWARE_FALLBACKS, build_domain_xml
from replicate.vm.guest import Guest, GuestError
from replicate.vm.iso import make_iso
from replicate.vm.unattended import render_autounattend, setup_manifest, write_setup_tree
from replicate.vm.virsh import Virsh, VirshError

log = logging.getLogger(__name__)

VIRTIO_WIN_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
WINDOWS_ISO_PAGE = "https://www.microsoft.com/software-download/windows11"


@dataclass
class VmStatus:
    defined: bool = False
    state: str = "undefined"  # libvirt words: running, paused, shut off, ...
    agent: bool = False
    ip: str | None = None
    installing: bool = False
    install_phase: str = ""
    setup: dict = field(default_factory=dict)  # guest setup-state.json, when readable

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def paused(self) -> bool:
        return self.state == "paused"

    @property
    def label(self) -> str:
        if not self.defined:
            return "Not created"
        if self.installing:
            return "Installing Windows"
        if self.state == "running":
            return "Running" if self.agent else "Starting"
        if self.state == "paused":
            return "Paused"
        if self.state in ("shut off", "shutoff"):
            return "Stopped"
        return self.state.capitalize()


@dataclass
class CreateOptions:
    windows_password: str = ""
    sql_admin_password: str = ""  # generated when empty
    recreate: bool = False


class VmManager:
    def __init__(self, config: AppConfig, state: AppState, secrets: SecretStore):
        self.config = config
        self.state = state
        self.secrets = secrets
        self._virsh = Virsh(config.vm.uri)

    # -- accessors ---------------------------------------------------------
    @property
    def vm(self) -> VmSettings:
        return self.config.vm

    @property
    def virsh(self) -> Virsh:
        if self._virsh.uri != self.vm.uri:
            self._virsh = Virsh(self.vm.uri)
        return self._virsh

    def guest(self) -> Guest:
        return Guest(self.virsh, self.vm.name)

    # -- status ------------------------------------------------------------
    def status(self, *, with_setup: bool = False) -> VmStatus:
        status = VmStatus()
        if not self.virsh.available():
            return status
        state = self.virsh.state(self.vm.name)
        status.defined = state != "undefined"
        status.state = state
        install = self.state.vm_install
        status.installing = bool(install) and install.get("phase") not in (None, "", "done", "failed")
        status.install_phase = install.get("message", "") if install else ""
        if status.running:
            status.agent = self.virsh.agent_ping(self.vm.name)
            if status.agent:
                status.ip = self.virsh.ip_address(self.vm.name)
                if with_setup:
                    status.setup = self.read_setup_state() or {}
        return status

    def read_setup_state(self) -> dict | None:
        try:
            data = self.guest().read_file(r"C:\ProgramData\Replicate\setup-state.json", max_bytes=65536)
            return json.loads(data.decode("utf-8-sig"))
        except (GuestError, VirshError, ValueError):
            return None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.virsh.ensure_network(self.vm.network)
        self.virsh.start(self.vm.name)

    def shutdown(self, wait: float = 0) -> None:
        self.virsh.shutdown(self.vm.name)
        if wait:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and self.virsh.state(self.vm.name) == "running":
                time.sleep(2)

    def force_off(self) -> None:
        self.virsh.destroy(self.vm.name)

    def reboot(self) -> None:
        self.virsh.reboot(self.vm.name)

    def suspend(self) -> None:
        self.virsh.suspend(self.vm.name)

    def resume(self) -> None:
        self.virsh.resume(self.vm.name)

    def delete(self, remove_disk: bool) -> None:
        if self.virsh.state(self.vm.name) == "running":
            self.virsh.destroy(self.vm.name)
        if self.virsh.exists(self.vm.name):
            self.virsh.undefine(self.vm.name)
        if remove_disk:
            for path in (self.vm.disk_path, self.vm.answers_iso, self.vm.setup_iso):
                host.remove_file(Path(path))
        self.state.vm_install = {}
        save_state(self.state)

    def open_console(self) -> None:
        env = dict(os.environ)
        viewer = which("virt-viewer", "remote-viewer")
        if viewer and viewer.endswith("virt-viewer"):
            cmd = [viewer, "--connect", self.vm.uri, "--wait", self.vm.name]
        elif viewer:
            cmd = [viewer, f"{self.vm.uri}#{self.vm.name}"]
        else:
            manager = which("virt-manager")
            if not manager:
                raise RuntimeError("Install virt-viewer or virt-manager to see the VM's screen")
            cmd = [manager, "--connect", self.vm.uri, "--show-domain-console", self.vm.name]
        subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    # -- SQL Server services in the guest ---------------------------------
    def sql_services(self) -> list[dict]:
        script = r"""
$svcs = Get-Service | Where-Object { $_.Name -eq 'MSSQLSERVER' -or $_.Name -like 'MSSQL$*' } |
    ForEach-Object { @{ name = $_.Name; display = $_.DisplayName; status = $_.Status.ToString(); startType = $_.StartType.ToString() } }
ConvertTo-Json -Compress @($svcs)
"""
        data = self.guest().powershell_json(script, timeout=60)
        return data if isinstance(data, list) else [data]

    def set_sql_service(self, service_name: str, start: bool) -> None:
        name = service_name.replace("'", "''")
        if start:
            script = f"Start-Service -Name '{name}'; (Get-Service -Name '{name}').Status.ToString()"
        else:
            script = f"Stop-Service -Name '{name}' -Force; (Get-Service -Name '{name}').Status.ToString()"
        result = self.guest().powershell(script, timeout=180)
        if result.exit_code != 0:
            raise GuestError(result.stderr.strip() or result.stdout.strip() or "Service control failed")

    # -- creation ----------------------------------------------------------
    def validate_for_create(self) -> list[str]:
        problems = []
        vm = self.vm
        if not vm.windows_iso or not Path(vm.windows_iso).is_file():
            problems.append(f"Windows 11 ISO not found. Download it from {WINDOWS_ISO_PAGE} and set its path in Settings.")
        if not vm.virtio_win_iso or not Path(vm.virtio_win_iso).is_file():
            problems.append("virtio-win ISO not found. Settings can download it.")
        if not vm.sql_instances:
            problems.append("Add at least one SQL Server instance name.")
        for name in vm.sql_instances:
            if not name.isalnum() or len(name) > 16:
                problems.append(f"Instance name {name!r} must be 1-16 letters or digits.")
        if not which("virsh"):
            problems.append("virsh is not installed.")
        if not which("qemu-img"):
            problems.append("qemu-img is not installed.")
        if not which("xorriso", "genisoimage", "mkisofs"):
            problems.append("xorriso or genisoimage is needed to build the setup CDs.")
        return problems

    def _set_install_phase(self, phase: str, message: str) -> None:
        self.state.vm_install = {
            "phase": phase,
            "message": message,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "started_at": self.state.vm_install.get("started_at") or dt.datetime.now().isoformat(timespec="seconds"),
        }
        save_state(self.state)

    def create(self, options: CreateOptions, progress: Progress) -> None:
        """Create the VM and install Windows + SQL Server unattended. Blocking;
        the UI runs it in a thread and follows ``progress``."""
        vm = self.vm
        problems = self.validate_for_create()
        if problems:
            raise RuntimeError("\n".join(problems))
        images = Path(vm.images_dir)
        uid = os.getuid()

        try:
            self._set_install_phase("prepare", "Preparing")
            progress.update("Checking libvirt", 0.02)
            self.virsh.ensure_network(vm.network)
            host.ensure_dir(images)

            if self.virsh.exists(vm.name):
                if not options.recreate:
                    raise RuntimeError(f"A VM named {vm.name} already exists")
                progress.update("Removing the previous VM", 0.04)
                if self.virsh.state(vm.name) == "running":
                    self.virsh.destroy(vm.name)
                self.virsh.undefine(vm.name)
                host.remove_file(Path(vm.disk_path))

            # Passwords: the Windows account and the SQL login Replicate uses.
            sql_password = options.sql_admin_password or pysecrets.token_urlsafe(18) + "aA1!"
            self.secrets.set("vm:windows", options.windows_password)
            self.secrets.set("vm:sql-admin", sql_password)

            progress.update("Writing the setup CD", 0.08)
            with tempfile.TemporaryDirectory(prefix="replicate-setup-") as tmp:
                stage = Path(tmp) / "cd"
                write_setup_tree(stage, setup_manifest(vm.sql_instances, vm.admin_login, sql_password, vm.windows_user))
                iso = Path(tmp) / "setup.iso"
                make_iso(stage, iso, "REPLICATE_SETUP")
                host.install_file(iso, Path(vm.setup_iso), mode="644")

            progress.update("Writing the unattended-install answers", 0.1)
            with tempfile.TemporaryDirectory(prefix="replicate-answers-") as tmp:
                os.chmod(tmp, 0o700)
                stage = Path(tmp) / "cd"
                stage.mkdir()
                (stage / "Autounattend.xml").write_text(
                    render_autounattend(vm.windows_user, options.windows_password, local_timezone()), encoding="utf-8"
                )
                iso = Path(tmp) / "answers.iso"
                make_iso(stage, iso, "REPLICATE_ANSWERS")
                host.install_file(iso, Path(vm.answers_iso), mode="644")

            progress.update(f"Creating the {vm.disk_size} system disk", 0.14)
            disk = Path(vm.disk_path)
            if not disk.exists():
                host.privileged(
                    ["qemu-img", "create", "-q", "-f", "qcow2", str(disk), vm.disk_size], path_hint=disk
                )

            progress.update("Defining the VM", 0.18)
            last_error: Exception | None = None
            for choice in FIRMWARE_FALLBACKS:
                try:
                    self.virsh.define(build_domain_xml(vm, choice))
                    last_error = None
                    break
                except VirshError as exc:
                    last_error = exc
                    log.info("define with %s failed: %s", choice, exc)
            if last_error:
                raise last_error

            progress.update("Starting the VM; Windows installs by itself", 0.22)
            self._set_install_phase("windows", "Installing Windows")
            self.virsh.start(vm.name)
            # Windows media waits for "Press any key to boot from CD or DVD".
            for _ in range(20):
                time.sleep(1)
                self.virsh.send_key(vm.name, "KEY_ENTER")

            self._wait_for_windows(progress)
            self._wait_for_sql_setup(progress)
            self._set_install_phase("done", "Ready")
            progress.update("The VM is ready", 1.0)
        except BaseException as exc:
            self._set_install_phase("failed", f"Failed: {exc}")
            raise

    def resume_install(self, progress: Progress) -> None:
        """Pick up watching an install that a previous run of the app started."""
        phase = self.state.vm_install.get("phase")
        try:
            if phase == "windows":
                self._wait_for_windows(progress)
                self._wait_for_sql_setup(progress)
            elif phase == "sql":
                self._wait_for_sql_setup(progress)
            else:
                return
            self._set_install_phase("done", "Ready")
            progress.update("The VM is ready", 1.0)
        except BaseException as exc:
            self._set_install_phase("failed", f"Failed: {exc}")
            raise

    def _wait_for_windows(self, progress: Progress) -> None:
        vm = self.vm
        guest = self.guest()
        start = time.monotonic()
        while True:
            if guest.ping():
                break
            if self.virsh.state(vm.name) != "running":
                raise RuntimeError("The VM stopped before Windows finished installing; open its screen to see why")
            elapsed = time.monotonic() - start
            if elapsed > 2 * 3600:
                raise RuntimeError("Windows did not finish installing within 2 hours")
            fraction = 0.22 + min(elapsed / 2400, 1.0) * 0.4
            progress.update(f"Installing Windows ({int(elapsed // 60)} min; this takes 20-40 minutes)", fraction)
            time.sleep(20)
        progress.update("Windows is up; removing the answers CD (it holds the password)", 0.63)
        self.virsh.change_media(vm.name, "sdd", None, live=True)
        host.remove_file(Path(vm.answers_iso))
        self.virsh.change_media(vm.name, "sda", None, live=True)

    def _wait_for_sql_setup(self, progress: Progress) -> None:
        """Follow Setup-SqlHost.ps1 through its state file and register the instances."""
        self._set_install_phase("sql", "Installing SQL Server")
        start = time.monotonic()
        while True:
            setup = self.read_setup_state() or {}
            status = setup.get("status", "")
            message = setup.get("message", "waiting for the setup script to start")
            elapsed = time.monotonic() - start
            if status == "done":
                break
            if status == "failed":
                raise RuntimeError(f"SQL Server setup failed in the guest: {message}")
            if elapsed > 3 * 3600:
                raise RuntimeError("SQL Server setup did not finish within 3 hours")
            if self.virsh.state(self.vm.name) != "running":
                # The guest reboots once after the guest tools install; wait for it.
                time.sleep(15)
                continue
            fraction = 0.65 + min(elapsed / 2400, 1.0) * 0.33
            progress.update(f"SQL Server: {message} ({int(elapsed // 60)} min)", fraction)
            time.sleep(20)
        progress.update("Registering the SQL Server instances", 0.99)
        self.register_instances(setup)

    def register_instances(self, setup: dict) -> list[SqlServer]:
        """Add (or refresh) the VM's instances in the server list from setup-state.json."""
        created: list[SqlServer] = []
        password = self.secrets.get("vm:sql-admin") or ""
        for inst in setup.get("instances", []):
            name = inst.get("name", "MSSQLSERVER")
            service = "MSSQLSERVER" if name.upper() == "MSSQLSERVER" else f"MSSQL${name}"
            existing = next((s for s in self.config.servers if s.vm_managed and s.service_name == service), None)
            if existing is None:
                existing = SqlServer(
                    name=f"{self.vm.name} ({'default' if service == 'MSSQLSERVER' else name})",
                    host="vm",
                    vm_managed=True,
                    service_name=service,
                    instance="" if service == "MSSQLSERVER" else name,
                )
                self.config.servers.append(existing)
                created.append(existing)
            existing.port = int(inst.get("port", existing.port))
            existing.username = self.vm.admin_login
            existing.auth = "sql"
            if password:
                self.secrets.set(existing.secret_key, password)
        save_config(self.config)
        return created

    # -- downloads ---------------------------------------------------------
    def download_virtio_win(self, dest: Path, progress: Progress) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        import urllib.request

        progress.update("Downloading virtio-win.iso", 0.0)
        with urllib.request.urlopen(VIRTIO_WIN_URL, timeout=60) as resp, tmp.open("wb") as out:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    progress.update(f"Downloading virtio-win.iso ({done * 100 // total}%)", done / total)
        shutil.move(str(tmp), str(dest))
        progress.update("virtio-win.iso downloaded", 1.0)
        return dest
