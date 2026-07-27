#!/usr/bin/env python3
"""Per-provider translation for thinking mode, reasoning effort and prompt caching.

Why this exists: everything in this repo talks exactly one protocol -
OpenAI-compatible /chat/completions - but "think harder" is spelled differently by
every provider sitting behind that protocol. DeepSeek wants a `thinking` object in
the request body plus a top-level `reasoning_effort` that only accepts high/max;
OpenRouter wants its own `reasoning` object; OpenAI wants a bare `reasoning_effort`
whose ladder stops at "high"; Ollama's OpenAI-compatible endpoint accepts only
low/medium/high and quietly ignores the rest. Keeping that table in one place means
custom_agent_loop stays a plain agent loop rather than a pile of provider ifs.

The rest of the app speaks one normalized ladder - minimal|low|medium|high|max -
and this module lowers it onto whatever endpoint is actually configured. Anything
it gets wrong is caught by the unsupported-parameter fallback in custom_agent_loop,
which strips the offending key and retries, so a new or unknown provider degrades
to a plain non-thinking call instead of failing the run.
"""
from collections import namedtuple

# The ladder the settings page, the chat picker and the env vars all speak.
# Ordered weakest -> strongest; index order is load-bearing for the per-provider
# clamping below.
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")
DEFAULT_EFFORT = "max"

# Optional request params this module may add. The fallback in custom_agent_loop
# only ever strips keys from this list, so a bad guess here can never remove
# something the request actually needs (model/messages/tools/tool_choice).
OPTIONAL_PARAMS = ("stream_options", "reasoning_effort", "thinking", "reasoning", "think")

# Model IDs DeepSeek retired on 2026-07-24. They now return HTTP 400, and this
# repo shipped `deepseek-chat` as its documented default for a long time, so an
# existing .env very likely still names one.
RETIRED_MODELS = {
    "deepseek-chat": "deepseek-v4-flash",
    "deepseek-reasoner": "deepseek-v4-flash",
}


class LLMProfile(namedtuple("LLMProfile", "dialect thinking effort base_url model")):
    """How to ask this endpoint to think. Immutable so the chat server can hand a
    per-request variant to run_loop without touching the process-global client."""

    def with_effort(self, effort):
        return self._replace(effort=effort)

    def with_thinking(self, thinking):
        return self._replace(thinking=bool(thinking))


def detect(base_url):
    """Classify an endpoint from its base URL alone.

    Only the URL is consulted, never the model name: on OpenRouter the model is
    `deepseek/deepseek-v4-flash` but the request dialect is OpenRouter's, so keying
    off the model would pick the wrong translation.
    """
    u = (base_url or "").lower()
    if "openrouter.ai" in u:
        return "openrouter"
    if "api.deepseek.com" in u:
        return "deepseek"
    if "api.openai.com" in u:
        return "openai"
    if ":11434" in u or "ollama" in u:
        return "ollama"
    # LM Studio (:1234), llama.cpp (:8080) and friends land here deliberately -
    # they mostly accept a plain reasoning_effort, and the fallback covers the rest.
    return "generic"


def _clamp(effort, allowed):
    """Snap a normalized effort onto the strongest level this provider accepts."""
    if effort in allowed:
        return effort
    want = EFFORT_LEVELS.index(effort)
    # Walk down from the requested level to the closest weaker one the provider
    # supports, then (if the provider has no weaker level) up to the closest stronger.
    for lvl in reversed(EFFORT_LEVELS[:want + 1]):
        if lvl in allowed:
            return lvl
    for lvl in EFFORT_LEVELS[want + 1:]:
        if lvl in allowed:
            return lvl
    return effort


def normalize_effort(value, default=DEFAULT_EFFORT):
    """Coerce a user-supplied effort string onto the ladder.

    Returns (thinking, effort). The literal "off" is how the chat picker and the
    settings toggle both say "don't think", so it is understood here rather than
    forcing every caller to special-case it. Unrecognized values fall back to the
    default instead of raising - a typo in .env should not take the pipeline down.
    """
    s = (value or "").strip().lower()
    if not s:
        return True, default
    if s in ("off", "0", "false", "none", "disabled"):
        return False, default
    if s in EFFORT_LEVELS:
        return True, s
    # Aliases people reasonably reach for.
    if s in ("min", "lowest"):
        return True, "minimal"
    if s in ("xhigh", "highest", "maximum"):
        return True, "max"
    return True, default


def reasoning_kwargs(profile):
    """(kwargs, extra_body) to merge into client.chat.completions.create().

    `kwargs` are native OpenAI SDK parameters; `extra_body` is merged verbatim into
    the JSON body for the provider-specific objects the SDK has no typed field for.
    """
    dialect, thinking, effort = profile.dialect, profile.thinking, profile.effort

    if dialect == "deepseek":
        # DeepSeek's own docs: low/medium map to high and xhigh maps to max, so the
        # only two values that actually change behaviour are high and max.
        if not thinking:
            return {}, {"thinking": {"type": "disabled"}}
        return ({"reasoning_effort": _clamp(effort, ("high", "max"))},
                {"thinking": {"type": "enabled"}})

    if dialect == "openrouter":
        # OpenRouter normalizes across model families itself - it maps effort onto
        # Anthropic's budget_tokens, Gemini's thinkingLevel and so on - so the full
        # ladder passes straight through.
        if not thinking:
            return {}, {"reasoning": {"enabled": False}}
        return {}, {"reasoning": {"enabled": True, "effort": effort}}

    if dialect == "openai":
        # Reasoning models cannot be told to stop reasoning outright; "minimal" is
        # the floor. Non-reasoning models reject the param entirely and the
        # fallback strips it.
        if not thinking:
            return {"reasoning_effort": "minimal"}, {}
        return {"reasoning_effort": _clamp(effort, ("minimal", "low", "medium", "high"))}, {}

    if dialect == "ollama":
        # Ollama's OpenAI-compatible endpoint takes only low/medium/high. Turning
        # thinking off is best-effort: `think` is honoured on the native /api/chat
        # endpoint and, depending on version, ignored on /v1/chat/completions.
        if not thinking:
            return {}, {"think": False}
        return {"reasoning_effort": _clamp(effort, ("low", "medium", "high"))}, {}

    # generic: reasoning_effort is the most widely accepted spelling. If the
    # endpoint has never heard of it the fallback drops it and remembers.
    if not thinking:
        return {}, {}
    return {"reasoning_effort": effort}, {}


_REASONING_FIELDS = ("reasoning_content", "reasoning")


def reasoning_from_delta(delta):
    """Reasoning text carried by a streaming delta (or a whole message), if any.

    DeepSeek calls it `reasoning_content`; OpenRouter and several others call it
    `reasoning`. The OpenAI SDK keeps unknown response fields, so both are readable
    as plain attributes.
    """
    for field in _REASONING_FIELDS:
        value = getattr(delta, field, None)
        if value:
            return value
    return None


def echoes_reasoning(dialect):
    """Whether an assistant turn that made tool calls must carry its reasoning back.

    DeepSeek is strict about this: "for turns that do perform tool calls, the
    reasoning_content must be fully passed back to the API in all subsequent
    requests", and it returns 400 if you don't. Every agent path in this repo is
    tool-calling, so getting this wrong breaks thinking mode outright.

    (OpenRouter+Anthropic has a softer version of the same idea via the structured
    `reasoning_details` array. Echoing the plaintext is not an exact substitute, but
    unlike DeepSeek it degrades rather than erroring.)
    """
    return dialect in ("deepseek", "openrouter")


def wants_cache_breakpoints(dialect, model):
    """Whether this endpoint needs an explicit cache_control breakpoint.

    Almost everywhere prompt caching is automatic and prefix-based - DeepSeek's disk
    KV cache, OpenAI above ~1024 tokens - and the only thing that matters is that a
    long stable prefix exists. OpenRouter is the exception: it caches OpenAI/DeepSeek
    /Grok automatically but needs a breakpoint marked by hand for Anthropic and
    Gemini models.
    """
    if dialect != "openrouter":
        return False
    m = (model or "").lower()
    return m.startswith("anthropic/") or m.startswith("google/")


def apply_cache_breakpoint(messages, profile):
    """Copy of `messages` with a cache_control breakpoint closing the system block.

    Returns the original list unchanged for every provider that caches
    automatically, so the common path allocates nothing. The copy is shallow and
    only the system message is rebuilt - callers pass live session history here and
    must not have it mutated underneath them.
    """
    if not messages or not wants_cache_breakpoints(profile.dialect, profile.model):
        return messages
    head = messages[0]
    if not isinstance(head, dict) or head.get("role") != "system":
        return messages
    content = head.get("content")
    if not isinstance(content, str) or not content:
        return messages  # already content-parts, or empty; leave it alone
    marked = {**head, "content": [{"type": "text", "text": content,
                                   "cache_control": {"type": "ephemeral"}}]}
    return [marked] + list(messages[1:])


def retired_model_warning(base_url, model):
    """A migration warning for a model ID the provider has switched off, else None."""
    replacement = RETIRED_MODELS.get((model or "").strip())
    if replacement and detect(base_url) == "deepseek":
        return (f"LLM_MODEL={model!r} was retired by DeepSeek on 2026-07-24 and now "
                f"returns HTTP 400. Use {replacement!r} (or 'deepseek-v4-pro') instead.")
    return None
