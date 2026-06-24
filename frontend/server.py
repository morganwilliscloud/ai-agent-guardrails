#!/usr/bin/env python3
"""Simple static file server for the frontend."""
import http.server
from pathlib import Path

PORT = 8080
DIR = Path(__file__).parent

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR), **kwargs)

    def do_GET(self):
        # Serve index.html for / and /callback (SPA routing)
        if self.path in ("/", "/callback") or self.path.startswith("/callback?"):
            self.path = "/index.html"
        super().do_GET()

if __name__ == "__main__":
    print(f"Serving on http://localhost:{PORT}")
    http.server.HTTPServer(("", PORT), Handler).serve_forever()
