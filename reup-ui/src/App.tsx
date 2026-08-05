import { useEffect, useState } from "react";
import SubmitJob from "./pages/SubmitJob";
import Jobs from "./pages/Jobs";
import JobDetail from "./pages/JobDetail";
import Monitor from "./pages/Monitor";
import ImportCsv from "./pages/ImportCsv";
import MusicLibrary from "./pages/MusicLibrary";

export interface RetryPayload {
  url?: string;
  video_name?: string;
  mode?: "review" | "dialogue" | "subtitle" | "audio" | "video";
  source_lang?: "zh" | "other";
  voice?: string;
  style?: string;
  subtitle_mode?: string;
}

type Route =
  | { name: "submit"; retry?: RetryPayload }
  | { name: "import" }
  | { name: "jobs" }
  | { name: "monitor" }
  | { name: "music" }
  | { name: "detail"; id: string };

function parseHash(): Route {
  const hash = window.location.hash.replace(/^#\/?/, "");
  if (hash.startsWith("job/")) return { name: "detail", id: hash.slice(4) };
  if (hash === "jobs") return { name: "jobs" };
  if (hash === "monitor") return { name: "monitor" };
  if (hash === "import") return { name: "import" };
  if (hash === "music") return { name: "music" };
  if (hash.startsWith("submit")) {
    const qIndex = hash.indexOf("?");
    if (qIndex === -1) return { name: "submit" };
    const raw = new URLSearchParams(hash.slice(qIndex + 1)).get("retry");
    if (!raw) return { name: "submit" };
    try {
      return { name: "submit", retry: JSON.parse(raw) as RetryPayload };
    } catch {
      return { name: "submit" };
    }
  }
  return { name: "submit" };
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash());
  const [menuOpen, setMenuOpen] = useState(false);

  useEffect(() => {
    const onHashChange = () => {
      setRoute(parseHash());
      setMenuOpen(false);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <div className="app">
      <header className="nav">
        <h1 className="nav-brand">reup pipeline</h1>
        <button
          type="button"
          className="nav-toggle"
          aria-label={menuOpen ? "Đóng menu" : "Mở menu"}
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((v) => !v)}
        >
          {menuOpen ? (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          ) : (
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
              <path d="M4 7h16M4 12h16M4 17h16" />
            </svg>
          )}
        </button>
        <nav className="nav-links" data-open={menuOpen} onClick={() => setMenuOpen(false)}>
          <div className="nav-group">
            <a href="#/submit" className={route.name === "submit" ? "active" : ""}>
              Tạo job
            </a>
            <a href="#/import" className={route.name === "import" ? "active" : ""}>
              Tạo nhiều job
            </a>
          </div>
          <div className="nav-group">
            <a href="#/jobs" className={route.name === "jobs" ? "active" : ""}>
              Danh sách job
            </a>
            <a href="#/monitor" className={route.name === "monitor" ? "active" : ""}>
              Monitor
            </a>
          </div>
          <div className="nav-group">
            <a href="#/music" className={route.name === "music" ? "active" : ""}>
              Nhạc nền
            </a>
          </div>
        </nav>
      </header>
      <main>
        {route.name === "submit" && <SubmitJob key={JSON.stringify(route.retry ?? {})} initial={route.retry} />}
        {route.name === "import" && <ImportCsv />}
        {route.name === "jobs" && <Jobs />}
        {route.name === "monitor" && <Monitor />}
        {route.name === "music" && <MusicLibrary />}
        {route.name === "detail" && <JobDetail pipelineId={route.id} />}
      </main>
    </div>
  );
}
