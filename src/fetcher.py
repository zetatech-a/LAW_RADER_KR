"""HTTP 세션: 브라우저 유사 헤더, 재시도, 한국어 인코딩 자동 처리."""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

# 다수의 한국 정부 사이트는 기본 파이썬 UA 를 차단하므로 브라우저 UA 를 사용한다.
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class Fetcher:
    """모든 스크래퍼가 공유하는 requests 세션 래퍼."""

    def __init__(self, timeout: float = 30.0, delay: float = 1.0):
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "application/json;q=0.8,*/*;q=0.7",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            }
        )
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _sleep(self) -> None:
        if self.delay:
            time.sleep(self.delay)

    def get(self, url: str, *, referer: Optional[str] = None, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if referer:
            headers.setdefault("Referer", referer)
        resp = self.session.get(url, timeout=self.timeout, headers=headers, **kwargs)
        resp.raise_for_status()
        self._sleep()
        return resp

    def post(self, url: str, *, referer: Optional[str] = None, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", {})
        if referer:
            headers.setdefault("Referer", referer)
        resp = self.session.post(url, timeout=self.timeout, headers=headers, **kwargs)
        resp.raise_for_status()
        self._sleep()
        return resp

    @staticmethod
    def text(resp: requests.Response) -> str:
        """응답을 올바른 인코딩으로 디코딩한 텍스트. EUC-KR/UTF-8 자동 감지."""
        # 서버가 charset 을 명시하지 않으면 requests 는 ISO-8859-1 로 잘못 추정하므로 보정.
        if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text

    def download(self, url: str, *, referer: Optional[str] = None) -> bytes:
        resp = self.get(url, referer=referer, stream=True)
        return resp.content
