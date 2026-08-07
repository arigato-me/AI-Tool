import { KeyboardEvent, useEffect, useRef, useState } from "react";

/** Thay `<select>` gốc: option list do OS/browser tự vẽ, không theo được token màu app (bug
 * nền trắng/chữ xám khi đổi theme) — tự vẽ popup thì kiểm soát 100% màu, hết bug tận gốc. */
export interface DropdownOption {
  value: string;
  label: string;
}

interface DropdownProps {
  value: string;
  onChange: (value: string) => void;
  options: DropdownOption[];
  className?: string;
  /** Tên đọc cho screen reader khi không có <label> văn bản hiện trước field (vd 1 dòng lặp
   * trong danh sách — hiện label chữ mỗi dòng gây rối mắt, nhưng vẫn cần tên cho AT). */
  ariaLabel?: string;
}

const LIST_MAX_HEIGHT = 256; // == CSS 16rem, giữ 2 nơi khớp nhau

export default function Dropdown({ value, onChange, options, className, ariaLabel }: DropdownProps) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const [openUp, setOpenUp] = useState(false);
  const [listMaxHeight, setListMaxHeight] = useState(LIST_MAX_HEIGHT);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const selected = options.find((o) => o.value === value) ?? options[0];

  /** Đo chỗ trống trên/dưới NGAY LÚC mở (không phải trong effect chạy sau render) để popup
   * hiện ra đã đúng hướng ngay từ khung hình đầu — tránh nháy "mở xuống rồi tự lật lên". Field
   * càng gần đáy màn hình (vd Nhạc nền — field cuối form) càng dễ thiếu chỗ dưới. */
  function openDropdown() {
    const idx = options.findIndex((o) => o.value === value);
    setHighlight(idx >= 0 ? idx : 0);
    const rect = rootRef.current?.getBoundingClientRect();
    if (rect) {
      const gap = 8;
      const spaceBelow = window.innerHeight - rect.bottom - gap;
      const spaceAbove = rect.top - gap;
      const up = spaceBelow < LIST_MAX_HEIGHT && spaceAbove > spaceBelow;
      setOpenUp(up);
      setListMaxHeight(Math.max(120, Math.min(LIST_MAX_HEIGHT, up ? spaceAbove : spaceBelow)));
    }
    setOpen(true);
  }

  useEffect(() => {
    if (!open) return;
    function onDocMouseDown(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    listRef.current?.querySelector<HTMLElement>('[data-highlighted="true"]')?.scrollIntoView({ block: "nearest" });
  }, [open, highlight]);

  function handleKeyDown(e: KeyboardEvent<HTMLButtonElement>) {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openDropdown();
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(h + 1, options.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const opt = options[highlight];
      if (opt) {
        onChange(opt.value);
        setOpen(false);
      }
    }
  }

  return (
    <div className={`dropdown${className ? ` ${className}` : ""}`} ref={rootRef}>
      <button
        type="button"
        className="dropdown-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? setOpen(false) : openDropdown())}
        onKeyDown={handleKeyDown}
      >
        <span className="dropdown-value">{selected?.label ?? ""}</span>
        <svg className="dropdown-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && (
        <ul
          className={`dropdown-list${openUp ? " dropdown-list--up" : ""}`}
          role="listbox"
          ref={listRef}
          style={{ maxHeight: listMaxHeight }}
        >
          {options.map((o, idx) => (
            <li
              key={o.value}
              role="option"
              aria-selected={o.value === value}
              data-highlighted={idx === highlight}
              className={`dropdown-option${idx === highlight ? " is-highlighted" : ""}${o.value === value ? " is-selected" : ""}`}
              onMouseEnter={() => setHighlight(idx)}
              onMouseDown={(e) => {
                e.preventDefault();
                onChange(o.value);
                setOpen(false);
              }}
            >
              {o.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
