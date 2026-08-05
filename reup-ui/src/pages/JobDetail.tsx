import { useEffect, useState } from "react";
import { cancelPipeline, getPipeline, outputUrl, retryPipeline, trimAudio, PipelineJob } from "../api";
import ImageLightbox from "../components/ImageLightbox";
import StageTimeline from "../components/StageTimeline";
import { formatDuration, totalElapsedS } from "../lib/jobDuration";

const IMAGE_EXT_RE = /\.(jpe?g|png|bmp|tiff?|webp)$/i;

export default function JobDetail({ pipelineId }: { pipelineId: string }) {
  const [job, setJob] = useState<PipelineJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [trimStart, setTrimStart] = useState("");
  const [trimEnd, setTrimEnd] = useState("");
  const [trimming, setTrimming] = useState(false);
  const [trimError, setTrimError] = useState<string | null>(null);
  const [contextCopied, setContextCopied] = useState(false);
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  async function handleCopyContext(text: string) {
    // `reup-ui` chạy http (không phải localhost/https) — window.isSecureContext = false nên
    // navigator.clipboard KHÔNG TỒN TẠI (undefined), không phải bị chặn quyền — gặp thật lúc
    // test trên chính domain thật của tool. Fallback execCommand('copy') qua textarea ẩn vẫn
    // hoạt động trên http, dù API cũ/deprecated nhưng trình duyệt hiện tại vẫn hỗ trợ.
    try {
      if (navigator.clipboard) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setContextCopied(true);
      setTimeout(() => setContextCopied(false), 2000);
    } catch {
      // Copy thất bại (trình duyệt cũ không hỗ trợ execCommand nữa...) — im lặng bỏ qua, người
      // dùng vẫn bôi đen/copy tay được từ đoạn text hiện sẵn bên dưới.
    }
  }

  async function handleResumeRetry() {
    setRetrying(true);
    try {
      const res = await retryPipeline(pipelineId);
      if (!res.ok) setError(res.error || "Không retry được job");
    } catch (err) {
      setError(String(err));
    } finally {
      setRetrying(false);
    }
  }

  async function handleCancel() {
    setCancelling(true);
    try {
      const res = await cancelPipeline(pipelineId);
      if (!res.ok) setError(res.error || "Không huỷ được job");
    } catch (err) {
      setError(String(err));
    } finally {
      setCancelling(false);
    }
  }

  async function handleTrim() {
    setTrimming(true);
    setTrimError(null);
    try {
      const res = await trimAudio(pipelineId, trimStart, trimEnd);
      if (!res.ok) {
        setTrimError(res.error || "Không cắt được file");
        return;
      }
      const updated = await getPipeline(pipelineId);
      if (updated.ok) setJob(updated);
    } catch (err) {
      setTrimError(String(err));
    } finally {
      setTrimming(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await getPipeline(pipelineId);
        if (cancelled) return;
        if (res.ok) setJob(res);
        else setError(res.error || "Không tải được job");
      } catch (err) {
        if (!cancelled) setError(String(err));
      }
    }
    load();
    const t = setInterval(load, 3000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [pipelineId]);

  const stagesData = job?.result?.stages ?? job?.partial_stages ?? {};
  const jobTotalElapsedS = job ? totalElapsedS(job) : 0;

  return (
    <div>
      <a className="back-link" href="#/jobs">
        &larr; Danh sách job
      </a>
      <div className="card">
        <h2>{job?.payload?.video_name || `Job ${pipelineId.slice(0, 8)}`}</h2>
        {error && <p className="error">{error}</p>}
        {job && (
          <>
            <p className="stage-detail">
              ID: {pipelineId.slice(0, 8)} · Nhánh: <strong>{job.payload?.mode ?? "-"}</strong> · Ngôn ngữ:{" "}
              <strong>{job.payload?.source_lang === "other" ? "khác" : "tiếng Trung"}</strong> · URL:{" "}
              {job.payload?.url ?? "-"}
            </p>
            <p>
              Trạng thái: <span className={`status status-${job.status}`}>{job.status}</span>
              {jobTotalElapsedS > 0 && (
                <>
                  {" · "}Tổng thời gian xử lý: <strong>{formatDuration(jobTotalElapsedS)}</strong>
                  <span className="stage-detail"> (không tính thời gian chờ)</span>
                </>
              )}
            </p>
            {job.error && (
              <>
                <p className="stage-detail">Chi tiết lỗi (stack trace, để chẩn đoán nguyên nhân gốc):</p>
                <pre className="error-block">{job.error}</pre>
              </>
            )}
            {(job.status === "failed" || job.status === "cancelled") && (
              <p>
                <button type="button" disabled={retrying} onClick={handleResumeRetry}>
                  {retrying ? "Đang chạy lại..." : "Chạy lại (bỏ qua bước đã xong)"}
                </button>
              </p>
            )}
            {(job.status === "pending" || job.status === "started") && (
              <p>
                <button type="button" disabled={cancelling} onClick={handleCancel}>
                  {cancelling ? "Đang huỷ..." : "Huỷ job"}
                </button>
                {job.status === "started" && (
                  <span className="stage-detail" style={{ marginLeft: "0.5rem" }}>
                    Sẽ dừng sau khi bước hiện tại xong, không dừng được ngay lập tức.
                  </span>
                )}
              </p>
            )}
          </>
        )}
        {!job && !error && <p className="stage-detail">Đang tải...</p>}
      </div>

      {job && job.status === "finished" && job.payload?.mode === "book" &&
        job.result?.source_files && job.result.source_files.length > 0 && (() => {
        const sourceFiles = job.result!.source_files!;
        const imageFiles = sourceFiles
          .filter((p) => IMAGE_EXT_RE.test(p))
          .map((p) => ({ url: outputUrl(p), name: p.split("/").pop() || p }));
        return (
          <div className="card">
            <p className="stage-detail">File đã import ({sourceFiles.length}):</p>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem" }}>
              {sourceFiles.map((path, i) => {
                const name = path.split("/").pop() || path;
                const isImage = IMAGE_EXT_RE.test(path);
                if (!isImage) {
                  return (
                    <a key={i} href={outputUrl(path)} download className="stage-detail">
                      {name}
                    </a>
                  );
                }
                const imgIndex = imageFiles.findIndex((f) => f.name === name);
                return (
                  <button
                    key={i}
                    type="button"
                    onClick={() => setLightboxIndex(imgIndex)}
                    style={{ padding: 0, border: "none", background: "none", cursor: "pointer" }}
                  >
                    <img
                      src={outputUrl(path)}
                      alt={name}
                      style={{ width: 100, height: 100, objectFit: "cover", borderRadius: 4 }}
                    />
                  </button>
                );
              })}
            </div>
            {lightboxIndex !== null && (
              <ImageLightbox
                images={imageFiles}
                startIndex={lightboxIndex}
                onClose={() => setLightboxIndex(null)}
              />
            )}
          </div>
        );
      })()}

      {job && (() => {
        const runningStage =
          job.status === "started" && job.current_stage && job.current_stage_started_at
            ? { name: job.current_stage, startedAt: job.current_stage_started_at }
            : null;
        if (Object.keys(stagesData).length === 0 && !runningStage) return null;
        return (
          <div className="card">
            <StageTimeline stages={stagesData} runningStage={runningStage} />
          </div>
        );
      })()}

      {job && job.status === "finished" && job.result?.output && (() => {
        const isAudio = job.result.output.toLowerCase().endsWith(".mp3");
        // Tên file thật (vd "video_<tên>.webm" ở nhánh mode="video" — không luôn là
        // .mp4) — trước đây ghi cứng "Tải final.mp4" cho mọi nhánh không phải audio, sai
        // nhãn khi output không phải .mp4 (bug thật gặp khi test nhánh video qua UI).
        const fileName = job.result.output.split("/").pop() || job.result.output;
        return (
          <div className="card">
            <p className="stage-detail">{isAudio ? "Audio mp3:" : "Video final:"}</p>
            {isAudio ? (
              <audio controls src={outputUrl(job.result.output)} style={{ width: "100%" }} />
            ) : (
              <video controls src={outputUrl(job.result.output)} />
            )}
            <p>
              <a href={outputUrl(job.result.output)} download>
                Tải {fileName}
              </a>
            </p>
          </div>
        );
      })()}

      {job && job.status === "finished" && (job.result?.video_context || job.result?.text_output || job.result?.text) && (
        <div className="card">
          {job.result?.video_context && (
            <>
              <div className="row-header">
                <p className="stage-detail" style={{ marginBottom: 0 }}>
                  Mô tả (dùng khi đăng bài):
                </p>
                <button type="button" className="btn-link" onClick={() => handleCopyContext(job.result!.video_context!)}>
                  {contextCopied ? "Đã copy!" : "Copy"}
                </button>
              </div>
              <p className="context-box">{job.result.video_context}</p>
            </>
          )}
          {job.result?.text_output && (
            <p>
              <a href={outputUrl(job.result.text_output)} download>
                Tải nội dung (.txt)
              </a>
            </p>
          )}
          {job.result?.text && (
            <>
              <div className="row-header">
                <p className="stage-detail" style={{ marginBottom: 0 }}>
                  Nội dung (narrator để trần, thoại nhân vật có nhãn [Tên]):
                </p>
                <button type="button" className="btn-link" onClick={() => handleCopyContext(job.result!.text!)}>
                  {contextCopied ? "Đã copy!" : "Copy"}
                </button>
              </div>
              <p className="context-box" style={{ whiteSpace: "pre-wrap" }}>{job.result.text}</p>
            </>
          )}
        </div>
      )}

      {job && job.status === "finished" && job.payload?.mode === "audio" && job.result?.output && (
        <div className="card">
          <p className="stage-detail">
            Cắt đầu/đuôi (điền ít nhất 1 ô — giây hoặc dạng "HH:MM:SS"):
          </p>
          <div className="row">
            <label>
              Cắt từ
              <input
                value={trimStart}
                onChange={(e) => setTrimStart(e.target.value)}
                placeholder="00:00:05"
              />
            </label>
            <label>
              Cắt đến
              <input
                value={trimEnd}
                onChange={(e) => setTrimEnd(e.target.value)}
                placeholder="00:01:30"
              />
            </label>
          </div>
          {trimError && <p className="error">{trimError}</p>}
          <p>
            <button
              type="button"
              disabled={trimming || (!trimStart.trim() && !trimEnd.trim())}
              onClick={handleTrim}
            >
              {trimming ? "Đang cắt..." : "Cắt"}
            </button>
          </p>
          {job.trim_output && (
            <>
              <p className="stage-detail">Bản đã cắt:</p>
              <audio controls src={outputUrl(job.trim_output)} style={{ width: "100%" }} />
              <p>
                <a href={outputUrl(job.trim_output)} download>
                  Tải bản đã cắt
                </a>
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}
