# Hướng dẫn `webhook`: test bằng server giả + cấu hình/code thật để chạy được

Guide này giải thích từng bước, từng lệnh làm gì — để mày tự chạy được và tự sửa được khi có
lỗi, không cần hỏi lại. Đọc từ trên xuống, đừng nhảy cóc. Gồm 2 phần: **Phần A** (mục 1-6) test
bằng server giả để hiểu luồng chạy; **Phần B** (mục 7 trở đi) — cấu hình + code thật cần thêm ở
phía webapp của mày để chạy được thật sự (webapp chạy trên **1 server khác**, không cùng máy
với `reup-notify-node`).

## 1. Hiểu luồng chạy trước khi động tay

```
[mày gọi curl POST /jobs]
        │
        ▼
reup-notify-node (chạy trong Docker, trên máy remote)
   api nhận job -> đẩy vào hàng đợi Redis -> worker lấy ra xử lý
        │
        ▼
worker đọc file data/config/webhooks.yaml, tìm đúng "name" mày chỉ định
        │
        ▼
worker gửi 1 HTTP POST thật tới "url" ghi trong webhooks.yaml
   (kèm header Authorization mày cấu hình, kèm message + file nếu có)
        │
        ▼
[webapp của mày nhận request này] <-- đây là chỗ mày cần tự dựng để test
```

Mấu chốt: **`reup-notify-node` không "biết" webapp của mày là gì** — nó chỉ bắn 1 request HTTP
tới đúng cái `url` mày ghi trong config. Nên muốn test, việc đầu tiên là phải có 1 thứ gì đó
đang lắng nghe ở `url` đó để nhận request và cho mày xem nó nhận được gì.

## 2. Bước 1 — chạy thử 1 "webapp giả" để xem request tới trông như thế nào

Tao đã viết sẵn 1 file `scripts/mock_webhook_server.py` — 1 server cực nhỏ, chỉ để **in ra màn
hình mọi thứ nó nhận được**, không lưu gì cả. Không cần cài thêm gì (chỉ dùng thư viện có sẵn
trong Python). Mục đích: cho mày thấy tận mắt request thật trông ra sao, TRƯỚC KHI đấu vào
webapp thật của mày — đỡ mất công đoán.

SSH vào máy remote, vào đúng thư mục:

```bash
ssh hmtran@100.99.150.90
cd /home/hmtran/Projects/docker_build/reup-notify-node/scripts
```

Chạy server giả:

```bash
python3 mock_webhook_server.py
```

Nếu thấy dòng này là nó đã chạy, đang chờ:
```
[mock-webhook] Đang lắng nghe ở cổng 9999 — Ctrl+C để dừng
```

**Để nguyên cửa sổ terminal này mở** (đừng đóng, đừng Ctrl+C) — nó sẽ tự in log mỗi khi có
request tới. Mở 1 cửa sổ terminal/tab SSH **thứ 2** để làm các bước tiếp theo.

## 3. Bước 2 — trỏ `webhooks.yaml` vào server giả đó

Ở terminal thứ 2 (SSH vào lại máy remote), tạo file cấu hình:

```bash
cd /home/hmtran/Projects/docker_build/reup-notify-node
mkdir -p data/config
cat > data/config/webhooks.yaml <<'EOF'
- name: test
  url: "http://172.26.0.1:9999/notify"
  header_name: "Authorization"
  header_value: "Bearer token-bat-ky-de-test"
EOF
```

**Giải thích từng dòng:**
- `name: test` — cái tên mày sẽ dùng khi gọi job (`platforms: ["webhook:test"]`). Đặt tên gì
  cũng được, miễn khớp giữa 2 chỗ.
- `url` — nơi `reup-notify-node` sẽ gửi request tới. **Không được ghi `http://localhost:9999`**
  — vì `reup-notify-node` chạy TRONG 1 container Docker riêng, `localhost` của nó là chính nó,
  không phải máy remote. `172.26.0.1` là địa chỉ đặc biệt Docker cấp cho container để "gọi
  ngược ra máy host" (máy remote, nơi server giả đang chạy) — mày không cần hiểu sâu tại sao,
  chỉ cần nhớ: **muốn gọi ra ngoài container tới máy host, dùng `172.26.0.1`, không dùng
  `localhost`/`127.0.0.1`**. (Kiểm tra lại địa chỉ này đúng chưa bằng lệnh
  `docker network inspect reup-net --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}'` — nếu
  ra số khác thì dùng số đó thay vào.)
- `header_name`/`header_value` — mô phỏng kiểu xác thực token mà webapp thật của mày sẽ cần
  (API key, bearer token...). Với server giả thì giá trị gì cũng được vì nó không check.

## 4. Bước 3 — bật node lên (nếu chưa chạy) và gửi thử 1 job

```bash
docker compose up -d
curl -s http://localhost:8109/health
```

Thấy `"ok":true` là node đang sống. Giờ gửi 1 job thử:

```bash
curl -s -X POST http://localhost:8109/jobs \
  -H 'Content-Type: application/json' \
  -d '{"platforms": ["webhook:test"], "message": "chao mung, day la tin nhan test"}'
```

Lệnh này làm gì: gửi 1 yêu cầu tới `reup-notify-node`, bảo nó "gửi thông báo này qua target
tên `test` mà mày vừa cấu hình". Nó trả về ngay 1 dòng dạng:

```json
{"ok":true,"job_id":"abc123..."}
```

`job_id` là mã để tra kết quả sau — job không xử lý ngay lập tức lúc trả response (chạy nền qua
hàng đợi), nên cần tra lại bằng lệnh dưới.

**Quay lại terminal thứ 1** (nơi đang chạy `mock_webhook_server.py`) — mày sẽ thấy nó tự in ra
1 khối log kiểu:

```
============================================================
[mock-webhook] Nhận request POST tới /notify
[mock-webhook] Header Authorization: Bearer token-bat-ky-de-test
[mock-webhook] Content-Type: application/x-www-form-urlencoded
============================================================
```

**Đây chính là bằng chứng request đã đi từ `reup-notify-node` tới đúng chỗ mày cấu hình.** Nếu
KHÔNG thấy log này xuất hiện, xem mục Troubleshooting bên dưới.

## 5. Bước 4 — tra kết quả job (để biết node có coi là "gửi thành công" không)

```bash
curl -s http://localhost:8109/jobs/<job_id>
```

(thay `<job_id>` bằng mã thật ở bước 3). Kết quả mong đợi:

```json
{"result": {"ok": true, "sent": 1, "total": 1, "results": {"webhook:test": {"ok": true}}}}
```

`results.webhook:test.ok = true` nghĩa là request gửi đi và webapp đích trả về mã HTTP thành
công (2xx). Nếu `false`, sẽ có kèm `error` giải thích lý do (vd webapp đích trả lỗi, hoặc không
kết nối được).

## 6. Bước 5 — thử gửi kèm file (giống lúc gửi kết quả video/audio thật)

```bash
echo "noi dung file test" > data/source/thu.txt
curl -s -X POST http://localhost:8109/jobs \
  -H 'Content-Type: application/json' \
  -d '{"platforms": ["webhook:test"], "message": "co kem file nay", "file_path": "/source/thu.txt"}'
```

Lưu ý `file_path` là `/source/thu.txt` (đường dẫn **trong container**), không phải đường dẫn
thật trên máy remote — file phải nằm trong thư mục `data/source/` của node này thì container
mới thấy được (đây là quy ước bind-mount chung của mọi node trong repo, không riêng gì node
này). Xem log ở terminal 1, giờ sẽ thấy thêm dòng `Field 'file': ... byte`.

---

# PHẦN B — cấu hình + code thật (webapp chạy trên 1 server khác)

Phần A ở trên dùng server giả chạy ngay trên máy remote (`172.26.0.1`) — chỉ để hiểu luồng.
Webapp thật của mày chạy trên **1 server khác hẳn** (có IP/domain riêng) — khác hẳn tình huống
đó, nên có 2 việc mới cần làm mà Phần A chưa cần: (1) đảm bảo 2 server nói chuyện được với
nhau qua mạng thật (không phải trick `172.26.0.1` nội bộ Docker nữa), và (2) viết code thật ở
phía webapp để nhận đúng request.

## 7. Việc đầu tiên: đảm bảo 2 server "thấy" được nhau qua mạng — làm TRƯỚC KHI đụng vào code

`reup-notify-node` (chạy trên máy remote `100.99.150.90`) phải gửi được request HTTP tới
server-của-webapp. Máy remote **không tự động thấy** server kia chỉ vì cả 2 đều "ở trên
Internet" — cần xác nhận từng nấc sau, đúng theo thứ tự (mỗi bước không qua được thì bước sau
chắc chắn cũng không qua):

1. **Webapp phải đang chạy VÀ đang lắng nghe đúng port** — tự hỏi: server-của-webapp có tự bind
   vào `0.0.0.0:<port>` không, hay chỉ bind `127.0.0.1:<port>` (chỉ nhận request từ chính nó,
   không nhận từ mạng ngoài)? Đa số framework có tuỳ chọn "host" khi start server — phải để
   `0.0.0.0` (hoặc IP LAN thật của máy đó), không phải `127.0.0.1`/`localhost`.
2. **Firewall của server-đó phải mở đúng port** cho traffic từ ngoài vào (vd `ufw allow 8443`
   trên Ubuntu, hoặc rule tương đương nếu dùng cloud — AWS Security Group/GCP Firewall...).
3. **Nếu server đó KHÔNG có IP public** (đứng sau router nhà/NAT, không có domain trỏ vào) —
   máy remote `100.99.150.90` không gọi thẳng vào được, bất kể code đúng cỡ nào. Cần 1 trong:
   port-forward trên router nhà đó, VPN nối 2 máy, hoặc dịch vụ tunnel (vd Cloudflare Tunnel,
   ngrok) để có 1 URL public trỏ vào server đó — đây là việc hạ tầng riêng, không nằm trong
   phạm vi guide này, tự tìm hiểu thêm nếu rơi vào trường hợp này.
4. **Test bằng `curl` từ máy remote TRƯỚC**, đừng test qua `reup-notify-node` vội — tách riêng
   "mạng có thông không" ra khỏi "code node/code webapp có đúng không", đỡ tốn công đoán sai
   chỗ:
   ```bash
   ssh hmtran@100.99.150.90
   curl -v http://<ip-hoặc-domain-webapp>:<port>/<endpoint>
   ```
   Thấy có response (dù là lỗi 404/405 cũng được — miễn KHÔNG phải "Connection refused"/
   "Connection timed out"/"Could not resolve host") nghĩa là mạng đã thông, lỗi còn lại (nếu
   có) chắc chắn nằm ở tầng code, không phải tầng mạng nữa.

## 8. Việc thứ 2: code phía webapp — nhận đúng request `webhook_adapter.py` gửi lên

Request gửi tới luôn có dạng (xem code gửi thật ở `scripts/webhook_adapter.py` nếu muốn đối
chiếu):

- Method `POST`, tới đúng `url` mày cấu hình trong `webhooks.yaml`.
- Header xác thực: tên header = `header_name`, giá trị = `header_value` (mày tự đặt 2 cái này
  trong `webhooks.yaml` — không có ý nghĩa cố định gì, chỉ cần code webapp check đúng giá trị
  mày đã đặt).
- Nếu job có `file_path`: `Content-Type: multipart/form-data`, 2 field: `message` (text) và
  `file` (file nhị phân, kèm tên file gốc).
- Nếu job KHÔNG có `file_path`: `Content-Type: application/x-www-form-urlencoded`, 1 field:
  `message`.
- Webapp trả về mã HTTP 2xx (vd 200) là được coi "gửi thành công" bên phía `reup-notify-node` —
  bất kỳ mã khác (4xx/5xx) hoặc không trả lời (timeout) đều bị coi là gửi thất bại.

Chọn đúng ngôn ngữ webapp của mày đang dùng, copy nguyên đoạn dưới vào làm 1 endpoint mới:

### 8.1. Python (Flask)

Cần cài trước: `pip install flask`.

```python
from flask import Flask, request, jsonify

app = Flask(__name__)
EXPECTED_TOKEN = "Bearer token-that-cua-may"  # phải khớp header_value trong webhooks.yaml

@app.route("/notify", methods=["POST"])
def notify():
    if request.headers.get("Authorization") != EXPECTED_TOKEN:
        return jsonify({"error": "sai token"}), 401

    message = request.form.get("message", "")
    file = request.files.get("file")  # None nếu job không gửi kèm file

    print(f"Nhận thông báo: {message!r}")
    if file:
        file.save(f"/duong/dan/luu/{file.filename}")
        print(f"Đã lưu file: {file.filename}")

    return jsonify({"ok": True}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443)  # host="0.0.0.0" bắt buộc, không phải "127.0.0.1"
```

### 8.2. Java (Spring Boot)

Cần dự án Spring Boot có sẵn dependency `spring-boot-starter-web` (mặc định đã có nếu tạo dự án
qua [start.spring.io](https://start.spring.io) chọn "Spring Web"). Import cần thêm ở đầu file:

```java
import org.springframework.web.bind.annotation.*;
import org.springframework.web.multipart.MultipartFile;
import org.springframework.http.ResponseEntity;
import java.io.File;
import java.io.IOException;
import java.util.Map;
```

```java
@RestController
public class NotifyController {

    private static final String EXPECTED_TOKEN = "Bearer token-that-cua-may";

    @PostMapping("/notify")
    public ResponseEntity<Map<String, Object>> notify(
            @RequestHeader("Authorization") String auth,
            @RequestParam("message") String message,
            @RequestParam(value = "file", required = false) MultipartFile file) throws IOException {

        if (!EXPECTED_TOKEN.equals(auth)) {
            return ResponseEntity.status(401).body(Map.of("error", "sai token"));
        }

        System.out.println("Nhận thông báo: " + message);
        if (file != null) {
            file.transferTo(new File("/duong/dan/luu/" + file.getOriginalFilename()));
            System.out.println("Đã lưu file: " + file.getOriginalFilename());
        }

        return ResponseEntity.ok(Map.of("ok", true));
    }
}
```

`application.properties` cần có `server.address=0.0.0.0` (mặc định Spring Boot đã bind
`0.0.0.0` sẵn — chỉ cần tự kiểm tra không có dòng nào ghi đè thành `127.0.0.1`).

### 8.3. Go (`net/http`, không cần cài thêm thư viện gì)

Chỉ dùng thư viện chuẩn (`net/http`), không cần `go get` gì cả — lưu thành `main.go` rồi
`go run main.go` là chạy được ngay.

```go
package main

import (
	"fmt"
	"io"
	"net/http"
	"os"
)

const expectedToken = "Bearer token-that-cua-may"

func notifyHandler(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Authorization") != expectedToken {
		http.Error(w, `{"error":"sai token"}`, http.StatusUnauthorized)
		return
	}

	r.ParseMultipartForm(32 << 20) // 32MB — chỉnh nếu file gửi lên lớn hơn
	message := r.FormValue("message")
	fmt.Printf("Nhận thông báo: %q\n", message)

	file, header, err := r.FormFile("file")
	if err == nil { // có file gửi kèm
		defer file.Close()
		out, _ := os.Create("/duong/dan/luu/" + header.Filename)
		defer out.Close()
		io.Copy(out, file)
		fmt.Printf("Đã lưu file: %s\n", header.Filename)
	}

	w.Header().Set("Content-Type", "application/json")
	w.Write([]byte(`{"ok":true}`))
}

func main() {
	http.HandleFunc("/notify", notifyHandler)
	http.ListenAndServe("0.0.0.0:8443", nil) // "0.0.0.0" bắt buộc, không phải "127.0.0.1"
}
```

**Lưu ý chung cho cả 3 bản trên**: `EXPECTED_TOKEN`/`expectedToken` phải khớp CHÍNH XÁC
`header_name`+`header_value` mày ghi trong `webhooks.yaml` ở bước 9 dưới đây — sai 1 ký tự
(thừa/thiếu khoảng trắng sau "Bearer" chẳng hạn) là bị từ chối 401 ngay.

## 9. Trỏ `webhooks.yaml` vào webapp thật + test lại

Dừng `mock_webhook_server.py` (Ctrl+C ở terminal 1) — không cần nữa. Sửa lại
`data/config/webhooks.yaml` trên máy remote:

```yaml
- name: myapp
  url: "http://<ip-hoặc-domain-webapp>:<port>/notify"
  header_name: "Authorization"
  header_value: "Bearer token-that-cua-may"
```

Chạy lại đúng các lệnh `curl -X POST http://localhost:8109/jobs ...` ở mục 4-6 (đổi
`"webhook:test"` thành `"webhook:myapp"` cho khớp `name` mới) — tra kết quả y hệt cách cũ, chỉ
khác giờ request đi thật qua mạng tới webapp thật.

## 10. Bảo mật — giờ đi qua mạng thật, không còn nội bộ nữa

Khác Phần A (chỉ chạy trong 1 máy remote, ai vào được máy đó mới đụng tới được), giờ request đi
qua Internet — vài điều nên làm:

- **Bắt buộc check `Authorization` header** đúng như code mẫu ở mục 8 — đừng bỏ qua bước này
  "để test cho nhanh rồi thêm sau", dễ quên thêm lại.
- Nếu webapp có domain/HTTPS sẵn, dùng `https://` thay vì `http://` cho `url` trong
  `webhooks.yaml` — token gửi qua `http://` thuần bị lộ nếu ai đó soi được traffic giữa đường.
  Nếu server-của-webapp chưa có HTTPS, cân nhắc giới hạn thêm bằng firewall chỉ cho phép IP của
  máy remote (`100.99.150.90`) gọi vào port đó, thay vì mở toang cho cả Internet.
- Endpoint `/notify` này không cần public/quảng bá ở đâu cả — chỉ `reup-notify-node` gọi tới,
  không phải API cho người dùng cuối.

## 11. Troubleshooting — lỗi hay gặp

| Triệu chứng | Nguyên nhân khả dĩ | Cách kiểm tra/sửa |
|---|---|---|
| `results.webhook:xxx.error` chứa `"Connection refused"` | Webapp không lắng nghe đúng port, hoặc đang bind `127.0.0.1` thay vì `0.0.0.0` | `curl` thẳng từ terminal SSH trên máy remote (mục 7 bước 4) để tách lỗi mạng ra khỏi lỗi code |
| `error` chứa `"Connection timed out"` | Mạng không thông — firewall chặn, hoặc server-của-webapp không có IP public/chưa port-forward | Xem lại mục 7 bước 2-3 |
| `error` chứa `"Could not resolve host"` | Sai domain trong `url`, hoặc domain đó chưa trỏ DNS đúng | Tự `nslookup <domain>` để xác nhận domain phân giải đúng IP |
| `error` chứa mã HTTP 401 | Header `Authorization` gửi lên và code webapp check không khớp | So lại chính xác từng ký tự `header_value` trong `webhooks.yaml` với `EXPECTED_TOKEN` trong code — kể cả khoảng trắng |
| `error` chứa mã HTTP 4xx/5xx khác | Request TỚI ĐƯỢC webapp, nhưng webapp lỗi khi xử lý — sai format field, code webapp bug... | Xem log/console phía server webapp lúc request tới — lỗi cụ thể nằm ở đó, không nằm ở phía `reup-notify-node` |
| `error` chứa `"webhook 'xxx' chưa cấu hình"` | Tên trong `platforms: ["webhook:XXX"]` không khớp `name:` trong `webhooks.yaml` | Xem lại đúng chính tả 2 chỗ, `cat data/config/webhooks.yaml` để tự xác nhận |
| Sửa `webhooks.yaml` xong mà job vẫn dùng config cũ | File được đọc lại **mỗi lần có job mới** (không cache) — nhiều khả năng mày quên lưu đúng đường dẫn | `cat data/config/webhooks.yaml` để tự xác nhận nội dung mới đã nằm đúng chỗ |

## 12. Dọn dẹp sau khi test xong (Phần A — server giả)

```bash
rm -f data/config/webhooks.yaml    # nếu chưa muốn dùng thật, xoá đi khỏi bị nhầm sau này
rm -f data/source/thu.txt
```

(Không cần xoá gì trong Docker — `docker compose down` chỉ cần chạy nếu mày muốn tắt hẳn node,
không bắt buộc.)
