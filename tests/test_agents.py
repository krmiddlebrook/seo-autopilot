"""Provider "agent": signed-in agent CLIs with fallback. A fake runner stands in for the CLIs."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autopilot.agents import AgentCLILLM, agent_command
from autopilot.config import LLMConfig
from autopilot.llm import complete_json, make_llm


class FakeRunner:
    """Answers per agent: a string (stdout), an int (exit code), or "timeout". Records every call."""

    def __init__(self, answers: dict[str, object]):
        self.answers, self.calls = answers, []

    def __call__(self, argv, input, capture_output, text, timeout, cwd, env):
        name = Path(argv[0]).name
        self.calls.append({"name": name, "argv": argv, "stdin": input, "cwd": cwd, "env": env})
        answer = self.answers[name]
        if answer == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if isinstance(answer, int):
            return subprocess.CompletedProcess(argv, answer, "", "Not logged in. Please run /login")
        if name == "codex":  # codex writes its answer to the -o file, stdout is the event log
            Path(argv[argv.index("-o") + 1]).write_text(str(answer))
            return subprocess.CompletedProcess(argv, 0, "[codex] thinking...", "")
        return subprocess.CompletedProcess(argv, 0, str(answer), "")


def make(answers, agents=("claude", "codex", "gemini"), installed=None, **kw):
    runner = FakeRunner(answers)
    installed = set(answers) if installed is None else set(installed)
    llm = AgentCLILLM(LLMConfig(provider="agent", agents=list(agents), **kw), runner=runner,
                      finder=lambda n: f"/bin/{n}" if n in installed else None)
    return llm, runner


def test_first_signed_in_agent_answers():
    llm, runner = make({"claude": "from claude", "codex": "from codex", "gemini": "from gemini"})
    assert llm.complete("sys", "hello") == "from claude"
    assert [c["name"] for c in runner.calls] == ["claude"]
    assert llm.usage["claude"] == 1


def test_falls_back_and_skips_failed_agent_for_rest_of_run():
    llm, runner = make({"claude": 1, "codex": "from codex", "gemini": "from gemini"})
    assert llm.complete("sys", "one") == "from codex"
    assert llm.complete("sys", "two") == "from codex"
    assert [c["name"] for c in runner.calls] == ["claude", "codex", "codex"]
    assert "Not logged in" in llm.down["claude"]
    summary = llm.summary()
    assert "codex (2 calls)" in summary and "Skipped: claude" in summary


def test_missing_cli_and_timeout_fall_through():
    llm, runner = make({"codex": "timeout", "gemini": "from gemini"}, installed={"codex", "gemini"})
    assert llm.complete("sys", "hi") == "from gemini"
    assert llm.down["claude"] == "not installed"
    assert "timed out" in llm.down["codex"]


def test_all_agents_failing_raises_with_every_reason():
    llm, _ = make({"claude": 1, "codex": 1}, agents=("claude", "codex"))
    with pytest.raises(RuntimeError) as err:
        llm.complete("sys", "hi")
    assert "No signed-in agent answered" in str(err.value)
    assert "claude:" in str(err.value) and "codex:" in str(err.value)


def test_empty_answer_counts_as_failure():
    llm, _ = make({"claude": "   ", "codex": "ok"})
    assert llm.complete("sys", "hi") == "ok"
    assert "empty" in llm.down["claude"]


def test_json_mode_through_agent():
    llm, _ = make({"claude": '{"a": 1}'})
    assert complete_json(llm, "sys", "give json") == {"a": 1}


def test_api_keys_are_stripped_and_cwd_is_empty_temp_dir(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    monkeypatch.setenv("KEEP_ME", "1")
    llm, runner = make({"claude": "ok"})
    llm.complete("sys", "hi")
    call = runner.calls[0]
    assert "ANTHROPIC_API_KEY" not in call["env"] and "OPENAI_API_KEY" not in call["env"]
    assert call["env"]["KEEP_ME"] == "1"
    assert "autopilot-agent-" in call["cwd"]


def test_commands_disable_tools_and_pass_prompts(tmp_path):
    argv, stdin, out = agent_command("claude", "claude", "SYS", "PROMPT", "", tmp_path)
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--system-prompt") + 1] == "SYS"
    assert stdin == "PROMPT" and out is None
    argv, stdin, out = agent_command("codex", "codex", "SYS", "PROMPT", "gpt-x", tmp_path)
    assert argv[argv.index("--sandbox") + 1] == "read-only" and argv[-1] == "-"
    assert argv[argv.index("-m") + 1] == "gpt-x" and out == tmp_path / "last-message.txt"
    assert stdin.startswith("SYS") and stdin.endswith("PROMPT")
    argv, stdin, _ = agent_command("gemini", "gemini", "SYS", "PROMPT", "", tmp_path)
    assert argv[argv.index("--approval-mode") + 1] == "default" and "PROMPT" in stdin


def test_agent_models_are_passed_per_agent():
    llm, runner = make({"claude": "ok"}, agent_models={"claude": "claude-sonnet-5-5"})
    llm.complete("sys", "hi")
    argv = runner.calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"


def test_probe_reports_each_agent():
    llm, _ = make({"claude": 1, "codex": "OK", "gemini": "OK"})
    rows = llm.probe()
    assert [(n, ok) for n, ok, _ in rows] == [("claude", False), ("codex", True), ("gemini", True)]


def test_unknown_agent_rejected_and_make_llm_builds_agent():
    with pytest.raises(ValueError):
        AgentCLILLM(LLMConfig(provider="agent", agents=["cursor"]))
    assert isinstance(make_llm(LLMConfig(provider="agent"), client=None), AgentCLILLM)
