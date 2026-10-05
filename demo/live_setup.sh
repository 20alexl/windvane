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
WINDOW="${WV_WINDOW:-60000}"

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
           projects={proj: {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True,
                            "allowedTools": [], "projectOnboardingSeenCount": 1}})
with open(dest, "w", encoding="utf-8", newline="\n") as f:
    json.dump(out, f, indent=2)
PY

# The compaction window, in the settings too: the engine honours the
# environment variable only from 100K up, the settings value at any size.
printf '{\n  "autoCompactWindow": %s\n}\n' "$WINDOW" > "$ROOT/config/settings.json"

(cd "$REPO" && python3 demo/build_fixture.py --seed-live "$ROOT" "$ROOT/config") >&2

# The plugin snapshot the tape loads, so an edit to the repository during
# the take reloads nothing.
mkdir -p "$ROOT/plugin"
tar -C "$REPO" --exclude=.git --exclude=__pycache__ --exclude=.claude-plugin/types \
    --exclude=demo/screenshots-live --exclude=demo/windvane.gif --exclude=demo/windvane.mp4 \
    -cf - . | tar -C "$ROOT/plugin" -xf -
echo "seeded $ROOT (window $WINDOW), plugin snapshot at $ROOT/plugin" >&2

cat <<EOF
export PATH="\$HOME/.local/bin:\$PATH"
export CLAUDE_CONFIG_DIR=$ROOT/config
export WINDVANE_DIR=$ROOT/store
export WINDVANE_PYTHON=python3
export WINDVANE_NO_DAEMON=1
export WINDVANE_SEMANTIC=0
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=$WINDOW
export DISABLE_AUTOUPDATER=1
export PS1='\$ '
EOF
