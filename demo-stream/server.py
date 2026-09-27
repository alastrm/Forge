import http.server
import socketserver
import time
import threading

def logger_loop():
    counter = 1
    while True:
        print(f"[heartbeat] event #{counter} at {time.strftime('%X')}", flush=True)
        counter += 1
        time.sleep(1)

threading.Thread(target=logger_loop, daemon=True).start()

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK\n")

    def log_message(self, format, *args):
        # Suppress noisy standard request log lines so heartbeat stands out
        pass

with socketserver.TCPServer(("", 8080), Handler) as httpd:
    print("[server] Started HTTP server on :8080", flush=True)
    httpd.serve_forever()
