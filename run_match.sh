#!/bin/sh
# Start or resume a durable match; auto-restarts until it finishes.
# Usage: ./run_match.sh [name] [players-spec]
#   ./run_match.sh mymatch "llm:z-ai/glm-5.2,llm:deepseek/deepseek-v4-flash,llm:qwen/qwen3.7-flash"
# Watch it live from another terminal:  .venv/bin/python watch.py mymatch_g0 --follow
NAME="${1:-match1}"
PLAYERS="${2:-llm:z-ai/glm-5.2,llm:deepseek/deepseek-v4-flash,llm:qwen/qwen3.7-flash}"
echo "match $NAME: logging moves to $NAME.out, state to ${NAME}_g*.jsonl/.pkl"
until .venv/bin/python arena.py --games 1 --players "$PLAYERS" --log "$NAME" --resume >> "$NAME.out" 2>&1; do
  echo "[supervisor] arena died, resuming in 3s" >> "$NAME.out"
  sleep 3
done
echo "match finished:"
tail -8 "$NAME.out"
