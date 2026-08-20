"""OpenRouter-backed Catan player: any model on openrouter.ai can take a seat.

Each decide() is one stateless chat-completions call: compact game summary +
numbered list of legal actions in, JSON {"reason", "action_index"} out. Any
failure (missing OPENROUTER_API_KEY, API error, refusal, unparseable reply,
bad index) falls back to a random legal action so a game never crashes
mid-arena; fallbacks are counted so contaminated runs are visible.
"""

import json
import os
import random
import re

import httpx

from catanatron import RESOURCES, Player
from catanatron.state_functions import (
    get_actual_victory_points,
    get_visible_victory_points,
    player_key,
    player_num_resource_cards,
)

API_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = (
    "You are an expert Settlers of Catan player. You get the current game "
    "situation and a numbered list of legal actions; pick the one that "
    "maximizes your chance of reaching 10 victory points first.\n"
    "Reminders: settle high-probability numbers (6/8, then 5/9), secure "
    "ore+wheat for cities, avoid holding 8+ cards (robber discard), and use "
    "the robber to block the current leader.\n"
    'Answer with ONLY a JSON object: {"reason": "<one short sentence>", '
    '"action_index": <integer>}'
)

JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)  # first flat JSON object

MAX_ACTIONS_LISTED = 80


def _api_key():
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:  # ponytail: 5-line .env reader beats a python-dotenv dependency
        with open(env_path) as f:
            for line in f:
                name, _, value = line.strip().partition("=")
                if name == "OPENROUTER_API_KEY" and value:
                    return value.strip().strip("'\"")
    except OSError:
        pass
    raise KeyError("OPENROUTER_API_KEY not set (env var or .env file)")


class LLMPlayer(Player):
    def __init__(self, color, model="deepseek/deepseek-v4-flash", timeout=90.0):
        super().__init__(color)
        self.model = model
        self.timeout = timeout
        self._http = None  # lazy: lets keyless runs degrade to random moves
        self.api_calls = 0
        self.fallbacks = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.last_reason = ""
        self.last_error = ""

    def decide(self, game, playable_actions):
        actions = list(playable_actions)
        if len(actions) == 1:  # forced move (roll, end turn): skip the API
            return actions[0]
        try:
            return actions[self._ask(game, actions)]
        except Exception as exc:
            self.fallbacks += 1
            self.last_error = repr(exc)[:200]
            return random.choice(actions)

    def _ask(self, game, actions):
        prompt = self.build_prompt(game, actions)
        if self._http is None:
            key = _api_key()  # raises -> random fallback
            self._http = httpx.Client(
                headers={"Authorization": f"Bearer {key}"}, timeout=self.timeout
            )
        payload = {
            "model": self.model,
            "max_tokens": 4000,  # reasoning models need headroom before content
            "usage": {"include": True},  # exact cost accounting per call
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
        }
        for attempt in (1, 2):  # one retry on transient errors
            response = self._http.post(API_URL, json=payload)
            if response.status_code < 500 and response.status_code != 429:
                break
        response.raise_for_status()
        data = response.json()
        self.api_calls += 1
        usage = data.get("usage") or {}
        self.input_tokens += usage.get("prompt_tokens") or 0
        self.output_tokens += usage.get("completion_tokens") or 0
        self.cost_usd += usage.get("cost") or 0.0
        message = data["choices"][0]["message"]
        choice = None  # reasoning models sometimes leave the JSON outside content
        for text in (message.get("content") or "", message.get("reasoning") or ""):
            for candidate in JSON_RE.findall(text):
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                if "action_index" in parsed:
                    choice = parsed
                    break
            if choice is not None:
                break
        if choice is None:
            raise ValueError(f"no action JSON in reply: {(message.get('content') or '')[:120]!r}")
        self.last_reason = str(choice.get("reason", ""))
        index = int(choice["action_index"])
        if not 0 <= index < len(actions):
            raise ValueError(f"action_index {index} out of range")
        return index

    def build_prompt(self, game, actions):
        # ponytail: v0 prompt = scores + hand + action list, no board geometry.
        # Upgrade path: serialize tiles/ports/buildings when moves look blind.
        state = game.state
        me = player_key(state, self.color)
        hand = ", ".join(
            f"{r}:{state.player_state[f'{me}_{r}_IN_HAND']}" for r in RESOURCES
        )
        opponents = "; ".join(
            f"{c.value}: {get_visible_victory_points(state, c)} VPs, "
            f"{player_num_resource_cards(state, c)} cards"
            for c in state.colors
            if c != self.color
        )
        lines = [
            f"Turn {state.num_turns}. You are {self.color.value}.",
            f"Your hand: {hand}.",
            f"Your victory points: {get_actual_victory_points(state, self.color)}/10.",
            f"Opponents: {opponents}.",
            "",
            "Legal actions:",
        ]
        for i, action in enumerate(actions[:MAX_ACTIONS_LISTED]):
            value = "" if action.value is None else f" {action.value}"
            lines.append(f"{i}: {action.action_type.value}{value}")
        if len(actions) > MAX_ACTIONS_LISTED:
            lines.append(f"(+{len(actions) - MAX_ACTIONS_LISTED} more actions omitted)")
        lines.append('Reply with ONLY the JSON object for your chosen action.')
        return "\n".join(lines)


if __name__ == "__main__":  # smoke test: one real LLM decision (<0.01 cent)
    from catanatron import Color, Game, RandomPlayer

    llm = LLMPlayer(Color.RED)
    others = [RandomPlayer(c) for c in (Color.BLUE, Color.WHITE, Color.ORANGE)]
    game = Game([llm, *others])
    for _ in range(40):  # tick until the LLM faces one multi-option decision
        if llm.api_calls or llm.fallbacks:
            break
        game.play_tick()
    assert llm.api_calls == 1 and llm.fallbacks == 0, (
        llm.api_calls,
        llm.fallbacks,
        llm.last_error,
    )
    print(
        f"OK model={llm.model} tokens={llm.input_tokens}/{llm.output_tokens} "
        f"cost=${llm.cost_usd:.5f} reason={llm.last_reason!r}"
    )
