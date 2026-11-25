"""Static sites (Next.js, Astro, Hugo...): write a .md/.mdx file with frontmatter, optionally git commit/push."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config
from . import PublishResult, RemoteDoc, slug_from_url


def frontmatter(data: dict[str, Any], style: str = "yaml") -> str:
    if style == "json":
        return "---\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n---\n"
    # JSON scalars are valid YAML, which keeps quoting correct without a YAML dependency
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in data.items()) + "\n---\n"


def split_frontmatter(text: str) -> tuple[str, str]:
    """(frontmatter block without the --- fences, body). No frontmatter gives ("", text)."""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end < 0:
        return "", text
    after = text.find("\n", end + 4)
    return text[3:end].strip("\n"), text[after + 1:] if after >= 0 else ""


def parse_frontmatter(block: str) -> dict[str, Any]:
    if block.lstrip().startswith("{"):
        try:
            return json.loads(block)
        except ValueError:
            return {}
    data: dict[str, Any] = {}
    for line in block.splitlines():
        if ":" not in line or line[:1].isspace():
            continue
        key, raw = line.split(":", 1)
        raw = raw.strip()
        try:
            data[key.strip()] = json.loads(raw)
        except ValueError:
            data[key.strip()] = raw.strip("'\"")
    return data


def set_frontmatter(block: str, values: dict[str, Any]) -> str:
    """Change only the given keys and leave every other line (comments, YAML lists, ordering) as it was."""
    if block.lstrip().startswith("{"):
        data = json.loads(block)
        data.update(values)
        return json.dumps(data, ensure_ascii=False, indent=2)
    lines = block.splitlines()
    for key, value in values.items():
        new = f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for i, line in enumerate(lines):
            if line.split(":", 1)[0].strip() == key and not line[:1].isspace():
                lines[i] = new
                break
        else:
            lines.append(new)
    return "\n".join(lines)


class MarkdownPublisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mc = cfg.publish.markdown
        self.dir = cfg.resolve(self.mc.dir)

    def url_for(self, slug: str) -> str:
        return self.mc.url_pattern.format(site=self.cfg.site.url.rstrip("/"), slug=slug)

    def publish(self, article: dict[str, Any], status: str) -> PublishResult:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{article['slug']}.{self.mc.format}"
        url = self.url_for(article["slug"])
        if path.exists():
            head = path.read_text().split("---", 2)[1] if path.read_text().startswith("---") else ""
            if f'"{article["uid"]}"' in head:
                # a rerun after a failed push: nothing new to commit, but the push may still be owed
                error = self._git(path, article) if self.mc.git_commit else ""
                return PublishResult(str(path), url, status, duplicate=True, git_error=error)
            # same cannibalization guard as the WP plugin: never overwrite someone else's post
            path = self.dir / f"{article['slug']}-{article['uid'][:6]}.{self.mc.format}"
            status = "draft"

        image = article.get("image_url") or ""
        if article.get("image_path") and self.mc.public_dir:
            src = Path(article["image_path"])
            dest_dir = self.cfg.resolve(self.mc.public_dir) / self.mc.image_url_prefix.strip("/")
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_dir / src.name)
            image = src.name if self.mc.image_ref == "filename" else f"{self.mc.image_url_prefix.rstrip('/')}/{src.name}"

        fm = {
            "title": article["h1"],
            "seoTitle": article["meta_title"],
            "description": article["meta_description"],
            "slug": article["slug"],
            "date": datetime.now(timezone.utc).date().isoformat(),
            "keyword": article["keyword"],
            "excerpt": article.get("excerpt") or "",
            "image": image,
            "imageAlt": article.get("image_alt") or "",
            "draft": status != "publish",
            "autopilotId": article["uid"],
            "schema": json.loads(article.get("schema_json") or "[]"),
        }
        fm = {self.mc.fields.get(k, k): v for k, v in fm.items()}
        fm.update({k: v for k, v in self.mc.defaults.items() if fm.get(k) in (None, "")})
        path.write_text(frontmatter(fm, self.mc.frontmatter) + "\n" + article["markdown"])
        error = self._git(path, article) if self.mc.git_commit else ""
        return PublishResult(str(path), url, status, slug_conflict=path.stem != article["slug"], image_url=image,
                             git_error=error)

    # -- refresh an existing post ------------------------------------------
    def find(self, url: str) -> Path:
        slug = slug_from_url(url)
        for ext in (self.mc.format, "md", "mdx"):
            if (self.dir / f"{slug}.{ext}").exists():
                return self.dir / f"{slug}.{ext}"
        for path in sorted(self.dir.rglob("*")) if self.dir.exists() else []:
            if path.suffix in (".md", ".mdx") and (path.stem == slug or (path.stem == "index" and path.parent.name == slug)):
                return path
        raise LookupError(f"No {slug}.md or {slug}.mdx under {self.dir}")

    def _key(self, name: str) -> str:
        return self.mc.fields.get(name, name)

    def fetch(self, url: str) -> RemoteDoc:
        path = self.find(url)
        block, body = split_frontmatter(path.read_text())
        fm = parse_frontmatter(block)
        h1 = str(fm.get(self._key("title")) or "")
        return RemoteDoc(
            remote_id=str(path), url=url, title=str(fm.get(self._key("seoTitle")) or h1),
            description=str(fm.get(self._key("description")) or ""), body=body.lstrip("\n"), format="markdown",
            version=str(path.stat().st_mtime_ns), h1=h1, extra={"path": str(path)},
        )

    def update(self, doc: RemoteDoc, title: str, description: str, body: str) -> PublishResult:
        path = Path(doc.remote_id)
        block, _ = split_frontmatter(path.read_text())
        fm = parse_frontmatter(block)
        # Write the SEO title where the site keeps it; without a seoTitle field the title field is the one that ranks
        title_key = self._key("seoTitle") if self._key("seoTitle") in fm else self._key("title")
        values = {title_key: title, self._key("description"): description}
        values = {k: v for k, v in values.items() if fm.get(k) != v}
        if values:
            block = set_frontmatter(block, values)
        path.write_text(("---\n" + block + "\n---\n\n" if block else "") + body.lstrip("\n"))
        error = self._git(path, {"h1": f"refresh {path.stem}"}) if self.mc.git_commit else ""
        return PublishResult(str(path), doc.url, "publish", git_error=error)

    def _git(self, path: Path, article: dict[str, Any]) -> str:
        """Commit the post, then rebase onto the remote and push. Returns "" or a message saying what to run by hand.

        Add and commit failures still raise: the file is not safely recorded. Pull and push failures are returned,
        because the post is written and committed locally and only needs a push."""
        repo = path.parent

        def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)

        files = [str(path.resolve())]
        if self.mc.public_dir and article.get("image_path"):
            image_dir = self.cfg.resolve(self.mc.public_dir) / self.mc.image_url_prefix.strip("/")
            image = image_dir / Path(article["image_path"]).name
            if image.is_file():
                files.append(str(image))
        run("add", "--", *files)
        if run("diff", "--cached", "--quiet", "--", *files, check=False).returncode != 0:
            run("commit", "--only", "-m", f"blog: {article['h1']}", "--", *files)
        if not self.mc.git_push:
            return ""

        def failed(step: str, proc: subprocess.CompletedProcess[str]) -> str:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            return (f"git {step} failed in {repo}: {detail[-1] if detail else f'exit {proc.returncode}'}. "
                    f"The post is committed locally. Fix it with: git -C {repo} pull --rebase && git -C {repo} push")

        has_upstream = run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).returncode == 0
        if has_upstream:
            pulled = run("pull", "--rebase", "--autostash", check=False)
            if pulled.returncode != 0:
                run("rebase", "--abort", check=False)
                return failed("pull --rebase", pulled)
            if run("rev-list", "--count", "@{u}..HEAD").stdout.strip() == "0":
                return ""
        pushed = run("push", check=False)
        return failed("push", pushed) if pushed.returncode != 0 else ""
