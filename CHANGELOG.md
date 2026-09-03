# Changelog

Notable changes are recorded here. The project follows semantic versioning once versioned releases
begin.

## 0.1.0 — 2026-09-04

Initial reference implementation:

- policy-first routing across OpenAI, Anthropic, Ollama, and deterministic adapters;
- normalized complete and streaming paths;
- tenant rate limits, route circuit breakers, semantic cache, and compatible fallback;
- immutable artifact, release, evaluation, and evidence stores;
- deterministic shadow/canary allocation and automated rollback;
- native and OpenAI-compatible APIs;
- Prometheus metrics, structured logs, Docker, Kubernetes, and CI;
- 60 deterministic tests with branch coverage above the enforced threshold;
- engineering handbook covering architecture, routing, providers, reliability, evaluation, release,
  security, observability, deployment, operations, and testing.
