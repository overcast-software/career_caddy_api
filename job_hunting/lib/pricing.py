from decimal import Decimal

# Pricing per 1M tokens (input/output)
# Updated: 2025-05 — check https://openai.com/pricing for current rates
MODEL_PRICING = {
    "openai:gpt-4o-mini": {
        "input_per_1m": Decimal("0.15"),
        "output_per_1m": Decimal("0.60"),
    },
    "openai:gpt-4o": {
        "input_per_1m": Decimal("2.50"),
        "output_per_1m": Decimal("10.00"),
    },
    "openai:gpt-4.1-mini": {
        "input_per_1m": Decimal("0.40"),
        "output_per_1m": Decimal("1.60"),
    },
    "openai:gpt-4.1-nano": {
        "input_per_1m": Decimal("0.10"),
        "output_per_1m": Decimal("0.40"),
    },
    "anthropic:claude-3-5-sonnet-20241022": {
        "input_per_1m": Decimal("3.00"),
        "output_per_1m": Decimal("15.00"),
    },
    "anthropic:claude-3-5-haiku-20241022": {
        "input_per_1m": Decimal("0.80"),
        "output_per_1m": Decimal("4.00"),
    },
}

# Fallback for local/Ollama models or unknown models
_DEFAULT_PRICING = {
    "input_per_1m": Decimal("0"),
    "output_per_1m": Decimal("0"),
}


def estimate_cost(model_name: str, input_tokens: int, output_tokens: int) -> Decimal:
    pricing = MODEL_PRICING.get(model_name, _DEFAULT_PRICING)
    input_cost = pricing["input_per_1m"] * Decimal(input_tokens) / Decimal(1_000_000)
    output_cost = pricing["output_per_1m"] * Decimal(output_tokens) / Decimal(1_000_000)
    return (input_cost + output_cost).quantize(Decimal("0.000001"))
