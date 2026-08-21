# Catan LLM Arena

An arena where AI models play Settlers of Catan against each other. Any model
on [OpenRouter](https://openrouter.ai) can take a seat — one API key, 400+
models, exact per-seat cost accounting.

Built on [Catanatron](https://github.com/bcollazo/catanatron), the open-source
Catan engine (full rules, thousands of games per minute). Inspired by
[Agents of Change](https://arxiv.org/abs/2506.04651) (LLM agents playing Catan).

## Setup

```bash
uv venv --python 3.12
uv pip install catanatron httpx
echo "OPENROUTER_API_KEY=sk-or-..." > .env
```

## Usage

One decision smoke test (fraction of a cent):

```bash
.venv/bin/python llm_player.py
```

Run a durable match (auto-resumes on crash/hang until finished):

```bash
./run_match.sh mymatch "llm:z-ai/glm-5.2,llm:deepseek/deepseek-v4-flash,llm:qwen/qwen3.7-flash"
```

Watch it live from another terminal — board tiles, VPs, hands, buildings,
last moves (state is reconstructed exactly from the move log):

```bash
.venv/bin/python watch.py mymatch_g0 --follow
```

Move-by-move commentary (each model's stated reasoning and running cost):

```bash
tail -f mymatch.out
```

Or watch every match at once in a browser — drawn hex board, seat standings,
and live commentary at http://localhost:8765 :

```bash
.venv/bin/python dashboard.py
```

Seat specs: `llm:<openrouter-slug>`, plus free scripted baselines `random`,
`weighted`, `vp`. 2-4 seats per game. Standings print wins, turns, token usage,
retry counts, and exact dollar cost per seat.

## Running many games at once

One game is an anecdote. `batch.py` runs many matches in parallel, each its own
resumable process, and reports win rates with confidence intervals:

```bash
.venv/bin/python batch.py --matches 50 --dir runs
```

```bash
.venv/bin/python dashboard.py 8765 runs
```

Seats rotate per match (match `i` starts the lineup at `i % 4`), so each model
takes each colour equally often. This does **not** balance turn order: the
engine shuffles play order itself at game start, so first position is random
per game rather than evenly dealt. Turn order is worth real points in Catan
(first position won 36% of a measured 50-game run), so check the seat table on
`/stats` before reading much into a small run.
A crashed match restarts with `--resume`; a fatal setup error stops that match
alone and is reported at the end. Rerun the same command to resume the batch.

`--dry-run` swaps in the free bot seats, which exercises the whole pipeline
without spending anything — useful for checking parallelism on a new machine.

Measured on this setup: 50 concurrent matches is fine (~25 MB per process, no
OpenRouter rate limiting at 50 in-flight requests). Reckon on roughly the
wall-clock of a single game, and $0.10-0.15 per game.

## How it works

The engine enumerates legal actions each step. For every multi-option decision,
the seat's model gets a compact state summary plus a numbered action list and
replies with JSON `{"reason", "action_index"}`. Forced moves skip the API.

**Every move is the model's own.** A failed call (timeout, API error,
unparseable reply, bad index) is never replaced by a random move — it is
retried, with the per-attempt deadline escalating from 20s to a 120s ceiling,
until the model answers. Retries are logged and shown against the decision they
delayed. Only a permanently fatal setup error (bad key, no credit, malformed
request) stops a match, with exit code 3 so the supervisor doesn't loop.

Caveat on cost: a call that misses its deadline was still generated, and so
still billed, but its usage is not counted — measured on one game, 68 abandoned
calls against 187 counted, so real spend runs up to ~36% above the logged
figure.

Cost reference (measured): a cheap-model seat costs about $0.01-0.05 per game;
frontier seats $0.2-0.8. A 3-4 seat game of popular open-weights models runs a
few cents.

Known v0 limits: the prompt has no board geometry yet (models place
settlements semi-blind), and Catanatron supports bank/port trades only — no
player-to-player trade negotiation.

## License

GPL-3.0 — required for compatibility with the Catanatron dependency.
