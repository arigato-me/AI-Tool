"""Server giả lập webapp nhận thông báo — CHỈ dùng để tự test adapter `webhook:<name>` bằng
tay, không phải code chạy thật trong pipeline. Chạy độc lập (không cần Docker, không cần cài gì
thêm ngoài Python 3 có sẵn), in ra màn hình mọi thứ nó nhận được để soi request thật trông thế
nào trước khi trỏ vào webapp thật. Xem hướng dẫn dùng đầy đủ: GUIDE_TEST_WEBHOOK.md."""
from __future__ import annotations

import http.server
import json
import sys

PORT = 9999


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")

        print("\n" + "=" * 60)
        print(f"[mock-webhook] Nhận request POST tới {self.path}")
        print(f"[mock-webhook] Header Authorization: {self.headers.get('Authorization', '(không có)')}")
        print(f"[mock-webhook] Content-Type: {ctype}")

        if "multipart/form-data" in ctype:
            boundary = ctype.split("boundary=")[1].encode()
            for part in body.split(b"--" + boundary):
                if b"\r\n\r\n" not in part:
                    continue
                # Chỉ tìm name="..." trong phần HEADER (trước \r\n\r\n) — nếu file upload có
                # nội dung chứa đúng chữ 'name="message"' (vd tự upload code của chính script
                # này để test) thì tìm trong cả part sẽ nhận nhầm nội dung file thành field khác.
                header, content = part.split(b"\r\n\r\n", 1)
                content = content.rsplit(b"\r\n", 1)[0]
                if b'name="message"' in header:
                    print(f"[mock-webhook] Field 'message': {content.decode('utf-8', 'ignore')!r}")
                elif b'name="file"' in header:
                    print(f"[mock-webhook] Field 'file': {len(content)} byte")
        else:
            print(f"[mock-webhook] Body thô ({len(body)} byte): {body[:200]!r}")
        print("=" * 60)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, fmt, *args) -> None:
        pass  # tắt log mặc định (mỗi request 1 dòng "- -"), đã tự in chi tiết hơn ở trên


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # in ra ngay lập tức, không đợi buffer đầy
    print(f"[mock-webhook] Đang lắng nghe ở cổng {PORT} — Ctrl+C để dừng", file=sys.stderr)
    http.server.HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
