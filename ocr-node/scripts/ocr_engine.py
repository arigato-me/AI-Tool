"""Tier 2 — nhận dạng chữ từ pixel (ảnh chụp/trang scan), GPU. Port nguyên logic từ
ocr-service tham khảo (remote hmtran@100.99.150.90:~/Projects/ocr-service/ocr-node/app/worker/
engine.py) — đã verify chạy thật trên card 4GB cho ảnh đơn, không đổi gì thuật toán.

PaddleOCR's built-in "vi" recognizer dùng chung bộ ký tự "latin" với ~40 ngôn ngữ khác, thiếu
phần lớn dấu tiếng Việt (ơ, ư, ă và mọi dấu thanh tổ hợp) — không đại diện được số lớn từ tiếng
Việt dù ảnh đầu vào sạch tới đâu. Vì vậy lang="vi" chỉ dùng PaddleOCR để *detect* dòng chữ, giao
mỗi dòng cắt ra cho VietOCR — bộ nhận dạng huấn luyện riêng cho tiếng Việt, bộ ký tự đầy đủ.
Ngôn ngữ khác (en/fr) giữ nguyên pipeline detect+recognize gốc PaddleOCR."""
from __future__ import annotations

import logging

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def _sort_boxes_reading_order(dt_boxes: list) -> list:
    """Sắp lại box detect theo thứ tự đọc (trên->dưới, trái->phải cùng dòng) — thuật toán y hệt
    `sorted_boxes()` nội bộ của PaddleOCR (sort theo y góc trên-trái, rồi bubble-pass đổi chỗ
    2 box liền kề nếu cùng dòng — lệch y < 10px — mà box sau lại đứng bên trái box trước).

    Bug thật gặp lúc test job 2b3ad20e (ảnh 1 trang văn bản dày): gọi `detector.ocr(path,
    rec=False)` (chỉ detect, bỏ qua recognize) để tách riêng bước detect (PaddleOCR)/recognize
    (VietOCR) — nhưng bước sort thứ tự đọc của PaddleOCR nằm TRONG pipeline full det+rec, đường
    det-only KHÔNG chạy qua sort đó. Kết quả: các dòng chữ bị recognize đúng nhưng GHÉP LẠI xáo
    trộn thứ tự (câu chuyện không mạch lạc dù từng câu đọc riêng vẫn đúng chữ) — không tự sort
    lại ở đây thì `_run_paddle()` (full pipeline, có sort sẵn) không gặp, chỉ riêng nhánh
    `_run_vietnamese()` này mới cần."""
    boxes = sorted(dt_boxes, key=lambda b: (b[0][1], b[0][0]))
    n = len(boxes)
    for i in range(n - 1):
        for j in range(i, -1, -1):
            if abs(boxes[j + 1][0][1] - boxes[j][0][1]) < 10 and boxes[j + 1][0][0] < boxes[j][0][0]:
                boxes[j], boxes[j + 1] = boxes[j + 1], boxes[j]
            else:
                break
    return boxes


def _crop_line(image: np.ndarray, box: list[list[int]]) -> np.ndarray:
    """Perspective-crop 1 dòng chữ đã detect ra khỏi *image*.

    Box detect là 4 điểm góc (có thể xoay nghiêng); crop chữ nhật thường sẽ cắt xén dòng bị
    xoay, nên warp về chữ nhật thẳng thay vì crop trực tiếp."""
    pts = np.array(box, dtype=np.float32)
    width = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
    height = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
    width, height = max(width, 1), max(height, 1)
    dst = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(image, matrix, (width, height), borderMode=cv2.BORDER_REPLICATE)


class OCREngine:
    """Detect dòng chữ bằng PaddleOCR rồi nhận dạng.

    1 instance load 1 lần lúc worker khởi động, dùng lại cho mọi job (model-load-once, đúng
    convention mọi node GPU khác trong repo). Ngôn ngữ khác `lang` mặc định lúc khởi tạo được
    load thêm (lazy) khi job đầu tiên cần tới, cache lại cho các job sau."""

    def __init__(
        self,
        lang: str = "vi",
        vietocr_model_name: str = "vgg_transformer",
        vietocr_device: str = "cuda:0",
    ) -> None:
        from paddleocr import PaddleOCR

        self._default_lang = lang
        self._engines: dict[str, "PaddleOCR"] = {}
        self._vietocr_model_name = vietocr_model_name
        self._vietocr_device = vietocr_device
        self._vietocr_predictor = None  # lazy-load, chỉ cần khi có job lang="vi"

        logger.info("Loading PaddleOCR engine (lang=%s) ...", lang)
        self._engines[lang] = PaddleOCR(
            use_angle_cls=True,
            lang=lang,
            use_gpu=True,
            show_log=False,
        )
        if lang == "vi":
            self._load_vietocr()
        logger.info("PaddleOCR engine ready (lang=%s)", lang)

    def _load_vietocr(self) -> None:
        if self._vietocr_predictor is not None:
            return
        logger.info(
            "Loading VietOCR recognizer (model=%s, device=%s) ...",
            self._vietocr_model_name,
            self._vietocr_device,
        )
        from vietocr.tool.config import Cfg
        from vietocr.tool.predictor import Predictor

        config = Cfg.load_config_from_name(self._vietocr_model_name)
        config["device"] = self._vietocr_device
        self._vietocr_predictor = Predictor(config)
        logger.info("VietOCR recognizer ready")

    def _get_engine(self, lang: str):
        if lang not in self._engines:
            from paddleocr import PaddleOCR

            logger.info("Loading additional PaddleOCR engine for lang=%s", lang)
            self._engines[lang] = PaddleOCR(
                use_angle_cls=True,
                lang=lang,
                use_gpu=True,
                show_log=False,
            )
        return self._engines[lang]

    def run(self, image_path: str, lang: str | None = None) -> list[dict]:
        """OCR *image_path*, trả về list dòng chữ dạng::

            [{"text": "...", "confidence": 0.99, "bbox": [[x0,y0],...,[x3,y3]]}, ...]
        """
        effective_lang = lang or self._default_lang
        if effective_lang == "vi":
            return self._run_vietnamese(image_path)
        return self._run_paddle(image_path, effective_lang)

    def _run_paddle(self, image_path: str, lang: str) -> list[dict]:
        engine = self._get_engine(lang)
        raw = engine.ocr(image_path, cls=True)

        lines: list[dict] = []
        for page in raw or []:
            for entry in page or []:
                bbox_raw, (text, confidence) = entry
                lines.append({
                    "text": text,
                    "confidence": round(float(confidence), 4),
                    "bbox": [[int(pt[0]), int(pt[1])] for pt in bbox_raw],
                })
        return lines

    def _run_vietnamese(self, image_path: str) -> list[dict]:
        self._load_vietocr()
        detector = self._get_engine("vi")

        image = cv2.imread(image_path)
        if image is None:
            raise ValueError(f"Could not read image: {image_path}")

        raw = detector.ocr(image_path, rec=False)
        boxes = _sort_boxes_reading_order(list(raw[0]) if raw and raw[0] is not None else [])

        lines: list[dict] = []
        for box in boxes:
            bbox = [[int(pt[0]), int(pt[1])] for pt in box]
            crop = _crop_line(image, bbox)
            crop_rgb = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
            text, prob = self._vietocr_predictor.predict(crop_rgb, return_prob=True)
            lines.append({
                "text": text,
                "confidence": round(float(prob), 4),
                "bbox": bbox,
            })
        return lines
