/** Client gọi `reup-orchestrator-node` API qua path tương đối `/api/*` — nginx (xem
 * `nginx.conf`) reverse-proxy sang `reup-orchestrator-node-api:8000`, tránh phải bật CORS. */

const BASE = "/api";

export interface PipelineStage {
  // "started": đã submit job con, job_id còn sống nhưng chưa có kết quả (xem
  // pipeline_runner.py::_persist_started) — elapsed_s luôn null ở trạng thái này.
  status: "started" | "finished" | "failed" | "cancelled";
  result?: Record<string, unknown>;
  error?: string;
  elapsed_s?: number | null;
  resumed?: boolean;
  job_id?: string;
}

export interface PipelineResult {
  ok: boolean;
  output: string;
  stages: Record<string, PipelineStage>;
  // Tóm tắt bối cảnh video (xem reup-translate-node/scripts/translate_cli.py
  // summarize_video_context) — dùng làm mô tả video khi đăng bài. Chỉ có ở nhánh
  // review/dialogue/subtitle (mode="audio"/"video" dừng sau ytdlp, bỏ qua bước translate, không
  // có field này).
  video_context?: string;
  // mode="book" — file text đầy đủ (narrator để trần, thoại nhân vật có nhãn "[Tên]") luôn có
  // khi job xong. `text` (nội dung đầy đủ) chỉ có khi NGẮN (~<2 trang giấy, xem
  // BOOK_TEXT_PREVIEW_MAX_CHARS bên pipeline_runner.py) — sách dài chỉ tải qua text_output.
  text_output?: string;
  text?: string;
  // mode="book" — ảnh/file gốc đã import lúc submit, đã copy sang /outputs/<id>/ của chính
  // pipeline này để reup-ui hiển thị lại (khác /source của ocr-node, không phục vụ HTTP ra
  // ngoài). Đúng thứ tự đã gộp vào transcript (1 ảnh = 1 trang).
  source_files?: string[];
}

export interface PipelineJob {
  ok: boolean;
  pipeline_id: string;
  status: "pending" | "started" | "finished" | "failed" | "cancelled";
  created_at?: string;
  updated_at?: string;
  error?: string;
  current_stage?: string;
  current_stage_started_at?: number | null;
  partial_stages?: Record<string, PipelineStage>;
  payload?: { url?: string; mode?: string; video_name?: string; voice?: string; ref_audio_b64?: string; [k: string]: unknown };
  result?: PipelineResult | null;
  trim_output?: string;
  // Chỉ có ở GET /pipelines (list) — orchestrator tính sẵn tổng elapsed_s rồi mới pop "result"
  // (đầy đủ segments/usage/windows quá nặng cho danh sách 200 job). GET /pipelines/{id} (single)
  // không có field này vì đã có nguyên "result.stages" để FE tự cộng.
  total_elapsed_s?: number | null;
}

// Style chữ khi subtitle_mode="burn" — khớp 7 field DEFAULT_SUB_STYLE ở
// reup-editor-node/scripts/edit_cli.py (giá trị mặc định ở đây PHẢI khớp y hệt bên đó, đây
// là style đã verify kỹ, không đổi nếu người dùng không chỉnh gì).
export interface SubStyle {
  bold: boolean;
  text_color: string;
  outline_color: string;
  outline_width: number;
  background_enabled: boolean;
  background_color: string;
  background_opacity: number;
}

export const DEFAULT_SUB_STYLE: SubStyle = {
  bold: true,
  text_color: "#FFFFFF",
  outline_color: "#000000",
  outline_width: 1,
  background_enabled: true,
  background_color: "#FFFFFF",
  background_opacity: 55,
};

/** Proxy qua orchestrator (xem api.py preview_subtitle_style) — render ảnh demo (Pillow,
 * không qua ffmpeg) mỗi lần chỉnh style, trả Blob để tạo object URL cho <img>. */
export async function previewSubtitleStyle(style: SubStyle): Promise<Blob> {
  const res = await fetch(`${BASE}/subtitle-style/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(style),
  });
  return res.blob();
}

/** Style sub người dùng đã "Lưu làm mặc định" — con trỏ đơn (không phải preset), cùng pattern
 * getDefaultMusic bên dưới. SubmitJob đọc lúc mount để tiền điền form thay vì luôn dùng
 * DEFAULT_SUB_STYLE hardcode. */
export async function getDefaultSubStyle(): Promise<{ ok: boolean; default: SubStyle | null; error?: string }> {
  const res = await fetch(`${BASE}/subtitle-style/default`);
  return res.json();
}

export async function setDefaultSubStyle(style: SubStyle): Promise<{ ok: boolean; default?: SubStyle; error?: string }> {
  const res = await fetch(`${BASE}/subtitle-style/default`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(style),
  });
  return res.json();
}

export async function clearDefaultSubStyle(): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${BASE}/subtitle-style/default`, { method: "DELETE" });
  return res.json();
}

export interface BookDocument {
  data_b64: string;
  ext: string;
}

export interface SubmitPipelineBody {
  // Bắt buộc cho mọi mode TRỪ "book" (nhánh sách dùng documents thay vì url).
  url?: string;
  video_name?: string;
  mode: "review" | "dialogue" | "subtitle" | "audio" | "video" | "book";
  source_lang?: "zh" | "other";
  voice?: string;
  style?: string;
  ref_audio_b64?: string;
  ref_audio_ext?: string;
  subtitle_mode?: string;
  sub_style?: SubStyle;
  // Nhạc nền — chỉ áp dụng khi mode="review" (xem reup-orchestrator-node/pipeline_runner.py).
  music_preset?: string;
  music_b64?: string;
  music_ext?: string;
  music_level?: number;
  // Chọn theo thư viện project/theme (mới) — ưu tiên hơn music_preset, thua music_b64.
  music_project?: string;
  music_track?: string;
  // mode="book" — 1 HOẶC NHIỀU file pdf/docx/pptx/xlsx/ảnh upload tay (base64) thay cho url —
  // nhiều file dùng cho sách chụp nhiều ảnh (1 ảnh/trang), gộp theo đúng thứ tự mảng, xem
  // reup-orchestrator-node/scripts/pipeline_runner.py::_run_book_pipeline.
  documents?: BookDocument[];
  ocr_lang?: "vi" | "en" | "fr";
  // Ngôn ngữ đọc (dịch sang) — mặc định "tiếng Việt" ở orchestrator nếu không gửi. mode="book"
  // mới cho chọn tay (Tiếng Việt/Tiếng Anh); nhánh video không gửi field này, giữ nguyên default.
  target_lang?: string;
}

export async function submitPipeline(
  body: SubmitPipelineBody,
): Promise<{ ok: boolean; pipeline_id?: string; error?: string }> {
  const res = await fetch(`${BASE}/pipelines`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}

export interface Voice {
  label: string;
  id: string;
}

export async function getVoices(): Promise<{ ok: boolean; voices: Voice[]; count: number; error?: string }> {
  const res = await fetch(`${BASE}/voices`);
  return res.json();
}

/** Sample wav 15-20s tạo sẵn (xem reup-tts-gpu-node/scripts/generate_voice_samples.py) để nghe
 * thử giọng trước khi chọn — cùng pattern với musicTrackRawUrl() bên dưới. */
export function voiceSampleUrl(id: string): string {
  return `${BASE}/voices/${encodeURIComponent(id)}/sample`;
}

export interface Style {
  id: string;
  label: string;
}

export async function getStyles(): Promise<{ ok: boolean; styles: Style[] }> {
  const res = await fetch(`${BASE}/styles`);
  return res.json();
}

export async function getMusicPresets(): Promise<{ ok: boolean; presets: string[] }> {
  const res = await fetch(`${BASE}/music`);
  return res.json();
}

export interface MusicProject {
  slug: string;
  display_name: string;
  track_count: number;
}

export interface MusicTrack {
  track: string;
  display_name: string;
  ext: string;
  size: number;
}

export async function getMusicProjects(): Promise<{ ok: boolean; projects: MusicProject[]; error?: string }> {
  const res = await fetch(`${BASE}/music/projects`);
  return res.json();
}

export async function getMusicTracks(
  slug: string,
): Promise<{ ok: boolean; tracks: MusicTrack[]; error?: string }> {
  const res = await fetch(`${BASE}/music/projects/${encodeURIComponent(slug)}/tracks`);
  return res.json();
}

export async function createMusicProject(
  display_name: string,
): Promise<{ ok: boolean; project?: MusicProject; error?: string }> {
  const res = await fetch(`${BASE}/music/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ display_name }),
  });
  return res.json();
}

export async function uploadMusicTrack(
  slug: string,
  filename: string,
  data_b64: string,
): Promise<{ ok: boolean; track?: MusicTrack; error?: string }> {
  const res = await fetch(`${BASE}/music/projects/${encodeURIComponent(slug)}/tracks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename, data_b64 }),
  });
  return res.json();
}

export function musicTrackRawUrl(slug: string, track: string): string {
  return `${BASE}/music/projects/${encodeURIComponent(slug)}/tracks/${encodeURIComponent(track)}/raw`;
}

export async function deleteMusicTrack(slug: string, track: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(
    `${BASE}/music/projects/${encodeURIComponent(slug)}/tracks/${encodeURIComponent(track)}`,
    { method: "DELETE" },
  );
  return res.json();
}

/** Xoá cả chủ đề (project) — kèm mọi track bên trong. Editor-node từ chối xoá "_ungrouped"
 * (file rời ở gốc, không phải project thật) — xem music_library.py. */
export async function deleteMusicProject(slug: string): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${BASE}/music/projects/${encodeURIComponent(slug)}`, { method: "DELETE" });
  return res.json();
}

export interface MusicDefault {
  project: string;
  track: string;
}

/** Track nhạc nền mặc định cho nhánh review — con trỏ đơn toàn thư viện (xem
 * music_library.get_default), SubmitJob dùng để tự chọn sẵn thay vì heuristic "track đầu
 * tiên" cũ khi chưa ai tick default. */
export async function getDefaultMusic(): Promise<{ ok: boolean; default: MusicDefault | null; error?: string }> {
  const res = await fetch(`${BASE}/music/default`);
  return res.json();
}

export async function setDefaultMusic(project: string, track: string): Promise<{ ok: boolean; default?: MusicDefault; error?: string }> {
  const res = await fetch(`${BASE}/music/default`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project, track }),
  });
  return res.json();
}

export async function clearDefaultMusic(): Promise<{ ok: boolean; error?: string }> {
  const res = await fetch(`${BASE}/music/default`, { method: "DELETE" });
  return res.json();
}

export async function getPipeline(id: string): Promise<PipelineJob> {
  const res = await fetch(`${BASE}/pipelines/${id}`);
  return res.json();
}

/** Chạy lại 1 job failed — resume từ đúng bước lỗi (bỏ qua bước đã xong), không tạo pipeline
 * mới, không tải/xử lý lại từ đầu. Khác `retryHref()` (điền lại form Submit để SỬA rồi tạo job
 * mới hoàn toàn) — 2 đường riêng, xem Monitor.tsx/Jobs.tsx. */
export async function retryPipeline(id: string): Promise<{ ok: boolean; pipeline_id?: string; error?: string }> {
  const res = await fetch(`${BASE}/pipelines/${id}/retry`, { method: "POST" });
  return res.json();
}

/** Huỷ 1 job đang 'pending' (gỡ thẳng khỏi hàng đợi) hoặc 'started' (đặt cờ hợp tác — không
 * dừng ngay bước đang chạy dở, chỉ chặn bước kế tiếp, xem reup-orchestrator-node/README.md
 * mục "Huỷ job"). `status` trả về "cancelled" (huỷ ngay) hoặc "cancelling" (đang chờ bước
 * hiện tại xong). */
export async function cancelPipeline(
  id: string,
): Promise<{ ok: boolean; pipeline_id?: string; status?: string; error?: string }> {
  const res = await fetch(`${BASE}/pipelines/${id}/cancel`, { method: "POST" });
  return res.json();
}

/** Cắt đầu/đuôi output mp3 của job mode="audio" đã finished — start/end: giây hoặc "HH:MM:SS",
 * truyền 1 hoặc cả 2. Xem reup-orchestrator-node/README.md mục "Cắt đầu/đuôi mp3". */
export async function trimAudio(
  id: string,
  start: string,
  end: string,
): Promise<{ ok: boolean; output?: string; duration_s?: number; error?: string }> {
  const res = await fetch(`${BASE}/pipelines/${id}/trim`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ start: start.trim() || undefined, end: end.trim() || undefined }),
  });
  return res.json();
}

export async function listPipelines(limit = 50): Promise<{ ok: boolean; items: PipelineJob[] }> {
  const res = await fetch(`${BASE}/pipelines?limit=${limit}`);
  return res.json();
}

export interface OrchestratorHealth {
  ok: boolean;
  service: string;
  worker_alive: boolean;
  total: number;
  pending: number;
  processing: number;
}

export async function getHealth(): Promise<OrchestratorHealth> {
  const res = await fetch(`${BASE}/health`);
  return res.json();
}

export interface NodeStatus {
  name: string;
  alive: boolean;
  pending: number | null;
  processing: number | null;
  error?: string;
}

export async function getNodesStatus(): Promise<{ ok: boolean; nodes: NodeStatus[] }> {
  const res = await fetch(`${BASE}/nodes/status`);
  return res.json();
}

export interface TranslateTokenStats {
  ok: boolean;
  today: number;
  this_week: number;
  this_month: number;
}

export async function getTranslateTokenStats(): Promise<TranslateTokenStats> {
  const res = await fetch(`${BASE}/stats/translate-tokens`);
  return res.json();
}

/** `result.output` trả về dạng "/outputs/<id>/final.mp4" — nối vào /api để đi qua nginx proxy. */
export function outputUrl(path: string): string {
  return `${BASE}${path}`;
}
