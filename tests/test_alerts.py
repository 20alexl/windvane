"""The alert command, through the machine's real shell: cmd.exe on Windows, sh elsewhere.

The message can hold text from a session, so nothing in it may run.
"""

import os
import sys
from pathlib import Path

import pytest

from windvane import alerts

RECEIVER = "import sys\nopen(sys.argv[1], 'w', encoding='utf-8').write(sys.argv[2] + '|' + str(len(sys.argv)))\n"

HOSTILE = [
    'a" & echo pwned> pwned.txt & "b',
    "a & echo pwned> pwned.txt",
    "a | echo pwned> pwned.txt",
    "a; echo pwned> pwned.txt",
    "$(echo pwned> pwned.txt) and `echo pwned> pwned.txt`",
    "a' ; echo pwned> pwned.txt ; 'b",
    "%COMSPEC% and %PATH%",
    "caret ^ bang ! angle < > done",
]


@pytest.fixture
def command(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "receive.py"
    script.write_bytes(RECEIVER.encode("utf-8"))
    return f'"{sys.executable}" "{script}" out.txt {{message}}'


def _got(tmp_path: Path) -> str:
    return (tmp_path / "out.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("message", HOSTILE)
def test_nothing_in_a_message_runs(tmp_path: Path, command: str, message: str):
    rec = alerts.send(message, command=command)
    assert rec["sent"] is True, rec
    assert not (tmp_path / "pwned.txt").exists()
    text, argc = _got(tmp_path).rsplit("|", 1)
    assert argc == "3", text  # the message stayed one argument
    if os.name != "nt":
        assert text == message


def test_percent_signs_do_not_expand(tmp_path: Path, command: str):
    alerts.send("%COMSPEC% at 90%", command=command)
    text = _got(tmp_path)
    assert "cmd.exe" not in text.lower() and "at 90" in text


@pytest.mark.parametrize("message", ["failed in C:\\work\\dir\\", "two at the end\\\\", "mid\\dle stays"])
def test_a_backslash_reaches_the_command_as_written(tmp_path: Path, command: str, message: str):
    """On Windows a backslash before the closing quote escaped it for the
    program reading its arguments: the backslash was lost and a quote came in."""
    alerts.send(message, command=command)
    text, argc = _got(tmp_path).rsplit("|", 1)
    assert text == message and argc == "3"


def test_a_command_without_the_placeholder_gets_the_message_on_stdin(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = tmp_path / "stdin.py"
    script.write_bytes(b"import sys\nopen('out.txt', 'w', encoding='utf-8').write(sys.stdin.read())\n")
    rec = alerts.send('a" & echo pwned> pwned.txt', command=f'"{sys.executable}" "{script}"')
    assert rec["sent"] is True and not (tmp_path / "pwned.txt").exists()
    assert "pwned" in _got(tmp_path)
