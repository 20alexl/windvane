# The windvane demo, replayed from captured output, ~45 s. Render from the repo root in WSL or Linux:
#
#   vhs demo/windvane-replay.tape
#
# demo/play.py prints demo/fixture/*.txt, the real engine output that
# demo/build_fixture.py captures (run it first: the fixture is not
# committed); this tape only records it, and takes one screenshot per beat
# for review without the gif.

Output demo/windvane.gif
Output demo/windvane.mp4

Require python3

Set Shell "bash"
Set FontFamily "DejaVu Sans Mono"
Set FontSize 20
Set LineHeight 1.0
Set Width 1000
Set Height 620
Set Padding 28
Set Framerate 20
Set TypingSpeed 0
Set CursorBlink false
Set Theme { "name": "windvane-dark", "background": "#121417", "foreground": "#d8dadf", "cursor": "#121417", "selection": "#2a2e36", "black": "#121417", "red": "#e06c75", "green": "#98c379", "yellow": "#e5c07b", "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#d8dadf", "brightBlack": "#5c6370", "brightRed": "#e06c75", "brightGreen": "#98c379", "brightYellow": "#e5c07b", "brightBlue": "#61afef", "brightMagenta": "#c678dd", "brightCyan": "#56b6c2", "brightWhite": "#ffffff" }

Hide
Type "clear && python3 demo/play.py"
Enter
Sleep 300ms
Show

Wait+Screen@20s /the model actually reads/
Sleep 1s
Screenshot demo/screenshots/0-title.png

Wait+Screen@30s /Since this checkpoint/
Sleep 500ms
Screenshot demo/screenshots/1-session-start.png

Wait+Screen@30s /checkpoint call comes at/
Sleep 300ms
Screenshot demo/screenshots/2-headsup.png

Wait+Screen@30s /Drafted by the recorder/
Sleep 500ms
Screenshot demo/screenshots/2-checkpoint-now.png

Wait+Screen@30s /Handoff note/
Sleep 500ms
Screenshot demo/screenshots/3-compact-brief.png

Wait+Screen@30s /Plan work in units/
Sleep 300ms
Screenshot demo/screenshots/3-compact-banner.png

Wait+Screen@30s /\/windvane-edit-reminder/
Sleep 300ms
Screenshot demo/screenshots/4-pre-edit.png

Wait+Screen@30s /asked the user/
Sleep 300ms
Screenshot demo/screenshots/5-rule.png

# Hold on the outro.
Wait+Screen@30s /install windvane@windvane/
Sleep 500ms
Screenshot demo/screenshots/6-outro.png
Sleep 2500ms
