import { PipelineStage } from "../api";

// mode="book" (xem reup-orchestrator-node/scripts/pipeline_runner.py::_run_book_pipeline) dùng
// tên stage KHÁC hẳn 5 bước video cũ: ocr gọi TUẦN TỰ từng file ("ocr_00", "ocr_01", ... — nhiều
// ảnh/trang) thay "ytdlp"/"transcribe", và tts gọi TUẦN TỰ từng segment ("tts_seg_0001", ...)
// thay vì 1 bước "tts" duy nhất — số lượng file/segment tuỳ sách nên không liệt kê cứng được,
// phải dò động bằng regex thay vì đưa vào STAGE_ORDER.
const STAGE_ORDER = [
  "ytdlp", "transcribe", "ocr", "translate", "tts",
  "editor_srt", "editor_mix_dialogue", "editor_mix_music", "editor_edit",
];
const OCR_STAGE_RE = /^ocr_(\d+)$/;
const TTS_SEG_STAGE_RE = /^tts_seg_(\d+)$/;

const STAGE_LABEL: Record<string, string> = {
  ytdlp: "Tải video",
  transcribe: "Nhận diện + timestamp",
  ocr: "Trích văn bản (OCR)",
  translate: "Dịch",
  tts: "Text-to-speech",
  editor_srt: "Sinh phụ đề",
  editor_mix_dialogue: "Trộn nền + TTS",
  editor_mix_music: "Trộn nhạc nền",
  editor_edit: "Mux video final",
};

function stageLabel(name: string): string {
  const ocrMatch = name.match(OCR_STAGE_RE);
  if (ocrMatch) return `Trích văn bản (file ${parseInt(ocrMatch[1], 10) + 1})`;
  const segMatch = name.match(TTS_SEG_STAGE_RE);
  if (segMatch) return `Đọc giọng (đoạn ${parseInt(segMatch[1], 10)})`;
  return STAGE_LABEL[name] ?? name;
}

/** Nhóm các stage được đánh số (`ocr_00`/`tts_seg_0001`, ...) sắp theo đúng thứ tự số — thay
 * THẾ vị trí tên gốc ("ocr"/"tts") trong STAGE_ORDER (nhánh sách gọi tuần tự nhiều lần thay vì
 * 1 bước duy nhất), không phải nối vào cuối danh sách. */
function _numberedGroup(stages: Record<string, PipelineStage>, singularName: string, re: RegExp): string[] {
  if (singularName in stages) return [singularName];
  return Object.keys(stages)
    .filter((n) => re.test(n))
    .sort((a, b) => parseInt(a.match(re)![1], 10) - parseInt(b.match(re)![1], 10));
}

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
  // "_speaker_voice_map" (bookkeeping nội bộ nhánh sách, xem pipeline_runner.py) cố tình không
  // khớp OCR_STAGE_RE/TTS_SEG_STAGE_RE nào — không phải bước thật, không hiển thị.
  const ocrIndex = STAGE_ORDER.indexOf("ocr");
  const ttsIndex = STAGE_ORDER.indexOf("tts");
  const beforeOcr = STAGE_ORDER.slice(0, ocrIndex).filter((n) => n in stages);
  const betweenOcrTts = STAGE_ORDER.slice(ocrIndex + 1, ttsIndex).filter((n) => n in stages);
  const afterTts = STAGE_ORDER.slice(ttsIndex + 1).filter((n) => n in stages);
  const ocrGroup = _numberedGroup(stages, "ocr", OCR_STAGE_RE);
  const ttsGroup = _numberedGroup(stages, "tts", TTS_SEG_STAGE_RE);
  const names = [...beforeOcr, ...ocrGroup, ...betweenOcrTts, ...ttsGroup, ...afterTts];
  const showRunning = !!runningStage && !(runningStage.name in stages) && runningStage.name !== "_speaker_voice_map";
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
              <div className="stage-name">{stageLabel(name)}</div>
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
              <div className="stage-name">{stageLabel(name)}</div>
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
          <div className="stage-name">{stageLabel(runningStage.name)}</div>
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
