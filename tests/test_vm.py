import xml.etree.ElementTree as ET

from replicate.config import VmSettings
from replicate.vm.domain_xml import FIRMWARE_FALLBACKS, FirmwareChoice, build_data_disk_xml, build_domain_xml
from replicate.vm.unattended import default_instance_port, render_autounattend, setup_manifest, windows_time_zone


def test_domain_xml_is_well_formed_and_carries_settings():
    vm = VmSettings(name="sql & co", memory_mib=16384, vcpus=6, windows_iso="/isos/Win11.iso", virtio_win_iso="/isos/virtio.iso")
    xml = build_domain_xml(vm)
    root = ET.fromstring(xml)
    assert root.find("name").text == "sql & co"
    assert root.find("memory").text == "16384"
    assert root.find("vcpu").text == "6"
    sources = [d.find("source").get("file") for d in root.iter("disk")]
    assert vm.disk_path in sources
    assert "/isos/Win11.iso" in sources and "/isos/virtio.iso" in sources
    assert root.find("os").get("firmware") == "efi"
    assert root.find(".//tpm") is not None


def test_firmware_fallbacks_degrade_gracefully():
    vm = VmSettings()
    assert len(FIRMWARE_FALLBACKS) == 3
    plain = build_domain_xml(vm, FirmwareChoice(secure_boot=False, enrolled_keys=False))
    assert 'name="secure-boot"' in plain and "<smm" not in plain
    root = ET.fromstring(plain)
    assert root.find(".//firmware/feature").get("enabled") == "no"


def test_domain_without_install_media_boots_from_disk():
    vm = VmSettings(windows_iso="/x.iso")
    xml = build_domain_xml(vm, install_media=False)
    root = ET.fromstring(xml)
    assert [b.get("dev") for b in root.find("os").findall("boot")] == ["hd"]
    assert all(d.get("device") != "cdrom" for d in root.iter("disk"))


def test_data_disk_xml():
    xml = build_data_disk_xml("/clones/a.qcow2", "vdc", "rp-abc", readonly=True)
    root = ET.fromstring(xml)
    assert root.find("target").get("dev") == "vdc"
    assert root.find("serial").text == "rp-abc"
    assert root.find("readonly") is not None


def test_autounattend_substitutes_and_escapes():
    xml = render_autounattend("dev", "p<a>&'\"", "America/Chicago")
    root = ET.fromstring(xml)  # escaping must keep it valid XML
    text = xml
    assert "@USER@" not in text and "@PASSWORD@" not in text and "@TIMEZONE@" not in text
    assert "Central Standard Time" in text
    assert "Setup-SqlHost.ps1" in text
    assert root is not None


def test_time_zone_mapping_defaults_to_utc():
    assert windows_time_zone("Europe/Berlin") == "W. Europe Standard Time"
    assert windows_time_zone("Mars/Olympus") == "UTC"


def test_setup_manifest_assigns_fixed_ports():
    m = setup_manifest(["MSSQLSERVER", "ACCT", "DEV"], "replicate", "pw", "user")
    ports = {i["name"]: i["port"] for i in m["instances"]}
    assert ports == {"MSSQLSERVER": 1433, "ACCT": 14331, "DEV": 14332}
    assert default_instance_port(1, "ACCT") == 14331
    assert m["adminLogin"] == "replicate"
