import base64
import json

from replicate.vm.guest import ExecResult, Guest


class FakeVirsh:
    """Answers guest-exec like a real agent would."""

    def __init__(self):
        self.commands = []

    def agent_command(self, name, command, timeout=30):
        self.commands.append(command)
        if command["execute"] == "guest-exec":
            return {"return": {"pid": 42}}
        if command["execute"] == "guest-exec-status":
            out = base64.b64encode(json.dumps({"ok": True, "letters": ["E"]}).encode()).decode()
            return {"return": {"exited": True, "exitcode": 0, "out-data": out}}
        raise AssertionError(command)

    def agent_ping(self, name):
        return True


def test_powershell_is_sent_encoded_and_json_comes_back():
    virsh = FakeVirsh()
    guest = Guest(virsh, "vm")
    data = guest.powershell_json("Get-Disk | ConvertTo-Json")
    assert data == {"ok": True, "letters": ["E"]}
    exec_cmd = virsh.commands[0]["arguments"]
    assert exec_cmd["path"].lower().endswith("powershell.exe")
    idx = exec_cmd["arg"].index("-EncodedCommand")
    encoded = exec_cmd["arg"][idx + 1]
    assert base64.b64decode(encoded).decode("utf-16-le") == "Get-Disk | ConvertTo-Json"


def test_exec_result_json_skips_leading_noise():
    r = ExecResult(0, 'WARNING: something\n{"a": 1}\n', "")
    assert r.json() == {"a": 1}
