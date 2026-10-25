#!/usr/bin/env python3
"""
キャプチャ録画タイマー用ヘルパー

ブラウザだけではPCのスリープ／シャットダウンはできないため、
録画終了後にこのヘルパーへ依頼してPCを操作します。
このPC（127.0.0.1）でだけ待ち受け、許可したページからの依頼しか受け付けません。

使い方:
    python capture_helper.py            # 起動（録画前に起動しておく）
    python capture_helper.py --dry-run  # 動作確認用（実際には操作しない）

Ctrl+C で終了します。
"""
import argparse
import json
import platform
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_ORIGINS = [
    "https://yoppyy.github.io",  # 公開ページ
    "http://localhost",
    "http://127.0.0.1",
]

COMMANDS = {
    "Windows": {
        "sleep": ["powershell", "-NoProfile", "-Command",
                  "Add-Type -AssemblyName System.Windows.Forms; "
                  "[System.Windows.Forms.Application]::SetSuspendState("
                  "[System.Windows.Forms.PowerState]::Suspend, $false, $false)"],
        "shutdown": ["shutdown", "/s", "/t", "0"],
    },
    "Darwin": {
        "sleep": ["pmset", "sleepnow"],
        "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
    },
    "Linux": {
        "sleep": ["systemctl", "suspend"],
        "shutdown": ["systemctl", "poweroff"],
    },
}

ALLOWED = set(DEFAULT_ORIGINS)
DRY_RUN = False
LOCAL_RE = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")


def origin_allowed(origin):
    return origin in ALLOWED or bool(origin and LOCAL_RE.match(origin))


def run_action(action):
    cmd = COMMANDS.get(platform.system(), {}).get(action)
    if not cmd:
        print(f"[!] このOSでは '{action}' に未対応です: {platform.system()}")
        return
    if DRY_RUN:
        print(f"[dry-run] 実行する予定のコマンド: {' '.join(cmd)}")
        return
    print(f"[実行] {action}")
    subprocess.Popen(cmd)


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        origin = self.headers.get("Origin")
        if origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin_allowed(origin)  # Originなし = curlなどの直接アクセス

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if not self._origin_ok():
            return self._json(403, {"ok": False, "error": "origin not allowed"})
        if self.path.split("?")[0] == "/ping":
            return self._json(200, {"ok": True, "os": platform.system(), "dryRun": DRY_RUN})
        self._json(404, {"ok": False})

    def do_POST(self):
        if not self._origin_ok():
            return self._json(403, {"ok": False, "error": "origin not allowed"})
        if self.path.split("?")[0] != "/action":
            return self._json(404, {"ok": False})
        try:
            n = min(int(self.headers.get("Content-Length", "0")), 1024)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "bad request"})
        action = data.get("action")
        if action not in ("sleep", "shutdown"):
            return self._json(400, {"ok": False, "error": "unknown action"})
        self._json(200, {"ok": True, "action": action})
        threading.Timer(0.5, run_action, args=(action,)).start()  # 応答を返してから実行

    def log_message(self, fmt, *args):
        print("[{}] {}".format(self.address_string(), fmt % args))


def main():
    global DRY_RUN
    ap = argparse.ArgumentParser(description="キャプチャ録画タイマー用ヘルパー")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dry-run", action="store_true", help="実際には操作せず、実行予定を表示するだけ")
    ap.add_argument("--allow-origin", action="append", default=[],
                    help="許可するページのオリジンを追加（例: https://example.com）")
    ap.add_argument("--allow-file", action="store_true",
                    help="file:// で開いたページからの依頼も許可する")
    args = ap.parse_args()
    DRY_RUN = args.dry_run
    ALLOWED.update(args.allow_origin)
    if args.allow_file:
        ALLOWED.add("null")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"ヘルパー起動: http://127.0.0.1:{args.port}  ({platform.system()}{' / dry-run' if DRY_RUN else ''})")
    print("許可するページ:", ", ".join(sorted(ALLOWED)))
    print("終了するには Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n終了しました")


if __name__ == "__main__":
    main()
