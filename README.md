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

A 3-model match of the currently most popular open-weights models:

```bash
.venv/bin/python arena.py --games 1 --players "llm:deepseek/deepseek-v4-flash-0731,llm:tencent/hy3,llm:xiaomi/mimo-v2.5"
```

Seat specs: `llm:<openrouter-slug>`, plus free scripted baselines `random`,
`weighted`, `vp`. 2-4 seats per game. Standings print wins, turns, token usage,
fallback counts, and exact dollar cost per seat.

## How it works

The engine enumerates legal actions each step. For every multi-option decision,
the seat's model gets a compact state summary plus a numbered action list and
replies with JSON `{"reason", "action_index"}`. Forced moves skip the API. Any
failure (API error, unparseable reply, bad index) becomes a random legal move
and is counted, so contaminated games are visible in the standings.

Cost reference (measured): a cheap-model seat costs about $0.01-0.05 per game;
frontier seats $0.2-0.8. A 3-4 seat game of popular open-weights models runs a
few cents.

Known v0 limits: the prompt has no board geometry yet (models place
settlements semi-blind), and Catanatron supports bank/port trades only — no
player-to-player trade negotiation.

## License

GPL-3.0 — required for compatibility with the Catanatron dependency.
