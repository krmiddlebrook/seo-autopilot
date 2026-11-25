"""IndexNow: one POST notifies Bing, Yandex, Naver and Seznam. (Google does not use IndexNow.)"""

from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlparse

import httpx

DB_KEY = "indexnow"


def new_key() -> str:
    return secrets.token_hex(16)


def write_key_file(public_dir: Path, key: str) -> Path:
    public_dir.mkdir(parents=True, exist_ok=True)
    path = public_dir / f"{key}.txt"
    path.write_text(key)
    return path


def payload(urls: list[str], key: str, key_location: str = "") -> dict[str, object]:
    host = urlparse(urls[0]).netloc
    body: dict[str, object] = {"host": host, "key": key, "urlList": urls}
    body["keyLocation"] = key_location or f"https://{host}/{key}.txt"
    return body


def submit(client: httpx.Client, urls: list[str], key: str, key_location: str = "",
           endpoint: str = "https://api.indexnow.org/indexnow") -> int:
    if not urls:
        return 0
    r = client.post(endpoint, json=payload(urls, key, key_location),
                    headers={"Content-Type": "application/json; charset=utf-8"})
    # 200 OK and 202 Accepted are both success; 422 means the URL host doesn't match the key
    if r.status_code not in (200, 202):
        raise RuntimeError(f"IndexNow rejected submission: HTTP {r.status_code} {r.text[:200]}")
    return r.status_code
