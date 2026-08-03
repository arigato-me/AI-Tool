# Design — reup-ui

Hệ thống design khoá cho app này. Mọi trang đọc file này trước khi sửa giao diện — không tự
bịa lại mỗi trang, chỉ mở rộng file này khi cần thêm.

## Genre
modern-minimal (dashboard/ops tool nội bộ — không phải trang marketing, không cần
hero/testimonial/pricing/footer quảng cáo).

## Macrostructure family
App pages only (1 family, không có marketing/content page):
- Tất cả 6 trang dùng chung khung "Workbench" giản lược: thanh nav trên cùng + nội dung dạng
  card xếp dọc. Không hero, không CTA-strip, không footer trang trí — công cụ vận hành, chức
  năng đứng trước trang trí.

## Theme — lệch có chủ đích khỏi catalog mặc định

Không dùng webfont catalog (Space Grotesk/Geist Mono...) — **font hệ thống (`system-ui`)**,
0 request mạng, load tức thời. Đây là yêu cầu tường minh của người dùng (web nhẹ/nhanh), ưu
tiên hơn quy tắc "luôn có 1 display face riêng biệt" mặc định của Hallmark. Giữ nguyên hue
xanh (`--color-accent`) đã có sẵn trong code cũ (`#2f6fed`) — không đổi màu thương hiệu người
dùng đã quen mắt.

- `--color-paper`   oklch(97% 0.004 250)   (light) / oklch(19% 0.012 255) (dark)
- `--color-paper-2` oklch(100% 0 0)        (light) / oklch(24% 0.014 255) (dark)
- `--color-ink`     oklch(21% 0.02 260)    (light) / oklch(93% 0.006 250) (dark)
- `--color-ink-2`   oklch(48% 0.02 260)    (light) / oklch(68% 0.016 250) (dark)
- `--color-rule`    oklch(90% 0.008 250)   (light) / oklch(33% 0.016 255) (dark)
- `--color-accent`  oklch(58% 0.19 258)    (light) / oklch(72% 0.15 258) (dark)
- `--color-focus`   = `--color-accent`

## Typography
- Display + Body + Mono: `system-ui` stack (3 vai trò, cùng 1 font vật lý — hệ thống nhẹ, không
  webfont). Phân biệt bằng weight/size, không phải bằng face khác nhau.
- Type scale: `--text-xs` .. `--text-2xl`, xem `tokens.css`.

## Spacing
4-point scale, named tokens trong `tokens.css` (`--space-3xs` .. `--space-2xl`).

## Motion
- 1 easing duy nhất `--ease-out`, duration `--dur-fast`/`--dur-base`.
- Chỉ animate `transform`/`opacity`/`background-color`/`border-color`/`color` — không động layout.
- Tôn trọng `prefers-reduced-motion: reduce`.
- Không có reveal-on-scroll — đây là dashboard công cụ, không phải trang trình bày.

## Microinteractions stance
- Silent success (job submit thành công điều hướng thẳng sang trang detail, không toast).
- `:focus-visible` hiện ring ngay lập tức, không animate.
- Nút chạm ≥ 44px trên thiết bị `pointer: coarse`.

## CTA voice
- Primary: nền `--color-accent`, chữ trắng, bo góc `--radius-input`.
- Secondary: viền `--color-rule`, nền trong suốt.
- Danger (xoá): icon-only, viền, hover mới hiện đỏ — giữ nguyên hành vi cũ.

## Per-page allowances
- Không trang nào dùng enrichment (ảnh minh hoạ/illustration) — đây là công cụ vận hành, mọi
  pixel phải phục vụ chức năng.

## What pages MUST share
- Nav trên cùng (đổi thành nav co giãn: hàng ngang ở desktop, menu ẩn/hiện ở mobile).
- Bảng dữ liệu (Jobs/Monitor/Import) đổi thành dạng thẻ dưới 640px thay vì cuộn ngang.
- Nút chạm ≥ 44px, `:focus-visible` ring, cùng bộ token màu/spacing.

## What pages MAY differ on
- Bố cục nội dung riêng theo chức năng từng trang (form 1 cột, bảng, layout 2 cột
  MusicLibrary...) — miễn dùng chung token.

## Ghi chú kỹ thuật (không phải Hallmark chuẩn, đặc thù dự án)
- Không thêm dependency mới (không router lib, không icon lib, không animation lib) — giữ đúng
  quy ước "tự viết hash-routing tối giản" đã ghi trong README.
- `api.ts` và toàn bộ logic fetch/state trong các trang **không đổi** — bản redesign này chỉ
  đụng CSS + cấu trúc JSX hiển thị (className, data-label, gộp/tách wrapper), không đụng
  handler/business logic.

## Amend 2026-07-31 — sửa bug tương phản dropdown + nâng cấp visual "tinh tế vừa phải"

- **`<select>` gốc bị thay bằng `src/components/Dropdown.tsx`** ở mọi nơi có option list
  (Voice/Style/Subtitle mode/Music project/Track, filter trạng thái ở Jobs/Monitor). Lý do:
  popup option của `<select>` do OS/browser tự vẽ, không theo được token màu app — trên nhiều
  máy di động nền popup bị ép trắng cố định trong khi chữ kế thừa `--color-ink` (rất sáng ở dark
  mode) → trắng-trên-trắng gần như không đọc được. `color-scheme: light dark` ở `tokens.css`
  không đủ khắc phục. `Dropdown` tự vẽ popup (`<button>` + `<ul role="listbox">`) nên kiểm soát
  100% màu theo token, không phụ thuộc render gốc của trình duyệt. Vẫn 0 dependency mới.
- **Voice field ở `SubmitJob.tsx` tách ra hàng riêng (full-width)**, không còn chen chung `.row`
  3 cột với Style/Subtitle mode — nguyên nhân cũ: `<audio>` gốc có min-width nội tại của trình
  duyệt (~300px Chrome), bất chấp `width:100%`, tràn khỏi cột hẹp và đè lên ô bên cạnh. Player
  nghe thử giờ bọc trong `.audio-preview` (nhãn "Nghe thử giọng đã chọn:" + audio full-width).
- **`.card` có `box-shadow: var(--shadow-card)`** (token mới trong `tokens.css`, có bản dark
  riêng) — thêm chiều sâu nhẹ, không đổi border/radius/padding hiện có.
- **`.radio-group` chuyển sang dạng pill**: mỗi option bọc viền bo tròn `--radius-pill`, nền
  `--color-info-bg` + viền `--color-accent` khi được chọn (`:has(input:checked)`, hỗ trợ rộng
  rãi ở trình duyệt hiện tại), input gốc vẫn giữ nguyên (không ẩn) để giữ focus ring/accessibility
  mặc định, chỉ thêm `accent-color: var(--color-accent)` cho đúng màu thương hiệu.
- **`.context-box`** (khung `video_context`/mô tả video ở `JobDetail.tsx`) đổi nền
  `--color-info-bg` + viền trái 3px `--color-accent` để nổi rõ là 1 khối callout riêng, thay vì
  nhìn giống các khối text thường khác trên trang.
- Không đổi genre/theme/font — vẫn modern-minimal, system-ui, accent xanh cũ, đúng constraint
  "web nhẹ/nhanh" gốc. Đây là nâng cấp lớp component/độ sâu, không phải đổi hệ thống.
