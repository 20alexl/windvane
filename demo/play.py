"""Play the windvane demo in a terminal (what vhs records).

Every windvane line comes from demo/fixture/*.txt, which
demo/build_fixture.py captures from the engine itself. Written here by hand:
the title card, the caption bar on each beat, the terse lines of work
(prompts, tool calls, "compacted") and the outro. The fixture text is
printed as captured, with one change: the absolute paths of the temporary
directory it was captured in (named in fixture/paths.json) are rewritten as
~/proj/... (the project) or ~/.windvane/... (the store). Beyond that this
script only paces the text, colours it by pattern, wraps it at the terminal
width and splits a long block into pages at its section breaks.

    python3 demo/play.py                # the clip, ~45 s (demo/windvane-replay.tape)
    python3 demo/play.py --fast         # no pauses (the smoke test)
    python3 demo/play.py --check        # exit 1 if a rendered line leaks a local path
    python3 demo/play.py --width N      # wrap at N columns (default: the terminal)

stdlib only, ANSI colours only.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import textwrap
import time
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixture"
# README.md > Install: the three commands a new user types.
CLONE_URL = "https://github.com/20alexl/windvane.git"
INSTALL = (
    f"git clone {CLONE_URL} windvane",
    "claude plugin marketplace add ./windvane",
    "claude plugin install windvane@windvane",
)

ESC = "\x1b["
RESET = ESC + "0m"
# Styles are tuples of SGR codes, joined when printed.
ACCENT = "38;5;80"
YELLOW = "38;5;221"
RED = "38;5;203"
GREEN = "38;5;114"
MAGENTA = "38;5;176"
WHITE = "38;5;255"
TEXT = "38;5;252"
MUTED = "38;5;245"
FAINT = "38;5;240"
PROMPT = "38;5;110"
BOLD = "1"
DIM_ITALIC = ("3", FAINT)
CAPTION = ("1", "48;5;80", "38;5;234")

# Sections of windvane's output, by their first line.
_HEADING = re.compile(r"^(Rules \(|Rules seeded|Past mistakes|CHECKPOINT \[|Last session|Compaction #|  Since this)")
# Spans that are bookkeeping, not content: ids, task ids, reasons, tags.
_NOISE = re.compile(r"\[[0-9a-f]{12}\]|task_\d+|\(Reason: [^)]*\)|</?windvane-[a-z-]+>")
# What a rendered line must never hold: a drive path, a home or temp folder.
_LEAK = re.compile(r"[A-Za-z]:[\\/]|[\\/](?:Users|home|AppData|Temp|tmp)[\\/]", re.IGNORECASE)


# ---------------------------------------------------------------------------
# The capture's paths, rewritten
# ---------------------------------------------------------------------------


def _forms(path: str) -> list[str]:
    """Every spelling of one absolute path the engine may have printed."""
    out = {path, path.replace("\\", "/"), path.replace("\\", "\\\\")}
    for p in list(out):
        if len(p) > 1 and p[1] == ":":
            out.add(p[0].lower() + p[1:])
            out.add(p[0].upper() + p[1:])
    return sorted(out, key=len, reverse=True)


def _load_rewrites() -> list[tuple[re.Pattern, str]]:
    try:
        paths = json.loads((FIXTURE / "paths.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for key, short in (("project", "~/proj"), ("store", "~/.windvane"), ("home", "~"), ("root", "~/tmp")):
        if not paths.get(key):
            continue
        alts = "|".join(re.escape(f) for f in _forms(paths[key]))
        # The root, then the rest of the path up to a separator in the prose.
        out.append((re.compile(f"(?:{alts})([^\\s,;)\"']*)", re.IGNORECASE), short))
    return out


_REWRITES = _load_rewrites()


def local(text: str) -> str:
    """The capture's temporary paths as ~/proj/... and ~/.windvane/..."""
    for pattern, short in _REWRITES:
        text = pattern.sub(lambda m, s=short: s + m.group(1).replace("\\", "/"), text)
    return text


def fixture(beat: str) -> str:
    return local((FIXTURE / f"{beat}.txt").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------


def sgr(*codes) -> str:
    flat = [c for code in codes for c in (code if isinstance(code, tuple) else (code,)) if c]
    return ESC + ";".join(flat) + "m" if flat else ""


class Line:
    """One fixture line: its text, a per-character style and a gutter colour."""

    def __init__(self, text: str, base: tuple, gutter: str):
        self.text = text
        self.gutter = gutter
        self.styles = [base] * len(text)
        for m in _NOISE.finditer(text):
            for i in range(m.start(), m.end()):
                self.styles[i] = (FAINT,)


def classify(text: str, beat: str) -> list[Line]:
    """Colour semantics by pattern; the text itself is never changed."""
    out: list[Line] = []
    section = ""
    whole = {"checkpoint-now": "yellow", "headsup": "amber", "rule": "magenta"}.get(beat, "")
    for raw in text.rstrip("\n").split("\n"):
        s = raw.strip()
        gutter, base = ACCENT, (TEXT,)
        if whole == "yellow":
            gutter, base = YELLOW, (YELLOW,)
        elif whole == "amber":
            gutter, base = YELLOW, (TEXT,)
        elif whole == "magenta":
            gutter, base = MAGENTA, (TEXT,)
            if s.startswith("[") or "This call matches" in s:
                base = (BOLD, MAGENTA)
        if _HEADING.match(raw):
            section = raw.split(" ")[0]
            base = (BOLD, ACCENT)
            if raw.startswith("Rules seeded"):
                base = (GREEN,)
            elif raw.startswith("  Since"):
                base = (MUTED,)
        elif raw.startswith("windvane session started"):
            base = (FAINT,)
        elif s.startswith("AUTO-CHECK: Past mistakes"):
            section, gutter, base = "mistake", RED, (BOLD, RED)
        elif s.startswith("Relevant memories"):
            section, gutter, base = "memories", GREEN, (BOLD, GREEN)
        elif re.match(r"^\s{2}(Task|Current step):", raw):
            base = (BOLD, WHITE)
        elif re.match(r"^\s{2}Completed", raw):
            section = "completed"
        elif re.match(r"^\s{2}Pending", raw):
            section = "pending"
        elif re.match(r"^\s{2}Warnings", raw):
            section = "warnings"
        elif re.match(r"^\s{2}\S", raw) and section in ("completed", "pending", "warnings"):
            section = ""
        elif re.match(r"^\s{4}- ", raw) and section == "completed":
            base = (GREEN,)
        elif re.match(r"^\s{4}- ", raw) and section == "pending":
            base = (YELLOW,)
        elif re.match(r"^\s{4}! ", raw):
            gutter, base = RED, (RED,)
        elif section == "mistake" and s.startswith("- "):
            gutter, base = RED, (BOLD, RED)
        elif section == "memories" and s.startswith("•"):
            gutter, base = GREEN, (BOLD, GREEN)
        elif not s:
            section = "" if section in ("mistake", "memories") else section
        if beat == "headsup" and "Context pressure" in raw:
            base = (YELLOW,)
        out.append(Line(raw, base, gutter))
    return out


def _balanced(blocks: list[list[str]], room: int) -> list[list[list[str]]]:
    """Contiguous pages of whole blocks: as few pages as fit in ``room``
    rows, then the split whose fullest page is smallest (a block taller
    than the room gets a page of its own)."""
    sizes = [len(b) for b in blocks]
    n = len(sizes)

    def splits(i: int, k: int, cap: int) -> "list[int] | None":
        # page boundaries for blocks[i:] in exactly k pages, each <= cap
        if k == 1:
            return [n] if sum(sizes[i:]) <= cap or n - i == 1 else None
        total = 0
        for j in range(i + 1, n - k + 2):
            total += sizes[j - 1]
            if total > cap and j - 1 > i:
                break
            rest = splits(j, k - 1, cap)
            if rest is not None:
                return [j] + rest
        return None

    for k in range(1, n + 1):
        cap = max(room, max(sizes or [0]))
        if splits(0, k, cap) is None:
            continue
        best = splits(0, k, cap)
        for tight in range(max(sizes or [0]), cap + 1):
            got = splits(0, k, tight)
            if got is not None:
                best = got
                break
        pages, start = [], 0
        for end in best or [n]:
            pages.append(blocks[start:end])
            start = end
        return pages
    return [blocks]


# ---------------------------------------------------------------------------
# The player
# ---------------------------------------------------------------------------


class Player:
    def __init__(self, fast: bool, width: int, rows: int):
        self.fast = fast
        self.width = max(40, width)
        self.rows = max(12, rows)

    # -- primitives ------------------------------------------------------
    def w(self, s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    def out(self, s: str = "") -> None:
        self.w(s + "\n")

    def pause(self, secs: float) -> None:
        if not self.fast:
            time.sleep(secs)

    def clear(self) -> None:
        self.w(ESC + "2J" + ESC + "3J" + ESC + "H")

    def caption(self, text: str) -> None:
        self.clear()
        self.out(sgr(*CAPTION) + (" " + text).ljust(self.width) + RESET)
        self.out()

    def type(self, text: str, style: str, prefix: str = "", cps: float = 32.0) -> None:
        self.w(prefix + style)
        for ch in text:
            self.w(ch)
            self.pause(1.0 / cps)
        self.w(RESET + "\n")

    def prompt(self, text: str) -> None:
        self.type(text, sgr(BOLD, WHITE), prefix=sgr(PROMPT) + "❯ " + RESET)
        self.pause(0.3)

    def work(self, text: str, gap: float = 0.3) -> None:
        self.out(sgr(GREEN) + "● " + RESET + sgr(MUTED) + text + RESET)
        self.pause(gap)

    def note(self, text: str, gap: float = 0.3) -> None:
        self.out(sgr(*DIM_ITALIC) + text + RESET)
        self.pause(gap)

    # -- windvane's text ---------------------------------------------------
    def _rows(self, line: Line) -> list[str]:
        """Wrap one styled line into printable rows (gutter included)."""
        avail = self.width - 2
        text = line.text
        if not text:
            return [sgr(line.gutter) + "│" + RESET]
        if len(text) <= avail:
            pieces = [(0, text)]
        else:
            lead = re.match(r"\s*(?:[-!•]\s+)?", text).group(0)
            if text.lstrip().startswith("["):
                # A rule under its id: a short hanging indent, not one the width of the id.
                lead = re.match(r"\s*", text).group(0) + "  "
            wrapped = textwrap.wrap(text, width=avail, subsequent_indent=" " * len(lead),
                                    break_long_words=True, break_on_hyphens=False)
            pieces, cursor = [], 0
            for w in wrapped:
                body = w.lstrip(" ")
                at = text.find(body, cursor)
                at = cursor if at < 0 else at
                pieces.append((at, w))
                cursor = at + len(body)
        rows = []
        for at, piece in pieces:
            pad = len(piece) - len(piece.lstrip(" "))
            body = piece[pad:]
            buf, cur = [" " * pad], None
            for i, ch in enumerate(body):
                st = line.styles[at + i] if at + i < len(line.styles) else line.styles[-1]
                if st != cur:
                    buf.append(RESET + sgr(*st))
                    cur = st
                buf.append(ch)
            rows.append(sgr(line.gutter) + "│" + RESET + " " + "".join(buf) + RESET)
        return rows

    def pages(self, beat: str, room: int, text: "str | None" = None) -> list[list[str]]:
        """The beat's rows, split at section starts so no page passes room."""
        blocks: list[list[str]] = []
        for line in classify(fixture(beat) if text is None else text, beat):
            starts = bool(line.text) and not line.text.startswith(" ") and not line.text.startswith("<")
            rows = self._rows(line)
            if starts or not blocks:
                blocks.append(rows)
            else:
                blocks[-1].extend(rows)
        return [sum(group, []) for group in _balanced(blocks, room)]

    def show(self, beat: str, caption: str, hold: float, lead: int = 0, per_line: float = 0.04,
             text: "str | None" = None) -> None:
        """Print a fixture beat under its caption, page by page; ``lead`` is
        the rows already printed under the caption."""
        room = self.rows - 3 - lead
        for n, page in enumerate(self.pages(beat, room, text)):
            if n:
                self.caption(caption)
            for row in page:
                self.out(row)
                self.pause(per_line)
            self.pause(hold)

    def rule(self, margin: str, n: int = 44) -> None:
        self.out(margin + sgr(ACCENT) + "─" * n + RESET)

    def centered(self, block_rows: int, block_cols: int) -> str:
        self.w("\n" * max(0, (self.rows - block_rows) // 2))
        return " " * max(2, (self.width - block_cols) // 2)


def clip(p: Player) -> None:
    # Title
    p.clear()
    m = p.centered(5, 44)
    p.pause(0.2)
    p.type("windvane", sgr(BOLD, ACCENT), prefix=m, cps=24)
    p.pause(0.3)
    p.type("keeps the state and steers the session", sgr(BOLD, WHITE), prefix=m, cps=40)
    p.pause(0.3)
    p.out(m + sgr(*DIM_ITALIC) + "what the model actually reads" + RESET)
    p.rule(m)
    p.pause(1.8)

    # 1: the session-start banner
    cap = "1 / 5  session start: rules seeded, yesterday's checkpoint back"
    p.caption(cap)
    p.prompt("claude")
    p.show("session-start", cap, hold=3.0, lead=1)

    # 2: the context fills; the heads-up, CHECKPOINT NOW, the bare save
    cap = "2 / 5  the context fills: a heads-up, then checkpoint now"
    p.caption(cap)
    p.work("Write(items_api/cursor.py)")
    p.work("Edit(items_api/db.py)")
    p.work("Bash(pytest -q)  14 passed")
    p.out()
    p.prompt("Good. Keep going.")
    p.show("headsup", cap, hold=1.8, lead=6)
    p.caption(cap)
    p.prompt("Now wire the cursor into the endpoint.")
    p.show("checkpoint-now", cap, hold=1.8, lead=1)
    p.work("windvane · checkpoint(operation: save)", gap=0.4)
    saved = fixture("checkpoint-saved").split("\n")
    shown = [saved[0]] + [s for s in saved if s.startswith("Drafted by the recorder")]
    for text in shown:
        for row in p._rows(Line(text, (GREEN,), GREEN)):
            p.out(row)
        p.pause(0.2)
    p.pause(2.6)

    # 3: the compaction: the brief inside the conversation, the lean banner
    cap = "3 / 5  compacted: the brief rides inside the conversation"
    p.caption(cap)
    p.note("✻ Conversation compacted", gap=0.5)
    p.show("compact-brief", cap, hold=2.4, lead=1)
    cap = "3 / 5  so the session-start banner leaves them out"
    p.caption(cap)
    p.note("✻ Conversation compacted", gap=0.3)
    p.show("compact-banner", cap, hold=2.2, lead=1)

    # 4: the pre-edit injection
    cap = "4 / 5  before an edit: a past mistake and a memory"
    p.caption(cap)
    p.work("Edit(items_api/api.py)", gap=0.4)
    p.show("pre-edit", cap, hold=2.6, lead=1)

    # 5: the rule before a destructive command
    cap = "5 / 5  before a destructive command: the rule it matches"
    p.caption(cap)
    p.work("Bash(git reset --hard HEAD~1)", gap=0.4)
    p.show("rule", cap, hold=1.6, lead=1)
    p.out()
    p.note("backed off; asked the user", gap=1.8)

    # Outro
    p.clear()
    m = p.centered(6, 44)
    p.rule(m, 44)
    p.out()
    for cmd in INSTALL:
        p.out(m + sgr(FAINT) + "$ " + RESET + sgr(TEXT) + cmd + RESET)
        p.pause(0.15)
    p.out()
    p.rule(m, 44)
    p.pause(4.0)  # outlasts the tape's closing hold, so no shell prompt is recorded


def check() -> int:
    """Every rendered fixture line, scanned for a local path. 0 when clean."""
    bad = []
    for f in sorted(FIXTURE.glob("*.txt")):
        for n, line in enumerate(fixture(f.stem).split("\n"), start=1):
            if _LEAK.search(line):
                bad.append(f"{f.name}:{n}: {line}")
    for b in bad:
        print(b)
    print("clean" if not bad else f"{len(bad)} leaking line(s)")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    if "--check" in argv:
        return check()
    fast = "--fast" in argv
    size = shutil.get_terminal_size((78, 28))
    width = int(argv[argv.index("--width") + 1]) if "--width" in argv else size.columns
    p = Player(fast, width, size.lines)
    p.w(ESC + "?25l")  # hide the cursor while it plays
    try:
        clip(p)
    finally:
        p.w(ESC + "?25h")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
