import { FormEvent, KeyboardEvent, useEffect, useState } from "react";
import {
  clearDefaultSubStyle,
  getDefaultMusic,
  getDefaultSubStyle,
  getMusicProjects,
  getMusicTracks,
  getStyles,
  getVoices,
  musicTrackRawUrl,
  previewSubtitleStyle,
  setDefaultSubStyle,
  submitPipeline,
  voiceSampleUrl,
  DEFAULT_SUB_STYLE,
  MusicDefault,
  MusicProject,
  MusicTrack,
  Style,
  SubStyle,
  Voice,
} from "../api";
import { fileToBase64 } from "../lib/fileToBase64";
import { normalizeVideoUrl } from "../lib/normalizeUrl";
import { RetryPayload } from "../App";
import Dropdown from "../components/Dropdown";
import FieldInfo from "../components/FieldInfo";

const CLONE_VALUE = "__clone__";
const CUSTOM_MUSIC_VALUE = "__custom_music__";
const NO_MUSIC_VALUE = "";

interface SubmitJobProps {
  initial?: RetryPayload;
}

export default function SubmitJob({ initial }: SubmitJobProps) {
  const [url, setUrl] = useState(initial?.url ?? "");
  const [videoName, setVideoName] = useState(initial?.video_name ?? "");
  const [mode, setMode] = useState<"review" | "dialogue" | "subtitle" | "audio" | "video" | "book">(initial?.mode ?? "review");
  // "audio"/"video" đều dừng ngay sau ytdlp (chỉ tải, không transcribe/dịch/TTS/mux) — mọi field
  // downstream (source_lang, voice, style, subtitle_mode, clone giọng, nhạc nền) đều vô nghĩa.
  const isDownloadOnly = mode === "audio" || mode === "video";
  // "book" (Sách → Audio) không có url/video nguồn (upload file thẳng), không dùng sub/nhạc
  // nền/clone giọng (nhiều nhân vật, mỗi người 1 giọng — clone chỉ cho 1 giọng, không hợp round-
  // robin nhiều nhân vật, xem reup-orchestrator-node/README.md).
  const isBook = mode === "book";
  const [sourceLang, setSourceLang] = useState<"zh" | "other">(initial?.source_lang ?? "zh");
  // Nhiều file (sách chụp nhiều ảnh, 1 ảnh/trang) — gộp theo đúng thứ tự chọn/kéo-thả của
  // input[multiple] (trình duyệt giữ nguyên thứ tự này, không tự sắp xếp lại theo tên file).
  const [documentFiles, setDocumentFiles] = useState<File[]>([]);
  const [ocrLang, setOcrLang] = useState<"vi" | "en" | "fr">("vi");
  // Ngôn ngữ đọc (target_lang) — độc lập với ocrLang (ngôn ngữ nhận dạng nguồn): sách nguồn
  // tiếng Việt vẫn đọc được ra tiếng Anh, và ngược lại.
  const [bookTargetLang, setBookTargetLang] = useState<"tiếng Việt" | "tiếng Anh">("tiếng Việt");
  const [voices, setVoices] = useState<Voice[]>([]);
  const [styles, setStyles] = useState<Style[]>([]);
  const [voice, setVoice] = useState(initial?.voice ?? "");
  const [style, setStyle] = useState(initial?.style ?? "tu_nhien");
  const [refAudioFile, setRefAudioFile] = useState<File | null>(null);
  const [musicProjects, setMusicProjects] = useState<MusicProject[]>([]);
  const [musicChoice, setMusicChoice] = useState(NO_MUSIC_VALUE);
  const [musicFile, setMusicFile] = useState<File | null>(null);
  const [musicTracks, setMusicTracks] = useState<MusicTrack[]>([]);
  const [musicTrack, setMusicTrack] = useState("");
  const [subtitleMode, setSubtitleMode] = useState(initial?.subtitle_mode ?? "burn");
  const [subStyle, setSubStyle] = useState<SubStyle>(DEFAULT_SUB_STYLE);
  const [subStylePreviewUrl, setSubStylePreviewUrl] = useState<string | null>(null);
  // Style đã "Lưu làm mặc định" (con trỏ đơn, xem getDefaultSubStyle) — có thì tiền điền form
  // thay vì DEFAULT_SUB_STYLE hardcode. hasSavedStyleDefault quyết định có hiện nút "Khôi phục
  // mặc định gốc" hay không (chỉ có ý nghĩa khi đã từng lưu 1 default tuỳ chỉnh).
  const [hasSavedStyleDefault, setHasSavedStyleDefault] = useState(false);
  const [styleSaveStatus, setStyleSaveStatus] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Nhạc nền mặc định: chưa ai đụng vào dropdown thì tự chọn sẵn track đã được tick "mặc định"
  // trong thư viện (xem MusicLibrary.tsx/music_library.get_default); chưa ai tick gì thì rơi về
  // heuristic cũ — chủ đề/track ĐẦU TIÊN trong thư viện thay vì "Không dùng nhạc nền" (khớp
  // luồng thật, nhánh review hầu như luôn có nhạc nền). Người dùng vẫn đổi tay bình thường qua
  // đúng dropdown cũ, chỉ đổi giá trị khởi tạo. Set true ngay khi người dùng tự bấm đổi (kể cả
  // chọn lại đúng giá trị mặc định).
  const [musicTouched, setMusicTouched] = useState(false);
  const [libraryDefault, setLibraryDefault] = useState<MusicDefault | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      const [voicesRes, stylesRes, musicRes, defaultRes, subStyleDefaultRes] = await Promise.all([
        getVoices(), getStyles(), getMusicProjects(), getDefaultMusic(), getDefaultSubStyle(),
      ]);
      if (cancelled) return;
      if (voicesRes.ok) setVoices(voicesRes.voices);
      else setLoadError(voicesRes.error || "Không tải được danh sách voice");
      if (stylesRes.ok) setStyles(stylesRes.styles);
      const libDefault = defaultRes.ok ? defaultRes.default : null;
      setLibraryDefault(libDefault);
      if (subStyleDefaultRes.ok && subStyleDefaultRes.default) {
        setSubStyle(subStyleDefaultRes.default);
        setHasSavedStyleDefault(true);
      }
      if (musicRes.ok) {
        setMusicProjects(musicRes.projects);
        // Chỉ auto-chọn chủ đề CÓ track (bỏ qua chủ đề rỗng, vd "Nhạc anime (0)") — chọn
        // nhầm chủ đề 0 track sẽ chặn submit (bắt chọn track nhưng không track nào để chọn).
        const firstWithTracks = musicRes.projects.find((p) => p.track_count > 0);
        const preferredSlug = libDefault?.project ?? firstWithTracks?.slug;
        if (!musicTouched && !musicChoice && preferredSlug) {
          setMusicChoice(preferredSlug);
        }
      }
    }
    load().catch((err) => !cancelled && setLoadError(String(err)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const isProject = musicChoice && musicChoice !== CUSTOM_MUSIC_VALUE;
    setMusicTrack("");
    if (!isProject) {
      setMusicTracks([]);
      return;
    }
    let cancelled = false;
    getMusicTracks(musicChoice).then((res) => {
      if (cancelled || !res.ok) return;
      setMusicTracks(res.tracks);
      if (!musicTouched && res.tracks.length > 0) {
        const preferred = libraryDefault?.project === musicChoice ? libraryDefault.track : res.tracks[0].track;
        setMusicTrack(preferred);
      }
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [musicChoice, libraryDefault]);

  // Demo ảnh phụ đề — debounce 400ms để không spam request khi kéo slider/đổi màu liên tục.
  // Chạy cả lúc mount (subtitleMode mặc định "burn") nên panel có sẵn ảnh demo ngay khi mở.
  useEffect(() => {
    if (subtitleMode !== "burn") return;
    const timer = setTimeout(() => {
      previewSubtitleStyle(subStyle).then((blob) => {
        setSubStylePreviewUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return URL.createObjectURL(blob);
        });
      });
    }, 400);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subStyle, subtitleMode]);

  async function handleSaveStyleDefault() {
    setStyleSaveStatus(null);
    const res = await setDefaultSubStyle(subStyle);
    setStyleSaveStatus(res.ok ? "Đã lưu làm mặc định." : res.error || "Lưu thất bại.");
    if (res.ok) setHasSavedStyleDefault(true);
  }

  async function handleResetStyleDefault() {
    setStyleSaveStatus(null);
    const res = await clearDefaultSubStyle();
    if (res.ok) {
      setHasSavedStyleDefault(false);
      setSubStyle(DEFAULT_SUB_STYLE);
      setStyleSaveStatus("Đã khôi phục mặc định gốc.");
    } else {
      setStyleSaveStatus(res.error || "Khôi phục thất bại.");
    }
  }

  function handleMusicChoiceChange(v: string) {
    setMusicTouched(true);
    setMusicChoice(v);
  }

  function handleUrlKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== "Enter") return;
    e.preventDefault();
    setUrl((prev) => normalizeVideoUrl(prev));
  }


  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (isBook) {
        if (documentFiles.length === 0) {
          setError("Chọn ít nhất 1 file pdf/docx/pptx/xlsx/ảnh để trích văn bản");
          return;
        }
        const documents = await Promise.all(
          documentFiles.map(async (f) => ({
            data_b64: await fileToBase64(f),
            ext: f.name.split(".").pop()?.toLowerCase() || "",
          })),
        );
        const res = await submitPipeline({
          mode: "book",
          video_name: videoName.trim() || undefined,
          documents,
          ocr_lang: ocrLang,
          target_lang: bookTargetLang,
          voice: voice || undefined,
          style,
        });
        if (!res.ok || !res.pipeline_id) {
          setError(res.error || "Không tạo được job");
          return;
        }
        window.location.hash = `#/job/${res.pipeline_id}`;
        return;
      }
      const isClone = mode !== "subtitle" && !isDownloadOnly && voice === CLONE_VALUE;
      if (isClone && !refAudioFile) {
        setError("Chọn file audio mẫu (3-5s, WAV) để clone giọng");
        return;
      }
      const isCustomMusic = mode === "review" && musicChoice === CUSTOM_MUSIC_VALUE;
      if (isCustomMusic && !musicFile) {
        setError("Chọn file nhạc nền để upload");
        return;
      }
      const isProjectMusic = mode === "review" && musicChoice && !isCustomMusic;
      if (isProjectMusic && !musicTrack) {
        setError("Chọn 1 track trong chủ đề nhạc nền");
        return;
      }
      const normalizedUrl = normalizeVideoUrl(url);
      const res = await submitPipeline({
        url: normalizedUrl,
        video_name: videoName.trim() || undefined,
        mode,
        source_lang: sourceLang,
        voice: isClone || !voice ? undefined : voice,
        style,
        ref_audio_b64: isClone && refAudioFile ? await fileToBase64(refAudioFile) : undefined,
        ref_audio_ext: isClone ? "wav" : undefined,
        subtitle_mode: subtitleMode,
        sub_style: subtitleMode === "burn" ? subStyle : undefined,
        music_project: isProjectMusic ? musicChoice : undefined,
        music_track: isProjectMusic ? musicTrack : undefined,
        music_b64: isCustomMusic && musicFile ? await fileToBase64(musicFile) : undefined,
        music_ext: isCustomMusic && musicFile ? (musicFile.name.split(".").pop() || "mp3") : undefined,
      });
      if (!res.ok || !res.pipeline_id) {
        setError(res.error || "Không tạo được job");
        return;
      }
      window.location.hash = `#/job/${res.pipeline_id}`;
    } catch (err) {
      setError(String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="card" onSubmit={handleSubmit}>
      <h2>Tạo pipeline mới</h2>
      {initial && (
        <p className="stage-detail">
          Đã điền lại thông tin từ job lỗi trước đó — kiểm tra rồi bấm chạy lại.
        </p>
      )}
      <p className="stage-detail">
        Có nhiều video cùng lúc? <a href="#/import">Import cả danh sách qua CSV →</a>
      </p>
      {!isBook && (
        <label>
          <span className="field-label-row">
            URL video
            <FieldInfo text={'Dán link kiểu "jingxuan?modal_id=..." cũng được — tự quy đổi về dạng /video/<id> khi bạn nhấn Enter hoặc rời khỏi ô.'} />
          </span>
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={handleUrlKeyDown}
            onBlur={() => setUrl((prev) => normalizeVideoUrl(prev))}
            required
            placeholder="https://www.douyin.com/video/..."
          />
        </label>
      )}
      {isBook && (
        <label>
          <span className="field-label-row">
            File sách (pdf/docx/pptx/xlsx/ảnh — chọn nhiều ảnh nếu chụp mỗi trang 1 ảnh)
            <FieldInfo text="PDF có text layer (không phải scan) được trích thẳng, không tốn GPU. Trang scan/ảnh sẽ tự OCR. Chọn nhiều file thì gộp theo đúng thứ tự đã chọn (kéo-thả để sắp lại thứ tự trước khi chọn nếu trình duyệt hỗ trợ)." />
          </span>
          <input
            type="file"
            multiple
            accept=".pdf,.docx,.pptx,.xlsx,image/*"
            onChange={(e) => setDocumentFiles(Array.from(e.target.files ?? []))}
            required
          />
          {documentFiles.length > 1 && (
            <ol className="stage-detail" style={{ marginTop: "0.25rem" }}>
              {documentFiles.map((f, i) => (
                <li key={i}>{f.name}</li>
              ))}
            </ol>
          )}
        </label>
      )}
      <label>
        <span className="field-label-row">
          Tên video (tuỳ chọn)
          <FieldInfo text={"Dùng làm tên chuẩn xuyên suốt pipeline — đặt tên file trung gian và file xuất cuối (<tên>.mp4/.srt). Bỏ trống thì dùng ID job."} />
        </span>
        <input
          value={videoName}
          onChange={(e) => setVideoName(e.target.value)}
          placeholder="vd: meo_con_hai_huoc_tap1"
        />
      </label>
      <label>
        <span className="field-label-row">
          Nhánh
          <FieldInfo
            text={
              "review: mute audio gốc, 1 giọng thuyết minh. dialogue: giữ nền gốc, chỉ mute tiếng Trung. " +
              "subtitle: chỉ thêm phụ đề, giữ nguyên audio gốc. audio: chỉ tải mp3, không xử lý gì thêm. " +
              "video: chỉ tải video gốc, không xử lý gì thêm. book: Sách → Audio, tự động nhiều giọng theo nhân vật."
            }
          />
        </span>
        <div className="radio-group">
          <label>
            <input type="radio" name="mode" checked={mode === "review"} onChange={() => setMode("review")} />
            review
          </label>
          <label>
            <input type="radio" name="mode" checked={mode === "dialogue"} onChange={() => setMode("dialogue")} />
            dialogue
          </label>
          <label>
            <input type="radio" name="mode" checked={mode === "subtitle"} onChange={() => setMode("subtitle")} />
            subtitle
          </label>
          <label>
            <input type="radio" name="mode" checked={mode === "audio"} onChange={() => setMode("audio")} />
            audio
          </label>
          <label>
            <input type="radio" name="mode" checked={mode === "video"} onChange={() => setMode("video")} />
            video
          </label>
          <label>
            <input type="radio" name="mode" checked={mode === "book"} onChange={() => setMode("book")} />
            book
          </label>
        </div>
      </label>
      {isBook && (
        <label>
          Ngôn ngữ văn bản (OCR)
          <div className="radio-group">
            <label>
              <input type="radio" name="ocr_lang" checked={ocrLang === "vi"} onChange={() => setOcrLang("vi")} />
              Tiếng Việt
            </label>
            <label>
              <input type="radio" name="ocr_lang" checked={ocrLang === "en"} onChange={() => setOcrLang("en")} />
              Tiếng Anh
            </label>
            <label>
              <input type="radio" name="ocr_lang" checked={ocrLang === "fr"} onChange={() => setOcrLang("fr")} />
              Tiếng Pháp
            </label>
          </div>
        </label>
      )}
      {isBook && (
        <label>
          Ngôn ngữ đọc (output)
          <div className="radio-group">
            <label>
              <input
                type="radio"
                name="book_target_lang"
                checked={bookTargetLang === "tiếng Việt"}
                onChange={() => setBookTargetLang("tiếng Việt")}
              />
              Tiếng Việt
            </label>
            <label>
              <input
                type="radio"
                name="book_target_lang"
                checked={bookTargetLang === "tiếng Anh"}
                onChange={() => setBookTargetLang("tiếng Anh")}
              />
              Tiếng Anh
            </label>
          </div>
        </label>
      )}
      {isBook && (
        <p className="stage-detail">
          Người dẫn chuyện dùng giọng bạn chọn ở dưới (Voice) — mỗi nhân vật khác trong sách tự
          động được gán 1 giọng riêng từ danh sách có sẵn.
        </p>
      )}
      {!isDownloadOnly && !isBook && (
        <label>
          <span className="field-label-row">
            Ngôn ngữ gốc video
            <FieldInfo text="Quyết định engine nhận diện giọng: Tiếng Trung dùng Paraformer, ngôn ngữ khác dùng whisper (tự nhận diện ngôn ngữ cụ thể ngay lúc xử lý)." />
          </span>
          <div className="radio-group">
            <label>
              <input
                type="radio"
                name="source_lang"
                checked={sourceLang === "zh"}
                onChange={() => setSourceLang("zh")}
              />
              Tiếng Trung
            </label>
            <label>
              <input
                type="radio"
                name="source_lang"
                checked={sourceLang === "other"}
                onChange={() => setSourceLang("other")}
              />
              Ngôn ngữ khác
            </label>
          </div>
        </label>
      )}
      {loadError && <p className="error">{loadError}</p>}
      {mode === "subtitle" && (
        <p className="stage-detail">
          Nhánh subtitle không dùng TTS/giọng đọc — audio gốc được giữ nguyên, chỉ thêm phụ đề.
        </p>
      )}
      {mode === "audio" && (
        <p className="stage-detail">
          Nhánh audio chỉ tải video rồi xuất mp3 — không transcribe/dịch/TTS/mux, dừng ngay sau
          yt-dlp.
        </p>
      )}
      {mode === "video" && (
        <p className="stage-detail">
          Nhánh video chỉ tải nguyên file video gốc (giữ đúng đuôi file yt-dlp tải về) — không
          transcribe/dịch/TTS/mux, dừng ngay sau yt-dlp.
        </p>
      )}
      {isBook && (
        <label>
          Voice người dẫn chuyện ({voices.length} giọng có sẵn)
          <Dropdown
            value={voice}
            onChange={setVoice}
            options={[
              { value: "", label: "Mặc định" },
              ...voices.map((v) => ({ value: v.id, label: v.label })),
            ]}
          />
          {voice && (
            <div className="audio-preview">
              <span className="stage-detail">Nghe thử giọng đã chọn:</span>
              <audio controls preload="none" src={voiceSampleUrl(voice)} />
            </div>
          )}
        </label>
      )}
      {isBook && (
        <label>
          Style giọng đọc
          <Dropdown value={style} onChange={setStyle} options={styles.map((s) => ({ value: s.id, label: s.label }))} />
        </label>
      )}
      {!isDownloadOnly && !isBook && (
      <>
        {mode !== "subtitle" && (
          <label>
            Voice ({voices.length} giọng có sẵn)
            <Dropdown
              value={voice}
              onChange={setVoice}
              options={[
                { value: "", label: "Mặc định" },
                ...voices.map((v) => ({ value: v.id, label: v.label })),
                { value: CLONE_VALUE, label: "Khác — Clone giọng (upload mẫu 3-5s)" },
              ]}
            />
            {voice && voice !== CLONE_VALUE && (
              <div className="audio-preview">
                <span className="stage-detail">Nghe thử giọng đã chọn:</span>
                <audio controls preload="none" src={voiceSampleUrl(voice)} />
              </div>
            )}
          </label>
        )}
        <div className="row">
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
        {subtitleMode === "burn" && (
          <div className="sub-style-panel">
            <div className="sub-style-preview">
              {subStylePreviewUrl ? (
                <img src={subStylePreviewUrl} alt="Demo style phụ đề" />
              ) : (
                <span className="stage-detail">Đang tải demo...</span>
              )}
            </div>
            <details className="sub-style-details">
              <summary>Tuỳ chỉnh style phụ đề</summary>
            <div className="sub-style-fields">
              <label>
                <span>Chữ đậm</span>
                <input
                  type="checkbox"
                  checked={subStyle.bold}
                  onChange={(e) => setSubStyle({ ...subStyle, bold: e.target.checked })}
                />
              </label>
              <label>
                <span>Màu chữ</span>
                <input
                  type="color"
                  value={subStyle.text_color}
                  onChange={(e) => setSubStyle({ ...subStyle, text_color: e.target.value })}
                />
              </label>
              <label>
                <span>Màu viền</span>
                <input
                  type="color"
                  value={subStyle.outline_color}
                  onChange={(e) => setSubStyle({ ...subStyle, outline_color: e.target.value })}
                />
              </label>
              <label>
                <span>Độ dày viền ({subStyle.outline_width}px)</span>
                <input
                  type="range"
                  min={0}
                  max={4}
                  step={1}
                  value={subStyle.outline_width}
                  onChange={(e) => setSubStyle({ ...subStyle, outline_width: Number(e.target.value) })}
                />
              </label>
              <label>
                <span>Nền chữ</span>
                <input
                  type="checkbox"
                  checked={subStyle.background_enabled}
                  onChange={(e) => setSubStyle({ ...subStyle, background_enabled: e.target.checked })}
                />
              </label>
              {subStyle.background_enabled && (
                <>
                  <label>
                    <span>Màu nền</span>
                    <input
                      type="color"
                      value={subStyle.background_color}
                      onChange={(e) => setSubStyle({ ...subStyle, background_color: e.target.value })}
                    />
                  </label>
                  <label>
                    <span>Độ đục nền ({subStyle.background_opacity}%)</span>
                    <input
                      type="range"
                      min={0}
                      max={100}
                      step={5}
                      value={subStyle.background_opacity}
                      onChange={(e) => setSubStyle({ ...subStyle, background_opacity: Number(e.target.value) })}
                    />
                  </label>
                </>
              )}
            </div>
            <div className="sub-style-actions">
              <button type="button" className="btn-secondary" onClick={handleSaveStyleDefault}>
                Lưu làm mặc định
              </button>
              {hasSavedStyleDefault && (
                <button type="button" className="btn-secondary" onClick={handleResetStyleDefault}>
                  Khôi phục mặc định gốc
                </button>
              )}
              {styleSaveStatus && <span className="stage-detail">{styleSaveStatus}</span>}
            </div>
            </details>
          </div>
        )}
      </>
      )}
      {mode !== "subtitle" && !isDownloadOnly && voice === CLONE_VALUE && (
        <label>
          File audio mẫu (WAV, 3-5 giây)
          <input
            type="file"
            accept=".wav,audio/wav"
            onChange={(e) => setRefAudioFile(e.target.files?.[0] ?? null)}
          />
        </label>
      )}
      {mode === "review" && (
        <label>
          Nhạc nền (tuỳ chọn — {musicProjects.length} chủ đề có sẵn,{" "}
          <a href="#/music">quản lý thư viện nhạc →</a>)
          <Dropdown
            value={musicChoice}
            onChange={handleMusicChoiceChange}
            options={[
              { value: NO_MUSIC_VALUE, label: "Không dùng nhạc nền" },
              ...musicProjects.map((p) => ({ value: p.slug, label: `${p.display_name} (${p.track_count})` })),
              { value: CUSTOM_MUSIC_VALUE, label: "Khác — tự upload file nhạc" },
            ]}
          />
        </label>
      )}
      {mode === "review" && musicChoice && musicChoice !== CUSTOM_MUSIC_VALUE && (
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
              <audio controls preload="none" src={musicTrackRawUrl(musicChoice, musicTrack)} />
            </div>
          )}
        </label>
      )}
      {mode === "review" && musicChoice === CUSTOM_MUSIC_VALUE && (
        <label>
          File nhạc nền
          <input
            type="file"
            accept="audio/*"
            onChange={(e) => setMusicFile(e.target.files?.[0] ?? null)}
          />
        </label>
      )}
      {error && <p className="error">{error}</p>}
      <button type="submit" disabled={submitting}>
        {submitting ? "Đang gửi..." : "Chạy pipeline"}
      </button>
    </form>
  );
}
