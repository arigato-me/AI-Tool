import { useEffect, useState } from "react";

export interface LightboxImage {
  url: string;
  name: string;
}

/** Xem ảnh full-screen kiểu Messenger — overlay tối, mũi tên trái/phải lướt giữa nhiều ảnh,
 * phím ArrowLeft/ArrowRight/Escape, bấm ra ngoài để đóng. */
export default function ImageLightbox({
  images,
  startIndex,
  onClose,
}: {
  images: LightboxImage[];
  startIndex: number;
  onClose: () => void;
}) {
  const [index, setIndex] = useState(startIndex);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowLeft") setIndex((i) => (i - 1 + images.length) % images.length);
      else if (e.key === "ArrowRight") setIndex((i) => (i + 1) % images.length);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [images.length, onClose]);

  const current = images[index];
  if (!current) return null;

  return (
    <div
      onClick={onClose}
      style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.85)", zIndex: 1000,
        display: "flex", alignItems: "center", justifyContent: "center",
      }}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label="Đóng"
        style={{
          position: "absolute", top: "1rem", right: "1.5rem", background: "none", border: "none",
          color: "#fff", fontSize: "2rem", cursor: "pointer", lineHeight: 1,
        }}
      >
        &times;
      </button>
      {images.length > 1 && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setIndex((i) => (i - 1 + images.length) % images.length);
          }}
          aria-label="Ảnh trước"
          style={{
            position: "absolute", left: "1rem", top: "50%", transform: "translateY(-50%)",
            background: "none", border: "none", color: "#fff", fontSize: "2.5rem", cursor: "pointer",
          }}
        >
          &#8249;
        </button>
      )}
      <img
        src={current.url}
        alt={current.name}
        onClick={(e) => e.stopPropagation()}
        style={{ maxWidth: "90vw", maxHeight: "90vh", objectFit: "contain" }}
      />
      {images.length > 1 && (
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            setIndex((i) => (i + 1) % images.length);
          }}
          aria-label="Ảnh sau"
          style={{
            position: "absolute", right: "1rem", top: "50%", transform: "translateY(-50%)",
            background: "none", border: "none", color: "#fff", fontSize: "2.5rem", cursor: "pointer",
          }}
        >
          &#8250;
        </button>
      )}
      {images.length > 1 && (
        <div style={{ position: "absolute", bottom: "1rem", color: "#fff", fontSize: "0.9rem" }}>
          {index + 1} / {images.length}
        </div>
      )}
    </div>
  );
}
