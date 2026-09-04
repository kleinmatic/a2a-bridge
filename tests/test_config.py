"""Config mistakes should read like config mistakes, not Python tracebacks."""

from __future__ import annotations

import pytest

from a2a_bridge.config import BridgeConfig


def _write(tmp_path, body: str):
    p = tmp_path / "agents.yml"
    p.write_text(body)
    return p


def test_misspelled_option_names_the_agent_and_the_valid_options(tmp_path):
    p = _write(tmp_path, "agents:\n  - id: mine\n    card_urls: https://x.test/\n")
    with pytest.raises(ValueError) as e:
        BridgeConfig.load(p)
    msg = str(e.value)
    assert "agent 'mine'" in msg
    assert "card_urls" in msg
    assert "card_url" in msg  # the valid-options list tells you what to write


def test_misspelled_caller_option_is_reported_under_caller(tmp_path):
    p = _write(
        tmp_path,
        "agents:\n  - id: mine\n    card_url: https://x.test/\n"
        "    caller:\n      id_headers: X-Caller-Id\n",
    )
    with pytest.raises(ValueError, match="under 'caller'"):
        BridgeConfig.load(p)


def test_missing_required_option_says_which(tmp_path):
    p = _write(tmp_path, "agents:\n  - id: mine\n")
    with pytest.raises(ValueError, match="'card_url' is required"):
        BridgeConfig.load(p)


def test_unnamed_agent_is_located_by_index(tmp_path):
    p = _write(tmp_path, "agents:\n  - card_url: https://x.test/\n")
    with pytest.raises(ValueError, match=r"agents\[0\]"):
        BridgeConfig.load(p)


def test_agent_block_that_is_not_a_mapping(tmp_path):
    p = _write(tmp_path, "agents:\n  - just-a-string\n")
    with pytest.raises(ValueError, match="should be a block of settings"):
        BridgeConfig.load(p)


def test_loading_does_not_mutate_the_callers_parsed_yaml(tmp_path):
    """`caller` used to be popped out of the caller's own dict."""
    raw = {"agents": [{"id": "mine", "card_url": "https://x.test/", "caller": {}}]}
    import copy

    before = copy.deepcopy(raw)
    from a2a_bridge.config import _agent_from

    _agent_from(raw["agents"][0], path="agents.yml", index=0)
    assert raw == before
