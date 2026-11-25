"""A binary SQLite file must be refused even if it was deleted before pushing."""

import os
from pathlib import Path
import sqlite3
import subprocess


def test_private_binary_in_intermediate_commit_is_refused(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    hook = project / ".githooks" / "pre-push"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Content scanning is a separate check. This stub accepts every diff.
    binary = tmp_path / "gitleaks"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    def push_check(base, tip):
        return subprocess.run([str(hook)], cwd=tmp_path, text=True, capture_output=True,
                              input=f"refs/heads/test {tip} refs/heads/test {base}\n")

    git("init", "-q")
    git("config", "user.name", "Example")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / ".gitignore").write_text((project / ".gitignore").read_text())
    git("add", ".gitignore")
    git("commit", "-qm", "source only")
    clean = git("rev-parse", "HEAD")
    assert push_check("0" * len(clean), clean).returncode == 0

    with sqlite3.connect(tmp_path / "autopilot.db") as db:
        db.execute("create table customers(name text)")
    git("add", "-f", "autopilot.db")
    git("commit", "-qm", "private database")
    git("rm", "autopilot.db")
    git("commit", "-qm", "delete database")
    refused = push_check(clean, git("rev-parse", "HEAD"))
    assert refused.returncode == 1 and "autopilot.db" in refused.stderr
