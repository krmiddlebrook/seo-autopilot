"""Signed-in agent CLIs as the model: Claude Code (`claude -p`), Codex with ChatGPT (`codex exec`), Gemini (`gemini`).

Each call is text in, text out. The CLI runs in an empty temp directory with its tools off (Claude) or a read-only
sandbox (Codex, Gemini), so it answers like an API call and cannot touch the site. Agents are tried in order; one that
fails (not installed, signed out, out of quota, timed out) is skipped for the rest of the run and the next one answers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable

from .config import LLMConfig

KNOWN_AGENTS = ("claude", "codex", "gemini")
# launchd and cron start jobs with a bare PATH, so look where these CLIs usually install too.
EXTRA_BIN_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "~/.bun/bin")
# API keys in the site's .env would make a CLI bill the API instead of the signed-in subscription.
STRIPPED_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY",
                "GEMINI_API_KEY", "GOOGLE_API_KEY")
GEMINI_INSTRUCTION = "Answer the request given on stdin. Reply with the answer only."

Runner = Callable[..., subprocess.CompletedProcess]


def find_agent(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN_DIRS:
        candidate = Path(d).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def agent_command(name: str, exe: str, system: str, prompt: str, model: str,
                  workdir: Path) -> tuple[list[str], str, Path | None]:
    """argv, stdin text, and the file holding the answer (None = stdout) for one agent call."""
    if name == "claude":
        argv = [exe, "-p", "--output-format", "text", "--tools", "", "--system-prompt", system,
                "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"]
        if model:
            argv += ["--model", model]
        return argv, prompt, None
    combined = f"{system}\n\n---\n\n{prompt}"  # neither CLI takes a separate system prompt
    if name == "codex":
        out = workdir / "last-message.txt"
        argv = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--color", "never",
                "-C", str(workdir), "-o", str(out)]
        if model:
            argv += ["-m", model]
        return argv + ["-"], combined, out
    if name == "gemini":
        argv = [exe, "-o", "text", "--approval-mode", "default"]
        if model:
            argv += ["-m", model]
        return argv + [GEMINI_INSTRUCTION], combined, None
    raise ValueError(f"Unknown agent '{name}' (known: {', '.join(KNOWN_AGENTS)})")


def _tail(text: str, n: int = 300) -> str:
    text = " ".join((text or "").split())
    return text[-n:] if text else "no output"


class AgentCLILLM:
    """LLM provider "agent": the first signed-in agent CLI in `[llm] agents` that answers."""

    def __init__(self, cfg: LLMConfig, runner: Runner = subprocess.run,
                 finder: Callable[[str], str | None] = find_agent):
        self.cfg = cfg
        self.order = [a.strip().lower() for a in (cfg.agents or KNOWN_AGENTS) if a.strip()]
        unknown = [a for a in self.order if a not in KNOWN_AGENTS]
        if unknown:
            raise ValueError(f"Unknown agent(s) {unknown} in [llm] agents (known: {', '.join(KNOWN_AGENTS)})")
        if not self.order:
            raise ValueError("[llm] agents is empty")
        self.runner, self.finder = runner, finder
        self.down: dict[str, str] = {}  # agent -> why it was dropped for the rest of this run
        self.usage: Counter[str] = Counter()

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
        extra = [str(Path(d).expanduser()) for d in EXTRA_BIN_DIRS]
        env["PATH"] = os.pathsep.join([env.get("PATH", ""), *extra]).strip(os.pathsep)
        return env

    def call(self, name: str, system: str, prompt: str) -> str:
        """One call to one agent; raises with a short reason on any failure."""
        exe = self.finder(name)
        if not exe:
            raise RuntimeError("not installed")
        model = (self.cfg.agent_models or {}).get(name, "")
        with tempfile.TemporaryDirectory(prefix="autopilot-agent-") as tmp:
            argv, stdin, out = agent_command(name, exe, system, prompt, model, Path(tmp))
            try:
                proc = self.runner(argv, input=stdin, capture_output=True, text=True,
                                   timeout=self.cfg.timeout, cwd=tmp, env=self._env())
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"timed out after {self.cfg.timeout:.0f}s") from None
            if proc.returncode != 0:
                raise RuntimeError(f"exited {proc.returncode}: {_tail(proc.stderr or proc.stdout)}")
            text = out.read_text() if out is not None and out.exists() else proc.stdout
        text = (text or "").strip()
        if not text:
            raise RuntimeError("returned an empty answer")
        return text

    def complete(self, system: str, prompt: str, json_mode: bool = False, max_tokens: int | None = None) -> str:
        # max_tokens has no CLI equivalent; prompts already ask for the length they want.
        if json_mode:
            system += "\nRespond with a single valid JSON value and nothing else: no code fences, no commentary."
        errors = []
        for name in self.order:
            if name in self.down:
                errors.append(f"{name}: {self.down[name]}")
                continue
            try:
                text = self.call(name, system, prompt)
            except Exception as exc:
                self.down[name] = str(exc)
                errors.append(f"{name}: {exc}")
                continue
            self.usage[name] += 1
            return text
        raise RuntimeError("No signed-in agent answered. " + "; ".join(errors))

    def probe(self) -> list[tuple[str, bool, str]]:
        """Try each agent once with a tiny prompt: (agent, usable, detail)."""
        rows = []
        for name in self.order:
            try:
                rows.append((name, True, self.call(name, "Reply with OK.", "Say OK.")[:40]))
            except Exception as exc:
                rows.append((name, False, str(exc)))
        return rows

    def summary(self) -> str:
        used = ", ".join(f"{n} ({c} call{'s' if c != 1 else ''})" for n, c in self.usage.items()) or "none"
        text = f"Agents used: {used}"
        if self.down:
            text += ". Skipped: " + "; ".join(f"{n}: {why}" for n, why in self.down.items())
        return text
