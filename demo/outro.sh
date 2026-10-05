#!/usr/bin/env bash
# The demo's closing screen: the three install lines as one block, centred
# in the terminal. The live tape (demo/windvane-live.tape) types this after
# /exit, unrecorded, and records the screen it leaves. The script then holds
# the screen past the end of the take, so no prompt appears under the lines.
# The hold is WV_OUTRO_HOLD seconds (default 8).

lines=(
  "git clone https://github.com/20alexl/windvane.git windvane"
  "claude plugin marketplace add ./windvane"
  "claude plugin install windvane@windvane"
)

cols=$(tput cols 2>/dev/null || echo 80)
rows=$(tput lines 2>/dev/null || echo 24)
width=0
for l in "${lines[@]}"; do
  if (( ${#l} > width )); then width=${#l}; fi
done
pad=$(( (cols - width) / 2 ))
if (( pad < 0 )); then pad=0; fi
top=$(( (rows - ${#lines[@]}) / 2 ))
if (( top < 0 )); then top=0; fi

clear
tput civis 2>/dev/null
for (( i = 0; i < top; i++ )); do echo; done
for l in "${lines[@]}"; do
  printf '%*s%s\n' "$pad" '' "$l"
done
sleep "${WV_OUTRO_HOLD:-8}"
