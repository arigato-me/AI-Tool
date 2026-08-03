import { ChangeEvent, useEffect, useState } from "react";
import {
  getMusicProjects,
  getMusicTracks,
  getStyles,
  getVoices,
  musicTrackRawUrl,
  submitPipeline,
  MusicProject,
  MusicTrack,
  Style,
  Voice,
} from "../api";
import { fileToBase64 } from "../lib/fileToBase64";
import { normalizeVideoUrl } from "../lib/normalizeUrl";
import Dropdown from "../components/Dropdown";

const CLONE_VALUE = "__clone__";
const NO_MUSIC_VALUE = "";

interface CsvRow {
  stt: string;
  video_name: string;
  link: string;
  status: "pending" | "submitting" | "submitted" | "error";
  pipeline_id?: string;
  error?: string;
}

/** Parser CSV tối giản (RFC4180-ish) — tự viết thay vì thêm thư viện vì chỉ cần đọc đúng 3
 * cột đơn giản, có hỗ trợ field trong dấu ngoặc kép (video_name lỡ chứa dấu phẩy). */
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  const len = text.length;
  while (i < len) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i++;
        continue;
      }
      field += c;
      i++;
      continue;
    }
    if (c === '"') {
      inQuotes = true;
      i++;
      continue;
    }
    if (c === ",") {
      row.push(field);
      field = "";
      i++;
      continue;
    }
    if (c === "\r") {
      i++;
      continue;
    }
    if (c === "\n") {
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      i++;
      continue;
    }
    field += c;
    i++;
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}

function downloadTemplate() {
  const content =
    "STT,video_name,link\n" +
    "1,Video mau 1,https://www.douyin.com/video/1234567890123456789\n" +
    "2,Video mau 2,https://www.douyin.com/video/9876543210987654321\n";
  const blob = new Blob([content], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "reup_import_template.csv";
  a.click();
  URL.revokeObjectURL(url);
}

export default function ImportCsv() {
  const [voices, setVoices] = useState<Voice[]>([]);
  const [styles, setStyles] = useState<Style[]>([]);
  const [mode, setMode] = useState<"review" | "dialogue" | "subtitle">("review");
  const [sourceLang, setSourceLang] = useState<"zh" | "other">("zh");
  const [voice, setVoice] = useState("");
  const [style, setStyle] = useState("tu_nhien");
  const [refAudioFile, setRefAudioFile] = useState<File | null>(null);
  const [subtitleMode, setSubtitleMode] = useState("burn");
  const [musicProjects, setMusicProjects] = useState<MusicProject[]>([]);
  const [musicProject, setMusicProject] = useState(NO_MUSIC_VALUE);
  const [musicTracks, setMusicTracks] = useState<MusicTrack[]>([]);
  const [musicTrack, setMusicTrack] = useState("");
  const [rows, setRows] = useState<CsvRow[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  // Nhạc nền mặc định — cùng hành vi với SubmitJob.tsx: chưa ai đụng dropdown thì tự chọn sẵn
  // chủ đề/track đầu tiên trong thư viện thay vì "Không dùng nhạc nền".
  const [musicTouched, setMusicTouched] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getVoices(), getStyles(), getMusicProjects()]).then(([v, s, m]) => {
      if (cancelled) return;
      if (v.ok) setVoices(v.voices);
      if (s.ok) setStyles(s.styles);
      if (m.ok) {
        setMusicProjects(m.projects);
        // Chỉ auto-chọn chủ đề CÓ track — chủ đề rỗng sẽ chặn submit (bắt chọn track nhưng
        // không track nào để chọn), xem cùng lý do ở SubmitJob.tsx.
        const firstWithTracks = m.projects.find((p) => p.track_count > 0);
        if (!musicTouched && !musicProject && firstWithTracks) setMusicProject(firstWithTracks.slug);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    setMusicTrack("");
    if (!musicProject) {
      setMusicTracks([]);
      return;
    }
    let cancelled = false;
    getMusicTracks(musicProject).then((res) => {
      if (cancelled || !res.ok) return;
      setMusicTracks(res.tracks);
      if (!musicTouched && res.tracks.length > 0) setMusicTrack(res.tracks[0].track);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [musicProject]);

  function handleMusicProjectChange(v: string) {
    setMusicTouched(true);
    setMusicProject(v);
  }

  function handleFile(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setFileError(null);
    const reader = new FileReader();
    reader.onload = () => {
      // Excel "CSV UTF-8" thường chèn BOM ở đầu file — bỏ đi trước khi parse.
      const text = String(reader.result ?? "").replace(/^﻿/, "");
      const table = parseCsv(text).filter((r) => r.some((cell) => cell.trim() !== ""));
      const dataRows = table.slice(1); // dòng đầu là tiêu đề, bỏ qua
      const parsed: CsvRow[] = dataRows.map((r, idx) => {
        const link = normalizeVideoUrl((r[2] ?? "").trim());
        return {
          stt: (r[0] ?? "").trim() || String(idx + 1),
          video_name: (r[1] ?? "").trim(),
          link,
          status: link ? "pending" : "error",
          error: link ? undefined : "Thiếu link",
        };
      });
      if (parsed.length === 0) {
        setFileError(
          "Không đọc được dòng dữ liệu nào — kiểm tra lại file (dòng đầu phải là tiêu đề STT,video_name,link)",
        );
      }
      setRows(parsed);
    };
    reader.onerror = () => setFileError("Không đọc được file");
    reader.readAsText(file, "utf-8");
    e.target.value = "";
  }

  async function runAll() {
    setRunning(true);
    const isClone = mode !== "subtitle" && voice === CLONE_VALUE;
    const refAudioB64 = isClone && refAudioFile ? await fileToBase64(refAudioFile) : undefined;
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].status === "error" || rows[i].status === "submitted") continue;
      setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, status: "submitting" } : r)));
      try {
        const res = await submitPipeline({
          url: rows[i].link,
          video_name: rows[i].video_name || undefined,
          mode,
          source_lang: sourceLang,
          voice: isClone || !voice ? undefined : voice,
          style,
          ref_audio_b64: refAudioB64,
          ref_audio_ext: isClone ? "wav" : undefined,
          subtitle_mode: subtitleMode,
          music_project: mode === "review" && musicProject ? musicProject : undefined,
          music_track: mode === "review" && musicProject ? musicTrack || undefined : undefined,
        });
        setRows((prev) =>
          prev.map((r, idx) => {
            if (idx !== i) return r;
            return res.ok && res.pipeline_id
              ? { ...r, status: "submitted", pipeline_id: res.pipeline_id }
              : { ...r, status: "error", error: res.error || "Không tạo được job" };
          }),
        );
      } catch (err) {
        setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, status: "error", error: String(err) } : r)));
      }
    }
    setRunning(false);
  }

  const validCount = rows.filter((r) => r.status !== "error").length;
  const submittedCount = rows.filter((r) => r.status === "submitted").length;

  return (
    <div>
      <div className="card">
        <h2>Tạo nhiều job (import CSV)</h2>
        <p className="stage-detail">
          File CSV 3 cột: <code>STT,video_name,link</code> — dòng đầu là tiêu đề. Cài đặt bên dưới (nhánh /
          ngôn ngữ / voice / style / subtitle) áp dụng chung cho mọi video trong danh sách.
        </p>
        <div className="row">
          <button type="button" onClick={downloadTemplate}>
            Tải template CSV
          </button>
          <label>
            Chọn file CSV
            <input type="file" accept=".csv,text/csv" onChange={handleFile} />
          </label>
        </div>
        {fileError && <p className="error">{fileError}</p>}
      </div>

      <div className="card">
        <h2>Cài đặt chung</h2>
        <label>
          Nhánh
          <div className="radio-group">
            <label>
              <input type="radio" name="batch-mode" checked={mode === "review"} onChange={() => setMode("review")} />
              review (mute audio gốc)
            </label>
            <label>
              <input
                type="radio"
                name="batch-mode"
                checked={mode === "dialogue"}
                onChange={() => setMode("dialogue")}
              />
              dialogue (giữ nền)
            </label>
            <label>
              <input
                type="radio"
                name="batch-mode"
                checked={mode === "subtitle"}
                onChange={() => setMode("subtitle")}
              />
              subtitle (chỉ thêm sub, giữ nguyên audio gốc)
            </label>
          </div>
        </label>
        <label>
          Ngôn ngữ gốc video (áp dụng chung cả danh sách)
          <div className="radio-group">
            <label>
              <input
                type="radio"
                name="batch-source-lang"
                checked={sourceLang === "zh"}
                onChange={() => setSourceLang("zh")}
              />
              Tiếng Trung
            </label>
            <label>
              <input
                type="radio"
                name="batch-source-lang"
                checked={sourceLang === "other"}
                onChange={() => setSourceLang("other")}
              />
              Ngôn ngữ khác
            </label>
          </div>
        </label>
        {mode === "subtitle" && (
          <p className="stage-detail">
            Nhánh subtitle không dùng TTS/giọng đọc — audio gốc được giữ nguyên, chỉ thêm phụ đề.
          </p>
        )}
        <div className="row">
          {mode !== "subtitle" && (
            <label>
              Voice ({voices.length} giọng có sẵn)
              <Dropdown
                value={voice}
                onChange={setVoice}
                options={[
                  { value: "", label: "Mặc định" },
                  ...voices.map((v) => ({ value: v.id, label: v.label })),
                  { value: CLONE_VALUE, label: "Khác — Clone giọng (upload mẫu 3-5s, dùng chung cả danh sách)" },
                ]}
              />
            </label>
          )}
          {mode !== "subtitle" && (
            <label>
              Style
              <Dropdown value={style} onChange={setStyle} options={styles.map((s) => ({ value: s.id, label: s.label }))} />
            </label>
          )}
          <label>
            Subtitle mode
            <Dropdown
              value={subtitleMode}
              onChange={setSubtitleMode}
              options={[
                { value: "burn", label: "burn" },
                { value: "soft", label: "soft" },
                { value: "none", label: "none" },
              ]}
            />
          </label>
        </div>
        {mode !== "subtitle" && voice === CLONE_VALUE && (
          <label>
            File audio mẫu (WAV, 3-5 giây) — dùng chung cho mọi video trong danh sách
            <input
              type="file"
              accept=".wav,audio/wav"
              onChange={(e) => setRefAudioFile(e.target.files?.[0] ?? null)}
            />
          </label>
        )}
        {mode === "review" && (
          <label>
            Nhạc nền (tuỳ chọn, dùng chung cho cả danh sách — {musicProjects.length} chủ đề có
            sẵn, <a href="#/music">quản lý thư viện nhạc →</a>)
            <Dropdown
              value={musicProject}
              onChange={handleMusicProjectChange}
              options={[
                { value: NO_MUSIC_VALUE, label: "Không dùng nhạc nền" },
                ...musicProjects.map((p) => ({ value: p.slug, label: `${p.display_name} (${p.track_count})` })),
              ]}
            />
          </label>
        )}
        {mode === "review" && musicProject && (
          <label>
            Track ({musicTracks.length} có sẵn)
            <Dropdown
              value={musicTrack}
              onChange={(v) => {
                setMusicTouched(true);
                setMusicTrack(v);
              }}
              options={[
                { value: "", label: "Chọn track..." },
                ...musicTracks.map((t) => ({ value: t.track, label: t.display_name })),
              ]}
            />
            {musicTrack && (
              <div className="audio-preview">
                <audio controls preload="none" src={musicTrackRawUrl(musicProject, musicTrack)} />
              </div>
            )}
          </label>
        )}
      </div>

      {rows.length > 0 && (
        <div className="card">
          <h2>
            Xem trước ({validCount}/{rows.length} dòng hợp lệ)
          </h2>
          <div className="table-wrap table-wrap--compact">
            <table>
              <thead>
                <tr>
                  <th>STT</th>
                  <th>Tên video</th>
                  <th>Link</th>
                  <th>Trạng thái</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, idx) => (
                  <tr key={idx}>
                    <td data-label="STT">{r.stt}</td>
                    <td data-label="Tên video">{r.video_name || <span className="stage-detail">(không đặt tên)</span>}</td>
                    <td className="cell-truncate" data-label="Link" title={r.link || undefined}>
                      {r.link || <span className="error">thiếu link</span>}
                    </td>
                    <td data-label="Trạng thái">
                      {r.status === "submitted" && r.pipeline_id ? (
                        <a href={`#/job/${r.pipeline_id}`}>
                          <span className="status status-finished">đã tạo</span>
                        </a>
                      ) : r.status === "submitting" ? (
                        <span className="status status-started">đang gửi...</span>
                      ) : r.status === "error" ? (
                        <span className="status status-failed">{r.error || "lỗi"}</span>
                      ) : (
                        <span className="stage-detail">chờ chạy</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            <button type="button" disabled={running || validCount === 0} onClick={runAll}>
              {running ? `Đang chạy... (${submittedCount}/${validCount})` : `Chạy ${validCount} video`}
            </button>{" "}
            <button
              type="button"
              className="btn-secondary"
              disabled={running}
              onClick={() => {
                setRows([]);
                setFileError(null);
              }}
            >
              Hủy
            </button>{" "}
            {submittedCount > 0 && !running && (
              <a href="#/monitor" className="stage-detail">
                Xem tiến độ ở Monitor →
              </a>
            )}
          </p>
        </div>
      )}
    </div>
  );
}
