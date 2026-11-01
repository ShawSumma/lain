from __future__ import annotations

from config import Provider

API_KEY = "sk-################################"
OPENROUTER_KEY = "sk-or-v1-################################################################"
PROVIDER_KEYS: dict[Provider, str] = {"deepseek": API_KEY, "openrouter": OPENROUTER_KEY}


def given_keys() -> tuple[str, ...]:
    found: list[str] = []
    for key in PROVIDER_KEYS.values():
        if key != "":
            found.append(key)
    return tuple(found)


KEYS = given_keys()
