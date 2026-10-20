import sys
import types

from replicate.config import SqlServer
from replicate.sql import kerberos as k


def test_principal_and_realm():
    assert k.realm_for("corp.example.com.") == "CORP.EXAMPLE.COM"
    assert k.principal_for("travis", "corp.example.com") == "travis@CORP.EXAMPLE.COM"
    assert k.principal_for("CORP\\travis", "") == "travis@CORP"
    assert k.principal_for("someone@OTHER.REALM", "corp.example.com") == "someone@OTHER.REALM"
    assert k.principal_for("plain", "") == "plain"


def test_service_principal_uses_port_or_instance():
    assert k.service_principal("sql01.corp.example.com", 1433) == "MSSQLSvc/sql01.corp.example.com:1433"
    assert k.service_principal("sql01.corp.example.com", 0, "ACCT") == "MSSQLSvc/sql01.corp.example.com:ACCT"


def test_parse_klist():
    text = """Ticket cache: FILE:/tmp/krb5cc_1000
Default principal: travis@CORP.EXAMPLE.COM

Valid starting       Expires              Service principal
10/20/26 09:00:00  10/20/26 19:00:00  krbtgt/CORP.EXAMPLE.COM@CORP.EXAMPLE.COM
"""
    ticket = k.parse_klist(text)
    assert ticket is not None
    assert ticket.principal == "travis@CORP.EXAMPLE.COM"
    assert ticket.expires is not None and ticket.expires.year == 2026
    assert k.parse_klist("klist: No credentials cache found") is None


def test_account_display():
    assert SqlServer(auth="sql", username="replicate").account == "replicate"
    assert SqlServer(auth="windows", domain="CORP", username="travis").account == "CORP\\travis"
    assert SqlServer(auth="kerberos", domain="corp.example.com", username="travis").account == "travis@CORP.EXAMPLE.COM"
    assert SqlServer(auth="kerberos").auth_label == "Kerberos"


def test_ntlm_auth_combines_domain_and_user(monkeypatch):
    from replicate.sql import connection

    captured = {}

    class FakeNtlm:
        def __init__(self, user_name, password, ntlm_compatibility=3):
            captured["user"] = user_name
            captured["password"] = password

    fake_login = types.SimpleNamespace(NtlmAuth=FakeNtlm)
    monkeypatch.setitem(sys.modules, "pytds.login", fake_login)
    import pytds

    monkeypatch.setattr(pytds, "login", fake_login, raising=False)
    server = SqlServer(auth="windows", domain="CORP", username="travis", host="sql01")
    connection.build_windows_auth(server, "pw", "sql01", 1433)
    assert captured == {"user": "CORP\\travis", "password": "pw"}
    # Older entries that kept DOMAIN\\user in one field still work.
    legacy = SqlServer(auth="windows", username="OLD\\user", host="sql01")
    connection.build_windows_auth(legacy, "pw", "sql01", 1433)
    assert captured["user"] == "OLD\\user"


def test_gssapi_handshake_with_a_stub(monkeypatch):
    """Drive GssapiAuth with a stand-in for the gssapi module."""
    steps = []

    class Ctx:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.complete = False

        def step(self, token=None):
            steps.append(token)
            if token is None:
                return b"client-token"
            self.complete = True
            return None

    class Name:
        def __init__(self, value, name_type=None):
            self.value = value

    stub = types.SimpleNamespace(
        Name=Name,
        NameType=types.SimpleNamespace(kerberos_principal="krb", hostbased_service="host"),
        Credentials=lambda **kw: ("creds", kw),
        RequirementFlag=types.SimpleNamespace(mutual_authentication=1, integrity=2, confidentiality=4, out_of_sequence_detection=8),
        MechType=types.SimpleNamespace(kerberos="krb5"),
        SecurityContext=Ctx,
    )
    monkeypatch.setattr(k, "gssapi", stub)
    auth = k.GssapiAuth("MSSQLSvc/sql01.corp.example.com:1433", "travis@CORP.EXAMPLE.COM")
    assert auth.create_packet() == b"client-token"
    assert auth.handle_next(b"server-token") == b""
    assert steps == [None, b"server-token"]
    assert auth._ctx.kwargs["mech"] == "krb5"  # noqa: SLF001
