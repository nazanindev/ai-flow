"""
Billing utilities shared across flow.

Two surfaces:
  subscription  — Claude Code sessions run against claude.ai Pro/Max login.
                  They cost $0 marginal; we track token + message quota instead.
  api           — flow utility calls (clarify, ship, ci-review) hit the Anthropic
                  API directly with ANTHROPIC_API_KEY. These bill real money
                  (cents per call, Haiku-mostly).

metered_call() wraps every flow-internal SDK call so real API spend is recorded.
"""
import os
import uuid

# Per 1M tokens, USD — API surface only.
COSTS: dict[str, dict[str, float]] = {
    "claude-opus-4-5":             {"in": 15.0,  "out": 75.0},
    "claude-opus-4-7":             {"in": 15.0,  "out": 75.0},
    "claude-sonnet-4-6":           {"in": 3.0,   "out": 15.0},
    "claude-sonnet-4-5":           {"in": 3.0,   "out": 15.0},
    "claude-haiku-4-5-20251001":   {"in": 0.8,   "out": 4.0},
    "claude-haiku-4-5":            {"in": 0.8,   "out": 4.0},
    "gemini/gemini-2.5-flash":     {"in": 0.15,  "out": 0.60},
    "gemini/gemini-2.5-flash-lite":{"in": 0.075, "out": 0.30},
    "gemini/gemini-2.5-pro":       {"in": 1.25,  "out": 10.0},
}


def calc_cost(model: str, tokens_in: int, tokens_out: int, cache_read_tokens: int = 0) -> float:
    rates = COSTS.get(model, {"in": 3.0, "out": 15.0})
    cache_rate = rates["in"] * 0.1  # cache reads billed at 10% of input rate
    return (
        tokens_in * rates["in"]
        + tokens_out * rates["out"]
        + cache_read_tokens * cache_rate
    ) / 1_000_000


_MOCK_API: bool = os.getenv("FLOW_MOCK_API") == "1"


class _MockResponse:
    """Fake SDK response returned when FLOW_MOCK_API=1."""
    class _Usage:
        input_tokens = 0
        output_tokens = 0
    class _Content:
        text = "[mock] FLOW_MOCK_API=1 — real API call skipped"
    usage = _Usage()
    content = [_Content()]


class _GeminiResponse:
    """Adapts a LiteLLM response to the Anthropic messages shape callers expect."""
    class _Content:
        def __init__(self, text: str):
            self.text = text

    class _Usage:
        def __init__(self, inp: int, out: int):
            self.input_tokens = inp
            self.output_tokens = out

    def __init__(self, text: str, tokens_in: int, tokens_out: int):
        self.content = [self._Content(text)]
        self.usage = self._Usage(tokens_in, tokens_out)


def _litellm_call(model: str, *, run_id: str, purpose: str,
                  system: "str | None" = None, messages: list, **kwargs) -> _GeminiResponse:
    import litellm  # already a project dep; import lazily to keep startup fast

    lm = ([{"role": "system", "content": system}] if system else []) + list(messages)
    resp = litellm.completion(model=model, messages=lm, **kwargs)

    text = resp.choices[0].message.content or ""
    tokens_in = getattr(resp.usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(resp.usage, "completion_tokens", 0) or 0
    cost = calc_cost(model, tokens_in, tokens_out)

    try:
        from flow.tracker import save_session
        save_session(
            session_id=str(uuid.uuid4())[:8],
            run_id=run_id,
            project="ap-internal",
            branch="",
            phase=purpose,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            billing_source="api",
        )
    except Exception:
        pass

    return _GeminiResponse(text, tokens_in, tokens_out)


def metered_call(client, model: str, *, run_id: str = "none", purpose: str, **kwargs):
    """
    Drop-in wrapper around client.messages.create that records real API spend
    in the sessions table (billing_source='api').

    Set FLOW_MOCK_API=1 to skip the real API call and return a stub response —
    useful when API credits are unavailable but you want to test the pipeline.

    Usage:
        resp = metered_call(client, HAIKU, run_id=run.run_id,
                            purpose="commit_msg", max_tokens=120,
                            system=system, messages=[...])
    """
    if os.getenv("FLOW_MOCK_API") == "1":
        return _MockResponse()

    if model.startswith("gemini"):
        return _litellm_call(model, run_id=run_id, purpose=purpose, **kwargs)

    resp = client.messages.create(model=model, **kwargs)

    tokens_in = getattr(resp.usage, "input_tokens", 0)
    tokens_out = getattr(resp.usage, "output_tokens", 0)
    cost = calc_cost(model, tokens_in, tokens_out)

    try:
        from flow.tracker import save_session
        save_session(
            session_id=str(uuid.uuid4())[:8],
            run_id=run_id,
            project="ap-internal",
            branch="",
            phase=purpose,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            billing_source="api",
        )
    except Exception:
        pass  # never let tracking kill the actual call

    return resp
