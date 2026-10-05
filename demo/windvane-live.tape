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
# NOT YET RENDERED: the wait patterns below follow the plugin's source (the
# status segment and band in hooks/register.ts and hooks/band.tsx, the pane
# in hooks/pane.tsx) and have not been checked against a take.

Output demo/windvane.gif
Output demo/windvane.mp4

Require python3
Require claude

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 20
Set LineHeight 1.0
Set Width 1000
Set Height 620
Set Padding 28
Set Framerate 20
Set TypingSpeed 35ms
Set CursorBlink false
Set Theme { "name": "windvane-dark", "background": "#121417", "foreground": "#d8dadf", "cursor": "#121417", "selection": "#2a2e36", "black": "#121417", "red": "#e06c75", "green": "#98c379", "yellow": "#e5c07b", "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#d8dadf", "brightBlack": "#5c6370", "brightRed": "#e06c75", "brightGreen": "#98c379", "brightYellow": "#e5c07b", "brightBlue": "#61afef", "brightMagenta": "#c678dd", "brightCyan": "#56b6c2", "brightWhite": "#ffffff" }

# The world, the plain prompt, the session; none of it recorded.
Hide
Type `eval "$(bash demo/live_setup.sh 2>/dev/null)" && cd /tmp/wv-demo/proj && clear`
Enter
Wait+Line@60s /\$\s*$/
Type "claude --plugin-dir /mnt/e/workspace/windvane --dangerously-skip-permissions --model sonnet"
Enter
# The bypass-permissions acceptance screen: pick "Yes, I accept".
Wait+Screen@60s /Bypass Permissions/
Down
Enter
# Before the first turn the segment reads "windvane ctx ?": no figure yet.
Wait+Screen@60s /windvane ctx/
Show

# 1: the session opens: the status segment and the band above the prompt.
Sleep 1500ms
Screenshot demo/screenshots-live/1-open.png

# 2: one short prompt that edits the api file. The prompt names the file,
# so the wait is for the edit tool's own row, not the file name.
Type "Edit items_api/api.py with the Edit tool (no shell): add cursor pagination to GET /items and keep page= working"
Enter
Wait+Screen@240s /(?i)(update|edit|write)\(items_api|Bash\(/
Screenshot demo/screenshots-live/2-edit.png

# 3: the fill is in the band: the status segment says checkpoint now.
Wait+Screen@240s /(?i)checkpoint now/
Sleep 1s
Screenshot demo/screenshots-live/3-band.png

# 4: the model banks the checkpoint itself, a bare save through the
# plugin's tool, because the nudge is in its context. Nothing is typed:
# a queued prompt would start the next turn and block the compaction.
Wait+Screen@240s /(?i)checkpoint \(MCP\)|Checkpoint saved|operation: "save"/
Sleep 1s
Screenshot demo/screenshots-live/4-saved.png

# 5: the turn ends; the mod compacts at the boundary with the save banked,
# and the brief sits right after the summary.
Wait+Screen@300s /(?i)compacted|windvane-compact/
Sleep 2s
Screenshot demo/screenshots-live/5-compacted.png

# 6: the pane: the checkpoint, the rules, the mistakes.
Type "/windvane"
Enter
Wait+Screen@30s /(?i)rules/
Sleep 2500ms
Screenshot demo/screenshots-live/6-pane.png
Escape

# Leave the session unrecorded.
Hide
Ctrl+C
Sleep 500ms
Ctrl+C
Wait+Line@30s /\$\s*$/
Type "clear"
Enter
Sleep 300ms
Show

# 7: the outro.
Type "echo 'git clone <url> windvane'; echo 'claude plugin marketplace add ./windvane'; echo 'claude plugin install windvane@windvane'"
Enter
Wait+Screen@10s /install windvane@windvane\s*\n/
Sleep 500ms
Screenshot demo/screenshots-live/7-outro.png
Sleep 2500ms
