"""In-process fake NUT server (optionally with STARTTLS) for tests."""

import socket
import socketserver
import ssl
import threading
import time

DEFAULT_VARS = {
    "battery.charge": "100",
    "battery.runtime": "2389",
    "battery.voltage": "13.7",
    "input.voltage": "122.3",
    "input.voltage.nominal": "120.0",
    "ups.status": "OL",
    "ups.model": 'TOWER "1000VA"',
    "ups.mfr": "Ubiquiti",
    "ups.load": "14",
}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        srv: FakeNut = self.server.fake  # type: ignore[attr-defined]
        srv.connections += 1
        srv.active += 1
        srv.max_active = max(srv.max_active, srv.active)
        try:
            if srv.mode == "close":
                return
            if srv.mode == "hang":
                time.sleep(5)
                return
            self._converse(srv)
        except (OSError, ssl.SSLError):
            pass
        finally:
            srv.active -= 1

    def _converse(self, srv: "FakeNut") -> None:
        rfile, wfile = self.rfile, self.wfile
        while True:
            line = rfile.readline().decode().strip()
            if not line:
                return
            if line == "STARTTLS":
                if srv.ssl_context is None:
                    wfile.write(b"ERR FEATURE-NOT-CONFIGURED\n")
                    continue
                wfile.write(b"OK STARTTLS\n")
                wfile.flush()
                tls = srv.ssl_context.wrap_socket(self.request, server_side=True)
                rfile, wfile = tls.makefile("rb"), tls.makefile("wb", buffering=0)
            elif line.startswith("LIST VAR"):
                if srv.mode == "err":
                    wfile.write(b"ERR DATA-STALE\n")
                    continue
                ups = line.split()[2]
                out = [f"BEGIN LIST VAR {ups}"]
                for k, v in srv.vars.items():
                    out.append(f'VAR {ups} {k} "{v.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"')
                out.append(f"END LIST VAR {ups}")
                wfile.write(("\n".join(out) + "\n").encode())
            elif line == "LOGOUT":
                wfile.write(b"OK Goodbye\n")
                return
            else:
                wfile.write(b"ERR UNKNOWN-COMMAND\n")
            wfile.flush()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeNut:
    def __init__(self, ssl_context: ssl.SSLContext | None = None):
        self.vars = dict(DEFAULT_VARS)
        self.ssl_context = ssl_context
        self.mode = "normal"  # normal | close | hang | err
        self.connections = 0
        self.active = 0
        self.max_active = 0
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.fake = self  # type: ignore[attr-defined]
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeNut":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()


def unused_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
