"""Featured image: stock photo APIs (Unsplash/Pexels), OpenAI images, or any local CLI (mflux, ComfyUI, SDXL)."""

from __future__ import annotations

import base64
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import ImageConfig, secret

STYLES = {
    "clean": "clean flat editorial illustration, soft neutral palette, generous negative space",
    "photo": "natural editorial photograph, soft daylight, shallow depth of field, realistic",
    "watercolor": "loose watercolor painting, textured paper, gentle washes of color",
    "3d": "soft 3D render, clay-like materials, studio lighting, pastel colors",
    "vintage": "vintage print illustration, muted colors, halftone texture",
    "abstract": "abstract geometric composition, bold shapes, harmonious colors",
}


@dataclass
class ImageResult:
    alt: str
    url: str = ""  # public URL (stock APIs) when available
    path: str = ""  # local file when generated
    credit: str = ""


def image_prompt(title: str, scene: str, style: str) -> str:
    look = STYLES.get(style, STYLES["photo"])
    subject = scene or f"a scene that illustrates: {title}"
    return f"{subject}. {look}. Wide 16:9 composition. No text, no letters, no logos, no watermarks."


def make_image(cfg: ImageConfig, client: httpx.Client, *, title: str, keyword: str, scene: str, style: str,
               out_dir: Path, slug: str) -> ImageResult | None:
    provider = cfg.provider.lower()
    alt = title
    if provider in ("none", "site"):
        return None
    if provider == "unsplash":
        r = client.get(
            "https://api.unsplash.com/search/photos",
            params={"query": keyword, "orientation": "landscape", "per_page": 1, "content_filter": "high"},
            headers={"Authorization": f"Client-ID {secret(cfg.api_key_env or 'UNSPLASH_ACCESS_KEY')}"},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return None
        photo = results[0]
        # Unsplash API guidelines: trigger the download endpoint when a photo is used
        if photo.get("links", {}).get("download_location"):
            client.get(photo["links"]["download_location"],
                       headers={"Authorization": f"Client-ID {secret(cfg.api_key_env or 'UNSPLASH_ACCESS_KEY')}"})
        user = photo.get("user", {})
        return ImageResult(alt=photo.get("alt_description") or alt, url=photo["urls"]["regular"],
                           credit=f"Photo by {user.get('name', '')} on Unsplash")
    if provider == "pexels":
        r = client.get(
            "https://api.pexels.com/v1/search",
            params={"query": keyword, "orientation": "landscape", "per_page": 1},
            headers={"Authorization": secret(cfg.api_key_env or "PEXELS_API_KEY")},
        )
        r.raise_for_status()
        photos = r.json().get("photos", [])
        if not photos:
            return None
        p = photos[0]
        return ImageResult(alt=p.get("alt") or alt, url=p["src"]["large2x"], credit=f"Photo by {p.get('photographer', '')} on Pexels")

    prompt = image_prompt(title, scene, style)
    out_dir.mkdir(parents=True, exist_ok=True)
    if provider == "openai":
        r = client.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {secret(cfg.api_key_env or 'OPENAI_API_KEY')}"},
            json={"model": cfg.model, "prompt": prompt, "size": "1536x1024", "n": 1},
            timeout=180,
        )
        r.raise_for_status()
        item = r.json()["data"][0]
        path = out_dir / f"{slug}.png"
        if item.get("b64_json"):
            path.write_bytes(base64.b64decode(item["b64_json"]))
        else:
            path.write_bytes(client.get(item["url"]).content)
        return ImageResult(alt=alt, path=str(path))
    if provider == "command":
        path = out_dir / f"{slug}.png"
        cmd = cfg.command.format(prompt=shlex.quote(prompt), output=shlex.quote(str(path)))
        subprocess.run(cmd, shell=True, check=True, timeout=900)
        if not path.exists():
            raise RuntimeError(f"Image command finished but {path} was not created")
        return ImageResult(alt=alt, path=str(path))
    raise ValueError(f"Unknown image provider '{cfg.provider}'")
