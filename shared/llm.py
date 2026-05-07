import os


def complete(
    model: str,
    messages: list[dict],
    max_tokens: int = 1000,
    api_base: str | None = None,
    api_key: str | None = None,
) -> str:
    """Call any LLM and return the response text.

    model string conventions:
      "anthropic/claude-sonnet-4-6"  → Anthropic SDK
      "gpt-4o"                       → OpenAI SDK
      "llama3" + api_base            → any OpenAI-compatible local server
      "openrouter/..."               → OpenRouter via OpenAI SDK
    """
    if model.startswith("anthropic/"):
        return _call_anthropic(model.removeprefix("anthropic/"), messages, max_tokens)
    else:
        return _call_openai(model, messages, max_tokens, api_base, api_key)


def _call_anthropic(model_id: str, messages: list[dict], max_tokens: int) -> str:
    import anthropic

    system = next((m["content"] for m in messages if m["role"] == "system"), None)
    user_messages = [m for m in messages if m["role"] != "system"]

    client = anthropic.Anthropic()
    kwargs = dict(model=model_id, messages=user_messages, max_tokens=max_tokens)
    if system:
        kwargs["system"] = system

    resp = client.messages.create(**kwargs)
    return resp.content[0].text


def _call_openai(
    model_id: str,
    messages: list[dict],
    max_tokens: int,
    api_base: str | None,
    api_key: str | None,
) -> str:
    import openai

    key = api_key or os.environ.get("OPENAI_API_KEY", "local")
    base = api_base  # None → openai SDK uses its default (api.openai.com)

    client = openai.OpenAI(api_key=key, base_url=base)
    resp = client.chat.completions.create(
        model=model_id,
        messages=messages,
        max_tokens=max_tokens,
    )
    return resp.choices[0].message.content
