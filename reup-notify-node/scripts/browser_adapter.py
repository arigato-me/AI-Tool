"""1 driver Playwright dùng chung cho WhatsApp Web/Zalo Web/Messenger web — cá nhân, unofficial,
thay vì 3 lib reverse-engineer riêng (Baileys/zca-js/fbchat, đa số kém bảo trì + cần thêm
runtime Node.js). storage_state (cookie+localStorage) đóng vai trò "cookie đăng nhập" — nạp vào
context lúc gửi để bỏ qua login, tự ghi lại sau mỗi lần chạy để cookie xoay vòng tự refresh,
cùng nguyên lý cookiejar write-back của yt-dlp (xem reup-ytdlp-node/scripts/ytdlp_runner.py).

CẢNH BÁO selector: các CSS selector/role locator bên dưới là best-guess tại thời điểm viết —
DOM thật của web.whatsapp.com/chat.zalo.me/messenger.com đổi theo thời gian (đặc biệt
messenger.com, xem README mục "Facebook Messenger rủi ro"). Verify lại bằng
`docker compose run --rm worker cli browser-login <platform>` (chụp screenshot) trước khi tin
tưởng send() chạy đúng lần đầu.
"""
from __future__ import annotations

import getpass
import os
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

SESSION_DIR = Path("/config/browser_sessions")
TARGETS_PATH = "/config/browser_targets.yaml"

# Bug thật gặp lúc test: Playwright Chromium tự report UA "HeadlessChrome/..." — WhatsApp Web
# chặn cứng UA đó ("works with Google Chrome 100+"), dù bản Chromium thật (151) mới hơn 100
# rất nhiều — chặn theo chuỗi "Headless", không theo version. Giả UA desktop Chrome thật để
# qua được, không liên quan gì đến bot-detection nâng cao (chỉ là UA sniff đơn giản).
DESKTOP_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PLATFORM_CONFIGS = {
    "whatsapp": {
        "url": "https://web.whatsapp.com",
        "needs_credentials": False,
        "logged_in_selector": "div#pane-side",  # danh sách chat bên trái, xuất hiện khi đã login
        "message_input_selector": "div[contenteditable='true'][data-tab='10']",
        "send_button_selector": "button[data-tab='11']",
    },
    "zalo": {
        "url": "https://chat.zalo.me",
        "needs_credentials": False,
        "logged_in_selector": "div.conv-list, div[class*='conversation-list']",
        "message_input_selector": "div[contenteditable='true']",
        "send_button_selector": "button[aria-label='Gửi' i]",
    },
    "messenger": {
        "url": "https://www.messenger.com",
        "needs_credentials": True,
        "email_selector": "#email",
        "password_selector": "#pass",
        "login_button_selector": "#loginbutton",
        "logged_in_selector": "div[aria-label='Thread list' i], div[aria-label='Danh sách cuộc trò chuyện' i]",
        "message_input_selector": "div[aria-label='Message' i], div[aria-label='Tin nhắn' i]",
        "send_button_selector": "div[aria-label='Press enter to send' i]",
    },
}


def _session_path(platform: str) -> Path:
    return SESSION_DIR / f"{platform}.json"


def _load_targets() -> dict:
    if not os.path.exists(TARGETS_PATH):
        return {}
    with open(TARGETS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _thread_url(platform: str, cfg: dict, targets: dict) -> str:
    target = targets.get(platform) or {}
    if platform == "whatsapp":
        phone = target.get("phone")
        if not phone:
            raise RuntimeError("thiếu 'phone' cho whatsapp trong browser_targets.yaml")
        return f"https://web.whatsapp.com/send?phone={phone}"
    if platform == "messenger":
        thread_url = target.get("thread_url")
        if not thread_url:
            raise RuntimeError("thiếu 'thread_url' cho messenger trong browser_targets.yaml")
        return thread_url
    return cfg["url"]  # zalo: vào trang chủ rồi tự search contact bên dưới


def send(platform: str, message: str, file_path: str | None = None) -> dict:
    cfg = PLATFORM_CONFIGS.get(platform)
    if cfg is None:
        raise RuntimeError(f"platform không hỗ trợ: {platform!r}")
    session_path = _session_path(platform)
    if not session_path.exists():
        raise RuntimeError(
            f"chưa đăng nhập {platform} — chạy: docker compose run --rm worker cli browser-login {platform}"
        )

    targets = _load_targets()
    url = _thread_url(platform, cfg, targets)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(storage_state=str(session_path), user_agent=DESKTOP_UA)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(cfg["logged_in_selector"], timeout=20000)

            if platform == "zalo":
                contact_name = (targets.get("zalo") or {}).get("contact_name")
                if not contact_name:
                    raise RuntimeError("thiếu 'contact_name' cho zalo trong browser_targets.yaml")
                page.get_by_role("textbox", name="Tìm kiếm").fill(contact_name)
                page.get_by_text(contact_name, exact=False).first.click()

            input_box = page.locator(cfg["message_input_selector"]).first
            input_box.click()
            input_box.fill(message)

            if file_path:
                if not os.path.exists(file_path):
                    raise RuntimeError(f"file_path không tồn tại trong container: {file_path}")
                file_input = page.locator("input[type='file']").first
                file_input.set_input_files(file_path)
                time.sleep(2)  # chờ preview file render trước khi bấm gửi

            page.locator(cfg["send_button_selector"]).first.click()
            time.sleep(2)  # chờ request gửi thật đi trước khi đóng context
            return {"ok": True}
        finally:
            context.storage_state(path=str(session_path))  # tự refresh session dù thành công hay lỗi
            context.close()
            browser.close()


def interactive_login(platform: str, timeout_s: int = 300) -> None:
    """`docker compose run --rm worker cli browser-login <platform>` — bootstrap 1 lần trên máy
    remote chỉ có SSH (không màn hình): headless + chụp screenshot định kỳ để user quét QR qua
    ảnh scp về, không cần X11/VNC. Xem README mục "Bootstrap đăng nhập lần đầu"."""
    cfg = PLATFORM_CONFIGS.get(platform)
    if cfg is None:
        raise RuntimeError(f"platform không hỗ trợ: {platform!r}")
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = SESSION_DIR / f"{platform}_login.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = browser.new_context(user_agent=DESKTOP_UA)
        page = context.new_page()
        page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)

        if cfg["needs_credentials"]:
            email = input(f"Email/số điện thoại {platform}: ")
            password = getpass.getpass(f"Mật khẩu {platform}: ")
            page.locator(cfg["email_selector"]).fill(email)
            page.locator(cfg["password_selector"]).fill(password)
            page.locator(cfg["login_button_selector"]).click()

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            page.screenshot(path=str(screenshot_path))
            print(f"[browser-login] đã chụp {screenshot_path} — xem qua scp rồi quét QR nếu cần")
            try:
                page.wait_for_selector(cfg["logged_in_selector"], timeout=5000)
                context.storage_state(path=str(_session_path(platform)))
                print(f"[browser-login] {platform} đăng nhập thành công, đã lưu session")
                context.close()
                browser.close()
                return
            except Exception:
                continue  # chưa login xong (QR chưa quét/còn đang load) — chụp lại, thử tiếp

        context.close()
        browser.close()
        raise RuntimeError(
            f"login {platform} timeout sau {timeout_s}s — xem ảnh cuối tại {screenshot_path}. "
            f"Nếu có checkpoint/2FA cần thao tác thêm, chạy lại lệnh với --timeout-s lớn hơn."
        )
