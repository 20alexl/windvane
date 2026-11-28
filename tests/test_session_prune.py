"""The sessions folder is pruned whole: a session's markers, half-written temp
files and moved-aside copies go with its state once they are old."""

import os
import time

import pytest

common = pytest.importorskip("windvane.events.common", reason="the pruning is the hooks port's")


def test_old_markers_temp_files_and_damaged_copies_go_and_new_ones_stay(tmp_path, monkeypatch):
    monkeypatch.setenv("WINDVANE_DIR", str(tmp_path / "store"))
    folder = tmp_path / "store" / "sessions"
    folder.mkdir(parents=True)
    names = ["a.json", "a.mod", "a.briefed", "a.marks.json", "a.ctx.json", "a.json.4536.tmp", "a.json.corrupt-1795745452653"]
    old = time.time() - 8 * 86400
    for name in names:
        (folder / name).write_bytes(b"{}")
        os.utime(folder / name, (old, old))
    for name in ("b.json", "b.mod", "b.json.12.tmp"):
        (folder / name).write_bytes(b"{}")
    common._prune_session_states()
    assert sorted(p.name for p in folder.iterdir()) == ["b.json", "b.json.12.tmp", "b.mod"]
