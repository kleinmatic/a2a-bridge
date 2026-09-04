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
    # The point of the message is the list of valid options. Asserting
    # `"card_url" in msg` would pass on the echoed typo alone, so check the
    # list itself.
    valid = msg.split("Valid:", 1)[1]
    assert "card_url" in valid.split(", ")


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


def test_agent_from_does_not_mutate_its_input():
    """`caller` used to be popped out of the dict it was handed.

    Named for what it tests: BridgeConfig.load parses its own YAML from a path,
    so no caller of the public API could observe this. It matters for anyone
    building an AgentConfig from a dict they still hold.
    """
    raw = {"agents": [{"id": "mine", "card_url": "https://x.test/", "caller": {}}]}
    import copy

    before = copy.deepcopy(raw)
    from a2a_bridge.config import _agent_from

    _agent_from(raw["agents"][0], path="agents.yml", index=0)
    assert raw == before


def test_blank_api_keys_are_dropped(tmp_path):
    """An empty entry is not a key.

    It used to survive into api_keys, where it matched the empty token of a
    request carrying no Authorization header at all. `api_keys: ["${MY_KEY}"]`
    with the variable unset renders exactly that shape.
    """
    p = _write(
        tmp_path,
        'api_keys: ["good-key", "", "  "]\nagents:\n  - id: m\n    card_url: https://x.test/\n',
    )
    cfg = BridgeConfig.load(p)
    assert cfg.api_keys == ["good-key"]
    assert "" not in cfg.api_keys


def test_api_keys_of_only_blanks_leaves_the_bridge_visibly_open(tmp_path):
    """...and therefore trips the startup warning, rather than looking configured."""
    p = _write(tmp_path, 'api_keys: [""]\nagents:\n  - id: m\n    card_url: https://x.test/\n')
    assert BridgeConfig.load(p).api_keys == []


def test_top_level_typo_is_an_error_not_a_silent_open_bridge(tmp_path):
    """`api_key_env` (singular) used to load a bridge with no keys, quietly."""
    p = _write(
        tmp_path,
        "api_key_env: SOME_VAR\nagents:\n  - id: m\n    card_url: https://x.test/\n",
    )
    with pytest.raises(ValueError, match="unknown top-level option"):
        BridgeConfig.load(p)


def test_duplicate_agent_ids_are_rejected(tmp_path):
    """The second block used to overwrite the first without a word."""
    p = _write(
        tmp_path,
        "agents:\n  - id: m\n    card_url: https://a.test/\n  - id: m\n    card_url: https://b.test/\n",
    )
    with pytest.raises(ValueError, match="share the id 'm'"):
        BridgeConfig.load(p)


def test_yaml_bool_key_does_not_crash_the_validator(tmp_path):
    """YAML 1.1 parses a bare `on:` as the boolean True.

    Sorting or joining a mixed str/bool set raises the very TypeError this
    validation exists to replace.
    """
    p = _write(tmp_path, "agents:\n  - id: m\n    card_url: https://x.test/\n    on: whoops\n")
    with pytest.raises(ValueError, match="unknown option"):
        BridgeConfig.load(p)


def test_falsy_caller_block_is_an_error(tmp_path):
    """`caller: []` used to be accepted as 'no caller'."""
    p = _write(tmp_path, "agents:\n  - id: m\n    card_url: https://x.test/\n    caller: []\n")
    with pytest.raises(ValueError, match="'caller' should be a block"):
        BridgeConfig.load(p)


def test_bad_enum_value_names_the_file_and_the_agent(tmp_path):
    """__post_init__ errors get the same framing as every other config error."""
    p = _write(
        tmp_path,
        "agents:\n  - id: m\n    card_url: https://x.test/\n    stream_mode: sometimes\n",
    )
    with pytest.raises(ValueError) as e:
        BridgeConfig.load(p)
    msg = str(e.value)
    assert "agents.yml" in msg and "agent 'm'" in msg and "auto" in msg


def test_public_import_surface_is_intact():
    """These four are what an embedder imports. Every 0.1.x release had them."""
    import a2a_bridge

    for name in ("AgentConfig", "BridgeConfig", "CallerAuth", "A2AProtocolError"):
        assert hasattr(a2a_bridge, name), f"a2a_bridge.{name} disappeared"
    assert a2a_bridge.__version__
