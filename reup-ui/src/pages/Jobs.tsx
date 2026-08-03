import { useEffect, useState } from "react";
import { cancelPipeline, listPipelines, retryPipeline, PipelineJob } from "../api";
import Dropdown from "../components/Dropdown";

const STAGE_LABELS: Record<string, string> = {
  ytdlp: "Tải video (yt-dlp)",
  transcribe: "Nhận dạng giọng nói",
  translate: "Dịch",
  tts: "Tổng hợp giọng đọc",
  editor_srt: "Tạo phụ đề",
  editor_mix_dialogue: "Trộn nền",
  editor_edit: "Ghép video",
};

function stageLabel(job: PipelineJob): string {
  if (job.status === "pending") return "Đang chờ";
  if (job.status === "failed" || job.status === "cancelled") {
    const stage = job.current_stage;
    return stage ? STAGE_LABELS[stage] ?? stage : "Không rõ bước";
  }
  if (job.status !== "started") return "-";
  const stage = job.current_stage;
  if (!stage) return "Đang chờ";
  return STAGE_LABELS[stage] ?? stage;
}

/** Tên video hiển thị kèm tag nhánh (review_/dialogue_) — khớp công thức `export_stem` sinh
 * tên file final ở `pipeline_runner.py` (`f"{mode}_{video_name}"`), để không lệch với tên file
 * thật tải về. */
function displayVideoName(job: PipelineJob): string | null {
  const videoName = job.payload?.video_name;
  if (typeof videoName !== "string" || !videoName) return null;
  const mode = job.payload?.mode;
  return mode ? `${mode}_${videoName}` : videoName;
}

function voiceLabel(job: PipelineJob): string {
  const p = job.payload ?? {};
  if (p.ref_audio_b64) return "Giọng clone (upload)";
  if (typeof p.voice === "string" && p.voice) return p.voice;
  return "Mặc định";
}

const STATUS_FILTERS = ["all", "pending", "started", "finished", "failed", "cancelled"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];
const STATUS_FILTER_LABELS: Record<StatusFilter, string> = {
  all: "Tất cả",
  pending: "Đang chờ",
  started: "Đang chạy",
  finished: "Xong",
  failed: "Lỗi",
  cancelled: "Đã huỷ",
};

/** Link "Chạy lại" — điền lại form Tạo job từ payload cũ qua query param trên hash. Không
 * mang theo ref_audio_b64 (clone giọng) — file audio mẫu quá lớn để nhét vào URL, người dùng
 * cần chọn lại file nếu job cũ dùng clone giọng. */
function retryHref(job: PipelineJob): string {
  const p = job.payload ?? {};
  const retry = {
    url: p.url,
    video_name: p.video_name,
    mode: p.mode,
    source_lang: typeof p.source_lang === "string" ? p.source_lang : undefined,
    voice: typeof p.voice === "string" ? p.voice : undefined,
    style: typeof p.style === "string" ? p.style : undefined,
    subtitle_mode: typeof p.subtitle_mode === "string" ? p.subtitle_mode : undefined,
  };
  return `#/submit?retry=${encodeURIComponent(JSON.stringify(retry))}`;
}

export default function Jobs() {
  const [items, setItems] = useState<PipelineJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [retrying, setRetrying] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);

  /** Resume: chạy lại đúng job cũ (cùng pipeline_id), bỏ qua bước đã xong — khác đường "Sửa"
   * (điền lại form Submit, luôn tạo job MỚI hoàn toàn). */
  async function handleResumeRetry(pipelineId: string) {
    setRetrying(pipelineId);
    try {
      const res = await retryPipeline(pipelineId);
      if (res.ok) {
        window.location.hash = `#/job/${pipelineId}`;
      } else {
        setError(res.error || "Không retry được job");
      }
    } catch (err) {
      setError(String(err));
    } finally {
      setRetrying(null);
    }
  }

  async function handleCancel(pipelineId: string) {
    setCancelling(pipelineId);
    try {
      const res = await cancelPipeline(pipelineId);
      if (!res.ok) setError(res.error || "Không huỷ được job");
    } catch (err) {
      setError(String(err));
    } finally {
      setCancelling(null);
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await listPipelines();
        if (cancelled) return;
        if (res.ok) setItems(res.items);
        else setError("Không tải được danh sách job");
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const filteredItems = statusFilter === "all" ? items : items.filter((j) => j.status === statusFilter);

  return (
    <div className="card">
      <div className="row-header">
        <h2>Job gần đây ({filteredItems.length}/{items.length})</h2>
        <label className="filter-label">
          Lọc trạng thái
          <Dropdown
            value={statusFilter}
            onChange={(v) => setStatusFilter(v as StatusFilter)}
            options={STATUS_FILTERS.map((f) => ({ value: f, label: STATUS_FILTER_LABELS[f] }))}
          />
        </label>
      </div>
      {error && <p className="error">{error}</p>}
      <div className="table-wrap">
        <table>
          <colgroup>
            <col style={{ width: "16%" }} />
            <col style={{ width: "10%" }} />
            <col style={{ width: "8%" }} />
            <col style={{ width: "12%" }} />
            <col style={{ width: "10%" }} />
            <col style={{ width: "16%" }} />
            <col style={{ width: "14%" }} />
            <col style={{ width: "14%" }} />
          </colgroup>
          <thead>
            <tr>
              <th>Tên video</th>
              <th>Pipeline ID</th>
              <th>Nhánh</th>
              <th>Giọng đọc</th>
              <th>Trạng thái</th>
              <th>Giai đoạn xử lý</th>
              <th>Tạo lúc</th>
              <th>Thao tác</th>
            </tr>
          </thead>
          <tbody>
            {filteredItems.map((j) => (
              <tr key={j.pipeline_id}>
                <td className="cell-truncate" data-label="Tên video" title={displayVideoName(j) || undefined}>
                  {displayVideoName(j) || <span className="stage-detail">(không đặt tên)</span>}
                </td>
                <td className="cell-truncate" data-label="Pipeline ID" title={j.pipeline_id}>
                  <a href={`#/job/${j.pipeline_id}`}>{j.pipeline_id.slice(0, 8)}</a>
                </td>
                <td data-label="Nhánh">{j.payload?.mode ?? "-"}</td>
                <td className="cell-truncate" data-label="Giọng đọc" title={voiceLabel(j)}>
                  {voiceLabel(j)}
                </td>
                <td data-label="Trạng thái">
                  <span
                    className={`status status-${j.status}`}
                    title={j.status === "failed" ? j.error?.slice(0, 500) : undefined}
                  >
                    {j.status}
                  </span>
                </td>
                <td className="cell-truncate" data-label="Giai đoạn xử lý">
                  {j.status === "started" || j.status === "pending" || j.status === "failed" || j.status === "cancelled" ? (
                    <span
                      className={`stage-pill ${j.status === "failed" ? "stage-pill--error" : ""}`}
                      title={stageLabel(j)}
                    >
                      {stageLabel(j)}
                    </span>
                  ) : (
                    <span className="stage-detail">-</span>
                  )}
                </td>
                <td data-label="Tạo lúc">{j.created_at ? new Date(Number(j.created_at) * 1000).toLocaleString("vi-VN") : "-"}</td>
                <td data-label="Thao tác">
                  {j.status === "failed" || j.status === "cancelled" ? (
                    <>
                      <button
                        type="button"
                        className="btn-link"
                        disabled={retrying === j.pipeline_id}
                        onClick={() => handleResumeRetry(j.pipeline_id)}
                      >
                        {retrying === j.pipeline_id ? "Đang chạy lại..." : "Chạy lại"}
                      </button>
                      {" · "}
                      <a href={retryHref(j)}>Sửa</a>
                    </>
                  ) : j.status === "pending" || j.status === "started" ? (
                    <button
                      type="button"
                      className="btn-link"
                      disabled={cancelling === j.pipeline_id}
                      onClick={() => handleCancel(j.pipeline_id)}
                    >
                      {cancelling === j.pipeline_id ? "Đang huỷ..." : "Huỷ"}
                    </button>
                  ) : (
                    <span className="stage-detail">-</span>
                  )}
                </td>
              </tr>
            ))}
            {filteredItems.length === 0 && (
              <tr>
                <td colSpan={8}>Chưa có job nào</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
