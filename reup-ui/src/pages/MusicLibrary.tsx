import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import {
  clearDefaultMusic,
  createMusicProject,
  deleteMusicProject,
  deleteMusicTrack,
  getDefaultMusic,
  getMusicProjects,
  getMusicTracks,
  musicTrackRawUrl,
  setDefaultMusic,
  uploadMusicTrack,
  MusicDefault,
  MusicProject,
  MusicTrack,
} from "../api";
import { fileToBase64 } from "../lib/fileToBase64";
import TrashIcon from "../components/TrashIcon";

const UNGROUPED_SLUG = "_ungrouped"; // khớp music_library.UNGROUPED_SLUG (không import chéo)

interface UploadRow {
  name: string;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

export default function MusicLibrary() {
  const [projects, setProjects] = useState<MusicProject[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [tracks, setTracks] = useState<MusicTrack[]>([]);
  const [tracksError, setTracksError] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [uploadRows, setUploadRows] = useState<UploadRow[]>([]);
  const [deletingTrack, setDeletingTrack] = useState<string | null>(null);
  const [deletingProject, setDeletingProject] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // Track nhạc nền mặc định cho nhánh review — con trỏ đơn toàn thư viện (không phải mỗi
  // project 1 default), xem music_library.get_default. null = chưa ai tick default.
  const [defaultMusic, setDefaultMusicState] = useState<MusicDefault | null>(null);
  const [togglingDefault, setTogglingDefault] = useState<string | null>(null);

  async function reloadProjects() {
    const res = await getMusicProjects();
    if (res.ok) setProjects(res.projects);
    else setLoadError(res.error || "Không tải được danh sách project");
  }

  async function reloadDefault() {
    const res = await getDefaultMusic();
    if (res.ok) setDefaultMusicState(res.default);
  }

  useEffect(() => {
    reloadProjects().catch((err) => setLoadError(String(err)));
    reloadDefault().catch(() => undefined);
  }, []);

  async function handleToggleDefault(track: string) {
    if (!selected) return;
    const isCurrentDefault = defaultMusic?.project === selected && defaultMusic?.track === track;
    setTogglingDefault(track);
    setActionError(null);
    try {
      const res = isCurrentDefault ? await clearDefaultMusic() : await setDefaultMusic(selected, track);
      if (!res.ok) {
        setActionError(res.error || "Không đặt được nhạc nền mặc định");
        return;
      }
      await reloadDefault();
    } catch (err) {
      setActionError(String(err));
    } finally {
      setTogglingDefault(null);
    }
  }

  async function reloadTracks(slug: string) {
    const res = await getMusicTracks(slug);
    if (res.ok) {
      setTracks(res.tracks);
      setTracksError(null);
    } else {
      setTracksError(res.error || "Không tải được danh sách track");
    }
  }

  function selectProject(slug: string) {
    setSelected(slug);
    reloadTracks(slug).catch((err) => setTracksError(String(err)));
  }

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      const res = await createMusicProject(newName.trim());
      if (!res.ok) {
        setCreateError(res.error || "Không tạo được project");
        return;
      }
      setNewName("");
      await reloadProjects();
      if (res.project) selectProject(res.project.slug);
    } catch (err) {
      setCreateError(String(err));
    } finally {
      setCreating(false);
    }
  }

  async function handleUpload(e: ChangeEvent<HTMLInputElement>) {
    if (!selected) return;
    const files = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (files.length === 0) return;

    setUploadRows(files.map((f) => ({ name: f.name, status: "pending" })));
    for (let i = 0; i < files.length; i++) {
      setUploadRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, status: "uploading" } : r)));
      try {
        const b64 = await fileToBase64(files[i]);
        const res = await uploadMusicTrack(selected, files[i].name, b64);
        setUploadRows((prev) =>
          prev.map((r, idx) =>
            idx === i
              ? res.ok
                ? { ...r, status: "done" }
                : { ...r, status: "error", error: res.error || "lỗi upload" }
              : r,
          ),
        );
      } catch (err) {
        setUploadRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, status: "error", error: String(err) } : r)));
      }
    }
    await reloadTracks(selected);
    await reloadProjects();
  }

  async function handleDeleteTrack(track: string) {
    if (!selected) return;
    if (!window.confirm(`Xoá track "${track}"? Không phục hồi được.`)) return;
    setDeletingTrack(track);
    setActionError(null);
    try {
      const res = await deleteMusicTrack(selected, track);
      if (!res.ok) {
        setActionError(res.error || "Không xoá được track");
        return;
      }
      await reloadTracks(selected);
      await reloadProjects();
      // Xoá đúng track đang là default -> editor-node tự "quên" con trỏ (get_default() resolve
      // lại, không còn tồn tại thì trả None) — load lại để UI hết hiện tick sao ở nơi khác.
      await reloadDefault();
    } catch (err) {
      setActionError(String(err));
    } finally {
      setDeletingTrack(null);
    }
  }

  async function handleDeleteProject(slug: string, displayName: string) {
    if (!window.confirm(`Xoá cả chủ đề "${displayName}" cùng toàn bộ track bên trong? Không phục hồi được.`)) return;
    setDeletingProject(true);
    setActionError(null);
    try {
      const res = await deleteMusicProject(slug);
      if (!res.ok) {
        setActionError(res.error || "Không xoá được chủ đề");
        return;
      }
      if (selected === slug) {
        setSelected(null);
        setTracks([]);
      }
      await reloadProjects();
      await reloadDefault();
    } catch (err) {
      setActionError(String(err));
    } finally {
      setDeletingProject(false);
    }
  }

  return (
    <div className="split-panel">
      <div className="card">
        <h2>Chủ đề nhạc nền</h2>
        {loadError && <p className="error">{loadError}</p>}
        {projects.length === 0 && !loadError && <p className="stage-detail">Chưa có chủ đề nào.</p>}
        {actionError && <p className="error">{actionError}</p>}
        <ul className="list-plain">
          {projects.map((p) => (
            <li key={p.slug} className="list-row">
              <button
                type="button"
                className={`list-item-btn ${p.slug === selected ? "btn-secondary" : ""}`}
                onClick={() => selectProject(p.slug)}
              >
                {p.display_name} <span className="stage-detail">({p.track_count})</span>
              </button>
              {p.slug !== UNGROUPED_SLUG && (
                <button
                  type="button"
                  className="btn-icon-danger"
                  disabled={deletingProject}
                  onClick={() => handleDeleteProject(p.slug, p.display_name)}
                  title="Xoá chủ đề (kèm mọi track bên trong)"
                >
                  <TrashIcon />
                </button>
              )}
            </li>
          ))}
        </ul>
        <form onSubmit={handleCreate}>
          <label>
            Tạo chủ đề mới
            <input
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="vd: Nhạc buồn"
              required
            />
          </label>
          {createError && <p className="error">{createError}</p>}
          <button type="submit" disabled={creating}>
            {creating ? "Đang tạo..." : "Tạo chủ đề"}
          </button>
        </form>
      </div>

      <div className="card">
        <h2>Track</h2>
        {!selected && <p className="stage-detail">Chọn 1 chủ đề bên trái để xem/thêm track.</p>}
        {selected && (
          <>
            <label>
              Upload track (mp3/wav/m4a/aac/ogg/flac, có thể chọn nhiều file)
              <input type="file" accept="audio/*" multiple onChange={handleUpload} />
            </label>
            {uploadRows.length > 0 && (
              <ul>
                {uploadRows.map((r, idx) => (
                  <li key={idx}>
                    {r.name}:{" "}
                    {r.status === "done" ? (
                      <span className="status status-finished">xong</span>
                    ) : r.status === "error" ? (
                      <span className="status status-failed">{r.error}</span>
                    ) : r.status === "uploading" ? (
                      <span className="status status-started">đang tải...</span>
                    ) : (
                      <span className="stage-detail">chờ</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {tracksError && <p className="error">{tracksError}</p>}
            {tracks.length === 0 && !tracksError && <p className="stage-detail">Chưa có track nào.</p>}
            <ul className="list-plain">
              {tracks.map((t) => {
                const isDefault = defaultMusic?.project === selected && defaultMusic?.track === t.track;
                return (
                  <li key={t.track} className="track-row">
                    <div className="track-row-head">
                      <span>{t.display_name}</span>
                      <button
                        type="button"
                        className={`btn-icon-star ${isDefault ? "is-default" : ""}`}
                        disabled={togglingDefault === t.track}
                        onClick={() => handleToggleDefault(t.track)}
                        title={isDefault ? "Đang là nhạc nền mặc định (bấm để bỏ)" : "Đặt làm nhạc nền mặc định cho nhánh review"}
                      >
                        {isDefault ? "★" : "☆"} Mặc định
                      </button>
                      <button
                        type="button"
                        className="btn-icon-danger"
                        disabled={deletingTrack === t.track}
                        onClick={() => handleDeleteTrack(t.track)}
                        title="Xoá track"
                      >
                        <TrashIcon />
                      </button>
                    </div>
                    <audio controls preload="none" src={musicTrackRawUrl(selected, t.track)} style={{ width: "100%" }} />
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
