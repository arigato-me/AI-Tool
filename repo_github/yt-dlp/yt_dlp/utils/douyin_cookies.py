"""Auto-generate minimal Douyin web cookies without a browser.

Based on TokenManager / VerifyFpManager from
https://github.com/Evil0ctal/Douyin_TikTok_Download_API
"""

from __future__ import annotations

import http.cookiejar
import random
import secrets
import time
import urllib.request

__all__ = ['fetch_douyin_cookies', 'gen_s_v_web_id']

_TTWID_URL = 'https://ttwid.bytedance.com/ttwid/union/register/'
_TTWID_DATA = (
    b'{"region":"cn","aid":1768,"needFid":false,"service":"www.ixigua.com",'
    b'"migrate_info":{"ticket":"","source":"node"},"cbUrlProtocol":"https","union":true}'
)
_DOUYIN_HOME = 'https://www.douyin.com/'
_DEFAULT_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')


def gen_s_v_web_id() -> str:
    base_str = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
    t = len(base_str)
    ms = int(round(time.time() * 1000))
    base36 = ''
    while ms > 0:
        remainder = ms % 36
        base36 = (str(remainder) if remainder < 10 else chr(ord('a') + remainder - 10)) + base36
        ms //= 36
    parts = [''] * 36
    parts[8] = parts[13] = parts[18] = parts[23] = '_'
    parts[14] = '4'
    for i in range(36):
        if not parts[i]:
            n = int(random.random() * t)
            if i == 19:
                n = 3 & n | 8
            parts[i] = base_str[n]
    return f'verify_{base36}_' + ''.join(parts)


def _fetch_ttwid(user_agent: str = _DEFAULT_UA) -> str | None:
    request = urllib.request.Request(
        _TTWID_URL, data=_TTWID_DATA, method='POST',
        headers={'Content-Type': 'application/json', 'User-Agent': user_agent})
    with urllib.request.urlopen(request, timeout=15) as response:
        for part in response.headers.get_all('Set-Cookie') or []:
            if part.startswith('ttwid='):
                return part.split(';', 1)[0].split('=', 1)[1]
    return None


def _fetch_ac_nonce(user_agent: str = _DEFAULT_UA) -> str | None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(urllib.request.Request(_DOUYIN_HOME, headers={'User-Agent': user_agent}), timeout=15)
    for cookie in jar:
        if cookie.name == '__ac_nonce':
            return cookie.value
    return None


def fetch_douyin_cookies(user_agent: str = _DEFAULT_UA) -> dict[str, str]:
    cookies = {
        'ttwid': _fetch_ttwid(user_agent),
        's_v_web_id': gen_s_v_web_id(),
        '__ac_nonce': _fetch_ac_nonce(user_agent),
        'odin_tt': secrets.token_hex(80),
    }
    return {k: v for k, v in cookies.items() if v}
