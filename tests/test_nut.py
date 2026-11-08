import shutil
import ssl
import subprocess

import pytest

from app import nut
from tests.fake_nut import FakeNut, unused_port


@pytest.fixture(scope="session")
def tls_context(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not installed")
    d = tmp_path_factory.mktemp("cert")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=fake-ups", "-keyout", str(d / "k.pem"), "-out", str(d / "c.pem")],
        check=True, capture_output=True,
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(d / "c.pem", d / "k.pem")
    return ctx


def fetch(port, **kw):
    kw.setdefault("tls", False)
    kw.setdefault("timeout", 1.0)
    return nut.fetch_vars("127.0.0.1", port, "ups1", **kw)


def test_parse_handles_escaped_quotes_and_other_ups():
    lines = [
        'VAR ups1 ups.model "TOWER \\"1000VA\\""',
        'VAR other ups.status "OB"',
        "garbage",
        'VAR ups1 ups.status "OL"',
    ]
    assert nut.parse_list_var(lines, "ups1") == {"ups.model": 'TOWER "1000VA"', "ups.status": "OL"}


def test_fetch_plain():
    with FakeNut() as srv:
        values = fetch(srv.port)
    assert values["ups.status"] == "OL"
    assert values["ups.model"] == 'TOWER "1000VA"'
    assert srv.connections == 1


def test_fetch_starttls_self_signed(tls_context):
    with FakeNut(tls_context) as srv:
        values = fetch(srv.port, tls=True)
    assert values["battery.runtime"] == "2389"


def test_starttls_unsupported_is_protocol_error():
    with FakeNut(None) as srv, pytest.raises(nut.NutError) as exc:
        fetch(srv.port, tls=True)
    assert exc.value.kind == nut.PROTOCOL


def test_connection_refused():
    with pytest.raises(nut.NutError) as exc:
        fetch(unused_port())
    assert exc.value.kind == nut.REFUSED
    assert "client limit" in str(exc.value)


def test_closed_without_reply_is_refused():
    with FakeNut() as srv:
        srv.mode = "close"
        with pytest.raises(nut.NutError) as exc:
            fetch(srv.port)
    assert exc.value.kind == nut.REFUSED


def test_err_reply_is_protocol_error():
    with FakeNut() as srv:
        srv.mode = "err"
        with pytest.raises(nut.NutError) as exc:
            fetch(srv.port)
    assert exc.value.kind == nut.PROTOCOL
    assert "DATA-STALE" in str(exc.value)


def test_timeout_is_unreachable():
    with FakeNut() as srv:
        srv.mode = "hang"
        with pytest.raises(nut.NutError) as exc:
            fetch(srv.port, timeout=0.3)
    assert exc.value.kind == nut.UNREACHABLE


def test_sequential_polls_never_overlap():
    with FakeNut() as srv:
        for _ in range(5):
            fetch(srv.port)
    assert srv.connections == 5
    assert srv.max_active == 1
