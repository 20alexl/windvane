# The windvane demo, recorded live: a real interactive Claude Code session
# with the plugin loaded, ~45 s. Render from the repo root in WSL or Linux,
# with Claude Code installed and logged in (~/.claude/.credentials.json):
#
#   vhs demo/windvane-live.tape
#
# Each take spends a few short model turns. demo/live_setup.sh rebuilds
# /tmp/wv-demo (a scratch config dir with the login copied in, the windvane
# store seeded with yesterday's session, the fixture project) in the hidden
# block below, so every take starts the same.
#
# After a take, the long waits (the model thinking, the compaction) are
# cut out of the gif from the mp4 rendered beside it:
#
#   python3 demo/trim.py demo/windvane.mp4 demo/windvane.gif
#
# The wait patterns below were checked against takes on Claude Code 2.1.289
# with the fullscreen renderer (demo/live_setup.sh selects it).

Output demo/windvane.gif
Output demo/windvane.mp4

Require python3
Require claude

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 20
Set LineHeight 1.0
Set Width 1240
Set Height 780
Set Padding 28
Set Framerate 20
Set TypingSpeed 25ms
Set CursorBlink false
Set Theme { "name": "windvane-dark", "background": "#121417", "foreground": "#d8dadf", "cursor": "#121417", "selection": "#2a2e36", "black": "#121417", "red": "#e06c75", "green": "#98c379", "yellow": "#e5c07b", "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#d8dadf", "brightBlack": "#5c6370", "brightRed": "#e06c75", "brightGreen": "#98c379", "brightYellow": "#e5c07b", "brightBlue": "#61afef", "brightMagenta": "#c678dd", "brightCyan": "#56b6c2", "brightWhite": "#ffffff" }

# The world, the plain prompt, the session; none of it recorded.
Hide
Type `eval "$(bash demo/live_setup.sh 2>/dev/null)" && cd /tmp/wv-demo/proj && clear`
Enter
Wait+Line@60s /\$\s*$/
# The plugin is the snapshot live_setup.sh made, so nothing reloads mid-take.
Type "claude --plugin-dir /tmp/wv-demo/plugin --dangerously-skip-permissions --model sonnet"
Enter
# The bypass-permissions acceptance is answered in the demo settings, so no
# dialog opens here.
# Before the first turn the segment reads "windvane ctx ?": no figure yet.
# Then the prompt box, and a pause for the startup notices to settle.
Wait+Screen@60s /windvane ctx/
Wait+Screen@30s /❯/
Sleep 3s
Show

# 1: the session opens: the status segment and the band above the prompt.
Sleep 1500ms
Screenshot demo/screenshots-live/1-open.png

# 2: a first prompt that reads the API document. The read is what carries
# the fill into the checkpoint band (the document is sized for it in
# build_fixture.py), and the turn ends there with nothing banked. The typed
# text must be on screen before Enter (a dropped prompt fails here in
# seconds, not after the long wait); it may wrap inside the prompt box, so
# the check allows a line break between the words.
Type "Read docs/API.md and tell me in two lines how paging works today"
Wait+Screen@10s /paging\s+works/
Enter
Wait+Screen@240s /(?i)Read\s*\(|Read 1 file/
Screenshot demo/screenshots-live/2-read.png

# 3: the fill is in the band: the status segment says checkpoint now. The
# engine never blocks a turn's end, and a note that rides a tool call
# inside a turn races the ten-second mirror tick, so the demo lets the turn
# end and puts the note on the next prompt, where it always arrives.
Wait+Screen@240s /(?i)checkpoint now/
Sleep 1s
Screenshot demo/screenshots-live/3-band.png

# 4: the edit prompt. It carries the engine's CHECKPOINT NOW note, so the
# model banks the checkpoint itself, a bare save through the plugin's tool,
# and does the edit. Nothing is typed after this: a queued prompt would run
# before the compaction and before windvane's own continue.
Type "Edit items_api/api.py with the Edit tool (no shell): add cursor pagination to GET /items and keep page= working"
Wait+Screen@10s /cursor\s+pagination/
Enter
Wait+Screen@240s /(?i)checkpoint \(MCP\)|Checkpoint saved|operation: "save"/
Sleep 1s
Screenshot demo/screenshots-live/4-saved.png

# 5: the turn ends after the edit; the mod compacts at the boundary with
# the save banked, and the brief sits right after the summary.
Wait+Screen@300s /(?i)compacted|windvane-compact/
Sleep 2s
Screenshot demo/screenshots-live/5-compacted.png

# 5b: nothing is typed. One prompt of windvane's resumes the work from the
# checkpoint, and the model carries on where it was.
Wait+Screen@120s /(?i)windvane plugin sent a message|Continue from the checkpoint/
Sleep 4s
Screenshot demo/screenshots-live/5b-continue.png

# 6: the pane: the checkpoint, the rules, the mistakes. The continue turn
# is still running when this is typed, so Claude Code queues the command
# and runs it when that turn ends; the wait is for the pane's own Refresh
# button, which nothing else on the screen says, with time for the turn.
Type "/windvane"
Enter
Wait+Screen@240s /\[ Refresh \]/
Sleep 2500ms
Screenshot demo/screenshots-live/6-pane.png
Escape

# Leave the session unrecorded. /exit ends it from the prompt; two Ctrl+C
# presses did not in the fullscreen renderer, and a take whose last wait
# fails writes no gif at all.
Hide
Sleep 500ms
Type "/exit"
Enter
Wait+Line@60s /\$\s*$/

# 7: the outro: the three install lines alone on the screen. The command
# that prints them is typed unrecorded and clears the screen first, so the
# shell's echo of the command itself is gone before the lines appear.
Type "clear; echo 'git clone <url> windvane'; echo 'claude plugin marketplace add ./windvane'; echo 'claude plugin install windvane@windvane'"
Enter
Wait+Screen@10s /install windvane@windvane\s*\n/
Show
Sleep 500ms
Screenshot demo/screenshots-live/7-outro.png
Sleep 2500ms
