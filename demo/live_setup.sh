#!/usr/bin/env bash
# The world the live recording (demo/windvane-live.tape) starts from.
# Run from the repository root in WSL or Linux:
#
#   eval "$(bash demo/live_setup.sh)"
#
# Everything lives under /tmp/wv-demo and is rebuilt from nothing on every
# run, so each take starts the same:
#   config/  a scratch Claude Code config dir: the login copied from
#            ~/.claude/.credentials.json, the onboarding answered, the
#            project trusted, the compaction window set. No other plugin
#            loads from it. The semantic tier is switched off through the
#            environment, which also keeps the first-run question from
#            opening in the middle of a take.
#   store/   the windvane store, seeded with yesterday's session (a past
#            mistake for items_api/api.py, a remembered fact, a closing
#            checkpoint) by demo/build_fixture.py. The rules are seeded by
#            the session's own first start.
#   proj/    the fixture project, a git repository with two commits.
#   plugin/  a snapshot of this repository (no .git, no caches) that the
#            tape loads with --plugin-dir. Claude Code hot-reloads a plugin
#            folder when a file in it changes, and a reload during a take
#            drops the typed prompt; the snapshot keeps the take still
#            while the repository is being edited.
#
# The real stores (~/.windvane, ~/.claude) are only read: the credentials
# file and the onboarding answers are copied from them.
#
# Progress goes to stderr; stdout is the export lines to eval.

set -euo pipefail

ROOT=/tmp/wv-demo
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Claude Code accepts autoCompactWindow from 100,000 to 1,000,000 tokens and
# drops a smaller value when it next writes the settings file. At 100K the
# checkpoint band is 48K to 68K on a 1M-window model; the fixture's API
# document (demo/build_fixture.py) is sized so the first turn's read lands
# the fill inside it.
WINDOW="${WV_WINDOW:-100000}"

case "$ROOT" in /tmp/*) ;; *) echo "refusing: $ROOT is not under /tmp" >&2; exit 1 ;; esac

# The plugin's command hooks run `python`; some systems only have python3.
if ! command -v python >/dev/null 2>&1; then
  mkdir -p "$HOME/.local/bin"
  ln -sf "$(command -v python3)" "$HOME/.local/bin/python"
  echo "linked $HOME/.local/bin/python -> python3" >&2
fi

rm -rf "$ROOT"
mkdir -p "$ROOT/config"
cp "$HOME/.claude/.credentials.json" "$ROOT/config/.credentials.json"
chmod 600 "$ROOT/config/.credentials.json"

# The copied login must still be valid: a take that starts on an expired
# access token answers "Login expired" to its first prompt, and the refresh
# attempted inside the scratch config dir does not reach the real file. A
# real session (any `claude` run) refreshes it; the next take copies it.
LOGIN_NOTE=""
if ! python3 - "$ROOT/config/.credentials.json" <<'PY'
import json, sys, time
try:
    exp = (json.load(open(sys.argv[1])).get("claudeAiOauth") or {}).get("expiresAt") or 0
except (OSError, ValueError):
    exp = 0
sys.exit(0 if exp > time.time() * 1000 else 1)
PY
then
  LOGIN_NOTE="windvane demo: the login in ~/.claude/.credentials.json has expired; run claude once to refresh it, then record again"
  echo "$LOGIN_NOTE" >&2
fi

# Onboarding answered, the project trusted, the account details left out.
python3 - "$HOME/.claude.json" "$ROOT/config/.claude.json" "$ROOT/proj" <<'PY'
import json, sys
src, dest, proj = sys.argv[1:4]
try:
    with open(src, encoding="utf-8") as f:
        base = json.load(f)
except (OSError, ValueError):
    base = {}
drop = {"oauthAccount", "projects", "userID", "machineID", "pluginUsage", "claudeAiMcpEverConnected",
        "mcpNeedsAuthNoticed", "officialMarketplaceAutoInstallAttempted"}
out = {k: v for k, v in base.items() if k not in drop}
out.update(hasCompletedOnboarding=True, theme="dark", autoUpdates=False,
           officialMarketplaceAutoInstalled=True,
           # the "try the new fullscreen renderer" prompt shows below 3
           fullscreenUpsellSeenCount=3,
           # the first edit of a .py file otherwise opens the "install the
           # pyright LSP plugin" dialog, which swallows whatever is typed
           lspRecommendationDisabled=True,
           projects={proj: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
                            "allowedTools": [], "projectOnboardingSeenCount": 1}})
with open(dest, "w", encoding="utf-8", newline="\n") as f:
    json.dump(out, f, indent=2)
PY

# The compaction window, in the settings too, beside the bypass-permissions
# acceptance: answering that dialog makes Claude Code rewrite this file, so
# the answer is given here and the window survives. The fullscreen renderer
# is for the recorder: vhs matches a Screen wait against the first rows of
# the terminal buffer, which stop changing once the transcript scrolls, so
# the classic renderer leaves every wait staring at a frozen first page.
# The fullscreen layout draws on the alternate screen, which has no
# scrollback, so its rows are the viewport.
printf '{\n  "autoCompactWindow": %s,\n  "skipDangerousModePermissionPrompt": true,\n  "tui": "fullscreen"\n}\n' "$WINDOW" > "$ROOT/config/settings.json"

(cd "$REPO" && python3 demo/build_fixture.py --seed-live "$ROOT" "$ROOT/config") >&2

# The folder the tape's screenshots land in: ffmpeg does not create it, and
# a missing folder fails the take after the gif is rendered.
mkdir -p "$REPO/demo/screenshots-live"

# The plugin snapshot the tape loads, so an edit to the repository during
# the take reloads nothing.
mkdir -p "$ROOT/plugin"
tar -C "$REPO" --exclude=.git --exclude=__pycache__ --exclude=.claude-plugin/types \
    --exclude=demo/screenshots-live --exclude=demo/windvane.gif --exclude=demo/windvane.mp4 \
    -cf - . | tar -C "$ROOT/plugin" -xf -
echo "seeded $ROOT (window $WINDOW), plugin snapshot at $ROOT/plugin" >&2

if [ -n "$LOGIN_NOTE" ]; then
  printf 'echo %q >&2\n' "$LOGIN_NOTE"
fi
cat <<EOF
export PATH="\$HOME/.local/bin:\$PATH"
export CLAUDE_CONFIG_DIR=$ROOT/config
export WINDVANE_DIR=$ROOT/store
export WINDVANE_PYTHON=python3
export WINDVANE_NO_DAEMON=1
export WINDVANE_SEMANTIC=0
# The door would cut the API document's read to 60,000 characters; the take
# needs the whole read in the context to reach the band.
export WINDVANE_RESULT_BUDGET=120000
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=$WINDOW
export CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1
export DISABLE_AUTOUPDATER=1
export PS1='\$ '
EOF
