from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

type Provider = Literal["deepseek", "openrouter"]
type Commands = Literal["host", "docker"]
type Effort = Literal["low", "medium", "high"]

DOCKERFILE = """FROM python:3.12-slim
RUN apt-get update \\
    && apt-get install --yes --no-install-recommends git make gcc g++ libc6-dev \\
    && rm -rf /var/lib/apt/lists/*
"""

ENDPOINTS: dict[Provider, str] = {
    "deepseek": "https://api.deepseek.com/beta/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}


@dataclass(frozen=True)
class Settings:
    provider: Provider
    endpoint: str
    model: str
    review_model: str
    effort: Effort
    light_effort: Effort
    thinking: int
    review_thinking: int
    prompt_limit: int
    retry_waits: tuple[float, ...]
    call_timeout: float
    edit_window: float
    leads: bool
    lead_limit: int
    upkeep_every: float
    upkeep_rechecks: int
    pr_batch: int
    explore_turns: int
    parallel: int
    start_leads: int
    brief_limit: int
    step_limit: int
    record_step: int
    record_budget: int
    result_room: int
    failure_streak: int
    commands: Commands
    dockerfile: str
    container_network: bool
    port: int


SETTINGS = Settings(
    provider="openrouter",
    endpoint=ENDPOINTS["openrouter"],
    model="inception/mercury-3",
    review_model="deepseek/deepseek-v4-pro",
    effort="high",
    light_effort="low",
    thinking=0,
    review_thinking=1500,
    prompt_limit=360_000,
    retry_waits=(5.0, 20.0, 60.0),
    call_timeout=300.0,
    edit_window=3600.0,
    leads=True,
    lead_limit=5,
    upkeep_every=6 * 3600.0,
    upkeep_rechecks=10,
    pr_batch=50,
    explore_turns=60,
    parallel=6,
    start_leads=12,
    brief_limit=200_000,
    step_limit=4_000,
    record_step=6_000,
    record_budget=60_000,
    result_room=20_000,
    failure_streak=5,
    commands="docker",
    dockerfile=DOCKERFILE,
    container_network=False,
    port=7428,
)


def address(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def settings_for(model: str | None) -> Settings:
    if model is not None and model.startswith("deepseek-"):
        return replace(SETTINGS, provider="deepseek", endpoint=ENDPOINTS["deepseek"], model=model)
    elif model is not None:
        return replace(SETTINGS, provider="openrouter", endpoint=ENDPOINTS["openrouter"], model=model)
    else:
        return SETTINGS


def reviewing(settings: Settings) -> Settings:
    return replace(settings, model=settings.review_model, effort=settings.light_effort, thinking=settings.review_thinking)
