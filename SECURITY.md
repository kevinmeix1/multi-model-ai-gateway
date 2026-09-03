# Security policy

## Supported versions

This repository is an early reference implementation. Security fixes are applied to the latest
`main` revision; no long-term-support release line is currently maintained.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository when available. Do not put
credentials, customer prompts, model outputs, personal data, or a working exploit in a public issue.

Include:

- affected commit/version;
- component and deployment assumptions;
- minimal reproduction using synthetic data;
- security impact and affected trust boundary;
- whether a credential or third-party provider is involved;
- suggested mitigation if known.

If a live provider credential is exposed, revoke it immediately before waiting for repository
maintainers. Rewriting Git history does not invalidate a credential.

## Deployment notice

The default service is for local evaluation. Before public deployment, complete the hardening
checklist in [docs/security.md](docs/security.md), especially trusted tenant identity, scoped
control-plane authorization, managed secrets, egress controls, distributed quotas, encryption,
retention, and supply-chain verification.
