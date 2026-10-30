"""Run Shelf Check locally: PYTHONPATH=src python tools/local_server.py, then open http://localhost:8000
Wraps the same Lambda handler the hosted demo uses (stdlib only)."""
import base64, json
from http.server import BaseHTTPRequestHandler, HTTPServer
from shelfcheck.app import lambda_handler


class H(BaseHTTPRequestHandler):
    def _go(self, method):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8") if n else ""
        r = lambda_handler({"httpMethod": method, "path": self.path.split("?")[0], "body": body}, None)
        out = r.get("body", "")
        data = base64.b64decode(out) if r.get("isBase64Encoded") else out.encode("utf-8")
        self.send_response(r.get("statusCode", 200))
        for k, v in (r.get("headers") or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go("GET")

    def do_POST(self):
        self._go("POST")


if __name__ == "__main__":
    print("Shelf Check on http://localhost:8000")
    HTTPServer(("127.0.0.1", 8000), H).serve_forever()
