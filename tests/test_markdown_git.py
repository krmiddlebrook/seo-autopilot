"""Markdown publisher git handling against real temporary repos: a remote that moved on, a rejected push, reruns."""

import subprocess
from pathlib import Path

import pytest

from autopilot import pipeline
from autopilot.publish.markdown import MarkdownPublisher

from test_publish import ARTICLE


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """A bare 'GitHub' remote, the site clone autopilot writes into, and a second clone standing in for Replit."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    for k, v in {"GIT_CONFIG_GLOBAL": str(empty), "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
                 "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}.items():
        monkeypatch.setenv(k, v)
    remote, site, other = tmp_path / "remote.git", tmp_path / "site", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(site)], check=True)
    (site / "README.md").write_text("site\n")
    git(site, "add", "README.md")
    git(site, "commit", "-qm", "init")
    git(site, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    return remote, site, other


def remote_move_on(other: Path, name: str = "replit.txt", text: str = "edit from replit\n") -> None:
    git(other, "pull", "-q")
    (other / name).parent.mkdir(parents=True, exist_ok=True)
    (other / name).write_text(text)
    git(other, "add", "-A")
    git(other, "commit", "-qm", f"replit: {name}")
    git(other, "push", "-q")


def reject_pushes(remote: Path, on: bool = True) -> None:
    hook = remote / "hooks" / "pre-receive"
    if on:
        hook.write_text("#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n")
        hook.chmod(0o755)
    else:
        hook.unlink()


@pytest.fixture
def pub(cfg, repos):
    _, site, _ = repos
    m = cfg.publish.markdown
    m.dir, m.git_commit, m.git_push = str(site / "content"), True, True
    return MarkdownPublisher(cfg)


def test_rebases_onto_a_remote_that_moved_on(pub, repos):
    remote, site, other = repos
    remote_move_on(other)
    res = pub.publish(ARTICLE, "publish")
    assert res.git_error == ""
    log = git(remote, "log", "--format=%s", "main")
    assert log.splitlines()[:2] == ["blog: Gutter Cleaning Cost", "replit: replit.txt"]
    assert git(site, "status", "--porcelain") == ""


def test_failed_push_is_reported_not_raised(pub, repos):
    remote, site, _ = repos
    reject_pushes(remote)
    res = pub.publish(ARTICLE, "publish")
    assert "push failed" in res.git_error and "git -C" in res.git_error
    assert git(site, "log", "-1", "--format=%s") == "blog: Gutter Cleaning Cost"
    assert git(remote, "log", "-1", "--format=%s", "main") == "init"


def test_rerun_retries_push_without_an_empty_commit(pub, repos):
    remote, site, _ = repos
    reject_pushes(remote)
    pub.publish(ARTICLE, "publish")
    reject_pushes(remote, on=False)
    commits = git(site, "rev-list", "--count", "HEAD")
    res = pub.publish(ARTICLE, "publish")
    assert res.duplicate and res.git_error == ""
    assert git(site, "rev-list", "--count", "HEAD") == commits
    assert git(remote, "log", "-1", "--format=%s", "main") == "blog: Gutter Cleaning Cost"
    # and once everything is pushed, another rerun is a quiet no-op
    again = pub.publish(ARTICLE, "publish")
    assert again.duplicate and again.git_error == ""
    assert git(site, "rev-list", "--count", "HEAD") == commits


def test_rebase_conflict_is_aborted_and_reported(pub, repos):
    remote, site, other = repos
    remote_move_on(other, "content/gutter-cleaning-cost.mdx", "someone else's post\n")
    res = pub.publish(ARTICLE, "publish")
    assert "pull --rebase" in res.git_error
    assert not (site / ".git" / "rebase-merge").exists() and not (site / ".git" / "rebase-apply").exists()
    assert git(site, "log", "-1", "--format=%s") == "blog: Gutter Cleaning Cost"


def test_pipeline_marks_published_and_logs_push_failure(cfg, db, client, pub, repos):
    remote, _, _ = repos
    reject_pushes(remote)
    cfg.publish.target = "markdown"
    cfg.indexnow.enabled = False
    article_id = db.insert_article({**ARTICLE, "status": "pending_review"})
    lines: list[str] = []
    res = pipeline.publish_article(cfg, db, client, article_id, log=lines.append)
    assert res is not None and res.git_error
    assert db.article(article_id)["status"] == "published"
    assert any("push failed" in line for line in lines)


def test_publishes_only_the_post_and_its_image(pub, repos, tmp_path):
    remote, site, _ = repos
    public = site / "public"
    public.mkdir()
    (public / "local.csv").write_text("private data")
    pub.dir.mkdir()
    (pub.dir / "draft.md").write_text("unfinished")
    (site / "unrelated.txt").write_text("already staged")
    git(site, "add", "unrelated.txt")
    image = tmp_path / "cover.png"
    image.write_bytes(b"image")
    pub.mc.public_dir = str(public)
    pub.publish({**ARTICLE, "image_path": str(image)}, "publish")
    assert git(remote, "diff-tree", "--no-commit-id", "--name-only", "-r", "main").splitlines() == [
        "content/gutter-cleaning-cost.mdx", "public/images/blog/cover.png"]
    assert git(site, "diff", "--cached", "--name-only") == "unrelated.txt"
