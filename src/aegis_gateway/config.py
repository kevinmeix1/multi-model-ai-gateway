"""Environment and model-catalog configuration."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aegis_gateway.domain import ModelRoute


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AEGIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: Path = Path("data/aegis.db")
    model_catalog_path: Path = Path("configs/models.yaml")
    ollama_base_url: str = "http://localhost:11434"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    admin_token: SecretStr = SecretStr("development-only")
    log_level: str = "INFO"
    enable_console: bool = True
    request_rate_per_second: float = Field(default=5, gt=0)
    request_burst: int = Field(default=20, gt=0)
    cache_ttl_seconds: int = Field(default=900, gt=0)
    cache_similarity_threshold: float = Field(default=0.96, gt=0, le=1)
    circuit_failure_threshold: int = Field(default=3, gt=0)
    circuit_recovery_seconds: float = Field(default=30, gt=0)
    policy_version: str = "utility-v1"


def load_model_catalog(path: Path) -> list[ModelRoute]:
    """Load and strictly validate a YAML route catalog."""

    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict) or not isinstance(raw.get("routes"), list):
        raise ValueError("model catalog must contain a 'routes' list")
    routes = [ModelRoute.model_validate(item) for item in raw["routes"]]
    route_ids = [route.id for route in routes]
    if len(route_ids) != len(set(route_ids)):
        raise ValueError("model catalog contains duplicate route ids")
    return routes
