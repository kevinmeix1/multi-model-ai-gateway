"""Built-in model provider adapters."""

from aegis_gateway.providers.anthropic import AnthropicAdapter
from aegis_gateway.providers.base import ProviderAdapter, ProviderRegistry
from aegis_gateway.providers.mock import MockAdapter
from aegis_gateway.providers.ollama import OllamaAdapter
from aegis_gateway.providers.openai import OpenAIAdapter

__all__ = [
    "AnthropicAdapter",
    "MockAdapter",
    "OllamaAdapter",
    "OpenAIAdapter",
    "ProviderAdapter",
    "ProviderRegistry",
]
