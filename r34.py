from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("r34")

API_URL = "https://api.rule34.xxx/index.php"
UA = "Mozilla/5.0 (compatible; personal-tg-downloader/1.0)"
PAGE_SIZE = 100

IMAGE_EXT = {"jpg", "jpeg", "png", "webp", "bmp"}
ANIM_EXT = {"gif"}
VIDEO_EXT = {"mp4", "webm", "mov", "mkv", "avi"}

# Жёсткий фильтр. Не отключается настройками.
BANNED_TAGS = {
    "loli", "lolicon", "oppai_loli", "shota", "shotacon", "child", "children",
    "kid", "kids", "kiddo", "toddler", "toddlercon", "baby", "babies", "infant",
    "underage", "minor", "minors", "preteen", "pre_teen", "teen", "teens",
    "teenager", "young", "younger", "cub", "mesugaki", "kodomo", "kindergarten",
    "elementary_school", "middle_school", "little_girl", "little_boy",
    "aged_down", "age_regression",
}

RATING_MAP = {"e": "explicit", "q": "questionable", "s": "safe"}


def norm(tag: str) -> str:
    return tag.strip().lower().lstrip("+-")


def banned_in_query(tags: list[str]) -> list[str]:
    """Запрещённые ПОЛОЖИТЕЛЬНЫЕ теги в запросе (минус-теги — это исключения, они ок)."""
    return [t for t in tags if not t.startswith("-") and norm(t) in BANNED_TAGS]


def post_is_banned(tags: list[str]) -> bool:
    return any(t in BANNED_TAGS for t in tags)


class R34Error(Exception):
    pass


class FileTooBig(R34Error):
    pass


@dataclass(slots=True)
class Post:
    id: int
    file_url: str
    tags: list[str]
    rating: str
    score: int
    width: int
    height: int
    ext: str

    @property
    def page_url(self) -> str:
        return f"https://rule34.xxx/index.php?page=post&s=view&id={self.id}"


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class R34Client:
    def __init__(self, session: aiohttp.ClientSession, user_id: str = "",
                 api_key: str = "", min_interval: float = 1.1, max_file_mb: int = 49):
        self.session = session
        self.user_id = user_id
        self.api_key = api_key
        self.min_interval = min_interval
        self.max_bytes = max_file_mb * 1024 * 1024
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def _throttle(self):
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def search(self, tags: list[str], pid: int = 0, limit: int = PAGE_SIZE) -> list[Post]:
        params = {
            "page": "dapi", "s": "post", "q": "index", "json": "1",
            "tags": " ".join(tags), "limit": str(min(limit, PAGE_SIZE)), "pid": str(pid),
        }
        if self.user_id and self.api_key:
            params["user_id"] = self.user_id
            params["api_key"] = self.api_key

        last_err = "неизвестная ошибка"
        for attempt in range(4):
            await self._throttle()
            try:
                async with self.session.get(
                    API_URL, params=params,
                    headers={"User-Agent": UA},
                    timeout=aiohttp.ClientTimeout(total=35),
                ) as r:
                    if r.status in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {r.status}"
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if r.status in (401, 403):
                        raise R34Error("API отдал 401/403 — нужны R34_USER_ID и R34_API_KEY "
                                       "(rule34.xxx → Account → Options → API Access)")
                    if r.status != 200:
                        raise R34Error(f"API HTTP {r.status}")
                    text = (await r.text()).strip()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = type(e).__name__
                await asyncio.sleep(2 ** attempt)
                continue

            if not text:
                return []
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise R34Error("API вернул не JSON (Cloudflare / лимит / сайт лежит)")

            if isinstance(data, dict):
                data = data.get("posts") or data.get("post") or []
            if not isinstance(data, list):
                return []

            out: list[Post] = []
            for it in data:
                if not isinstance(it, dict):
                    continue
                url = it.get("file_url") or ""
                if not url:
                    continue
                if url.startswith("//"):
                    url = "https:" + url
                path = urlparse(url).path
                ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
                rating = str(it.get("rating", "")).lower()
                out.append(Post(
                    id=_int(it.get("id")),
                    file_url=url,
                    tags=(it.get("tags") or "").split(),
                    rating=RATING_MAP.get(rating, rating),
                    score=_int(it.get("score")),
                    width=_int(it.get("width")),
                    height=_int(it.get("height")),
                    ext=ext,
                ))
            return out

        raise R34Error(f"API недоступен ({last_err})")

    async def fetch(self, url: str) -> bytes:
        try:
            async with self.session.get(
                url, headers={"User-Agent": UA, "Referer": "https://rule34.xxx/"},
                timeout=aiohttp.ClientTimeout(total=180),
            ) as r:
                if r.status != 200:
                    raise R34Error(f"file HTTP {r.status}")
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > self.max_bytes:
                    raise FileTooBig()
                buf = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    buf.extend(chunk)
                    if len(buf) > self.max_bytes:
                        raise FileTooBig()
                return bytes(buf)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise R34Error(type(e).__name__) from e
