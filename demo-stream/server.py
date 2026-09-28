import http.server
import socketserver
import time
import threading

APP_VERSION = "v1.0.0"

def logger_loop():
    counter = 1
    while True:
        print(f"[heartbeat] event #{counter} at {time.strftime('%X')}", flush=True)
        counter += 1
        time.sleep(1)

threading.Thread(target=logger_loop, daemon=True).start()

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        body = f"OK {APP_VERSION}\n".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Version", APP_VERSION)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress noisy standard request log lines so heartbeat stands out
        pass

with socketserver.TCPServer(("", 8080), Handler) as httpd:
    print(f"[server] Started HTTP server on :8080 (version {APP_VERSION})", flush=True)
    httpd.serve_forever()

