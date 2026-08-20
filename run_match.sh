#!/bin/sh
# Start or resume a durable match; auto-restarts until every game finishes.
# Usage: ./run_match.sh [name] [players-spec] [games]
#   ./run_match.sh mymatch "llm:tencent/hy3,llm:google/gemini-3.7-flash" 5
# Watch it live:  .venv/bin/python watch.py mymatch_g0 --follow  (or the dashboard)
NAME="${1:-match1}"
PLAYERS="${2:-llm:deepseek/deepseek-v4-flash,llm:tencent/hy3,llm:google/gemini-3.7-flash,llm:openai/gpt-5.6-luna}"
GAMES="${3:-1}"
echo "match $NAME: $GAMES game(s), moves to $NAME.out, state to ${NAME}_g*.jsonl/.pkl"
while true; do
  .venv/bin/python arena.py --games "$GAMES" --players "$PLAYERS" --log "$NAME" --resume >> "$NAME.out" 2>&1 && break
  if [ $? -eq 3 ]; then
    echo "[supervisor] fatal setup error, not resuming" | tee -a "$NAME.out"
    exit 3
  fi
  echo "[supervisor] arena died, resuming in 3s" >> "$NAME.out"
  sleep 3
done
echo "match finished:"
tail -8 "$NAME.out"
