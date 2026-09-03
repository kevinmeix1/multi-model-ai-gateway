# Architecture decision records

These records preserve why the system is shaped this way. They are intentionally short and
decision-focused; implementation detail belongs in the linked handbook pages.

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-constraint-first-routing.md) | Apply hard policy constraints before utility ranking | Accepted |
| [0002](0002-no-midstream-provider-switch.md) | Never switch providers after exposing content | Accepted |
| [0003](0003-immutable-versioned-artifacts.md) | Store prompts, datasets, models, and policies as immutable versions | Accepted |
| [0004](0004-single-process-reference.md) | Ship a single-process reference with replaceable state interfaces | Accepted |
| [0005](0005-provider-anti-corruption-layer.md) | Normalize provider protocols behind adapters | Accepted |

New records use the next four-digit number and contain context, decision, consequences, alternatives,
and references. Accepted records are not edited to hide later changes; supersede them with a new ADR.
