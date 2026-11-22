"""The session index reads every shape a transcript record can take: a result
whose content is a list of blocks inside a list is text, and one record of a
shape it does not know costs that record, not the index of every session."""

import json
from pathlib import Path

from windvane.mining import search


def _write(path: Path, *records):
    path.write_bytes(("\n".join(json.dumps(r) for r in records) + "\n").encode("utf-8"))


def _call(ident, command):
    return {"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "message": {"content": [
        {"type": "tool_use", "id": ident, "name": "Bash", "input": {"command": command}}]}}


def _result(ident, content, **extra):
    return {"type": "user", "timestamp": "2026-11-20T10:00:01Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": content}]}, **extra}


def _prompt(text):
    return {"type": "user", "timestamp": "2026-11-20T10:00:02Z", "message": {"content": text}}


def _scan(tmp_path: Path, *records):
    _write(tmp_path / "s.jsonl", *records)
    chunks, texts, _progress = search._scan_session_chunks(tmp_path, "s", {"jsonl_file": "s.jsonl"}, {})
    return chunks, texts


def test_a_result_holding_a_list_inside_a_list_is_read_as_text(tmp_path):
    nested = [{"type": "tool_result", "content": [{"type": "text", "text": "the nested output line"}]}]
    chunks, texts = _scan(tmp_path, _call("a", "echo nested output"), _result("a", nested),
                          _prompt("the user's ruling: keep the paper code path"))
    assert any("nested output line" in t for t in texts)
    assert any(c.msg_type == "user" for c in chunks)


def test_a_shell_result_whose_output_is_not_text_is_skipped_quietly(tmp_path):
    chunks, _texts = _scan(tmp_path, _call("a", "echo odd output"), _result("a", "", toolUseResult={"stdout": ["x"], "stderr": {"y": 1}}),
                           _prompt("the user's ruling: keep the paper code path"))
    assert [c.msg_type for c in chunks].count("user") == 1


def test_a_record_of_an_unknown_shape_costs_only_that_record(tmp_path, monkeypatch):
    real = search._extract_tool_chunks

    def breaks_once(prev, curr, *args):
        if "boom" in json.dumps(prev):
            raise TypeError("a shape nobody planned for")
        return real(prev, curr, *args)

    monkeypatch.setattr(search, "_extract_tool_chunks", breaks_once)
    chunks, _texts = _scan(tmp_path, _call("a", "echo boom here"), _result("a", "out"),
                           _call("b", "echo fine here"), _result("b", "fine output"),
                           _prompt("the user's ruling: keep the paper code path"))
    kinds = [c.msg_type for c in chunks]
    assert kinds.count("user") == 1 and kinds.count("tool") == 1


def test_result_text_is_always_a_string():
    for content in ("plain", [{"text": "a"}], [{"content": [{"text": "deep"}]}], [{"content": {"x": 1}}], None, 5):
        text, _err = search._result_text({"content": content})
        assert isinstance(text, str)
    assert search._result_text({"content": [{"content": [{"type": "text", "text": "deep"}]}]})[0] == "deep"
