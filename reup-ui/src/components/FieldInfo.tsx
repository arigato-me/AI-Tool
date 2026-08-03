// Nút "i" cạnh label — thay cho <span className="stage-detail"> mô tả dài luôn hiện sẵn,
// gói mô tả vào tooltip chỉ hiện khi hover/focus (CSS thuần, không cần state).
export default function FieldInfo({ text }: { text: string }) {
  return (
    <span className="field-info">
      <button type="button" className="field-info-btn" aria-label="Thông tin thêm">
        i
      </button>
      <span className="field-info-popup" role="tooltip">
        {text}
      </span>
    </span>
  );
}
