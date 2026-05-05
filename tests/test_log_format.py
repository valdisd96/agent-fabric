"""Tests for the stream-json pretty-printer."""

from __future__ import annotations

import json

from fabric.log_format import format_event, format_stream


# ---------- format_event over individual event types ----------


def test_format_system_init_emits_one_summary_line() -> None:
    ev = {
        "type": "system",
        "subtype": "init",
        "session_id": "abcd1234efgh5678",
        "model": "claude-opus-4-7",
    }
    out = format_event(ev)
    assert out == ["[init] model=claude-opus-4-7 session=abcd1234"]


def test_format_system_non_init_is_silent() -> None:
    """We don't render every system event — only init."""
    ev = {"type": "system", "subtype": "session_resumed"}
    assert format_event(ev) == []


def test_format_assistant_text_block() -> None:
    ev = {
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": "Reading the issue body now.\n"}],
        },
    }
    out = format_event(ev)
    assert out == ["Reading the issue body now."]


def test_format_assistant_skips_empty_text_blocks() -> None:
    ev = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "   \n"}]},
    }
    assert format_event(ev) == []


def test_format_assistant_tool_use_read_shows_file_path() -> None:
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "Read",
                    "input": {"file_path": "/srv/projects/teach-me-eng-bot/bot.py"},
                }
            ],
        },
    }
    assert format_event(ev) == ["→ Read(/srv/projects/teach-me-eng-bot/bot.py)"]


def test_format_assistant_tool_use_bash_truncates_long_command() -> None:
    long = "find /srv/projects -type f -name '*.py' -exec grep -l fabric {} \\; | sort | uniq | head"
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": long}}
            ],
        },
    }
    [line] = format_event(ev)
    assert line.startswith("→ Bash(")
    assert line.endswith(")")
    assert "…" in line  # truncated


def test_format_assistant_tool_use_unknown_tool_uses_first_string_field() -> None:
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "MysteryTool",
                    "input": {"target": "thing-42", "depth": 3},
                }
            ],
        },
    }
    assert format_event(ev) == ["→ MysteryTool(thing-42)"]


def test_format_assistant_handles_text_and_tool_use_in_one_event() -> None:
    ev = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me check the file."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "x.py"}},
            ],
        },
    }
    assert format_event(ev) == ["Let me check the file.", "→ Read(x.py)"]


def test_format_user_tool_result_shows_truncated_preview() -> None:
    ev = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "line 1\nline 2\nline 3",
                }
            ]
        },
    }
    [line] = format_event(ev)
    assert line.startswith("← line 1 ⏎ line 2 ⏎ line 3")


def test_format_user_tool_result_handles_block_form() -> None:
    """The content field can be a list of {type:'text', text:'…'} blocks
    rather than a bare string — both shapes appear in real claude output."""
    ev = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "content": [
                        {"type": "text", "text": "first\n"},
                        {"type": "text", "text": "second"},
                    ],
                }
            ]
        },
    }
    [line] = format_event(ev)
    assert line == "← first ⏎ second"


def test_format_result_success_has_cost_and_duration() -> None:
    ev = {
        "type": "result",
        "is_error": False,
        "duration_ms": 12345,
        "total_cost_usd": 0.0123,
    }
    assert format_event(ev) == ["[result] ok duration=12345ms cost=$0.0123"]


def test_format_result_error_marks_failure() -> None:
    ev = {"type": "result", "is_error": True, "duration_ms": 500}
    assert format_event(ev) == ["[result] ERROR duration=500ms"]


def test_format_unknown_event_type_is_silent() -> None:
    assert format_event({"type": "no_such_type", "data": 42}) == []


# ---------- format_stream over a multi-line file ----------


def test_format_stream_renders_full_dispatch() -> None:
    events = [
        {"type": "system", "subtype": "init", "session_id": "deadbeef00", "model": "claude-opus-4-7"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading issue body."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "issue.md"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "issue body here"},
        ]}},
        {"type": "result", "is_error": False, "duration_ms": 100, "total_cost_usd": 0.001},
    ]
    text = "\n".join(json.dumps(e) for e in events) + "\n"
    out = format_stream(text)

    assert out.startswith("[init] model=claude-opus-4-7 session=deadbeef")
    assert "Reading issue body." in out
    assert "→ Read(issue.md)" in out
    assert "← issue body here" in out
    assert out.rstrip().endswith("[result] ok duration=100ms cost=$0.0010")
    assert out.endswith("\n")


def test_format_stream_tolerates_blank_lines() -> None:
    text = (
        "\n"
        + json.dumps({"type": "result", "is_error": False, "duration_ms": 1}) + "\n"
        + "\n"
    )
    out = format_stream(text)
    assert "[result] ok" in out


def test_format_stream_passes_through_non_json_lines() -> None:
    """Old plain-text logs (pre-stream-json) should still be readable —
    `--pretty` shouldn't crash on them."""
    text = "this is not json\n" + json.dumps({"type": "result", "is_error": False, "duration_ms": 1}) + "\n"
    out = format_stream(text)
    assert "[non-json] this is not json" in out
    assert "[result] ok" in out


def test_format_stream_skips_non_dict_json() -> None:
    text = "42\n[1,2,3]\n" + json.dumps({"type": "result", "is_error": False, "duration_ms": 1}) + "\n"
    out = format_stream(text)
    assert out.strip() == "[result] ok duration=1ms"


def test_format_stream_empty_returns_empty_string() -> None:
    assert format_stream("") == ""
    assert format_stream("\n\n\n") == ""
