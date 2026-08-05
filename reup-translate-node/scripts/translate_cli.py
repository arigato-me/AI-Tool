#!/usr/bin/env python3
"""CLI: transcript JSON (có timestamp) -> dịch từng segment bằng Deepseek -> transcript JSON tiếng Việt."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import queue_lib as q


class UsageTracker:
    """Gom prompt_tokens/completion_tokens từ MỌI lượt gọi Deepseek trong 1 lần chạy
    run_translate() — tạo MỚI mỗi job (không phải biến global) để không lẫn số liệu giữa các
    job chạy đồng thời trong cùng process; thread-safe (lock) vì nhiều batch/bisect/review chạy
    song song qua ThreadPoolExecutor. Trước đây field 'usage' Deepseek trả về bị bỏ hoàn toàn
    không dùng — thêm để có số liệu thật khi đánh giá các thay đổi giảm token (prompt tiếng Anh,
    giảm retry thừa ở tầng bisect, Google-polish fallback)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.calls_by_model: dict[str, int] = {}

    def add(self, model: str, usage: dict) -> None:
        with self._lock:
            self.prompt_tokens += usage.get("prompt_tokens", 0) or 0
            self.completion_tokens += usage.get("completion_tokens", 0) or 0
            self.calls_by_model[model] = self.calls_by_model.get(model, 0) + 1

    def summary(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "calls_by_model": dict(self.calls_by_model),
        }

CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
TRANSLATE_CONCURRENCY = 5  # số lượt gọi Deepseek chạy đồng thời — I/O-bound (chờ mạng), không
# đụng GIL, nên song song ở đây gần như free lunch; 5 là mức thận trọng để không ăn hết rate
# limit API khi 1 pipeline khác cũng đang dịch song song (orchestrator xử lý nhiều pipeline
# cùng lúc qua ThreadPoolExecutor riêng của nó).
VIET_DIACRITIC_RE = re.compile(
    "[đĐăĂâÂêÊôÔơƠưƯ"
    "àáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"
    "ÀÁẢÃẠẰẮẲẴẶẦẤẨẪẬÈÉẺẼẸỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌỒỐỔỖỘỜỚỞỠỢÙÚỦŨỤỪỨỬỮỰỲÝỶỸỴ]"
)
# Bug thật gặp lúc test video Douyin 2026-08-01: "哈哈哈哈。" (cười) được deepseek-chat dịch ĐÚNG
# ngay từ đầu ("Ha ha ha ha.") cả 3 lần thử — nhưng check dấu tiếng Việt bên dưới từ chối OAN vì
# tiếng cười vốn không có dấu, dài >=12 ký tự. Google dịch ra y hệt cũng bị từ chối oan luôn —
# tốn nguyên chuỗi retry+bisect+Google+reasoner (5 lượt gọi) cho câu ĐÃ ĐÚNG từ lượt đầu. Whitelist
# hẹp, chỉ khớp chuỗi CHỈ GỒM âm cười lặp lại (không khớp câu tiếng Anh thật vì câu thường không
# chỉ toàn "ha"/"he"/"hi"/"ho" lặp lại) — không nới lỏng check chung, chỉ vá đúng lớp lỗi đã thấy.
LAUGHTER_RE = re.compile(r"^(?:[\s.,!?~…]*(?:ha|he|hi|ho))+[\s.,!?~…]*$", re.IGNORECASE)
MIN_LEN_FOR_DIACRITIC_CHECK = 12  # câu quá ngắn (vd "ừ", "oh") có thể hợp lệ dù không dấu
TRANSLATE_MAX_RETRIES = 2
TRANSLATE_TEMPERATURE_SCHEDULE = [0.3, 0.7, 1.0]  # tăng dần mỗi lần retry — thoát "echo attractor"
# (model lặp/paraphrase nguyên văn tiếng Trung ở temp thấp cho câu thuật ngữ chuyên ngành,
# vd bài bạc; nhiệt độ cao hơn tạo đủ đa dạng để né hành vi lặp lại quyết định luận)
FALLBACK_MODEL = "deepseek-reasoner"  # phương án CUỐI CÙNG cho câu "cứng đầu" nhất (bisect xuống
# còn 1 câu mà vẫn lỗi, kể cả sau khi đã thử Google-dịch-nháp+sửa mượt bên dưới) — kiểm chứng
# thực nghiệm: deepseek-chat có "hố hút echo" (lặp/paraphrase nguyên tiếng Trung) với văn nói/
# tiếng lóng suồng sã (vd "俺", "鸟汉") Ở MỌI nhiệt độ/prompt, kể cả prompt tối giản không context;
# deepseek-reasoner dịch đúng ngay các câu y hệt. Chỉ dùng cho số ít câu thật sự cứng đầu, không
# thay hẳn model chính (reasoner chậm/đắt hơn nhiều — đây là lý do cần GOOGLE_POLISH_SYSTEM_PROMPT
# bên dưới chen vào TRƯỚC, rẻ hơn hẳn).

GOOGLE_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
GOOGLE_POLISH_SYSTEM_PROMPT = (
    "You receive an original Chinese sentence and a DRAFT Vietnamese translation (machine "
    "translation — basically correct in meaning but possibly stiff/unnatural). Your ONLY task "
    "is to rewrite the draft to be smoother and more natural in Vietnamese, suitable for "
    "spoken-dialogue dubbing, while preserving the exact meaning — do NOT retranslate from "
    "scratch, do NOT add/remove meaning. Return EXACTLY one JSON object of the form "
    "{\"text\": \"...\"}."
)

# Đổi sang tiếng Anh (2026-08-01) — prompt này cõng theo MỌI lượt gọi dịch (batch, review, cả
# retry/bisect) nên là phần tốn token cố định NHIỀU NHẤT trong toàn bộ pipeline; tiếng Việt có
# dấu tốn nhiều token/ký tự hơn hẳn tiếng Anh qua BPE tokenizer (tokenizer chủ yếu học từ dữ liệu
# Latin, chữ có dấu hay bị tách vụn). Ngôn ngữ RA LỆNH và ngôn ngữ OUTPUT là 2 việc tách biệt —
# đổi câu lệnh sang tiếng Anh không đổi yêu cầu output vẫn phải là target_lang (tiếng Việt, nối
# ở cuối). Đo token trước/sau bằng UsageTracker (xem run_translate) trước khi coi đây là xong.
SYSTEM_PROMPT = (
    "You are a professional translator specializing in video dialogue, translating into "
    "natural, spoken-style Vietnamese for voiceover dubbing. Preserve the exact meaning, keep "
    "it concise, add no annotations, and do NOT invent details/context that the source line "
    "does not contain (use the \"Additional context\" section below — if provided — only to "
    "correctly understand an ambiguous source line, never to add new plot details). Read the "
    "surrounding lines (before/after) in the list to track the emotional arc (happy, angry, "
    "sarcastic, tense, worried...) and translate with matching nuance — use natural Vietnamese "
    "spoken particles (đấy, chứ, mà, á, nhỉ, đâu...) instead of a stiff, lifeless translation. "
    "IMPORTANT: your answer must NEVER contain any Chinese characters — even when the source "
    "line is specialized jargon (gambling, sports, technical...) that is hard to translate "
    "literally, you must still paraphrase it in Vietnamese instead of repeating/paraphrasing "
    "the original Chinese text. When a digit sequence is used as a person's nickname/codename "
    "(e.g. \"零零七\" meaning \"007\", a James-Bond-style alias — not a real quantity), spell it "
    "out as Vietnamese number words (\"Không Không Bảy\"), NEVER as literal Sino-Vietnamese "
    "syllable-by-syllable transliteration (NOT \"Linh Linh Thất\") and NEVER as raw Arabic "
    "digits (NOT \"007\") — the voiceover TTS engine reads text literally with no number "
    "normalization, so raw digits get mispronounced as gibberish. Each line in the list has "
    "its original duration in seconds in "
    "parentheses — this is the ORIGINAL SPEECH LENGTH, meant only to help you sense the pacing "
    "(short/urgent vs long/leisurely), it is NOT a hard limit — the HIGHEST PRIORITY is a "
    "natural, continuous translation that reads like real spoken dialogue; NEVER deliberately "
    "cut words/filler particles just to match that duration — a choppy, truncated translation "
    "reads far worse than one that runs slightly longer than the source. Only consider "
    "trimming when the translation would clearly end up at least 1.5x longer than the source "
    "(genuinely verbose/redundant) — this does not apply to lines of normal or slightly longer "
    "length."
    " You will receive a numbered list of lines. Return EXACTLY one JSON object of the form "
    "{\"translations\": [\"...\", \"...\"]} with the same number of elements, in the same "
    "order, without adding line numbers inside the translated text."
)

# Nhánh target_lang không phải tiếng Việt (hiện chỉ "tiếng Anh", nhánh sách — xem
# _is_vietnamese_target()) — bản rút gọn của SYSTEM_PROMPT ở trên, bỏ các quy tắc CHỈ đúng cho
# tiếng Việt (hạt xưng hô đấy/chứ/mà..., cách đánh vần số theo âm Hán-Việt) thay vì nhét
# {target_lang} vào giữa câu tiếng Việt-cụ-thể — SYSTEM_PROMPT tiếng Việt giữ NGUYÊN, không đổi
# 1 ký tự, nhánh video/dubbing hiện tại đi đúng y hệt đường code cũ.
SYSTEM_PROMPT_EN = (
    "You are a professional translator specializing in video dialogue, translating into "
    "natural, spoken-style {target_lang} for voiceover dubbing. Preserve the exact meaning, "
    "keep it concise, add no annotations, and do NOT invent details/context that the source "
    "line does not contain (use the \"Additional context\" section below — if provided — only "
    "to correctly understand an ambiguous source line, never to add new plot details). Read "
    "the surrounding lines (before/after) in the list to track the emotional arc (happy, "
    "angry, sarcastic, tense, worried...) and translate with matching nuance — natural spoken "
    "phrasing, not a stiff, lifeless translation. IMPORTANT: your answer must NEVER contain "
    "any Chinese characters — even when the source line is specialized jargon (gambling, "
    "sports, technical...) that is hard to translate literally, you must still paraphrase it "
    "instead of repeating/paraphrasing the original Chinese text. When a digit sequence is "
    "used as a person's nickname/codename (e.g. \"零零七\" meaning \"007\", a James-Bond-style "
    "alias — not a real quantity), spell it out as words in {target_lang}, NEVER as raw Arabic "
    "digits — the voiceover TTS engine reads text literally with no number normalization, so "
    "raw digits get mispronounced as gibberish. Each line in the list has its original "
    "duration in seconds in parentheses — this is the ORIGINAL SPEECH LENGTH, meant only to "
    "help you sense the pacing (short/urgent vs long/leisurely), it is NOT a hard limit — the "
    "HIGHEST PRIORITY is a natural, continuous translation that reads like real spoken "
    "dialogue; NEVER deliberately cut words/filler just to match that duration — a choppy, "
    "truncated translation reads far worse than one that runs slightly longer than the "
    "source. Only consider trimming when the translation would clearly end up at least 1.5x "
    "longer than the source (genuinely verbose/redundant) — this does not apply to lines of "
    "normal or slightly longer length."
    " You will receive a numbered list of lines. Return EXACTLY one JSON object of the form "
    "{{\"translations\": [\"...\", \"...\"]}} with the same number of elements, in the same "
    "order, without adding line numbers inside the translated text."
)


def _is_vietnamese_target(target_lang: str) -> bool:
    """1 chỗ duy nhất quyết định route tiếng Việt (nguyên trạng, mọi nhánh video hiện tại) hay
    tiếng khác (nhánh sách target_lang="tiếng Anh", xem SYSTEM_PROMPT_EN/REVIEW_SYSTEM_PROMPT_EN
    + text_looks_untranslated() + translate_with_fallback() Google-rescue gate)."""
    return "việt" in target_lang.lower() or "vietnamese" in target_lang.lower()

# Tóm tắt NGỮ CẢNH nội bộ (dùng làm context truyền vào call_deepseek/_review_chunk, xem
# summarize_video_context) — KHÔNG cần ép ngôn ngữ gì cả, cứ để model tóm tắt tự nhiên bằng
# ngôn ngữ lời thoại gốc. Model đọc context tiếng Trung để dịch ĐÚNG câu thoại tiếng Trung sang
# tiếng Việt là việc hoàn toàn bình thường — không cần bản thân context đó phải là tiếng Việt.
#
# CỐ TÌNH GIỮ TIẾNG VIỆT (không đổi sang tiếng Anh như SYSTEM_PROMPT ở trên, 2026-08-01): đây +
# DESCRIBE_TRANSLATED_SYSTEM_PROMPT bên dưới là 2 prompt duy nhất KHÔNG ép ngôn ngữ output tường
# minh (dựa vào "input ngôn ngữ nào thì output tự mirror theo" — xem memory
# feedback_deepseek_summary_language: đã thử 5 cách ép ngôn ngữ khác nhau đều không ổn định).
# Đổi câu LỆNH sang tiếng Anh có nguy cơ khiến model lệch theo hướng trả lời bằng tiếng Anh
# (ngôn ngữ của lệnh) thay vì mirror theo ngôn ngữ input — đúng rủi ro đã từng vấp bug này. Lợi
# ích cũng thấp: 2 prompt này chỉ gọi ĐÚNG 1 LẦN/video (không lặp lại theo batch/retry như
# SYSTEM_PROMPT), nên giữ nguyên tiếng Việt — không đáng đánh đổi.
RAW_SUMMARY_SYSTEM_PROMPT = (
    "Bạn đọc toàn bộ lời thoại gốc (theo thứ tự) của 1 video. Tóm tắt ngắn gọn (3-5 câu) chủ "
    "đề video, bối cảnh, nhân vật/mối quan hệ giữa họ (nếu đoán được), tông giọng chung (hài "
    "hước, nghiêm túc, hồi hộp, tình cảm...) — dùng làm ngữ cảnh cho người dịch phụ đề, KHÔNG "
    "phải tóm tắt cốt truyện chi tiết. Với MỖI cặp nhân vật có quan hệ gia đình/thân thuộc rõ "
    "ràng (ông/bà - cháu, bố/mẹ - con, vợ - chồng, anh/chị - em...), PHẢI nêu rõ luôn cặp đại "
    "từ xưng hô tiếng Việt tương ứng ngay trong câu tóm tắt (vd \"Rick là ông ngoại của Morty, "
    "2 người xưng hô ông/con\") để người dịch dùng nhất quán xuyên suốt — không chỉ mô tả quan "
    "hệ chung chung. Trả về CHÍNH XÁC một JSON object dạng {\"summary\": \"...\"}, không thêm "
    "tiêu đề/giải thích ngoài field này."
)
CONTEXT_SUMMARY_MAX_CHARS = 6000  # giới hạn ký tự gửi tóm tắt — video reup thường ngắn (vài
# phút), nhưng vẫn cần chặn trần cho video dài bất thường, tránh 1 lệnh gọi quá nặng token

# Mô tả video XUẤT RA OUTPUT (dùng làm caption đăng bài) — gọi SAU khi mọi segment đã dịch
# xong, tóm tắt lại từ chính bản dịch tiếng Việt (không phải bản gốc tiếng Trung nữa). Input
# đã là tiếng Việt nên output tự nhiên cũng là tiếng Việt — né hẳn được vấn đề ép ngôn ngữ đầu
# ra đã vật lộn ở RAW_SUMMARY_SYSTEM_PROMPT (xem feedback_deepseek_summary_language trong
# memory, hoặc README): Deepseek không tuân thủ ổn định yêu cầu "viết bằng ngôn ngữ X" khi input
# là ngôn ngữ khác, kể cả đóng khung lại thành "dịch".
DESCRIBE_TRANSLATED_SYSTEM_PROMPT = (
    "Bạn đọc toàn bộ lời thoại (đã dịch, tiếng Việt) của 1 video, theo thứ tự. Tóm tắt lại "
    "thành 1 đoạn mô tả ngắn gọn (3-5 câu) dùng làm caption khi đăng video — nói về chủ đề, "
    "bối cảnh, nhân vật liên quan (nếu có), không liệt kê lại lời thoại. TUYỆT ĐỐI không mở "
    "đầu bằng \"Video ...\"/\"Đoạn video ...\" hay các cụm dẫn nhập tương tự — viết thẳng vào "
    "nội dung. Trả về CHÍNH XÁC một JSON object dạng {\"summary\": \"...\"}, không thêm tiêu "
    "đề/giải thích ngoài field này."
)

REVIEW_CHUNK_SIZE = 60  # số segment rà soát mỗi lượt gọi — đủ nhỏ để model đọc kỹ, đủ lớn để thấy mạch chuyện
REVIEW_CONTEXT_OVERLAP = 10  # số segment liền trước đưa vào làm ngữ cảnh (không sửa) — giữ mạch liền giữa các chunk

# Gán vai (speaker) cho nhánh sách (mode="book", xem tag_speakers() dưới) — dùng CHUNG
# REVIEW_CHUNK_SIZE/REVIEW_CONTEXT_OVERLAP với review_translation_context() thay vì hằng số
# riêng: cùng lý do cần "đọc đủ ngữ cảnh xung quanh + nhất quán xuyên suốt nhiều chunk" như review
# tên riêng, không cần tách config riêng.
SPEAKER_TAG_SYSTEM_PROMPT = (
    "You are analyzing a chunk of a story/book (already translated) to identify who is "
    "speaking each line, for a multi-voice audiobook narration. You receive a continuous "
    "chunk, each line with an id and its text. For each line, decide: \"narrator\" if it is "
    "narration/description (not spoken dialogue), or the SPEAKING CHARACTER's name if the line "
    "is dialogue spoken by a character (infer from dialogue attribution like \"said X\"/\"X "
    "asked\", or from turn-taking in a conversation). Use the EXACT SAME name for the same "
    "character throughout the whole chunk — a name glossary may be given below, reuse those "
    "names exactly, do not invent variant spellings. Lines marked [CONTEXT — CHỈ THAM KHẢO] are "
    "for context only, do NOT include them in the result. Return EXACTLY one JSON object of the "
    "form {\"speakers\": {\"<id>\": \"narrator or character name\"}}, one entry per non-context "
    "line id, no explanation."
)
# Đổi sang tiếng Anh cùng đợt với SYSTEM_PROMPT — có ép output tường minh qua target_lang nối
# cuối (giống call_deepseek), không dính rủi ro "mirror ngôn ngữ input" như RAW_SUMMARY/DESCRIBE.
REVIEW_SYSTEM_PROMPT = (
    "You are an editor reviewing a video subtitle translation into {target_lang}. You receive a "
    "continuous chunk of the video script, each line with an id, the original Chinese line, "
    "and its translation. Read the whole chunk to grasp the context, character personalities, "
    "and the emotional arc throughout. Only FIX translations that: (1) don't match the "
    "surrounding context (e.g. a proper name/term translated inconsistently with elsewhere in "
    "the chunk), (2) are correct in meaning but flat/lifeless, lacking the emotional nuance "
    "(happy/angry/sarcastic/tense...) the scene calls for, or (3) INVENT extra details/ideas "
    "the source line does not contain (e.g. the source is simply \"waiting for you\" but the "
    "translation adds \"after work\"/a place/time not present in the source) — fix these to "
    "stay closer to the source. Do NOT touch lines that are already fine. Lines marked "
    "[CONTEXT — DO NOT EDIT] are for context only, do NOT include them in the result. Return "
    "EXACTLY one JSON object of the form {{\"revisions\": {{\"<id>\": \"<new translation>\"}}}} — "
    "list ONLY ids that truly need fixing, skip ids that don't need changing, no explanation."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate CLI: transcript JSON -> transcript JSON tiếng Việt (Deepseek)"
    )
    parser.add_argument("-i", "--input", type=Path, required=True, help="File JSON transcript đầu vào")
    parser.add_argument("-o", "--output", type=Path, required=True, help="File JSON transcript đầu ra")
    parser.add_argument(
        "--api-key",
        default=os.environ.get("DEEPSEEK_API_KEY"),
        help="Deepseek API key (mặc định: env DEEPSEEK_API_KEY)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        help="Deepseek model (mặc định: env DEEPSEEK_MODEL hoặc deepseek-chat)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        help="Deepseek base URL (mặc định: env DEEPSEEK_BASE_URL)",
    )
    parser.add_argument(
        "--target-lang",
        default="tiếng Việt",
        help="Ngôn ngữ đích (mặc định: tiếng Việt)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="Số segment dịch mỗi lần gọi API (mặc định: 20)",
    )
    parser.add_argument(
        "--tag-speakers",
        action="store_true",
        help="Gán vai (narrator/tên nhân vật) cho từng segment — dùng cho nhánh sách (mode=book)",
    )
    return parser.parse_args()


def _post_chat(
    base_url: str, api_key: str, model: str, system_content: str, user_content: str, temperature: float,
    usage: "UsageTracker | None" = None,
) -> str:
    """Gọi thẳng Deepseek /chat/completions (json_object mode), trả về content thô (string
    JSON) chưa parse — dùng chung cho cả call_deepseek() (dịch) và _review_chunk() (rà soát
    ngữ cảnh), 2 nơi có schema JSON khác nhau nên việc parse để riêng ở từng nơi gọi."""
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Deepseek API lỗi HTTP {e.code}: {err_body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Không kết nối được Deepseek: {e.reason}") from e
    except (TimeoutError, OSError) as e:
        # Bug thật gặp lúc stress-test (nhiều pipeline chạy song song): timeout xảy ra lúc
        # resp.read() (đọc BODY response, SAU khi urlopen() đã kết nối xong) là 1 socket op
        # RIÊNG, không đi qua urlopen() nên KHÔNG được bọc thành urllib.error.URLError như lỗi
        # kết nối ban đầu — TimeoutError thô lọt thẳng qua try/except ở translate_with_fallback
        # (chỉ bắt RuntimeError), phá cả job dịch thay vì rơi vào retry/bisect sẵn có. Y hệt bug
        # JSONDecodeError đã fix trước đó (ValueError không phải RuntimeError) — bọc lại đây để
        # về đúng cơ chế retry, không phải sửa riêng từng lời gọi.
        raise RuntimeError(f"Deepseek timeout/lỗi kết nối lúc đọc response: {e}") from e

    if usage is not None and isinstance(payload.get("usage"), dict):
        usage.add(model, payload["usage"])
    return payload["choices"][0]["message"]["content"]


def call_deepseek(
    base_url: str, api_key: str, model: str, texts: list[str], durations: list[float], target_lang: str,
    temperature: float = 0.3, context: str = "", usage: "UsageTracker | None" = None,
) -> list[str]:
    numbered = "\n".join(f"{i} ({d:.1f}s): {t}" for i, (t, d) in enumerate(zip(texts, durations)))
    system_content = SYSTEM_PROMPT if _is_vietnamese_target(target_lang) else SYSTEM_PROMPT_EN.format(target_lang=target_lang)
    if context:
        system_content += f"\n\nAdditional context (reference only, for correct meaning/consistent proper nouns, do not invent): {context}"
    system_content += f" Target language: {target_lang}."
    content = _post_chat(base_url, api_key, model, system_content, numbered, temperature, usage)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        # Bug thật gặp lúc test: Deepseek thỉnh thoảng trả JSON hỏng (cắt cụt/lỗi escape) —
        # json.JSONDecodeError là ValueError, KHÔNG phải RuntimeError nên trước đây lọt qua
        # try/except của translate_with_fallback (chỉ bắt RuntimeError), làm chết cả job dịch
        # thay vì tự retry như các lỗi API khác. Bọc lại thành RuntimeError để rơi đúng vào cơ
        # chế retry/temperature-schedule đã có.
        raise RuntimeError(f"Deepseek trả JSON hỏng: {e}") from e
    translations = parsed.get("translations")
    if not isinstance(translations, list) or len(translations) != len(texts):
        raise RuntimeError(
            f"Deepseek trả về {len(translations) if isinstance(translations, list) else '?'} câu dịch, "
            f"cần {len(texts)} — dữ liệu không khớp, không dùng để tránh lệch timestamp."
        )
    return translations


# Đổi sang tiếng Anh cùng đợt với SYSTEM_PROMPT — đây là tác vụ TRÍCH XUẤT (không phải tóm
# tắt/viết tự do) nên không dính bug "ép ngôn ngữ đầu ra" đã gặp ở RAW_SUMMARY/DESCRIBE bên dưới
# (2 cái đó CỐ TÌNH giữ tiếng Việt, xem ghi chú tại chỗ khai báo).
NAME_EXTRACT_SYSTEM_PROMPT = (
    "You will read the full original dialogue (in order) of a video. List ALL proper names "
    "(person names, stage names, nicknames, band/group names...) that appear — write them "
    "EXACTLY as they appear in the original dialogue, do NOT translate/transliterate at this "
    "step. Return EXACTLY a JSON of the form {\"names\": [\"...\", ...]}, no duplicates, no "
    "explanation. If there are no proper names, return {\"names\": []}."
)
MAX_GLOSSARY_NAMES = 30  # chặn trần số tên riêng đưa vào bảng — video reup thường ít nhân vật,
# tránh 1 video lỗi (model liệt kê nhầm quá nhiều cụm không phải tên riêng) làm phình prompt

# Bảng tên riêng TĨNH cho tác phẩm kinh điển Trung Quốc hay bị reup (Thủy Hử, Tam Quốc Diễn
# Nghĩa, Tây Du Ký, tiểu thuyết Kim Dung) — nguồn gốc: batch dịch hay bị Deepseek echo nguyên
# tiếng Trung/dịch không nhất quán chính là các tên riêng cổ trang này (vd "宋江" tự dịch ra
# nhiều cách khác nhau giữa các batch, hoặc biệt danh như "及时雨" bị dịch nghĩa đen thành "Cơn
# mưa kịp thời" thay vì giữ "Cập Thời Vũ"). Đọc từ file YAML mount NGOÀI image
# (CLASSIC_NAMES_PATH, mặc định /config/classic_names.yaml — xem docker-compose.yml, cùng
# convention với dialogue.yaml của docker_transcribe) thay vì hardcode trong .py — sửa/thêm tên
# chỉ cần sửa file YAML, KHÔNG cần build lại image. Khác extract_name_glossary() (gọi Deepseek
# trích + dịch tên MỖI VIDEO, tốn 2 lượt gọi API, vẫn có thể dịch sai/thiếu), bảng này cho ĐÁP ÁN
# CHUẨN có sẵn — không tốn thêm lượt gọi Deepseek nào. File thiếu/lỗi không được làm hỏng job
# dịch chính — coi như rỗng, extract_name_glossary() vẫn tự bắt qua Deepseek như cũ.
CLASSIC_NAMES_PATH = os.environ.get("CLASSIC_NAMES_PATH", "/config/classic_names.yaml")


def _load_classic_name_glossary(path: str) -> dict[str, str]:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, ImportError) as e:
        print(f"Cảnh báo: không đọc được bảng tên riêng kinh điển ({path}: {e}) — bỏ qua, "
              f"vẫn dịch bình thường không có bảng này", file=sys.stderr)
        return {}
    flat: dict[str, str] = {}
    for section in data.values():
        if isinstance(section, dict):
            flat.update(section)
    return flat


CLASSIC_NAME_GLOSSARY: dict[str, str] = _load_classic_name_glossary(CLASSIC_NAMES_PATH)


def build_classic_glossary_note(segments: list[dict]) -> str:
    """Quét toàn bộ lời thoại video, chỉ đưa vào context những tên CÓ XUẤT HIỆN thật (tránh
    dội cả bảng ~300 tên vào prompt của video không liên quan gì tới cổ trang/kiếm hiệp)."""
    full_text = "".join(s["text"] for s in segments)
    matches = [f"{zh} = {vi}" for zh, vi in CLASSIC_NAME_GLOSSARY.items() if zh in full_text]
    if not matches:
        return ""
    return "Tên riêng tác phẩm kinh điển (PHẢI dùng đúng, không tự đổi cách viết): " + "; ".join(matches)


# Bảng quan hệ xưng hô TĨNH cho series hay bị reup lặp lại (Rick and Morty...) — CÙNG cơ chế
# CLASSIC_NAME_GLOSSARY ở trên (mount YAML ngoài image, đọc 1 lần lúc worker khởi động, không
# tốn thêm lượt gọi Deepseek nào), khác ở chỗ đây là QUAN HỆ xưng hô (ông/cháu, bố/con...),
# không phải cách viết tên. Root cause thật (job 114d7aa5, đoạn 6:20-7:59): không có bảng này,
# model tự suy luận xưng hô mỗi batch riêng — đúng ở câu tường thuật tiếng Trung (tự chứa từ
# quan hệ như "外孙"), sai ở câu thoại trực tiếp nguồn tiếng Anh (you/I không tự mang thông tin
# quan hệ) → bị dịch thành tôi/cậu ngang vai, sai vai vế gia đình.
RELATIONSHIP_NAMES_PATH = os.environ.get("RELATIONSHIP_NAMES_PATH", "/config/character_relationships.yaml")


def _load_relationship_glossary(path: str) -> dict[str, str]:
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, ImportError) as e:
        print(f"Cảnh báo: không đọc được bảng quan hệ nhân vật ({path}: {e}) — bỏ qua, "
              f"vẫn dịch bình thường không có bảng này", file=sys.stderr)
        return {}
    flat: dict[str, str] = {}
    for section in data.values():
        if isinstance(section, dict):
            flat.update(section)
    return flat


RELATIONSHIP_GLOSSARY: dict[str, str] = _load_relationship_glossary(RELATIONSHIP_NAMES_PATH)


def build_relationship_glossary_note(segments: list[dict]) -> str:
    """Quét toàn bộ lời thoại, chỉ đưa vào context những CẶP nhân vật có MẶT CẢ 2 tên trong
    video (key tiếng Trung, khớp cách build_classic_glossary_note() quét full_text TRƯỚC khi
    dịch) — tránh dội bảng vào video không liên quan."""
    full_text = "".join(s["text"] for s in segments)
    matches = []
    for pair, note in RELATIONSHIP_GLOSSARY.items():
        names = pair.split("-")
        if len(names) == 2 and all(n in full_text for n in names):
            matches.append(note)
    if not matches:
        return ""
    return "Quan hệ xưng hô nhân vật (PHẢI dùng đúng, nhất quán mọi câu): " + "; ".join(matches)


def extract_name_glossary(
    base_url: str, api_key: str, model: str, segments: list[dict], target_lang: str,
    usage: "UsageTracker | None" = None,
) -> str:
    """Gọi Deepseek 2 lần: (1) liệt kê tên riêng xuất hiện trong video (giữ nguyên ngôn ngữ
    gốc — tác vụ trích xuất, không phải tóm tắt/viết tự do nên không dính vấn đề ép ngôn ngữ đã
    gặp ở summarize_video_context), (2) DỊCH danh sách đó qua đúng call_deepseek() — hạ tầng
    dịch câu đã chứng minh ổn định — để có bản phiên âm/dịch tiếng Việt CHUẨN HOÁ 1 LẦN DUY
    NHẤT. Trả về chuỗi "tên gốc = tên Việt; ..." để nhét vào context mọi batch dịch + review,
    thay vì để mỗi batch tự dịch tên riêng theo cách khác nhau (bug thật user báo: cùng 1 người
    lúc dịch ra "Châu Kiệt Luân", lúc bỏ sót để nguyên tiếng Trung — review pass theo chunk cục
    bộ không đủ tầm nhìn toàn video để bắt hết, đặc biệt tên xuất hiện lần đầu ở chunk sau).
    Lỗi ở bất kỳ bước nào cũng không được làm hỏng job dịch chính — trả chuỗi rỗng."""
    full_text = " ".join(s["text"] for s in segments)
    if len(full_text) > CONTEXT_SUMMARY_MAX_CHARS:
        full_text = full_text[:CONTEXT_SUMMARY_MAX_CHARS]
    try:
        content = _post_chat(base_url, api_key, model, NAME_EXTRACT_SYSTEM_PROMPT, full_text, 0.2, usage)
        names = json.loads(content).get("names")
    except (RuntimeError, json.JSONDecodeError, AttributeError) as e:
        print(f"Cảnh báo: trích tên riêng thất bại ({e}) — dịch tiếp không có bảng tên riêng", file=sys.stderr)
        return ""
    if not isinstance(names, list):
        return ""
    names = [n.strip() for n in names if isinstance(n, str) and n.strip()][:MAX_GLOSSARY_NAMES]
    if not names:
        return ""

    # Tên đã có sẵn đáp án chuẩn trong CLASSIC_NAME_GLOSSARY (bảng hardcode, đã nhét vào
    # translate_context qua build_classic_glossary_note() TRƯỚC hàm này trong run_translate) —
    # không cần tốn thêm lượt gọi Deepseek dịch lại, chỉ gọi cho tên THỰC SỰ MỚI. Trước đây bảng
    # hardcode chỉ CỘNG THÊM context chứ không cắt được lượt gọi này — video cổ trang quen thuộc
    # (mọi tên đã có sẵn) vẫn tốn đúng 2 lượt gọi như video không có bảng, không tiết kiệm gì cả.
    unknown_names = [n for n in names if n not in CLASSIC_NAME_GLOSSARY]
    if not unknown_names:
        return ""  # mọi tên đã có sẵn trong classic_note rồi — khỏi cần thêm gì/gọi gì nữa

    try:
        translated_names = call_deepseek(base_url, api_key, model, unknown_names, [0.0] * len(unknown_names),
                                          target_lang, 0.2, usage=usage)
    except RuntimeError as e:
        print(f"Cảnh báo: dịch bảng tên riêng thất bại ({e}) — dịch tiếp không có bảng tên riêng", file=sys.stderr)
        return ""

    pairs = [f"{orig} = {tr.strip()}" for orig, tr in zip(unknown_names, translated_names) if tr.strip() and tr.strip() != orig]
    return "; ".join(pairs)


def summarize_video_context(
    base_url: str, api_key: str, model: str, segments: list[dict], usage: "UsageTracker | None" = None,
) -> str:
    """Gọi Deepseek 1 lần TRƯỚC khi dịch batch nào — tóm tắt chủ đề/bối cảnh/tông giọng cả
    video từ lời thoại gốc, dùng làm NGỮ CẢNH NỘI BỘ truyền vào mọi lượt dịch batch + review
    sau đó (không xuất ra output — xem describe_translated_video() cho bản xuất ra ngoài).
    Trước đây mỗi batch 20 câu dịch hoàn toàn tách rời, không biết video đang nói về gì — dễ
    dịch sai/bịa nghĩa cho câu ngắn mơ hồ (vd "chờ bạn" -> tự thêm "tan làm"). Lỗi ở đây KHÔNG
    được phép làm hỏng cả job dịch chính — trả chuỗi rỗng (coi như không có context) nếu gọi
    API thất bại, không raise.

    KHÔNG dịch kết quả sang target_lang — cứ để nguyên ngôn ngữ gốc (thường là tiếng Trung).
    Model đọc context tiếng Trung để dịch ĐÚNG câu thoại tiếng Trung khác là việc bình thường,
    không cần bản thân context phải cùng ngôn ngữ đích. Tránh hẳn được vụ Deepseek không tuân
    thủ ổn định yêu cầu ngôn ngữ đầu ra khi input tóm tắt dồn nhiều tên riêng (đã thử 5 cách ép
    ngôn ngữ khác nhau, không cách nào ổn định — xem memory feedback_deepseek_summary_language)."""
    full_text = " ".join(s["text"] for s in segments)
    if len(full_text) > CONTEXT_SUMMARY_MAX_CHARS:
        full_text = full_text[:CONTEXT_SUMMARY_MAX_CHARS]
    try:
        content = _post_chat(base_url, api_key, model, RAW_SUMMARY_SYSTEM_PROMPT, full_text, 0.3, usage)
        raw_summary = json.loads(content).get("summary")
    except (RuntimeError, json.JSONDecodeError, AttributeError) as e:
        print(f"Cảnh báo: tóm tắt bối cảnh video thất bại ({e}) — dịch tiếp không có ngữ cảnh chung", file=sys.stderr)
        return ""
    return raw_summary.strip() if isinstance(raw_summary, str) else ""


def describe_translated_video(
    base_url: str, api_key: str, model: str, segments: list[dict], usage: "UsageTracker | None" = None,
) -> str:
    """Gọi Deepseek 1 lần SAU KHI mọi segment đã dịch xong (field 'text' lúc này là tiếng Việt)
    — tóm tắt lại từ CHÍNH bản dịch, dùng làm mô tả video xuất ra output (khác
    summarize_video_context() ở trên — hàm đó tóm tắt bản GỐC, chỉ dùng nội bộ, không xuất ra).
    Input đã là tiếng Việt nên output tự nhiên cũng tiếng Việt — không cần ép ngôn ngữ gì, né
    hẳn vấn đề Deepseek không tuân thủ ổn định yêu cầu ngôn ngữ đầu ra. Lỗi ở đây không được
    phép làm hỏng job dịch chính — trả chuỗi rỗng nếu gọi API thất bại."""
    full_text = " ".join(s["text"] for s in segments)
    if len(full_text) > CONTEXT_SUMMARY_MAX_CHARS:
        full_text = full_text[:CONTEXT_SUMMARY_MAX_CHARS]
    try:
        content = _post_chat(base_url, api_key, model, DESCRIBE_TRANSLATED_SYSTEM_PROMPT, full_text, 0.3, usage)
        summary = json.loads(content).get("summary")
    except (RuntimeError, json.JSONDecodeError, AttributeError) as e:
        print(f"Cảnh báo: tạo mô tả video thất bại ({e}) — bỏ qua, không có mô tả", file=sys.stderr)
        return ""
    return summary.strip() if isinstance(summary, str) else ""


def text_looks_untranslated(translated: str, target_lang: str = "tiếng Việt") -> bool:
    """1 câu dịch coi là "chưa dịch" nếu (1) còn nhiều CJK (Deepseek trả nguyên văn gốc)
    hoặc (2) target là tiếng Việt VÀ đủ dài (>=12 ký tự) mà không có dấu tiếng Việt nào (nghi
    dịch sang ngôn ngữ khác). Check TỪNG CÂU thay vì cả batch — bản cũ tính % lỗi trên cả batch
    20 câu nên 1-2 câu lỗi lẻ tẻ (vd 10%) không đủ ngưỡng, lọt qua không retry (bug thật
    gặp: 2 câu bị bỏ sót nguyên tiếng Trung giữa 1 batch 20 câu dịch đúng).

    Check dấu tiếng Việt CHỈ áp dụng khi target_lang là tiếng Việt (mặc định, mọi nhánh video
    hiện tại — hành vi giữ NGUYÊN) — target khác (vd nhánh sách target="tiếng Anh") câu dịch
    đúng tất nhiên không có dấu tiếng Việt, check này sẽ từ chối OAN mọi câu, gây retry/bisect
    vô tận. Chỉ còn dựa vào tín hiệu CJK (thật sự quan trọng, đúng cho mọi ngôn ngữ đích)."""
    if not translated:
        return True
    if len(CJK_RE.findall(translated)) / len(translated) > 0.3:
        return True
    if LAUGHTER_RE.match(translated):
        return False
    if _is_vietnamese_target(target_lang) and len(translated) >= MIN_LEN_FOR_DIACRITIC_CHECK \
            and not VIET_DIACRITIC_RE.search(translated):
        return True
    return False


def _google_translate_zh_vi(text: str) -> str:
    """Endpoint KHÔNG chính thức (không cần API key) — chỉ dùng để lấy 1 bản dịch NHÁP cho số ít
    câu deepseek-chat từ chối dịch (echo attractor), không phải bộ dịch chính. Có thể bị Google
    chặn/đổi bất kỳ lúc nào — lỗi ở đây do CALLER log + fallback, không tự retry ở tầng này."""
    params = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "vi", "dt": "t", "q": text})
    req = urllib.request.Request(
        f"{GOOGLE_TRANSLATE_URL}?{params}", headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return "".join(chunk[0] for chunk in data[0] if chunk[0])


def _polish_google_draft(
    base_url: str, api_key: str, model: str, original: str, draft: str, context: str,
    usage: "UsageTracker | None" = None,
) -> str:
    system_content = GOOGLE_POLISH_SYSTEM_PROMPT
    if context:
        system_content += f"\n\nAdditional context (reference only, do not invent): {context}"
    user_content = f"Original (Chinese): {original}\nDraft (Vietnamese): {draft}"
    content = _post_chat(base_url, api_key, model, system_content, user_content, 0.3, usage)
    parsed = json.loads(content)
    text = parsed.get("text")
    if not isinstance(text, str):
        raise RuntimeError("Deepseek không trả field 'text' hợp lệ khi sửa mượt bản Google dịch")
    return text


def _try_google_polish_fallback(
    base_url: str, api_key: str, model: str, text: str, context: str,
    label: str, pipeline_id: str | None, video_name: str | None, usage: "UsageTracker | None" = None,
) -> str | None:
    """Ưu tiên thử TRƯỚC deepseek-reasoner (đắt hơn nhiều lần/token): dịch nháp bằng Google
    Translate (free) rồi nhờ deepseek-chat CHỈ sửa mượt lại — đổi thành nhiệm vụ "sửa câu tiếng
    Việt có sẵn" thay vì "dịch câu tiếng Trung suồng sã", né đúng lỗi echo-attractor (deepseek-chat
    từ chối/lặp nguyên văn khi được yêu cầu DỊCH những câu đó, nhưng đây không còn là yêu cầu dịch
    nữa). Trả None nếu BẤT KỲ bước nào lỗi (Google bị chặn, hoặc bản polish vẫn không đạt) — log
    rõ nguyên nhân để user tự biết fix (endpoint không chính thức, có thể tự đổi/bị chặn), KHÔNG
    raise — caller phải rơi xuống được fallback reasoner sẵn có, không được phép hỏng job dịch."""
    try:
        draft = _google_translate_zh_vi(text)
    except Exception as e:
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "google_translate_blocked",
                     "label": label, "text_original": text[:300], "error": str(e)})
        print(f"Cảnh báo: Google Translate lỗi/bị chặn ({e}) — fallback sang {FALLBACK_MODEL}", file=sys.stderr)
        return None
    if not draft or text_looks_untranslated(draft):
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "google_translate_bad_draft",
                     "label": label, "text_original": text[:300], "draft": draft[:300]})
        return None

    try:
        polished = _polish_google_draft(base_url, api_key, model, text, draft, context, usage)
    except (RuntimeError, json.JSONDecodeError) as e:
        print(f"Cảnh báo: sửa mượt bản Google dịch thất bại ({e}) — fallback sang {FALLBACK_MODEL}", file=sys.stderr)
        return None
    if not polished or text_looks_untranslated(polished):
        return None

    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "translate_google_polish_rescued",
                 "label": label, "text_original": text[:300], "google_draft": draft[:300], "text": polished[:300]})
    return polished.strip()


def translate_with_fallback(
    base_url: str, api_key: str, model: str, texts: list[str], durations: list[float], target_lang: str,
    label: str, max_retries: int = TRANSLATE_MAX_RETRIES, context: str = "",
    pipeline_id: str | None = None, video_name: str | None = None, depth: int = 0,
    usage: "UsageTracker | None" = None,
) -> list[str]:
    """Dịch 1 danh sách câu, retry tối đa max_retries lần. Sau mỗi lần gọi, kiểm tra TỪNG
    CÂU (text_looks_untranslated) — lần thử tiếp theo CHỈ gửi lại đúng những câu lỗi, giữ
    nguyên các câu đã dịch tốt (đỡ tốn API, không risk dịch lại làm hỏng câu đang đúng).
    Mỗi lần retry tăng temperature theo TRANSLATE_TEMPERATURE_SCHEDULE — quan sát thực tế:
    câu thuật ngữ chuyên ngành (vd bài bạc) khiến Deepseek lặp/paraphrase nguyên văn tiếng
    Trung Ở MỌI temperature thấp một cách gần như quyết định luận, retry cùng temperature cũ
    gần như chắc chắn lặp lại lỗi — cần temperature cao hơn để thoát "echo attractor" đó.
    Nếu hết lượt thử mà vẫn còn nhiều câu lỗi, chia đôi đệ quy để cô lập đúng (các) câu gây
    vấn đề — thường do nội dung nhạy cảm bị Deepseek từ chối.

    `depth` (0 = lượt gọi gốc từ _translate_batch, >0 = đã bisect ít nhất 1 lần): các câu đi
    vào 1 lượt bisect (depth>0) CHẮC CHẮN đã thất bại ở CẢ 3 nhiệt độ của lượt cha rồi (đó là lý
    do bị coi "still_bad" và bị bisect) — thử lại NGUYÊN cả thang nhiệt độ 1 lần nữa ở mỗi tầng
    bisect là lãng phí gần như chắc chắn (lỗi echo-attractor không đổi theo nhiệt độ, đã kiểm
    chứng thực nghiệm), mỗi tầng cõng thêm 2 lượt gọi thừa (đều mang nguyên context+glossary,
    phần tốn token nhất) chỉ để xác nhận lại điều đã biết. depth>0 nên chỉ thử ĐÚNG 1 lần (ở
    nhiệt độ CAO NHẤT — cha đã loại nhiệt độ thấp/vừa rồi) trước khi bisect tiếp/rơi xuống
    fallback — giảm ~60% lượt gọi cho đường xử lý câu cứng đầu mà không đổi model chính hay
    chất lượng (chỉ cắt phần lặp lại vô ích, không cắt phần cần thiết)."""
    result: list[str | None] = [None] * len(texts)
    pending_idx = list(range(len(texts)))
    last_error: RuntimeError | None = None
    effective_max_retries = max_retries if depth == 0 else 0

    for attempt in range(1, effective_max_retries + 2):
        if depth == 0:
            temperature = TRANSLATE_TEMPERATURE_SCHEDULE[min(attempt - 1, len(TRANSLATE_TEMPERATURE_SCHEDULE) - 1)]
        else:
            temperature = TRANSLATE_TEMPERATURE_SCHEDULE[-1]
        pending_texts = [texts[i] for i in pending_idx]
        pending_durations = [durations[i] for i in pending_idx]
        try:
            candidate = call_deepseek(base_url, api_key, model, pending_texts, pending_durations,
                                       target_lang, temperature, context, usage)
        except RuntimeError as e:
            last_error = e
            print(f"Cảnh báo: {label} lỗi ({e}) — thử lại lần {attempt}/{effective_max_retries + 1}", file=sys.stderr)
            continue

        still_bad = []
        for local_i, global_i in enumerate(pending_idx):
            t = candidate[local_i]
            if text_looks_untranslated(t, target_lang):
                still_bad.append(global_i)
                q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                             "event": "translate_rejected", "label": label, "attempt": attempt,
                             "temperature": temperature, "text_original": texts[global_i][:300],
                             "rejected_candidate": t[:300]})
            else:
                result[global_i] = t

        if not still_bad:
            return result  # type: ignore[return-value]

        last_error = RuntimeError(f"{len(still_bad)}/{len(pending_idx)} câu có vẻ chưa dịch")
        print(
            f"Cảnh báo: {label} có {len(still_bad)} câu có vẻ Deepseek chưa dịch/dịch sai ngôn ngữ — "
            f"thử lại lần {attempt}/{effective_max_retries + 1}",
            file=sys.stderr,
        )
        pending_idx = still_bad

    if len(pending_idx) == 1:
        i = pending_idx[0]

        # Google Translate rescue chỉ dịch được sang tiếng Việt (_google_translate_zh_vi ép cứng
        # tl=vi) — target khác (nhánh sách target="tiếng Anh") bỏ qua bước này, rơi thẳng xuống
        # fallback deepseek-reasoner bên dưới (không đụng gì, đã ổn định sẵn cho mọi target_lang).
        if _is_vietnamese_target(target_lang):
            polished = _try_google_polish_fallback(base_url, api_key, model, texts[i], context,
                                                    label, pipeline_id, video_name, usage)
            if polished:
                result[i] = polished
                return result  # type: ignore[return-value]

        try:
            fallback_candidate = call_deepseek(base_url, api_key, FALLBACK_MODEL, [texts[i]],
                                                [durations[i]], target_lang, 0.3, context, usage)[0]
        except RuntimeError:
            fallback_candidate = None
        if fallback_candidate and not text_looks_untranslated(fallback_candidate, target_lang):
            q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                         "event": "translate_fallback_model_rescued", "label": label,
                         "text_original": texts[i][:300], "text": fallback_candidate[:300]})
            result[i] = fallback_candidate.strip()
            return result  # type: ignore[return-value]
        print(
            f"Cảnh báo: {label} câu #{i} không dịch được sau Google-polish + model dự "
            f"phòng {FALLBACK_MODEL} ({last_error}) — giữ nguyên văn gốc, cần kiểm tra tay.",
            file=sys.stderr,
        )
        result[i] = texts[i]
        return result  # type: ignore[return-value]

    mid = len(pending_idx) // 2
    left_idx, right_idx = pending_idx[:mid], pending_idx[mid:]
    # Nhánh trái/phải độc lập hoàn toàn (context cố định, không đọc/ghi chung gì) — chạy song
    # song thay vì đợi nhánh trái xong mới tới nhánh phải, giảm gần nửa thời gian mỗi tầng bisect.
    with ThreadPoolExecutor(max_workers=2) as pool:
        left_future = pool.submit(
            translate_with_fallback, base_url, api_key, model, [texts[i] for i in left_idx],
            [durations[i] for i in left_idx], target_lang, f"{label}[bad:{mid}]", max_retries,
            context, pipeline_id, video_name, depth + 1, usage,
        )
        right_future = pool.submit(
            translate_with_fallback, base_url, api_key, model, [texts[i] for i in right_idx],
            [durations[i] for i in right_idx], target_lang, f"{label}[bad:{len(pending_idx) - mid}]",
            max_retries, context, pipeline_id, video_name, depth + 1, usage,
        )
        left = left_future.result()
        right = right_future.result()
    for local_i, global_i in enumerate(left_idx):
        result[global_i] = left[local_i]
    for local_i, global_i in enumerate(right_idx):
        result[global_i] = right[local_i]
    return result  # type: ignore[return-value]


def _review_chunk(
    base_url: str, api_key: str, model: str,
    context_segs: list[dict], target_segs: list[dict], target_lang: str, video_context: str = "",
    usage: "UsageTracker | None" = None,
) -> dict[int, str]:
    """1 l\u01b0\u1ee3t r\u00e0 so\u00e1t: g\u1eedi context_segs (ch\u1ec9 tham kh\u1ea3o, kh\u00f4ng s\u1eeda) + target_segs (\u0111\u01b0\u1ee3c ph\u00e9p
    s\u1eeda) cho model, nh\u1eadn v\u1ec1 JSON sparse {id: c\u00e2u d\u1ecbch m\u1edbi} \u2014 ch\u1ec9 nh\u1eefng id th\u1ef1c s\u1ef1 c\u1ea7n \u0111\u1ed5i.
    B\u1ea5t k\u1ef3 l\u1ed7i API/JSON h\u1ecfng n\u00e0o \u0111\u1ec1u b\u1ecb nu\u1ed1t t\u1ea1i \u0111\u00e2y (tr\u1ea3 {} r\u1ed7ng) \u2014 \u0111\u00e2y l\u00e0 b\u01b0\u1edbc N\u00c2NG CAO
    ch\u1ea5t l\u01b0\u1ee3ng, kh\u00f4ng ph\u1ea3i b\u01b0\u1edbc b\u1eaft bu\u1ed9c; l\u1ed7i \u1edf l\u01b0\u1ee3t review kh\u00f4ng \u0111\u01b0\u1ee3c ph\u00e9p l\u00e0m h\u1ecfng job
    d\u1ecbch ch\u00ednh \u0111\u00e3 ch\u1ea1y th\u00e0nh c\u00f4ng tr\u01b0\u1edbc \u0111\u00f3."""
    lines = []
    for s in context_segs:
        lines.append(f"[NG\u1eee C\u1ea2NH \u2014 KH\u00d4NG S\u1eecA] id={s['id']}: g\u1ed1c=\"{s['text_original']}\" | d\u1ecbch=\"{s['text']}\"")
    for s in target_segs:
        lines.append(f"id={s['id']}: g\u1ed1c=\"{s['text_original']}\" | d\u1ecbch=\"{s['text']}\"")
    user_content = "\n".join(lines)
    system_content = REVIEW_SYSTEM_PROMPT.format(target_lang=target_lang)
    if video_context:
        system_content += f"\n\nAdditional context (reference/use consistent proper nouns, do not invent): {video_context}"
    system_content += f" Target language: {target_lang}."

    try:
        content = _post_chat(base_url, api_key, model, system_content, user_content, 0.4, usage)
        parsed = json.loads(content)
        revisions = parsed.get("revisions")
        if not isinstance(revisions, dict):
            return {}
    except (RuntimeError, json.JSONDecodeError):
        print("C\u1ea3nh b\u00e1o: review ng\u1eef c\u1ea3nh th\u1ea5t b\u1ea1i \u2014 b\u1ecf qua chunk n\u00e0y, gi\u1eef b\u1ea3n d\u1ecbch g\u1ed1c", file=sys.stderr)
        return {}

    valid_ids = {s["id"] for s in target_segs}
    out: dict[int, str] = {}
    for k, v in revisions.items():
        try:
            seg_id = int(k)
        except (TypeError, ValueError):
            continue
        if seg_id in valid_ids and isinstance(v, str) and v.strip():
            out[seg_id] = v.strip()
    return out


def review_translation_context(
    base_url: str, api_key: str, model: str, segments: list[dict], target_lang: str,
    video_context: str = "", pipeline_id: str | None = None, video_name: str | None = None,
    usage: "UsageTracker | None" = None,
) -> None:
    """R\u00e0 so\u00e1t l\u1ea1i TO\u00c0N B\u1ed8 b\u1ea3n d\u1ecbch sau khi d\u1ecbch xong t\u1eebng batch ri\u00eang l\u1ebb (translate_segment \u1edf
    tr\u00ean ch\u1ec9 d\u1ecbch t\u1eebng batch 20 c\u00e2u, kh\u00f4ng bi\u1ebft ng\u1eef c\u1ea3nh to\u00e0n video) \u2014 s\u1eeda T\u1ea0I CH\u1ed6 (in-place)
    field 'text' c\u1ee7a c\u00e1c segment b\u1ecb l\u1ec7ch ng\u1eef c\u1ea3nh chung (t\u00ean ri\u00eang/thu\u1eadt ng\u1eef kh\u00f4ng nh\u1ea5t qu\u00e1n)
    ho\u1eb7c thi\u1ebfu s\u1eafc th\u00e1i c\u1ea3m x\u00fac so v\u1edbi m\u1ea1ch video. Chia theo REVIEW_CHUNK_SIZE, m\u1ed7i chunk
    mang th\u00eam REVIEW_CONTEXT_OVERLAP segment li\u1ec1n tr\u01b0\u1edbc l\u00e0m ng\u1eef c\u1ea3nh (\u0111\u00e3 d\u1ecbch, kh\u00f4ng s\u1eeda l\u1ea1i)
    \u0111\u1ec3 gi\u1eef m\u1ea1ch xuy\u00ean su\u1ed1t gi\u1eefa c\u00e1c chunk.

    Kh\u00e1c nh\u00e1nh d\u1ecbch ch\u00ednh (translate_with_fallback \u2014 lu\u00f4n validate + retry + bisect tr\u01b0\u1edbc khi
    ch\u1ea5p nh\u1eadn 1 c\u00e2u d\u1ecbch), review TR\u01af\u1edaC \u0110\u00c2Y \u00e1p th\u1eb3ng b\u1ea5t k\u1ef3 revision n\u00e0o model tr\u1ea3 v\u1ec1, kh\u00f4ng
    ki\u1ec3m tra g\u00ec \u2014 1 l\u01b0\u1ee3t review hoang t\u01b0\u1edfng (hallucinate) c\u00f3 th\u1ec3 bi\u1ebfn 1 c\u00e2u \u0110ANG D\u1ecaCH \u0110\u00daNG th\u00e0nh
    c\u00e2u h\u1ecfng (c\u00f2n ti\u1ebfng Trung/sai ng\u00f4n ng\u1eef), v\u00e0 kh\u00f4ng c\u00f3 c\u01a1 ch\u1ebf n\u00e0o b\u1eaft l\u1ea1i. Validate b\u1eb1ng
    text_looks_untranslated() y h\u1ec7t nh\u00e1nh d\u1ecbch ch\u00ednh tr\u01b0\u1edbc khi \u00e1p d\u1ee5ng \u2014 revision h\u1ecfng th\u00ec b\u1ecf
    qua, gi\u1eef nguy\u00ean b\u1ea3n d\u1ecbch c\u0169 (\u0111\u00e3 qua validate \u1edf batch ch\u00ednh) thay v\u00ec ghi \u0111\u00e8 m\u00f9."""
    for start in range(0, len(segments), REVIEW_CHUNK_SIZE):
        target_segs = segments[start:start + REVIEW_CHUNK_SIZE]
        ctx_start = max(0, start - REVIEW_CONTEXT_OVERLAP)
        context_segs = segments[ctx_start:start]
        revisions = _review_chunk(base_url, api_key, model, context_segs, target_segs, target_lang, video_context, usage)
        for seg in target_segs:
            new_text = revisions.get(seg["id"])
            if not new_text or new_text == seg["text"]:
                continue
            if text_looks_untranslated(new_text, target_lang):
                print(f"C\u1ea3nh b\u00e1o: review \u0111\u1ec1 xu\u1ea5t s\u1eeda segment id={seg['id']} nh\u01b0ng c\u00e2u m\u1edbi c\u00f3 v\u1ebb "
                      f"ch\u01b0a d\u1ecbch/d\u1ecbch h\u1ecfng \u2014 b\u1ecf qua, gi\u1eef nguy\u00ean b\u1ea3n d\u1ecbch c\u0169: {new_text[:80]!r}",
                      file=sys.stderr)
                q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                             "event": "translate_review_rejected", "segment_id": seg["id"],
                             "kept": seg["text"], "rejected_candidate": new_text})
                continue
            q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                         "event": "translate_reviewed", "segment_id": seg["id"],
                         "before": seg["text"], "after": new_text})
            seg["text"] = new_text


def _tag_speaker_chunk(
    base_url: str, api_key: str, model: str,
    context_segs: list[dict], target_segs: list[dict], video_context: str = "",
    usage: "UsageTracker | None" = None,
) -> dict[int, str]:
    """1 l\u01b0\u1ee3t g\u00e1n vai: g\u1eedi context_segs (ch\u1ec9 tham kh\u1ea3o) + target_segs (c\u1ea7n g\u00e1n) cho model, nh\u1eadn
    v\u1ec1 JSON {id: speaker}. B\u1ea5t k\u1ef3 l\u1ed7i API/JSON h\u1ecfng n\u00e0o \u0111\u1ec1u b\u1ecb nu\u1ed1t t\u1ea1i \u0111\u00e2y (tr\u1ea3 {} r\u1ed7ng) \u2014 l\u1ed7i \u1edf
    l\u01b0\u1ee3t g\u00e1n vai kh\u00f4ng \u0111\u01b0\u1ee3c ph\u00e9p l\u00e0m h\u1ecfng job d\u1ecbch ch\u00ednh \u0111\u00e3 ch\u1ea1y th\u00e0nh c\u00f4ng tr\u01b0\u1edbc \u0111\u00f3; segment
    kh\u00f4ng g\u00e1n \u0111\u01b0\u1ee3c gi\u1eef nguy\u00ean 'narrator' m\u1eb7c \u0111\u1ecbnh (xem tag_speakers())."""
    lines = []
    for s in context_segs:
        lines.append(f"[CONTEXT \u2014 CH\u1ec8 THAM KH\u1ea2O] id={s['id']}: \"{s['text']}\"")
    for s in target_segs:
        lines.append(f"id={s['id']}: \"{s['text']}\"")
    user_content = "\n".join(lines)
    system_content = SPEAKER_TAG_SYSTEM_PROMPT
    if video_context:
        system_content += f"\n\nName glossary / context (reuse names exactly, do not invent): {video_context}"

    try:
        content = _post_chat(base_url, api_key, model, system_content, user_content, 0.2, usage)
        parsed = json.loads(content)
        speakers = parsed.get("speakers")
        if not isinstance(speakers, dict):
            return {}
    except (RuntimeError, json.JSONDecodeError):
        print("C\u1ea3nh b\u00e1o: g\u00e1n vai (speaker tagging) th\u1ea5t b\u1ea1i \u2014 b\u1ecf qua chunk n\u00e0y, gi\u1eef 'narrator' m\u1eb7c \u0111\u1ecbnh", file=sys.stderr)
        return {}

    valid_ids = {s["id"] for s in target_segs}
    out: dict[int, str] = {}
    for k, v in speakers.items():
        try:
            seg_id = int(k)
        except (TypeError, ValueError):
            continue
        if seg_id in valid_ids and isinstance(v, str) and v.strip():
            out[seg_id] = v.strip()
    return out


def tag_speakers(
    base_url: str, api_key: str, model: str, segments: list[dict],
    video_context: str = "", pipeline_id: str | None = None, video_name: str | None = None,
    usage: "UsageTracker | None" = None,
) -> None:
    """G\u00e1n `seg['speaker']` ('narrator' ho\u1eb7c t\u00ean nh\u00e2n v\u1eadt) cho T\u1eeaNG segment \u2014 ch\u1ec9 d\u00f9ng cho nh\u00e1nh
    s\u00e1ch (mode='book', xem reup-orchestrator-node), KH\u00d4NG g\u1ecdi cho nh\u00e1nh video (review/dialogue/
    subtitle gi\u1eef nguy\u00ean h\u00e0nh vi c\u0169, kh\u00f4ng c\u00f3 field 'speaker'). reup-orchestrator-node d\u00f9ng field
    n\u00e0y \u0111\u1ec3 build map speaker->voice (m\u1ed7i nh\u00e2n v\u1eadt 1 gi\u1ecdng TTS ri\u00eang, ng\u01b0\u1eddi d\u1eabn chuy\u1ec7n 1 gi\u1ecdng
    ri\u00eang) tr\u01b0\u1edbc khi g\u1ecdi reup-tts-gpu-node tu\u1ea7n t\u1ef1 t\u1eebng segment.

    C\u00f9ng c\u01a1 ch\u1ebf chunk/overlap v\u1edbi review_translation_context() \u2014 1 nh\u00e2n v\u1eadt xu\u1ea5t hi\u1ec7n xuy\u00ean su\u1ed1t
    s\u00e1ch c\u1ea7n \u0111\u01b0\u1ee3c g\u1ecdi \u0110\u00daNG 1 t\u00ean nh\u1ea5t qu\u00e1n qua nhi\u1ec1u chunk, gi\u1ed1ng l\u00fd do glossary t\u00ean ri\u00eang
    (extract_name_glossary) t\u1ed3n t\u1ea1i; `video_context` truy\u1ec1n v\u00e0o \u0111\u00e2y n\u00ean l\u00e0 `translate_context` \u0111\u00e3
    c\u00f3 s\u1eb5n b\u1ea3ng t\u00ean ri\u00eang, kh\u00f4ng ph\u1ea3i m\u00f4 t\u1ea3 t\u00f3m t\u1eaft ri\u00eang.

    M\u1eb7c \u0111\u1ecbnh M\u1eccI segment l\u00e0 'narrator' tr\u01b0\u1edbc khi g\u1ecdi model \u2014 l\u1ed7i \u1edf b\u1ea5t k\u1ef3 chunk n\u00e0o (API l\u1ed7i,
    JSON h\u1ecfng) ch\u1ec9 khi\u1ebfn c\u00e1c segment trong chunk \u0111\u00f3 gi\u1eef nguy\u00ean 'narrator', kh\u00f4ng l\u00e0m h\u1ecfng job
    d\u1ecbch ch\u00ednh, kh\u00f4ng bao gi\u1edd \u0111\u1ec3 field 'speaker' thi\u1ebfu (tr\u00e1nh KeyError ph\u00eda orchestrator khi build
    map gi\u1ecdng)."""
    for seg in segments:
        seg.setdefault("speaker", "narrator")
    for start in range(0, len(segments), REVIEW_CHUNK_SIZE):
        target_segs = segments[start:start + REVIEW_CHUNK_SIZE]
        ctx_start = max(0, start - REVIEW_CONTEXT_OVERLAP)
        context_segs = segments[ctx_start:start]
        speakers = _tag_speaker_chunk(base_url, api_key, model, context_segs, target_segs, video_context, usage)
        for seg in target_segs:
            speaker = speakers.get(seg["id"])
            if speaker:
                seg["speaker"] = speaker
        q.log_event({
            "pipeline_id": pipeline_id, "video_name": video_name, "event": "translate_speakers_tagged",
            "chunk_start": start, "speakers": {s["id"]: s["speaker"] for s in target_segs},
        })


# K\u00fd t\u1ef1 k\u1ebft c\u00e2u ti\u1ebfng Trung \u2014 KH\u00c1C SENTENCE_SPLIT_RE b\u00ean d\u01b0\u1edbi (c\u00e1i \u0111\u00f3 t\u00e1ch c\u00e2u b\u1ea3n d\u1ecbch ti\u1ebfng
# Vi\u1ec7t/Latin SAU khi d\u1ecbch; 2 h\u1eb1ng s\u1ed1 n\u00e0y ki\u1ec3m tra c\u00e2u ti\u1ebfng Trung G\u1ed0C TR\u01af\u1edaC khi d\u1ecbch). Verify
# th\u1eadt tr\u00ean transcript fb3c208b (143 segment): punctuation-restoration KH\u00d4NG ch\u1ec9 d\u00f9ng d\u1ea5u
# full-width (\u3002\uff01\uff1f) \u2014 52/143 segment k\u1ebft th\u00fac b\u1eb1ng d\u1ea5u `.` half-width, 4 b\u1eb1ng `?` half-width
# (\u0111o\u1ea1n n\u00f3i l\u1eabn ti\u1ebfng Anh/model kh\u00f4ng nh\u1ea5t qu\u00e1n). Thi\u1ebfu b\u1ed9 half-width n\u00e0y l\u00fac \u0111\u1ea7u khi\u1ebfn heuristic
# coi nh\u1ea7m 56 segment ho\u00e0n to\u00e0n b\u00ecnh th\u01b0\u1eddng l\u00e0 "c\u1ee5t c\u00e2u", g\u1ed9p d\u01b0 kh\u00f4ng c\u1ea7n thi\u1ebft (kh\u00f4ng sai \u2014
# false positive v\u1eabn an to\u00e0n theo docstring d\u01b0\u1edbi \u2014 nh\u01b0ng kh\u00f4ng c\u1ea7n thi\u1ebft, s\u1eeda cho ch\u00ednh x\u00e1c h\u01a1n).
ZH_SENTENCE_END_CHARS = "\u3002\uff01\uff1f\u2026.!?"
ZH_TRAILING_QUOTES = "\u201d\u2019\"'\uff09\u3011\u300b\u300d\u300f)"

# reup-transcribe-node d\u00f9ng FunASR VAD v\u1edbi tr\u1ea7n C\u1ee8NG max_single_segment_time=20s (xem
# MAX_SEGMENT_SECONDS \u1edf reup-transcribe-node/scripts/pipeline.py) \u2014 T\u1ef0 \u00dd c\u1eaft b\u1ea5t k\u1ef3 \u0111o\u1ea1n n\u00f3i
# li\u00ean t\u1ee5c n\u00e0o d\u00e0i h\u01a1n 20s, k\u1ec3 c\u1ea3 \u0111ang gi\u1eefa c\u00e2u (comment g\u1ed1c b\u00ean \u0111\u00f3: "ch\u1ea5p nh\u1eadn \u0111\u00e1nh \u0111\u1ed5i, c\u00f3 th\u1ec3
# c\u1eaft ngang t\u1eeb/c\u00e2u \u2014 v\u1eabn t\u1ed1t h\u01a1n 1 segment qu\u00e1 d\u00e0i"). Punctuation-restoration (ct-punc) CH\u1ec8 th\u1ea5y
# \u0111\u00fang audio TRONG segment \u0111\u00f3 n\u00ean v\u1eabn t\u1ef1 tin ch\u00e8n d\u1ea5u \u3002 \u1edf \u0111\u00fang \u0111i\u1ec3m b\u1ecb c\u1eaft \u00e9p, d\u00f9 c\u00e2u ch\u01b0a ho\u00e0n
# ch\u1ec9nh \u2014 bug th\u1eadt g\u1eb7p (job fb3c208b): 1 l\u1ea7n th\u1eed fix \u0111\u1ea7u ti\u00ean CH\u1ec8 check d\u1ea5u c\u00e2u
# (_ends_with_zh_sentence_terminator) \u0111\u00e3 FAIL ngay \u1edf ch\u00ednh segment g\u00e2y bug, v\u00ec segment \u0111\u00f3 k\u1ebft
# th\u00fac "...\u8fd9\u756a\u8bdd\u8ba9\u3002" \u2014 C\u00d3 d\u1ea5u \u3002 h\u1ee3p l\u1ec7 d\u00f9 c\u00e2u c\u1ee5t l\u1eedng ("l\u1eddi n\u00e0y khi\u1ebfn [ai \u0111\u00f3]..." thi\u1ebfu h\u1eb3n v\u1ebf
# sau). Verify th\u00eam: segment d\u00e0i 19.09s + segment k\u1ebf 19.08s (\u0111\u1ec1u s\u00e1t tr\u1ea7n 20s) l\u00e0 c\u1eb7p g\u00e2y bug;
# qu\u00e9t c\u1ea3 transcript (143 segment) th\u1ea5y \u0111\u00fang 1 segment KH\u00c1C c\u0169ng s\u00e1t tr\u1ea7n (19.25s) \u2014 kh\u00f4ng ph\u1ea3i
# tr\u00f9ng h\u1ee3p 1 l\u1ea7n, \u0111\u00fang l\u00e0 c\u01a1 ch\u1ebf tr\u1ea7n MAX_SEGMENT_SECONDS l\u1eb7p l\u1ea1i. N\u00ean KH\u00d4NG th\u1ec3 ch\u1ec9 d\u1ef1a d\u1ea5u
# c\u00e2u \u2014 ph\u1ea3i k\u1ebft h\u1ee3p th\u00eam t\u00edn hi\u1ec7u \u0111\u1ed9 d\u00e0i segment.
PROBABLE_FORCED_CUT_MIN_DURATION_S = 18.5  # bi\u00ean d\u01b0\u1edbi tr\u1ea7n 20.0s th\u1eadt \u2014 \u0111i\u1ec3m c\u1eaft VAD quan s\u00e1t
# \u0111\u01b0\u1ee3c kh\u00f4ng r\u01a1i \u0111\u00fang 19.99s (19.09/19.08/19.25s), ch\u1eeba bi\u00ean \u0111\u1ee7 r\u1ed9ng \u0111\u1ec3 b\u1eaft c\u00e1c case t\u01b0\u01a1ng t\u1ef1
# m\u00e0 kh\u00f4ng \u0111\u00e1nh \u0111\u1ed3ng nh\u1ea7m 1 segment t\u1ef1 nhi\u00ean d\u00e0i ~15-17s (v\u1eabn d\u01b0\u1edbi h\u1eb3n ng\u01b0\u1ee1ng n\u00e0y).

MERGE_MAX_CHAIN = 3          # t\u1ed1i \u0111a g\u1ed9p 3 segment li\u00ean ti\u1ebfp \u2014 ch\u1eb7n 1 chu\u1ed7i VAD c\u1eaft l\u1ed7i li\u00ean
                              # t\u1ee5c bi\u1ebfn th\u00e0nh 1 d\u00f2ng kh\u1ed5ng l\u1ed3 kh\u00f3 d\u1ecbch/kh\u00f3 bisect khi l\u1ed7i
MERGE_MAX_GAP_S = 1.5        # ch\u1ec9 g\u1ed9p n\u1ebfu kho\u1ea3ng l\u1eb7ng gi\u1eefa 2 segment \u0111\u1ee7 NH\u1ece (ngh\u1ec9 gi\u1eefa c\u00e2u t\u1ef1
                              # nhi\u00ean) \u2014 kho\u1ea3ng l\u1eb7ng l\u1edbn th\u01b0\u1eddng l\u00e0 \u0111\u1ed5i c\u1ea3nh/ch\u1ee7 \u0111\u1ec1 th\u1eadt, kh\u00f4ng
                              # ph\u1ea3i c\u00e2u b\u1ecb c\u1eaft ngang gi\u1eefa ch\u1eebng. L\u01b0u \u00fd: transcript th\u1eadt cho
                              # th\u1ea5y gap=0.000s l\u00e0 PH\u1ed4 BI\u1ebeN (\u0111a s\u1ed1 segment li\u1ec1n k\u1ec1 nhau), kh\u00f4ng
                              # ph\u1ea3i t\u00edn hi\u1ec7u b\u1ea5t th\u01b0\u1eddng ri\u00eang \u2014 ch\u1ec9 d\u00f9ng l\u00e0m \u0111i\u1ec1u ki\u1ec7n CH\u1eb6N g\u1ed9p
                              # khi gap L\u1edaN, kh\u00f4ng d\u00f9ng \u0111\u1ec3 t\u1ef1 n\u00f3 ph\u00e1t hi\u1ec7n c\u00e2u c\u1ee5t.
MERGE_MAX_DURATION_S = 45.0  # tr\u1ea7n t\u1ed5ng th\u1eddi l\u01b0\u1ee3ng sau g\u1ed9p


def _ends_with_zh_sentence_terminator(text: str) -> bool:
    t = text.rstrip()
    while t and t[-1] in ZH_TRAILING_QUOTES:
        t = t[:-1]
    return bool(t) and t[-1] in ZH_SENTENCE_END_CHARS


def _is_probable_dangling_segment(seg: dict) -> bool:
    """2 t\u00edn hi\u1ec7u \u0110\u1ed8C L\u1eacP, k\u1ebft h\u1ee3p OR (xem gi\u1ea3i th\u00edch \u0111\u1ea7y \u0111\u1ee7 \u1edf PROBABLE_FORCED_CUT_MIN_DURATION_S
    ph\u00eda tr\u00ean \u2014 v\u00ec sao ch\u1ec9 d\u00f9ng ri\u00eang d\u1ea5u c\u00e2u l\u00e0 KH\u00d4NG \u0111\u1ee7)."""
    if not _ends_with_zh_sentence_terminator(seg["text"]):
        return True
    return (seg["end"] - seg["start"]) >= PROBABLE_FORCED_CUT_MIN_DURATION_S


def merge_dangling_sentence_segments(segments: list[dict]) -> list[dict]:
    """G\u1ed9p 1 segment VAD nhi\u1ec1u kh\u1ea3 n\u0103ng b\u1ecb c\u1eaft GI\u1eeeA c\u00e2u (xem `_is_probable_dangling_segment`)
    v\u1edbi segment k\u1ebf ti\u1ebfp, TR\u01af\u1edaC khi \u0111\u01b0a qua Deepseek d\u1ecbch.

    Root cause bug th\u1eadt g\u1eb7p (job fb3c208b, ph\u00fat \u0111\u1ea7u tho\u1ea1i+sub b\u1ecb l\u1eb7p): VAD c\u1eaft tr\u00fang gi\u1eefa 1 c\u00e2u
    do ch\u1ea1m tr\u1ea7n MAX_SEGMENT_SECONDS=20s ("...\u8fd9\u756a\u8bdd\u8ba9\u3002" c\u1ee5t l\u1eedng \u1edf cu\u1ed1i segment 19.09s), trong
    khi SYSTEM_PROMPT l\u1ea1i y\u00eau c\u1ea7u model \u0111\u1ecdc "surrounding lines (before/after) in the list" \u0111\u1ec3
    b\u1eaft m\u1ea1ch c\u1ea3m x\u00fac \u2014 segment k\u1ebf ti\u1ebfp (ch\u1ee9a v\u1ebf c\u00f2n l\u1ea1i c\u1ee7a c\u00e2u + n\u1ed9i dung m\u1edbi) n\u1eb1m S\u1eb4N trong
    c\u00f9ng 1 l\u01b0\u1ee3t g\u1ecdi batch, n\u00ean model "m\u01b0\u1ee3n" lu\u00f4n n\u1ed9i dung \u0111\u00f3 \u0111\u1ec3 ho\u00e0n thi\u1ec7n c\u00e2u c\u1ee5t, tr\u1ea3 v\u1ec1 b\u1ea3n
    d\u1ecbch D\u00c0I h\u01a1n h\u1eb3n c\u00e2u g\u1ed1c (verify th\u1eadt: segment g\u1ed1c 6 c\u00e2u ti\u1ebfng Trung nh\u01b0ng b\u1ea3n d\u1ecbch tr\u1ea3 v\u1ec1
    10 c\u00e2u ti\u1ebfng Vi\u1ec7t \u2014 4 c\u00e2u d\u01b0 ra tr\u00f9ng g\u1ea7n nh\u01b0 y h\u1ec7t n\u1ed9i dung segment k\u1ebf ti\u1ebfp). Do 2 segment
    v\u1eabn \u0111\u01b0\u1ee3c d\u1ecbch RI\u00caNG, segment k\u1ebf ti\u1ebfp sau \u0111\u00f3 v\u1eabn d\u1ecbch \u0111\u00fang n\u1ed9i dung c\u1ee7a ch\u00ednh n\u00f3 \u2014 k\u1ebft qu\u1ea3 l\u00e0
    c\u00f9ng 1 \u0111o\u1ea1n n\u1ed9i dung b\u1ecb d\u1ecbch (v\u00e0 TTS \u0111\u1ecdc) 2 L\u1ea6N.

    G\u1ed9p tr\u01b0\u1edbc khi d\u1ecbch xo\u00e1 tri\u1ec7t \u0111\u1ec3 ti\u1ec1n \u0111\u1ec1 g\u00e2y l\u1ed7i: Deepseek kh\u00f4ng c\u00f2n th\u1ea5y c\u00e2u c\u1ee5t n\u00e0o \u0111\u1ec3
    "m\u01b0\u1ee3n" n\u1eefa, v\u00ec n\u1ed9i dung th\u1eadt \u0111\u00e3 n\u1eb1m g\u1ecdn trong CH\u00cdNH segment \u0111ang d\u1ecbch. Sau khi d\u1ecbch xong,
    `split_segment_by_sentences()` b\u00ean d\u01b0\u1edbi (\u0111\u00e3 c\u00f3 s\u1eb5n, v\u1ed1n d\u00f9ng cho segment g\u1ed1c nhi\u1ec1u c\u00e2u) t\u1ef1
    chia l\u1ea1i th\u00e0nh nhi\u1ec1u segment con \u0111\u00fang c\u00e2u, \u0111\u00fang timestamp t\u1ef7 l\u1ec7 \u2014 kh\u00f4ng c\u1ea7n c\u01a1 ch\u1ebf m\u1edbi cho
    vi\u1ec7c n\u00e0y, t\u1eadn d\u1ee5ng \u0111\u00fang logic \u0111ang ch\u1ea1y.

    3 \u0111i\u1ec1u ki\u1ec7n ch\u1eb7n (MERGE_MAX_CHAIN/GAP/DURATION) kh\u00f4ng c\u1ea7n ch\u00ednh x\u00e1c tuy\u1ec7t \u0111\u1ed1i \u2014 g\u1ed9p nh\u1ea7m 1
    segment v\u1ed1n \u0111\u00e3 tr\u1ecdn c\u00e2u (d\u01b0\u01a1ng t\u00ednh gi\u1ea3, vd 1 segment t\u1ef1 nhi\u00ean d\u00e0i \u226518.5s nh\u01b0ng th\u1eadt s\u1ef1 \u0111\u00e3
    h\u1ebft c\u00e2u) ch\u1ec9 t\u1ea1o ra 1 kh\u1ed1i h\u01a1i d\u00e0i h\u01a1n, KH\u00d4NG g\u00e2y tr\u00f9ng l\u1eb7p g\u00ec; b\u1ecf s\u00f3t 1 c\u00e2u c\u1ee5t th\u1eadt th\u00ec t\u1ec7
    nh\u1ea5t b\u1eb1ng hi\u1ec7n tr\u1ea1ng tr\u01b0\u1edbc fix, kh\u00f4ng t\u1ec7 h\u01a1n \u2014 heuristic kh\u00f4ng c\u1ea7n ho\u00e0n h\u1ea3o \u0111\u1ec3 c\u00f3 l\u1ee3i."""
    if not segments:
        return segments
    merged: list[dict] = []
    i = 0
    n = len(segments)
    while i < n:
        cur = dict(segments[i])
        # `last_original` luôn trỏ ĐÚNG segment GỐC (chưa gộp) vừa được nối vào cur — điều kiện
        # gộp tiếp PHẢI xét trên chính segment gốc này, KHÔNG xét trên độ dài cộng dồn của cur.
        # Lý do: nếu xét cộng dồn, 1 chain đã bắt đầu gộp (vì lý do chính đáng) sẽ tiếp tục
        # "nuốt" thêm segment kế tiếp dù segment đó hoàn toàn bình thường/đã trọn câu, chỉ vì
        # tổng cộng dồn tình cờ đã vượt PROBABLE_FORCED_CUT_MIN_DURATION_S — biến tín hiệu "segment
        # này có khả năng bị cắt ép" thành "khối đã đủ dài thì cứ gộp tiếp", sai bản chất.
        last_original = segments[i]
        chain_len = 1
        while (
            chain_len < MERGE_MAX_CHAIN
            and i + 1 < n
            and _is_probable_dangling_segment(last_original)
            and (segments[i + 1]["start"] - cur["end"]) <= MERGE_MAX_GAP_S
            and (segments[i + 1]["end"] - cur["start"]) <= MERGE_MAX_DURATION_S
        ):
            nxt = segments[i + 1]
            cur["text"] = f"{cur['text']}{nxt['text']}"
            cur["end"] = nxt["end"]
            if "words" in cur or "words" in nxt:
                cur["words"] = (cur.get("words") or []) + (nxt.get("words") or [])
            i += 1
            last_original = segments[i]
            chain_len += 1
        merged.append(cur)
        i += 1
    return merged


SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?\u2026])\s+")


def split_sentences(text: str) -> list[str]:
    """Tách 1 đoạn dịch thành các câu theo dấu kết câu (. ! ? …). 1 segment transcript gốc
    (VAD-speech) có thể gộp nhiều câu vào 1 cửa sổ thời gian — cần tách trước khi ghi ra
    translated.json để cả TTS lẫn SRT (tiêu thụ chung schema segments) đều thấy 1 segment
    = 1 câu, tránh: (1) sub cắt giữa câu (SRT chỉ word-wrap theo ký tự, không biết ranh
    giới câu), (2) TTS phải đọc trọn khối nhiều câu trong 1 slot ngắn, dễ tràn giờ đè lên
    segment kế tiếp (từng thấy log "bị đè 4.81s" ở đoạn thoại dồn dập nhiều câu)."""
    text = text.strip()
    if not text:
        return []
    parts = [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts or [text]


def split_segment_by_sentences(seg: dict) -> list[dict]:
    """Chia 1 segment thành nhiều segment con theo câu, chia timestamp tỷ lệ số ký tự mỗi
    câu (xấp xỉ tốc độ nói đều — cùng kỹ thuật distribute_cues() bên reup-editor-node dùng
    để chia dòng sub, áp dụng sớm hơn ở đây để TTS cũng hưởng lợi, không chỉ riêng SRT).
    `text_original` giữ nguyên full câu gốc trên mọi segment con — không có cách chia đúng
    ranh giới câu giữa 2 ngôn ngữ khác cấu trúc, nhưng field này chỉ dùng để tham khảo/log,
    không ảnh hưởng TTS/mix/sub (đều đọc field `text`)."""
    sentences = split_sentences(seg["text"])
    if len(sentences) <= 1:
        return [seg]

    start, end = seg["start"], seg["end"]
    total_chars = sum(len(s) for s in sentences) or 1
    out = []
    t = start
    for i, sentence in enumerate(sentences):
        if i == len(sentences) - 1:
            t_end = end
        else:
            share = len(sentence) / total_chars
            t_end = min(end, t + (end - start) * share)
        child = dict(seg)
        child["start"] = t
        child["end"] = t_end
        child["text"] = sentence
        out.append(child)
        t = t_end
    return out


def run_translate(
    input_path: str,
    output_path: str,
    api_key: str,
    model: str = "deepseek-chat",
    base_url: str = "https://api.deepseek.com",
    target_lang: str = "tiếng Việt",
    batch_size: int = 20,
    pipeline_id: str | None = None,
    video_name: str | None = None,
    tag_speakers_enabled: bool = False,
) -> dict:
    """Dịch 1 file transcript JSON -> file JSON tiếng Việt, trả dict kết quả (đúng
    contract JSON mà CLI vẫn in ra stdout). Tách riêng khỏi main()/argparse để dùng chung
    được cho cả CLI (chạy tay) lẫn worker (api.py enqueue, worker.py gọi hàm này)."""
    input_p = Path(input_path).resolve()
    if not input_p.is_file():
        raise RuntimeError(f"file input không tồn tại: {input_p}")

    data = json.loads(input_p.read_text(encoding="utf-8"))
    segments = data.get("segments", [])
    if not segments:
        raise RuntimeError(f"file input không có 'segments': {input_p}")

    # Gộp segment VAD cắt cụt giữa câu với segment kế tiếp TRƯỚC khi dịch — xem docstring
    # merge_dangling_sentence_segments() cho root cause đầy đủ (bug thật: job fb3c208b, thoại+sub
    # bị lặp phút đầu do Deepseek "mượn" nội dung segment sau khi gặp câu cụt). CHỈ áp dụng
    # transcript từ VAD audio thật (nhánh video) — BỎ QUA cho nhánh sách (tag_speakers_enabled,
    # xem ocr-node/document_extractor.py): segment ở đó là đoạn văn OCR/markitdown đã trọn vẹn,
    # không có khái niệm "bị cắt ngang giữa câu" như VAD; start/end chỉ là timestamp GIẢ (ước
    # lượng tốc độ đọc) — heuristic dựa vào duration (>=18.5s) sẽ GỘP NHẦM 2 đoạn văn không liên
    # quan chỉ vì đoạn trước dài hơn ~259 ký tự, làm lệch cả gán speaker chạy sau bước này.
    if not tag_speakers_enabled:
        segments_before_merge = len(segments)
        segments = merge_dangling_sentence_segments(segments)
        if len(segments) < segments_before_merge:
            q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                         "event": "translate_merged_dangling_segments",
                         "before_count": segments_before_merge, "after_count": len(segments)})

    output_p = Path(output_path).resolve()
    output_p.parent.mkdir(parents=True, exist_ok=True)

    start = time.time()
    usage = UsageTracker()  # 1 tracker MỚI mỗi lần chạy job — xem docstring UsageTracker
    translate_context = ""

    # Rollback 2026-08-04 (đã thử "translate_enabled=False khi ocr_lang==target_lang" cho nhánh
    # sách để khỏi tốn token dịch vi->vi — gây bug thật: text OCR tiếng Việt thường thiếu dấu,
    # bỏ qua Deepseek verify thì TTS đọc thẳng bản không dấu đó). LUÔN chạy qua Deepseek — kể cả
    # cùng ngôn ngữ, Deepseek vẫn có tác dụng chuẩn hoá dấu/câu chữ (đã verify thật: OCR
    # "Chao mung ban den voi sach dien tu." -> Deepseek trả đúng dấu "Chào mừng bạn đến với sách
    # điện tử.").
    #
    # Tóm tắt bối cảnh cả video 1 lần TRƯỚC khi dịch batch nào — dùng chung cho mọi batch +
    # review bên dưới (xem summarize_video_context docstring lý do: batch tách rời không biết
    # video nói về gì, dễ dịch sai/bịa nghĩa câu ngắn mơ hồ). Lỗi ở đây không làm hỏng job dịch.
    # Đây là context NỘI BỘ (ngôn ngữ gốc, không xuất ra output) — mô tả xuất ra output tính
    # riêng SAU KHI dịch xong, xem describe_translated_video() bên dưới.
    translate_context = summarize_video_context(base_url, api_key, model, segments, usage)
    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                 "event": "translate_context_summary", "context": translate_context})

    # Bảng tên riêng TĨNH cho tác phẩm kinh điển (xem CLASSIC_NAME_GLOSSARY) — không tốn thêm
    # lượt gọi Deepseek nào, có đáp án chuẩn sẵn trước khi extract_name_glossary() chạy.
    classic_note = build_classic_glossary_note(segments)
    if classic_note:
        translate_context = f"{translate_context}\n{classic_note}" if translate_context else classic_note
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                     "event": "translate_classic_glossary", "glossary": classic_note})

    # Bảng quan hệ xưng hô TĨNH cho series hay bị reup lặp lại (xem RELATIONSHIP_GLOSSARY) —
    # cùng cơ chế/vị trí với classic_note ở trên, không tốn thêm lượt gọi Deepseek nào.
    relationship_note = build_relationship_glossary_note(segments)
    if relationship_note:
        translate_context = f"{translate_context}\n{relationship_note}" if translate_context else relationship_note
        q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                     "event": "translate_relationship_glossary", "glossary": relationship_note})

    # Bảng tên riêng chuẩn hoá 1 lần cho cả video — gộp CHUNG vào context nội bộ truyền xuống
    # mọi batch + review, để mọi lượt dịch dùng ĐÚNG 1 cách viết tên (xem
    # extract_name_glossary() — bug thật user báo: cùng người, chỗ dịch ra tên Việt, chỗ bỏ
    # sót để nguyên tiếng Trung, do trước đây chỉ review pass theo chunk cục bộ mới bắt được).
    name_glossary = extract_name_glossary(base_url, api_key, model, segments, target_lang, usage)
    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                 "event": "translate_name_glossary", "glossary": name_glossary})
    if name_glossary:
        glossary_note = f"Bảng tên riêng (PHẢI dùng đúng, nhất quán mọi câu, không tự đổi cách viết): {name_glossary}"
        translate_context = f"{translate_context}\n{glossary_note}" if translate_context else glossary_note

    # Mỗi batch dịch độc lập (translate_context đã tính CỐ ĐỊNH ở trên, không đổi giữa các batch)
    # — chạy song song thay vì tuần tự, giảm tổng thời gian dịch từ TỔNG các batch xuống còn
    # khoảng thời gian của batch CHẬM NHẤT (batch có nhiều câu lỗi phải bisect sâu).
    batch_size = max(1, batch_size)
    batches = [(i, segments[i:i + batch_size]) for i in range(0, len(segments), batch_size)]

    def _translate_batch(item: tuple[int, list[dict]]) -> tuple[list[dict], list[str]]:
        i, batch = item
        texts = [s["text"] for s in batch]
        durations = [max(0.0, s["end"] - s["start"]) for s in batch]
        label = f"batch segment {i}-{i + len(batch) - 1}"
        translations = translate_with_fallback(base_url, api_key, model, texts, durations, target_lang,
                                                label, context=translate_context,
                                                pipeline_id=pipeline_id, video_name=video_name, usage=usage)
        return batch, translations

    with ThreadPoolExecutor(max_workers=min(TRANSLATE_CONCURRENCY, len(batches))) as pool:
        for batch, translations in pool.map(_translate_batch, batches):
            for seg, translated in zip(batch, translations):
                seg["text_original"] = seg["text"]
                seg["text"] = translated.strip()
                q.log_event({"pipeline_id": pipeline_id, "video_name": video_name, "event": "translate_segment",
                             "segment_id": seg.get("id"), "start": seg.get("start"), "end": seg.get("end"),
                             "text_original": seg["text_original"], "text": seg["text"]})

    review_translation_context(base_url, api_key, model, segments, target_lang, translate_context,
                                pipeline_id=pipeline_id, video_name=video_name, usage=usage)

    # Gán vai (narrator/tên nhân vật) cho nhánh sách (mode="book") — CHỈ bật khi orchestrator
    # yêu cầu (worker.py truyền tag_speakers_enabled=True cho payload mode="book"), không đổi
    # gì hành vi nhánh video review/dialogue/subtitle (segments không có field 'speaker').
    # translate_context (đã có sẵn bảng tên riêng qua extract_name_glossary) dùng làm ngữ cảnh
    # để model dùng ĐÚNG 1 tên nhất quán cho mỗi nhân vật xuyên suốt sách.
    #
    # Tách theo câu TRƯỚC khi gán vai (bug thật gặp lúc test truyện có thoại: ocr-node gộp cả
    # đoạn lời dẫn + nhiều lượt thoại thành 1 "segment" thô vì trang PDF không có blank-line rõ
    # giữa các dòng, xem ocr-node/document_extractor.py::_split_paragraphs — tag_speakers() chạy
    # trên nguyên khối đó chỉ gán được ĐÚNG 1 speaker cho cả đoạn lẫn lộn, không tách được câu
    # nào của narrator/câu nào là thoại nhân vật). Gọi split_segment_by_sentences() ở ĐÂY, sớm
    # hơn bước expand cuối hàm — bước expand cuối gọi lại hàm này lần 2 nhưng đã là no-op (mỗi
    # segment lúc đó đã đúng 1 câu, `split_sentences()` trả sớm), không tạo trùng lặp gì.
    if tag_speakers_enabled:
        expanded_for_tagging: list[dict] = []
        for seg in segments:
            expanded_for_tagging.extend(split_segment_by_sentences(seg))
        for new_id, seg in enumerate(expanded_for_tagging, start=1):
            seg["id"] = new_id
        segments = expanded_for_tagging

        tag_speakers(base_url, api_key, model, segments, translate_context,
                     pipeline_id=pipeline_id, video_name=video_name, usage=usage)

    # Mô tả video xuất ra output — tóm tắt lại từ bản ĐÃ DỊCH (tiếng Việt), sau khi review xong
    # (segments["text"] lúc này là bản dịch cuối cùng, đã qua rà soát ngữ cảnh).
    video_context = describe_translated_video(base_url, api_key, model, segments, usage)
    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                 "event": "translate_video_description", "video_context": video_context})
    q.log_event({"pipeline_id": pipeline_id, "video_name": video_name,
                 "event": "translate_usage_summary", **usage.summary()})

    expanded_segments: list[dict] = []
    for seg in segments:
        expanded_segments.extend(split_segment_by_sentences(seg))
    for new_id, seg in enumerate(expanded_segments, start=1):
        seg["id"] = new_id
    segments = expanded_segments

    output_p.write_text(
        json.dumps({
            "language": target_lang,
            "duration_s": data.get("duration_s"),
            "segments": segments,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "ok": True,
        "input": str(input_p),
        "output": str(output_p),
        "segments": len(segments),
        "elapsed_s": round(time.time() - start, 2),
        # Tóm tắt bối cảnh video (xem summarize_video_context) — vốn đã tính sẵn để dịch context-
        # aware, trả kèm ra đây làm sản phẩm phụ: dùng thẳng làm mô tả video khi đăng bài.
        "video_context": video_context,
        # Số liệu token thật (xem UsageTracker) — dùng để đánh giá các thay đổi giảm chi phí
        # Deepseek (prompt tiếng Anh, giảm retry thừa ở tầng bisect, Google-polish fallback).
        "usage": usage.summary(),
    }


def main() -> int:
    args = parse_args()

    if not args.api_key:
        print("Lỗi: thiếu Deepseek API key (--api-key hoặc env DEEPSEEK_API_KEY)", file=sys.stderr)
        return 1

    try:
        result = run_translate(
            str(args.input), str(args.output), args.api_key, args.model,
            args.base_url, args.target_lang, args.batch_size,
            tag_speakers_enabled=args.tag_speakers,
        )
    except RuntimeError as e:
        print(f"Lỗi: {e}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
