# Delta Prompt — Stage `extract_text` cho pipeline tool_reup (nhánh sách → audiobook)

> Đây là **delta prompt** cho Claude Code. Không viết lại toàn bộ pipeline. Chỉ thêm một stage mới `extract_text` vào pipeline hiện có, tuân theo các convention đã có trong CLAUDE.md (lazy model loading, YAML config, per-stage CLI, resume capability, GPU exclusive per-node đã có sẵn cơ chế khóa).

---

## Mục tiêu stage

Thêm stage `extract_text`: nhận input `pdf / docx / images / pptx / xlsx / epub`, trích text ra một artifact per-page có audit trail, rồi bàn giao cho stage `translate` (đã có) → TTS → ffmpeg.

**Ràng buộc bất biến (KHÔNG được vi phạm):**

1. **Resident thấp nhất có thể.** OCR chạy GPU exclusive (cơ chế khóa GPU per-node ĐÃ TỒN TẠI — dùng nó, không tự viết lại). Khi node OCR không chạy phải nhả GPU.
2. **Chống OOM tuyệt đối với file lớn (2000+ trang).** Resident phải **phẳng theo số trang** — xử streaming từng trang, mỗi trang xử từng batch dòng. Không bao giờ giữ nhiều hơn 1 trang + 1 batch dòng trong RAM/VRAM cùng lúc.
3. **Không có lỗ hổng câm (silent failure).** Mỗi trang PHẢI sinh một bản ghi trạng thái. Không trang nào được biến mất không dấu vết.
4. **Không dùng cloud OCR.** Không bật `markitdown[all]`, không cài `markitdown-ocr` (nó dùng LLM Vision cloud). OCR 100% local.
5. **Nhập vào kiến trúc job-queue của dự án, KHÔNG phải CLI đứng riêng.** Stage này chạy dạng worker tiêu thụ queue theo pattern hiện có (worker model-load-once, queue tách theo resource, job state ở Postgres, payload ở MinIO/S3, Redis queue-only, heartbeat/lease TTL + reaper). CLI ở Phase 1-3 chỉ là entrypoint để test/dev. Phải khảo sát repo và đề xuất tích hợp (Phase 0.5) trước, chờ duyệt.
6. **Đóng gói container per-node.** Mỗi node chạy container riêng. Stage phải build thành image khớp cách đóng gói của các stage cũ, nhả GPU đúng ở cấp container (Phase 5).

---

## Kiến trúc 2 tầng

### Tầng 1 — MarkItDown: trích text CÓ SẴN (không OCR, không GPU, resident ~0)

- Cài chọn lọc: `pip install 'markitdown[pdf,docx,pptx,xlsx]'`. TUYỆT ĐỐI không `[all]`, không `markitdown-ocr`.
- Dùng `convert_local()` (KHÔNG dùng `convert()`) để khóa chỉ đọc file local, tránh permissive URI/network fetch.
- Xử: docx, pptx, xlsx, html, epub, và PDF-có-text-layer.

### Tầng 2 — OCR local: nhận dạng chữ từ pixel (GPU exclusive, chỉ chạy khi cần)

- **Detection** (dùng chung, language-agnostic): tìm text-box. Vừa là bước OCR đầu, vừa là **bộ lọc ảnh** (0 box = ảnh không chữ → bỏ qua).
- **Recognition** (route theo `source_lang`):
  - `vi` → **VietOCR** (ưu tiên chất lượng dấu; convert ONNX nếu có để giảm resident)
  - `en` / `fr` → **rec Latin** (RapidOCR / PP-OCR ONNX)
- Recognition chạy **theo batch dòng** (batch trần cấu hình được), không đẩy cả trang box vào một lần.

---

## Luồng chạy per-page (streaming, chống OOM)

Với PDF, lặp **từng trang một**, KHÔNG render cả file:

```
Mở PDF (PyMuPDF handle, lazy)
for page in pages:                      # streaming, 1 trang/lần
    text_layer = page.get_text()
    char_count = len(text_layer.strip())

    # --- BƯỚC A: phân loại trang bằng NHIỀU tín hiệu, không chỉ đếm ký tự ---
    signals = {
        char_count,
        image_area_ratio,               # % diện tích ảnh chiếm trang
        has_cid_garbage,                # có (cid:xx) => text layer rác
        control_char_ratio,
        dict_word_ratio (nếu rẻ),
    }
    page_type = classify(signals)       # -> TEXT_CLEAN | SCAN | HYBRID | BLANK | SUSPECT_FAKE_TEXT

    if page_type == BLANK:
        record(page, status="empty"); continue

    if page_type == SCAN or page_type == SUSPECT_FAKE_TEXT:
        # cả trang là ảnh (hoặc text layer rác không tin được) -> OCR nguyên trang
        img = render_page(page, max_side=RENDER_MAX_SIDE)   # trần kích thước
        page_text = ocr_image(img, source_lang)             # det->filter->rec theo batch dòng
        free(img)
        record(page, status="ocr_full", text=page_text, confidence=...)

    elif page_type == TEXT_CLEAN:
        # giữ text layer, NHƯNG vẫn quét ảnh nhúng có chữ
        page_text = text_layer
        for img_ref in page.get_images():
            if image_area(img_ref) < IMG_MIN_AREA_RATIO: continue   # bỏ icon/logo nhỏ
            img = extract_image(img_ref, max_side=RENDER_MAX_SIDE)
            boxes = detect(img)                                     # DET = bộ lọc
            if len(boxes) < MIN_TEXT_BOXES: free(img); continue     # ảnh động vật -> bỏ qua
            if total_text_area(boxes) < MIN_TEXT_AREA_RATIO * img.area:
                free(img); continue                                 # vài chữ rác -> bỏ qua
            img_text = recognize_batched(boxes, source_lang)        # rec theo batch dòng
            page_text = insert_by_reading_order(page_text, img_text, position)
            free(img)
        record(page, status="text_layer(+ocr_image)", text=page_text, confidence=...)

    elif page_type == HYBRID:
        # trang vừa có text layer vừa có vùng scan lớn -> xử cả hai như trên rồi merge
        ...

    write_page_to_disk(page)            # append ra per-page store NGAY, drop khỏi RAM
    free_page_resources(page)           # dọn pixmap/handle
```

### `ocr_image()` — recognition theo batch dòng (chống OOM VRAM, đúng yêu cầu)

```
def ocr_image(img, source_lang):
    boxes = detect(img)                 # 1 lần det
    if len(boxes) < MIN_TEXT_BOXES: return ""      # ảnh không chữ
    boxes = boxes[:MAX_BOXES_PER_IMAGE]            # trang bệnh lý nghìn box -> cắt + cờ
    lines = []
    for batch in chunk(boxes, REC_BATCH_SIZE):     # ví dụ 8-16 box/lần
        crops = [crop(img, b) for b in batch]
        lines += recognize(crops, source_lang)     # chỉ 1 batch trong VRAM tại 1 thời điểm
        free(crops)
    return join_by_reading_order(boxes, lines)
```

---

## Chống OOM — checklist bắt buộc implement

1. **Streaming per-page:** PyMuPDF lazy iterate, KHÔNG `get_pixmap` cả file. Mỗi vòng lặp chỉ 1 trang.
2. **Trần kích thước render:** `RENDER_MAX_SIDE` (mặc định 2200px). Ảnh lớn hơn scale xuống trước khi vào GPU. Chặn ảnh 20000px giết VRAM.
3. **Recognition theo batch dòng:** `REC_BATCH_SIZE` (mặc định 12). Không đẩy cả trang box vào rec.
4. **Trần box/ảnh:** `MAX_BOXES_PER_IMAGE` (mặc định 400). Vượt -> cắt + set flag `truncated`.
5. **Ghi per-page ra đĩa ngay:** mỗi trang xong append ra store (JSONL hoặc SQLite per-page), drop text khỏi RAM. Gom cuối bằng đọc theo `page_index`.
6. **`try/finally` cho MỌI GPU op:** finally LUÔN nhả GPU + free pixmap + free crops, kể cả khi exception. GPU release không được phụ thuộc happy path.
7. **Timeout per-page:** `PAGE_TIMEOUT_S` (mặc định 120). Trang bệnh lý làm det treo -> kill, cờ `timeout`, sang trang sau.
8. **CUDA OOM handler:** bắt riêng OOM -> giảm `REC_BATCH_SIZE` runtime (ví dụ chia đôi) và retry trang đó 1 lần; vẫn OOM -> cờ `oom_failed`, không làm chết node.
9. **Dọn temp:** mọi ảnh render trung gian trong context manager / tempfile tự xóa. Không để rò disk (đã có tiền lệ disk exhaustion).

---

## Audit trail per-page (chống lỗ hổng câm — nguyên tắc số 1)

Mỗi trang sinh một bản ghi (JSONL / SQLite), KHÔNG trang nào thiếu:

```json
{
  "file_id": "...",
  "page_index": 42,
  "status": "text_layer | ocr_full | ocr_image | text_layer+ocr_image | hybrid | empty | skipped_error | timeout | oom_failed",
  "source_lang": "vi",
  "char_count": 1234,
  "confidence": 0.87,           // trung bình rec confidence, null nếu text_layer
  "flags": ["low_confidence", "truncated", "fake_text_suspected", "mixed_lang_suspected"],
  "image_regions_ocred": 1,
  "image_regions_skipped": 2,   // ảnh động vật bị loại
  "error": null
}
```

- **Confidence gate:** rec confidence < `CONF_THRESHOLD` (mặc định 0.6) -> flag `low_confidence`, vẫn giữ text nhưng đánh dấu để review. Không nhét câm vào audiobook.
- Trạng thái `skipped_error / timeout / oom_failed` -> trang có mặt trong log kèm `error`, không biến mất.

---

## Xử lý exception — map đầy đủ (không lỗ hổng)

Implement handler cho từng nhóm, mỗi cái set status/flag thay vì crash batch:

**Input / mở file:**
- File corrupt/truncate, sai định dạng thật (magic bytes khác đuôi), PDF mã hóa/password, file 0 byte, ảnh định dạng lạ (webp/tiff đa layer/CMYK/16-bit/alpha) -> normalize về RGB 8-bit trước OCR; không mở được -> `skipped_error` + error rõ ràng, sang file sau.
- File khổng lồ -> streaming đã lo; thêm trần tổng trang cảnh báo (log warning nếu > `PAGE_WARN_LIMIT`).

**Tầng 1 / text-layer:**
- **PDF giả-text (`(cid:)`, ligature vỡ, ký tự chồng):** detect qua `has_cid_garbage` + `control_char_ratio` -> phân loại `SUSPECT_FAKE_TEXT` -> ÉP đi OCR thay vì tin text layer. (Đây là lỗ hổng câm nguy hiểm nhất — phải chặn.)
- **Multi-column / reading order:** dùng cờ `mixed_layout` nếu phát hiện >1 cột; giữ text layer nhưng flag để review.
- **Header/footer/watermark/số trang lặp:** phát hiện dòng lặp across pages -> strip trước khi bàn giao TTS (tránh TTS đọc "trang 12... trang 13...").
- **Bảng / công thức / emoji / URL dài:** normalize cho TTS — bảng markdown -> đọc theo hàng có nhãn hoặc strip `|`; strip ký tự không đọc được; flag `non_speech_content`.

**Detection (bộ lọc ảnh):**
- Ảnh động vật (0 box) -> skip, đếm vào `image_regions_skipped`.
- Ảnh có watermark/caption cháy vào -> ngưỡng 2 lớp (`MIN_TEXT_BOXES` + `MIN_TEXT_AREA_RATIO`) lọc chữ lẻ.
- Ảnh chứa chữ ngoài vi/en/fr (Trung/Nhật/Ả Rập) -> nếu có script-detect rẻ thì flag `foreign_script` + skip rec; nếu không, rec ra rồi confidence thấp -> `low_confidence`.

**Recognition:**
- Sai `source_lang` / trang trộn ngôn ngữ -> nếu bật `lang_verify`, chạy language-id rẻ trên text ra, lệch `source_lang` -> flag `mixed_lang_suspected`.
- Route đúng: `vi`->VietOCR, `en/fr`->rec Latin. KHÔNG để vi rơi vào Latin (mất dấu).
- Reading order khi chèn OCR ảnh vào text layer -> chèn theo bbox vị trí ảnh trong trang.

**GPU / resource:** (đã ở checklist chống OOM) — acquire fail -> retry có backoff rồi `skipped_error`; nhả GPU trong finally; re-acquire rebuild CUDA context có retry.

**Output / bàn giao TTS:**
- File trích ra 0 text toàn bộ -> KHÔNG gửi rỗng xuống TTS (tránh audio câm) -> mark file `extraction_empty`, cờ để review.
- Strip control char / null byte trước khi bàn giao.
- Xác nhận thứ tự trang (`page_index` tăng dần) khi gom từ nhiều worker.
- Đoán ngôn ngữ text ra khớp `source_lang` trước khi vào TTS.

---

## Config YAML (thêm vào config hiện có)

```yaml
extract_text:
  tier1:
    markitdown_extras: [pdf, docx, pptx, xlsx]   # KHÔNG all, KHÔNG ocr plugin
    use_convert_local: true
  detect:
    scan_char_threshold: 20          # < ký tự/trang => nghi scan
    image_area_ratio_scan: 0.6       # ảnh chiếm >60% trang => nghi scan
    fake_text_cid_check: true
  tier2_ocr:
    det_model: <shared det>
    rec:
      vi: vietocr           # ưu tiên, ONNX nếu có
      en: latin
      fr: latin
    min_text_boxes: 2              # < => ảnh không chữ (động vật) => skip
    min_text_area_ratio: 0.05     # tổng text < 5% ảnh => skip (chữ rác)
    img_min_area_ratio: 0.02      # ảnh nhỏ hơn => bỏ (icon/logo)
    conf_threshold: 0.6
  oom_guard:
    render_max_side: 2200
    rec_batch_size: 12
    max_boxes_per_image: 400
    page_timeout_s: 120
    page_warn_limit: 1500
  output:
    per_page_store: sqlite         # audit trail per-page
    strip_repeated_headers: true
    strip_control_chars: true
    lang_verify: true
```

---

## CLI (theo convention per-stage đã có)

```
python -m tool_reup.extract_text \
    --input <path> \
    --source-lang vi \
    --config config.yaml \
    --resume            # bỏ qua trang đã có trong per-page store (idempotent)
```

- **Resume/idempotency:** mỗi trang là unit độc lập trong per-page store keyed by (file_id, page_index). Crash giữa chừng -> `--resume` bỏ qua trang đã xong, không làm lại, không nhân đôi.

---

## Nghiệm thu (acceptance)

1. **Chống OOM:** chạy PDF 2000 trang, resident RAM/VRAM phải phẳng (không tăng theo số trang). Đo peak — không được vượt trần một-trang + một-batch.
2. **Trang lai text+ảnh-text:** giữ text layer + OCR đúng phần ảnh chữ, chèn đúng vị trí.
3. **Trang text+ảnh-động-vật:** giữ text layer, ảnh động vật bị skip (0 box), `image_regions_skipped >= 1`.
4. **PDF giả-text:** không tin text layer rác, ép OCR, không lọt text bẩn.
5. **Audit trail:** mọi trang đều có bản ghi status; không trang nào thiếu; lỗi -> có status + error, không câm.
6. **Nhả GPU:** kill node giữa chừng -> GPU được nhả (finally), không kẹt.
7. **VietOCR route:** cuốn `vi` -> dùng VietOCR, giữ đủ dấu; cuốn `en/fr` -> rec Latin.

---

## Quy trình thực thi bắt buộc (đọc trước khi làm bất cứ gì)

Thực hiện theo đúng các phase dưới đây, tuần tự. KHÔNG nhảy sang code chính (Phase 1+) khi Phase 0 chưa xong và chưa báo cáo. KHÔNG dựng cả stage trong một lần — chia lượt theo ranh giới kiến trúc như quy định.

### PHASE 0 — Verify rủi ro (BẮT BUỘC, chưa code stage)

Đây là cổng chặn. Ba mục này quyết định nền tảng thiết kế; nếu code chính trước khi biết kết quả, rủi ro phải làm lại. Điều tra, rồi **báo cáo kết quả + tác động lên thiết kế**, chỉ tiếp tục khi đã rõ:

0.1. **VietOCR có đường ONNX không** (quyết định resident nhánh `vi`).
   - Có → dùng ONNX để giảm resident + tăng tốc re-acquire GPU.
   - Không → giữ PyTorch, NHƯNG chỉ load VietOCR khi thực sự có trang `source_lang == vi`; unload sau khi xong cuốn để nhả VRAM.

0.2. **Det model có tách được khỏi rec không** (cả ở RapidOCR/PP-OCR lẫn VietOCR).
   - Đây là điều kiện để det làm **bộ lọc ảnh** (0 box = ảnh động vật → skip) mà không kéo rec.
   - Nếu KHÔNG tách được ở engine nào → báo lại; logic lọc ảnh phải đổi (ví dụ dùng một det độc lập của RapidOCR cho mọi ảnh, chỉ route sang rec tương ứng sau khi có box).

0.3. **Benchmark peak VRAM** một trang nhiều dòng nhất với `rec_batch_size=12` trên (hoặc mô phỏng) RTX 3050 Mobile 4GB.
   - Chỉnh `render_max_side`, `rec_batch_size`, `max_boxes_per_image` cho khớp 4GB trước khi chốt config mặc định.
   - Ghi lại peak đo được vào báo cáo Phase 0.

**Đầu ra Phase 0:** một ghi chú ngắn (`docs/extract_text_phase0.md`) tổng hợp 3 kết quả + mọi thay đổi thiết kế phát sinh. Dừng lại, chờ xác nhận trước khi sang Phase 1.

### PHASE 0.5 — Khảo sát kiến trúc hiện tại + ĐỀ XUẤT tích hợp (chờ duyệt, chưa code)

Stage này KHÔNG phải một CLI đứng riêng — nó phải nhập vào kiến trúc job-queue mà dự án đang migrate tới (worker model-load-once, queue tách theo resource, job state ở Postgres, payload video/ảnh ở MinIO/S3, Redis queue-only, heartbeat/lease TTL + reaper, per-account rate limit). CLI ở Phase 1-3 chỉ là entrypoint để test/dev; bản chất runtime là worker tiêu thụ queue.

TRƯỚC khi code, khảo sát repo thật và ĐỀ XUẤT (không tự quyết), chờ người dùng duyệt:

0.5.1. **Đọc kiến trúc hiện có:** CLAUDE.md, cấu trúc worker/queue hiện tại, cơ chế khóa GPU per-node đã tồn tại, cách các stage cũ (download/transcribe/upload) được đóng gói và chạy. Tóm tắt lại đúng hiện trạng.

0.5.2. **Đề xuất queue topology cho nhánh sách:** dựa trên pattern hiện tại (queue tách theo resource type), đề xuất nên thêm queue riêng cho nhánh này hay tái dùng queue có sẵn. Cân nhắc: Tầng 1 (CPU, nhẹ, không GPU) và Tầng 2 (GPU exclusive) có đặc tính resource RẤT khác nhau — nếu gộp chung một queue/worker thì worker OCR phải giữ GPU cả khi làm việc CPU-only, phá mục tiêu nhả GPU. Nêu rõ đánh đổi của mỗi phương án. Đề xuất tên queue theo convention hiện có (ví dụ dạng `q:...`).

0.5.3. **Đề xuất mô hình worker:** stage này thành mấy worker? Tách worker Tầng 1 (CPU) khỏi worker OCR (GPU) hay gộp? Áp dụng model-load-once thế nào cho VietOCR/rec/det (load một lần khi worker start, không load-per-job). Nhả GPU khi worker idle theo cơ chế khóa đã có.

0.5.4. **Đề xuất chỗ đặt state & payload:** job state per-page (audit trail) đặt ở Postgres hay per-page store riêng rồi sync? File input (pdf/docx/ảnh) và ảnh render trung gian đặt ở MinIO/S3 hay local scratch? Giữ Redis queue-only, KHÔNG nhét payload lớn vào Redis.

0.5.5. **Idempotency & reliability:** áp heartbeat/lease TTL + reaper của dự án cho job OCR (trang treo → reaper thu hồi). Idempotency key theo (file_id, page_index) để không OCR lặp / không nhân đôi text khi retry.

**Đầu ra Phase 0.5:** ghi `docs/extract_text_integration.md` gồm: tóm tắt hiện trạng + đề xuất topology/worker/state + đánh đổi từng lựa chọn. DỪNG, chờ người dùng duyệt trước khi sang Phase 1. Sau khi duyệt, các Phase 1-3 phải hiện thực theo phương án đã duyệt (CLI là entrypoint, nhưng code tổ chức được để chạy dạng worker tiêu thụ queue).

### PHASE 1 — Tầng 1 + khung + audit trail (chưa đụng GPU)

- MarkItDown `convert_local()` (cài chọn lọc, KHÔNG `[all]`, KHÔNG `markitdown-ocr`).
- Detect per-page bằng nhiều tín hiệu (char_count, image_area_ratio, cid check…), phân loại `TEXT_CLEAN | SCAN | HYBRID | BLANK | SUSPECT_FAKE_TEXT`.
- Per-page store (SQLite) + audit trail record đầy đủ mỗi trang.
- Streaming per-page (PyMuPDF lazy), ghi ra đĩa ngay, resume/idempotency theo (file_id, page_index).
- Test được ngay với PDF digital-born + docx, KHÔNG cần GPU.

### PHASE 2 — Tầng 2 OCR

- Det làm bộ lọc ảnh (ngưỡng 2 lớp: `min_text_boxes` + `min_text_area_ratio`).
- Recognition theo batch dòng (`rec_batch_size`), route `source_lang`: `vi`→VietOCR, `en/fr`→rec Latin.
- Chèn OCR ảnh vào text layer theo reading order.

### PHASE 3 — OOM guard + exception handlers + output validation

- Toàn bộ checklist chống OOM (streaming, trần render, batch dòng, CUDA OOM handler tự giảm batch + retry, timeout per-page, `try/finally` luôn nhả GPU, dọn temp).
- Toàn bộ exception handler theo bảng map (mỗi cái set status/flag, không crash batch).
- Output validation trước khi bàn giao TTS (không gửi rỗng, strip control char, xác nhận thứ tự trang, lang-verify).

### PHASE 4 — Nghiệm thu bằng tài liệu THẬT

Không nghiệm thu bằng file bịa. Cần các mẫu thật (nếu chưa có, yêu cầu người dùng cung cấp trước khi chạy acceptance):

- PDF digital-born (text layer sạch)
- PDF scan thuần
- PDF **lai**: trang text+ảnh-text VÀ trang text+ảnh-động-vật (test bộ lọc ảnh)
- PDF giả-text (`(cid:)`) — test không tin text layer rác
- 1 cuốn `vi` + 1 cuốn `en/fr` — test route VietOCR vs Latin

Chạy đủ 7 mục acceptance ở mục trên, báo cáo pass/fail từng mục.

### PHASE 5 — Đóng gói container (ĐỀ XUẤT trước, rồi hiện thực sau duyệt)

Toàn dự án chạy container runtime, **mỗi node là một container riêng**. Stage này phải đóng gói được thành image chạy trên node riêng, khớp cách các stage cũ được build/chạy.

5.1. **Khảo sát cách đóng gói hiện có:** đọc Dockerfile / cách build image của các stage cũ trong repo. Theo đúng convention đó (base image, cách cài dep, entrypoint, cách mount config/secret).

5.2. **ĐỀ XUẤT số lượng image** (chờ duyệt), nêu đánh đổi:
   - **Phương án A — tách 2 image:** `extract-tier1` (base CPU/slim, chỉ MarkItDown + PyMuPDF, KHÔNG CUDA → image nhỏ, resident thấp, không chiếm GPU) và `extract-ocr` (base CUDA runtime + VietOCR/rec/det → image nặng, chạy node GPU). Ưu: worker Tầng 1 không giữ GPU, scale độc lập, image tier1 gọn. Nhược: 2 image, 2 deploy.
   - **Phương án B — 1 image chung:** đơn giản deploy, nhưng kéo CUDA + model vào cả phần chỉ cần CPU, image nặng, và worker phải giữ GPU cả khi làm việc CPU-only. Mâu thuẫn mục tiêu resident thấp / nhả GPU.
   - Đề xuất phương án hợp pattern dự án nhất; để người dùng duyệt.

5.3. **Yêu cầu bắt buộc cho image OCR:**
   - Base image CUDA khớp version với 3050 Mobile + driver trên node (khớp với các image GPU cũ nếu có).
   - **KHÔNG bundle PaddlePaddle framework nặng** nếu đi đường ONNX — chỉ onnxruntime-gpu + weights (theo kết quả Phase 0).
   - Model weights: bundle vào image hay mount volume / tải từ MinIO lúc start? Đề xuất theo cách dự án làm với model STT/TTS cũ (nhất quán, tránh image phình vì weights).
   - Nhả GPU ở cấp container: đảm bảo cơ chế khóa GPU per-node hoạt động đúng khi container start/stop/crash — GPU phải được giải phóng, không kẹt device.
   - `.dockerignore` loại data/test/scratch để image gọn.

5.4. **Yêu cầu cho image Tier 1 (nếu tách):** base slim, chỉ cài `markitdown[pdf,docx,pptx,xlsx]` + PyMuPDF, không CUDA, không torch — giữ image tối thiểu.

5.5. **Healthcheck & readiness:** worker báo sẵn sàng sau khi model load-once xong (không nhận job trước khi model ready). Khớp cách health/readiness của worker cũ.

5.6. **Đầu ra:** Dockerfile(s) + hướng dẫn build/run per-node, khớp orchestration hiện tại của dự án. Nếu repo đã có manifest deploy (compose/k8s) cho stage cũ, thêm manifest tương ứng cho (các) node extract_text theo đúng khuôn.

### Cập nhật CLAUDE.md

Sau Phase 1, thêm stage `extract_text` vào phần architecture của CLAUDE.md (input types, kiến trúc 2 tầng, quan hệ với `translate`/TTS) để các lượt sau nhất quán context.

