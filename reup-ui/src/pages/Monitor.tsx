import { useEffect, useState } from "react";
import {
  getHealth,
  getNodesStatus,
  getTranslateTokenStats,
  listPipelines,
  retryPipeline,
  NodeStatus,
  OrchestratorHealth,
  PipelineJob,
  TranslateTokenStats,
} from "../api";
import Dropdown from "../components/Dropdown";
import { formatDuration, totalElapsedS } from "../lib/jobDuration";

const PAGE_SIZE = 10;
const HISTORY_LIMIT = 200;

const NODE_LABELS: Record<string, string> = {
  orchestrator: "Orchestrator",
  ytdlp: "yt-dlp (tải video)",
  transcribe: "Transcribe (GPU)",
  translate: "Translate",
  "tts-gpu": "TTS GPU (giọng đọc)",
  editor: "Editor (ffmpeg)",
};

const STAGE_LABELS: Record<string, string> = {
  ytdlp: "Tải video (yt-dlp)",
  transcribe: "Nhận dạng giọng nói",
  translate: "Dịch",
  tts: "Tổng hợp giọng đọc",
  editor_srt: "Tạo phụ đề",
  editor_mix_dialogue: "Trộn nền",
  editor_edit: "Ghép video",
};

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

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function startOfWeek(d: Date): Date {
  const day = startOfDay(d);
  const dow = (day.getDay() + 6) % 7; // 0 = Thứ 2
  day.setDate(day.getDate() - dow);
  return day;
}

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function countFinishedSince(items: PipelineJob[], since: Date): number {
  const cutoff = since.getTime();
  return items.filter((j) => {
    if (j.status !== "finished") return false;
    const t = Number(j.updated_at ?? j.created_at ?? 0) * 1000;
    return t >= cutoff;
  }).length;
}

export default function Monitor() {
  const [health, setHealth] = useState<OrchestratorHealth | null>(null);
  const [nodes, setNodes] = useState<NodeStatus[]>([]);
  const [items, setItems] = useState<PipelineJob[]>([]);
  const [tokenStats, setTokenStats] = useState<TranslateTokenStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [retrying, setRetrying] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [h, n, jobs, tokens] = await Promise.all([
          getHealth(),
          getNodesStatus(),
          listPipelines(HISTORY_LIMIT),
          getTranslateTokenStats(),
        ]);
        if (cancelled) return;
        setHealth(h);
        if (n.ok) setNodes(n.nodes);
        if (jobs.ok) setItems(jobs.items);
        if (tokens.ok) setTokenStats(tokens);
        setError(null);
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

  const now = new Date();
  const today = countFinishedSince(items, startOfDay(now));
  const thisWeek = countFinishedSince(items, startOfWeek(now));
  const thisMonth = countFinishedSince(items, startOfMonth(now));

  const filteredItems = statusFilter === "all" ? items : items.filter((j) => j.status === statusFilter);
  const totalPages = Math.max(1, Math.ceil(filteredItems.length / PAGE_SIZE));
  const pageClamped = Math.min(page, totalPages);
  const pageItems = filteredItems.slice((pageClamped - 1) * PAGE_SIZE, pageClamped * PAGE_SIZE);

  function handleFilterChange(next: StatusFilter) {
    setStatusFilter(next);
    setPage(1);
  }

  /** Resume: chạy lại đúng job cũ (cùng pipeline_id), bỏ qua bước đã xong — khác đường "Sửa"
   * (điền lại form Submit, luôn tạo job MỚI hoàn toàn). Điều hướng sang trang job để theo dõi
   * tiến độ resume ngay. */
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

  const aliveCount = nodes.filter((n) => n.alive).length;

  return (
    <div>
      {error && <p className="error">{error}</p>}

      <div className="stat-grid">
        <div className="card stat-card">
          <div className="stat-label">Đang chờ</div>
          <div className="stat-value">{health?.pending ?? "-"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Đang xử lý</div>
          <div className="stat-value">{health?.processing ?? "-"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Tổng số job</div>
          <div className="stat-value">{health?.total ?? "-"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Worker hoạt động</div>
          <div className="stat-value">
            {aliveCount}/{nodes.length || 6}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="split-panel split-panel--even">
          <div>
            <h2>Video đã xử lý xong</h2>
            <div className="stat-grid">
              <div className="stat-card stat-card--inline">
                <div className="stat-label">Hôm nay</div>
                <div className="stat-value">{today}</div>
              </div>
              <div className="stat-card stat-card--inline">
                <div className="stat-label">Tuần này</div>
                <div className="stat-value">{thisWeek}</div>
              </div>
              <div className="stat-card stat-card--inline">
                <div className="stat-label">Tháng này</div>
                <div className="stat-value">{thisMonth}</div>
              </div>
            </div>
            <p className="stage-detail">Tính trong {items.length} job gần nhất (tối đa {HISTORY_LIMIT}).</p>
          </div>
          <div>
            <h2>Token dịch (Translate)</h2>
            <div className="stat-grid">
              <div className="stat-card stat-card--inline">
                <div className="stat-label">Hôm nay</div>
                <div className="stat-value">{tokenStats ? tokenStats.today.toLocaleString("vi-VN") : "-"}</div>
              </div>
              <div className="stat-card stat-card--inline">
                <div className="stat-label">Tuần này</div>
                <div className="stat-value">{tokenStats ? tokenStats.this_week.toLocaleString("vi-VN") : "-"}</div>
              </div>
              <div className="stat-card stat-card--inline">
                <div className="stat-label">Tháng này</div>
                <div className="stat-value">{tokenStats ? tokenStats.this_month.toLocaleString("vi-VN") : "-"}</div>
              </div>
            </div>
            <p className="stage-detail">Tổng token Deepseek (prompt + completion) dùng để dịch.</p>
          </div>
        </div>
      </div>

      <div className="card">
        <h2>Trạng thái worker</h2>
        <div className="worker-grid">
          {nodes.map((n) => (
            <div className="worker-row" key={n.name}>
              <span className={`dot ${n.alive ? "dot-green" : "dot-red"}`} />
              <div className="worker-info">
                <div className="worker-name">{NODE_LABELS[n.name] ?? n.name}</div>
                <div className="stage-detail">
                  {n.alive
                    ? `pending ${n.pending ?? 0} · processing ${n.processing ?? 0}`
                    : "down"}
                </div>
              </div>
            </div>
          ))}
          {nodes.length === 0 && <p className="stage-detail">Đang tải...</p>}
        </div>
      </div>

      <div className="card">
        <div className="row-header">
          <h2>Job gần đây ({filteredItems.length}/{items.length})</h2>
          <label className="filter-label">
            Lọc trạng thái
            <Dropdown
              value={statusFilter}
              onChange={(v) => handleFilterChange(v as StatusFilter)}
              options={STATUS_FILTERS.map((f) => ({ value: f, label: STATUS_FILTER_LABELS[f] }))}
            />
          </label>
        </div>
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
              {pageItems.map((j) => (
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
                    ) : j.status === "finished" ? (
                      <span className="stage-detail" title="Tổng thời gian xử lý (không tính thời gian chờ)">
                        {formatDuration(totalElapsedS(j))}
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
                    ) : (
                      <span className="stage-detail">-</span>
                    )}
                  </td>
                </tr>
              ))}
              {pageItems.length === 0 && (
                <tr>
                  <td colSpan={8}>Chưa có job nào</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="pagination">
          <button type="button" disabled={pageClamped <= 1} onClick={() => setPage(pageClamped - 1)}>
            &larr; Trước
          </button>
          <span className="stage-detail">
            Trang {pageClamped}/{totalPages}
          </span>
          <button type="button" disabled={pageClamped >= totalPages} onClick={() => setPage(pageClamped + 1)}>
            Sau &rarr;
          </button>
        </div>
      </div>
    </div>
  );
}
