"""Attach images or clones to the VM and make Windows see them."""

from __future__ import annotations

import datetime as dt
import json
import logging
import uuid

from replicate.config import AppState, Mount, save_state
from replicate.vhd.clones import create_clone
from replicate.vhd.images import ImageError, ImageInfo, inspect
from replicate.vm.domain_xml import build_data_disk_xml
from replicate.vm.guest import GuestError
from replicate.vm.manager import VmManager
from replicate.vm.virsh import VirshError

log = logging.getLogger(__name__)

# Runs as SYSTEM through the guest agent: bring the hot-plugged disk online,
# writable, give its volumes drive letters and list the database files on it.
ONLINE_SCRIPT = r"""
param()
$serial = '@SERIAL@'
$ErrorActionPreference = 'Stop'
Update-HostStorageCache
$disk = $null
for ($i = 0; $i -lt 20 -and -not $disk; $i++) {
    $disk = Get-Disk | Where-Object { $_.SerialNumber -and $_.SerialNumber.Trim() -eq $serial } | Select-Object -First 1
    if (-not $disk) { Start-Sleep -Seconds 1; Update-HostStorageCache }
}
if (-not $disk) {
    # Fallback: the newest raw virtio disk that is still offline.
    $disk = Get-Disk | Where-Object { $_.BusType -eq 'SCSI' -or $_.BusType -eq 'Unknown' } |
        Where-Object { $_.IsOffline -and $_.Number -ne 0 } | Sort-Object Number -Descending | Select-Object -First 1
}
if (-not $disk) { throw "No disk with serial $serial showed up in Windows" }
if ($disk.IsOffline) { Set-Disk -Number $disk.Number -IsOffline $false }
if ($disk.IsReadOnly) { Set-Disk -Number $disk.Number -IsReadOnly $false }
Start-Sleep -Seconds 1
$letters = @()
foreach ($part in Get-Partition -DiskNumber $disk.Number -ErrorAction SilentlyContinue) {
    if ($part.Type -in 'Reserved', 'System', 'Recovery', 'Unknown' -and $part.Size -lt 600MB) { continue }
    if (-not $part.DriveLetter) {
        try { $part | Add-PartitionAccessPath -AssignDriveLetter | Out-Null } catch { }
        $part = Get-Partition -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber
    }
    if ($part.DriveLetter) { $letters += [string]$part.DriveLetter }
}
$volumes = @()
$files = @()
foreach ($letter in $letters) {
    $vol = Get-Volume -DriveLetter $letter -ErrorAction SilentlyContinue
    if ($vol) {
        $volumes += @{ letter = $letter; label = $vol.FileSystemLabel; sizeBytes = [long]$vol.Size; freeBytes = [long]$vol.SizeRemaining; fileSystem = $vol.FileSystem }
    }
    $root = "${letter}:\"
    Get-ChildItem -Path $root -Recurse -Depth 4 -File -Include *.mdf, *.ndf, *.ldf, *.bak, *.trn, *.dif -ErrorAction SilentlyContinue |
        Select-Object -First 500 | ForEach-Object {
            $files += @{ path = $_.FullName; sizeBytes = [long]$_.Length; kind = $_.Extension.TrimStart('.').ToLower(); modified = $_.LastWriteTimeUtc.ToString('o') }
        }
}
@{ diskNumber = $disk.Number; serial = $serial; size = [long]$disk.Size; partitionStyle = [string]$disk.PartitionStyle; letters = $letters; volumes = $volumes; files = $files } |
    ConvertTo-Json -Compress -Depth 5
"""

OFFLINE_SCRIPT = r"""
$serial = '@SERIAL@'
$ErrorActionPreference = 'SilentlyContinue'
$disk = Get-Disk | Where-Object { $_.SerialNumber -and $_.SerialNumber.Trim() -eq $serial } | Select-Object -First 1
if ($disk) { Set-Disk -Number $disk.Number -IsOffline $true }
'ok'
"""


class MountError(RuntimeError):
    pass


class Mounter:
    def __init__(self, vm: VmManager, state: AppState, clones_dir: str):
        self.vm = vm
        self.state = state
        self.clones_dir = clones_dir

    # -- queries -----------------------------------------------------------
    def mounts(self) -> list[Mount]:
        return [m for m in self.state.mounts if m.backend == "vm" and m.vm_name == self.vm.vm.name]

    def mount_for_clone(self, clone_path: str) -> Mount | None:
        return next((m for m in self.state.mounts if m.clone_path == clone_path), None)

    # -- attach ------------------------------------------------------------
    def attach(self, image: ImageInfo, *, clone: bool = True, label: str = "", readonly: bool = False) -> Mount:
        """Attach ``image`` to the VM, through a fresh clone unless ``clone`` is False.

        The disk is hot-plugged when the VM runs and added to its config
        either way, so it is there at the next boot too.
        """
        if image.error:
            raise MountError(f"{image.name}: {image.error}")
        vm_name = self.vm.vm.name
        if not self.vm.virsh.exists(vm_name):
            raise MountError("Create the VM first")
        if clone:
            disk_path = str(create_clone(image, self.clones_dir, label or image.name))
            fmt = "qcow2"
        else:
            if image.is_differencing and image.format != "qcow2":
                raise ImageError(f"{image.name} is a differencing disk; QEMU cannot open its parent chain")
            disk_path = image.path
            fmt = image.format
        serial = "rp-" + uuid.uuid4().hex[:16]
        target = self.vm.virsh.free_virtio_target(vm_name)
        running = self.vm.virsh.state(vm_name) == "running"
        xml = build_data_disk_xml(disk_path, target, serial, fmt=fmt, readonly=readonly)
        try:
            self.vm.virsh.attach_device(vm_name, xml, live=running)
        except VirshError as exc:
            if clone:
                from replicate.vhd.clones import delete_clone

                delete_clone(disk_path)
            raise MountError(f"libvirt refused the disk: {exc}") from exc
        mount = Mount(
            image_path=image.path,
            clone_path=disk_path,
            vm_name=vm_name,
            target_dev=target,
            serial=serial,
            created_at=dt.datetime.now().isoformat(timespec="seconds"),
            label=label or image.name,
        )
        self.state.mounts.append(mount)
        save_state(self.state)
        return mount

    def online(self, mount: Mount) -> dict:
        """Bring the disk online in Windows; returns volumes and database files found."""
        script = ONLINE_SCRIPT.replace("@SERIAL@", mount.serial)
        try:
            data = self.vm.guest().powershell_json(script, timeout=240)
        except (GuestError, VirshError) as exc:
            raise MountError(f"Windows did not bring the disk online: {exc}") from exc
        mount.drive_letters = [str(x) for x in data.get("letters", [])]
        save_state(self.state)
        return data

    # -- detach ------------------------------------------------------------
    def detach(self, mount: Mount, *, delete_clone_file: bool = False) -> None:
        vm_name = mount.vm_name
        running = self.vm.virsh.state(vm_name) == "running"
        if running and self.vm.virsh.agent_ping(vm_name):
            try:
                self.vm.guest().powershell(OFFLINE_SCRIPT.replace("@SERIAL@", mount.serial), timeout=60)
            except (GuestError, VirshError) as exc:
                log.warning("could not offline disk %s in the guest: %s", mount.serial, exc)
        try:
            if self.vm.virsh.exists(vm_name):
                self.vm.virsh.detach_disk(vm_name, mount.target_dev, live=running)
        except VirshError as exc:
            raise MountError(f"libvirt could not detach {mount.target_dev}: {exc}") from exc
        if mount in self.state.mounts:
            self.state.mounts.remove(mount)
        save_state(self.state)
        if delete_clone_file and not mount.is_direct:
            from replicate.vhd.clones import delete_clone

            delete_clone(mount.clone_path)

    def forget(self, mount: Mount) -> None:
        """Drop a mount record whose disk is already gone from the VM."""
        if mount in self.state.mounts:
            self.state.mounts.remove(mount)
            save_state(self.state)

    def reconcile(self) -> None:
        """Remove records for disks that are no longer in the VM's config."""
        vm_name = self.vm.vm.name
        if not self.vm.virsh.exists(vm_name):
            return
        try:
            devices = self.vm.virsh.block_devices(vm_name)
        except VirshError:
            return
        stale = [m for m in self.mounts() if m.target_dev not in devices]
        for m in stale:
            self.state.mounts.remove(m)
        if stale:
            save_state(self.state)

    def image_for(self, mount: Mount) -> ImageInfo:
        return inspect(mount.image_path)

    @staticmethod
    def describe_files(data: dict) -> str:
        files = data.get("files", [])
        kinds: dict[str, int] = {}
        for f in files:
            kinds[f.get("kind", "?")] = kinds.get(f.get("kind", "?"), 0) + 1
        return ", ".join(f"{n} .{k}" for k, n in sorted(kinds.items())) or "no database files"

    @staticmethod
    def dumps(data: dict) -> str:
        return json.dumps(data, indent=2)
