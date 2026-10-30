"""Agent-pane switcher: which entries can start, and what switching writes."""
import json
import shutil
from pathlib import Path

import pytest

from redline import agentpick
from redline.model import RedlineError
from redline.state import Project

FIX = Path(__file__).parent / "fixtures" / "mini.tex"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    shutil.copy(FIX, tmp_path / "paper.tex")
    monkeypatch.setenv("REDLINE_ROOT", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr(agentpick.shutil, "which", lambda exe: "/bin/" + exe)
    monkeypatch.setattr(agentpick, "_opencode_models",
                        lambda proj, refresh=False: {"deepseek/deepseek-flash", "ollama/qwen3.8"})
    monkeypatch.setattr(agentpick, "_local_endpoint", lambda provider: None)
    p = Project.open()
    (tmp_path / "opencode.json").write_text(json.dumps({"mcp": {"mine": {"type": "local"}}}))
    return p


def test_listing_flags_models_opencode_does_not_have(proj):
    items = {it["name"]: it for it in agentpick.listing(proj)}
    assert items["claude"]["problem"] == "" and items["deepseek"]["problem"] == ""
    assert "openai/gpt-6-sol is not set up" in items["gpt"]["problem"]
    assert not any(it["current"] for it in items.values())


def test_switching_to_opencode_writes_its_model_and_keeps_other_config(proj):
    msg = agentpick.switch(proj, "deepseek")
    assert "next `redline open`" in msg  # no tmux here
    data = json.loads((proj.cfg.root / "opencode.json").read_text())
    assert data["model"] == "deepseek/deepseek-flash#high"
    assert data["provider"]["deepseek"]["api"] == "https://api.deepseek.com"
    assert "mine" in data["mcp"] and "redline" in data["mcp"]
    assert "redline" in json.loads((proj.cfg.root / ".mcp.json").read_text())["mcpServers"]
    assert agentpick.current(proj) == "deepseek"
    assert agentpick.command(proj) == "opencode --standalone"


def test_unavailable_entries_are_refused(proj):
    with pytest.raises(RedlineError, match="not set up"):
        agentpick.switch(proj, "gpt")
    assert agentpick.current(proj) is None and agentpick.command(proj) == proj.cfg.agent


def test_unreachable_local_model(proj, monkeypatch):
    monkeypatch.setattr(agentpick, "_local_endpoint", lambda provider: "http://192.0.2.1:9/v1")
    monkeypatch.setattr(agentpick.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    item = next(it for it in agentpick.listing(proj) if it["name"] == "qwen-local")
    assert "not reachable" in item["problem"]
