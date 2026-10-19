"""Tinklo zondas (CI derinimui): ar paprastas Python HTTP serveris atsako klientui toje pačioje mašinoje?"""
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def probe(bind, host):
    server = ThreadingHTTPServer((bind, 0), H)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.3)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(8)
            s.connect((host, port))
            s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        result = "OK" if data.endswith(b"ok") else f"neteisingas atsakymas {data[:60]!r}"
    except Exception as error:  # noqa: BLE001
        result = f"KLAIDA {error!r}"
    server.shutdown()
    print(f"probe bind={bind} klientas={host}: {result}", flush=True)
    return result == "OK"


ok = [probe("127.0.0.1", "127.0.0.1"), probe("0.0.0.0", "127.0.0.1")]
print("python", sys.version.split()[0])
sys.exit(0)
