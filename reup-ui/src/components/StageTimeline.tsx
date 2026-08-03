import { PipelineStage } from "../api";

const STAGE_ORDER = [
  "ytdlp", "transcribe", "translate", "tts",
  "editor_srt", "editor_mix_dialogue", "editor_mix_music", "editor_edit",
];

const STAGE_LABEL: Record<string, string> = {
  ytdlp: "Tải video",
  transcribe: "Nhận diện + timestamp",
  translate: "Dịch",
  tts: "Text-to-speech",
  editor_srt: "Sinh phụ đề",
  editor_mix_dialogue: "Trộn nền + TTS",
  editor_mix_music: "Trộn nhạc nền",
  editor_edit: "Mux video final",
};

/** Bước đang chạy dở CHƯA có trong `stages` (race hiếm giữa `update_stage()` và
 * `save_stage_progress()` lúc mới submit) hiển thị thêm 1 dòng live ở nhánh `showRunning` dưới
 * cùng, dựa trên `current_stage_started_at` — cập nhật theo đúng chu kỳ poll 3s có sẵn của
 * JobDetail, không cần timer riêng. */
interface RunningStage {
  name: string;
  startedAt: number;
}

export default function StageTimeline({
  stages,
  runningStage,
}: {
  stages: Record<string, PipelineStage>;
  runningStage?: RunningStage | null;
}) {
  const names = STAGE_ORDER.filter((n) => n in stages);
  const showRunning = !!runningStage && !(runningStage.name in stages);
  if (names.length === 0 && !showRunning) return <p className="stage-detail">Chưa có bước nào chạy xong.</p>;

  return (
    <div className="stage-list">
      {names.map((name) => {
        const stage = stages[name];
        // Bước "started" (job_id đã submit xong nhưng chưa có kết quả, xem
        // pipeline_runner.py::_persist_started) — nếu đúng là bước orchestrator đang chờ NGAY
        // LÚC NÀY (current_stage khớp tên), hiện đồng hồ chạy sống giống nhánh runningStage bên
        // dưới, thay vì badge tĩnh "started" đứng yên suốt cả bước (bug thật: trước đây bước
        // này KHÔNG nằm trong `stages` cho tới lúc xong, nên luôn rơi vào nhánh runningStage có
        // đồng hồ sống; giờ nằm trong `stages` sớm hơn nên cần xử lý riêng ở đây).
        if (stage.status === "started" && runningStage && runningStage.name === name) {
          return (
            <div className="stage-row" key={name}>
              <div className="stage-name">{STAGE_LABEL[name] ?? name}</div>
              <div className="stage-detail">
                <span className="status status-started">đang chạy</span>
                {" · "}
                {Math.max(0, Math.floor(Date.now() / 1000 - runningStage.startedAt))}s
              </div>
            </div>
          );
        }
        const usage =
          name === "translate"
            ? (stage.result as { usage?: { total_tokens?: number; calls_by_model?: Record<string, number> } } | undefined)
                ?.usage
            : undefined;
        return (
          <div className="stage-row" key={name}>
            <div>
              <div className="stage-name">{STAGE_LABEL[name] ?? name}</div>
              {stage.error && <div className="stage-detail error">{stage.error}</div>}
            </div>
            <div className="stage-detail">
              <span className={`status status-${stage.status}`}>{stage.status}</span>
              {stage.resumed && " · bỏ qua (resume)"}
              {typeof stage.elapsed_s === "number" && !stage.resumed && <> · {stage.elapsed_s.toFixed(1)}s</>}
              {usage?.total_tokens != null && (
                <>
                  {" · "}
                  {usage.total_tokens.toLocaleString("vi-VN")} token
                  {usage.calls_by_model &&
                    ` (${Object.entries(usage.calls_by_model)
                      .map(([m, c]) => `${c} ${m}`)
                      .join(", ")})`}
                </>
              )}
            </div>
          </div>
        );
      })}
      {showRunning && runningStage && (
        <div className="stage-row" key={runningStage.name}>
          <div className="stage-name">{STAGE_LABEL[runningStage.name] ?? runningStage.name}</div>
          <div className="stage-detail">
            <span className="status status-started">đang chạy</span>
            {" · "}
            {Math.max(0, Math.floor(Date.now() / 1000 - runningStage.startedAt))}s
          </div>
        </div>
      )}
    </div>
  );
}
