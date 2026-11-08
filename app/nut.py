"""Minimal NUT client: one short-lived connection per poll, optional STARTTLS (self-signed cert)."""

import re
import socket
import ssl

REFUSED = "refused"          # connection refused/closed on accept; UPS may be at its client limit
UNREACHABLE = "unreachable"  # timeout, no route, connection dropped mid-conversation
PROTOCOL = "protocol"        # reachable but replied with an error or something unexpected

_MAX_RESPONSE_BYTES = 1 << 20
_VAR_RE = re.compile(r'^VAR (\S+) (\S+) "(.*)"$')


class NutError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class _LineReader:
    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""
        self.received = False

    def readline(self) -> str:
        while b"\n" not in self._buf:
            if len(self._buf) > _MAX_RESPONSE_BYTES:
                raise NutError(PROTOCOL, "response too large")
            chunk = self._sock.recv(4096)
            if not chunk:
                if not self.received:
                    raise NutError(REFUSED, "connection closed with no reply (UPS may be at its client limit)")
                raise NutError(UNREACHABLE, "connection closed by UPS")
            self.received = True
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").rstrip("\r")


def _send(sock: socket.socket, command: str) -> None:
    sock.sendall(command.encode() + b"\n")


def _unescape(value: str) -> str:
    return re.sub(r'\\(["\\])', r"\1", value)


def parse_list_var(lines: list[str], ups: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        m = _VAR_RE.match(line)
        if m and m.group(1) == ups:
            result[m.group(2)] = _unescape(m.group(3))
    return result


def fetch_vars(host: str, port: int, ups: str, *, tls: bool = True, timeout: float = 3.0) -> dict[str, str]:
    """Return every variable of `ups`. Raises NutError with kind REFUSED, UNREACHABLE or PROTOCOL."""
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError:
        raise NutError(REFUSED, "connection refused (UPS may be at its client limit)") from None
    except OSError as exc:
        raise NutError(UNREACHABLE, f"cannot connect to {host}:{port}: {exc}") from exc

    sock = raw
    reader = _LineReader(sock)
    try:
        sock.settimeout(timeout)
        if tls:
            _send(sock, "STARTTLS")
            reply = reader.readline()
            if reply != "OK STARTTLS":
                raise NutError(PROTOCOL, f"STARTTLS refused: {reply}")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            # The UPS uses a self-signed certificate on a trusted LAN.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw)
            reader = _LineReader(sock)

        _send(sock, f"LIST VAR {ups}")
        lines: list[str] = []
        while True:
            line = reader.readline()
            if line.startswith("ERR"):
                raise NutError(PROTOCOL, f"UPS replied: {line}")
            if line.startswith("END LIST VAR"):
                break
            lines.append(line)

        try:
            _send(sock, "LOGOUT")
        except OSError:
            pass
        return parse_list_var(lines, ups)
    except NutError:
        raise
    except ssl.SSLError as exc:
        raise NutError(PROTOCOL, f"TLS error: {exc}") from exc
    except ConnectionResetError as exc:
        kind = UNREACHABLE if reader.received else REFUSED
        raise NutError(kind, f"connection reset: {exc}") from exc
    except OSError as exc:  # includes socket timeouts
        raise NutError(UNREACHABLE, f"{type(exc).__name__}: {exc}") from exc
    finally:
        sock.close()
        raw.close()
