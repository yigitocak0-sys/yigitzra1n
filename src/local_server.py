#!/usr/bin/env python3
import argparse
import os
import re
from http.server import HTTPServer, SimpleHTTPRequestHandler

class RangeHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("LOCAL_SERVER_VERBOSE") == "1":
            super().log_message(fmt, *args)

    def do_GET(self):
        path = self.translate_path(self.path.split("?", 1)[0])
        if not os.path.isfile(path):
            self.send_error(404)
            return
        fsize = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-(\d*)", rng)
            if m:
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else fsize - 1
                if end >= fsize:
                    end = fsize - 1
                length = max(0, end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{fsize}")
                self.send_header("Content-Length", str(length))
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json" if "/device/" in self.path else "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fsize))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Directory containing device/iPhone8,1 and local_1588.ipsw")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    os.chdir(os.path.abspath(args.root))
    HTTPServer((args.host, args.port), RangeHandler).serve_forever()

if __name__ == "__main__":
    main()
