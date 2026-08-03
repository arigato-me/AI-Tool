/** Douyin hay trả link dạng "modal_id" (mở từ tab Khám phá/Đề xuất, vd
 * https://www.douyin.com/jingxuan?modal_id=123) thay vì link "chuẩn" yt-dlp cần
 * (https://www.douyin.com/video/123) — patch douyin trong repo_github/yt-dlp chỉ nhận biết
 * dạng /video/<id>, nên "modal_id" phải quy đổi trước khi submit. */
export function normalizeVideoUrl(rawUrl: string): string {
  const trimmed = rawUrl.trim();
  if (!trimmed) return trimmed;

  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return trimmed;
  }

  const isDouyin = parsed.hostname === "douyin.com" || parsed.hostname.endsWith(".douyin.com");
  if (!isDouyin) return trimmed;

  const modalId = parsed.searchParams.get("modal_id");
  if (modalId && /^\d+$/.test(modalId)) {
    return `https://www.douyin.com/video/${modalId}`;
  }
  return trimmed;
}
