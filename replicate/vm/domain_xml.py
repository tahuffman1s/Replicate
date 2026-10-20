"""Build the libvirt domain XML for the SQL host VM.

This is the Windows 11 guest definition from the author's NixOS config made
distro-neutral: firmware is picked by libvirt's own descriptors (``firmware=
"efi"``) instead of hard-coded OVMF paths, and the disks and CDs come from
:class:`~replicate.config.VmSettings`.
"""

from __future__ import annotations

from dataclasses import dataclass

from replicate.config import VmSettings
from replicate.util import xml_escape


@dataclass
class FirmwareChoice:
    secure_boot: bool = True
    enrolled_keys: bool = True

    def xml(self) -> str:
        if not self.secure_boot:
            return (
                '      <firmware>\n'
                '        <feature enabled="no" name="secure-boot"/>\n'
                '      </firmware>\n'
            )
        keys = "yes" if self.enrolled_keys else "no"
        return (
            '      <firmware>\n'
            '        <feature enabled="yes" name="secure-boot"/>\n'
            f'        <feature enabled="{keys}" name="enrolled-keys"/>\n'
            '      </firmware>\n'
        )


FIRMWARE_FALLBACKS = [
    FirmwareChoice(True, True),  # the real thing: Microsoft keys enrolled
    FirmwareChoice(True, False),  # secure-boot capable firmware, nothing enrolled
    FirmwareChoice(False, False),  # plain UEFI; Windows 11 still installs in a VM
]


def _cdrom(path: str, dev: str) -> str:
    if not path:
        return ""
    return (
        '      <disk type="file" device="cdrom">\n'
        '        <driver name="qemu" type="raw"/>\n'
        f'        <source file="{xml_escape(path)}" startupPolicy="optional"/>\n'
        f'        <target dev="{dev}" bus="sata"/>\n'
        "        <readonly/>\n"
        "      </disk>\n"
    )


def _hyperv(enabled: bool) -> str:
    if not enabled:
        return ""
    return (
        '        <hyperv mode="custom">\n'
        '          <relaxed state="on"/>\n'
        '          <vapic state="on"/>\n'
        '          <spinlocks state="on" retries="8191"/>\n'
        '          <vpindex state="on"/>\n'
        '          <runtime state="on"/>\n'
        '          <synic state="on"/>\n'
        '          <stimer state="on"/>\n'
        '          <frequencies state="on"/>\n'
        "        </hyperv>\n"
    )


def build_domain_xml(vm: VmSettings, firmware: FirmwareChoice | None = None, *, install_media: bool = True) -> str:
    """Return the domain XML.

    ``install_media`` attaches the Windows ISO, the virtio-win ISO, the
    Replicate setup CD and the answers CD. After the install they stay
    attached with ``startupPolicy="optional"`` so the VM still boots when
    they are gone; the answers CD is ejected and deleted by the installer.
    """
    firmware = firmware or FIRMWARE_FALLBACKS[0]
    name = xml_escape(vm.name)
    smm = '        <smm state="on"/>\n' if firmware.secure_boot else ""
    cds = ""
    if install_media:
        cds += _cdrom(vm.windows_iso, "sda")
        cds += _cdrom(vm.virtio_win_iso, "sdb")
        cds += _cdrom(vm.setup_iso, "sdc")
        cds += _cdrom(vm.answers_iso, "sdd")
    boot = '        <boot dev="cdrom"/>\n        <boot dev="hd"/>\n' if install_media else '        <boot dev="hd"/>\n'
    return f"""<domain type="kvm">
  <name>{name}</name>
  <metadata>
    <libosinfo:libosinfo xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
      <libosinfo:os id="http://microsoft.com/win/11"/>
    </libosinfo:libosinfo>
    <replicate:info xmlns:replicate="https://github.com/tahuffman1s/Replicate">
      <replicate:managed>true</replicate:managed>
    </replicate:info>
  </metadata>
  <memory unit="MiB">{int(vm.memory_mib)}</memory>
  <vcpu placement="static">{int(vm.vcpus)}</vcpu>
  <os firmware="efi">
    <type arch="x86_64" machine="q35">hvm</type>
{firmware.xml()}{boot}  </os>
  <features>
    <acpi/>
    <apic/>
{_hyperv(vm.hyperv_enlightenments)}    <vmport state="off"/>
{smm}  </features>
  <cpu mode="host-passthrough" check="none" migratable="on"/>
  <clock offset="localtime">
    <timer name="rtc" tickpolicy="catchup"/>
    <timer name="pit" tickpolicy="delay"/>
    <timer name="hpet" present="no"/>
    <timer name="hypervclock" present="yes"/>
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <pm>
    <suspend-to-mem enabled="no"/>
    <suspend-to-disk enabled="no"/>
  </pm>
  <devices>
    <disk type="file" device="disk">
      <driver name="qemu" type="qcow2" discard="unmap"/>
      <source file="{xml_escape(vm.disk_path)}"/>
      <target dev="vda" bus="virtio"/>
      <serial>replicate-system</serial>
    </disk>
{cds}    <controller type="usb" model="qemu-xhci" ports="15"/>
    <controller type="pci" model="pcie-root"/>
    <controller type="virtio-serial"/>
    <interface type="network">
      <source network="{xml_escape(vm.network)}"/>
      <model type="virtio"/>
    </interface>
    <input type="tablet" bus="usb"/>
    <input type="keyboard" bus="usb"/>
    <tpm model="tpm-crb">
      <backend type="emulator" version="2.0"/>
    </tpm>
    <graphics type="spice" autoport="yes">
      <listen type="address"/>
      <image compression="off"/>
    </graphics>
    <video>
      <model type="virtio" heads="1"/>
    </video>
    <sound model="ich9">
      <audio id="1"/>
    </sound>
    <audio id="1" type="spice"/>
    <channel type="spicevmc">
      <target type="virtio" name="com.redhat.spice.0"/>
    </channel>
    <channel type="unix">
      <target type="virtio" name="org.qemu.guest_agent.0"/>
    </channel>
    <redirdev bus="usb" type="spicevmc"/>
    <redirdev bus="usb" type="spicevmc"/>
    <memballoon model="virtio"/>
    <rng model="virtio">
      <backend model="random">/dev/urandom</backend>
    </rng>
  </devices>
</domain>
"""


def build_data_disk_xml(path: str, target_dev: str, serial: str, fmt: str = "qcow2", readonly: bool = False) -> str:
    """XML for a hot-pluggable data disk (a VHD clone) with a serial the guest can find."""
    ro = "      <readonly/>\n" if readonly else ""
    return (
        '<disk type="file" device="disk">\n'
        f'  <driver name="qemu" type="{xml_escape(fmt)}" discard="unmap"/>\n'
        f'  <source file="{xml_escape(path)}"/>\n'
        f'  <target dev="{xml_escape(target_dev)}" bus="virtio"/>\n'
        f"  <serial>{xml_escape(serial)}</serial>\n"
        f"{ro}"
        "</disk>\n"
    )
