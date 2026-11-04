# Reference projects

No maintained project combines a byte-exact PBIP mirror, reversible typed tokens, a value-blind
broker and agent hooks. These are the building blocks worth reusing or studying. Licenses and
activity as reported by the research; re-check before vendoring.

## Parsing, validation, lint

| Project | Lang / license | Use for pbz |
|---|---|---|
| [TabularEditor/TabularEditor `DAXLexer.g4`](https://github.com/TabularEditor/TabularEditor/blob/master/AntlrGrammars/DAXLexer.g4) | C# / ANTLR, MIT | Replace the hand-written DAX lexer (strings, comments, identifiers) |
| [microsoft/powerquery-parser](https://github.com/microsoft/powerquery-parser) (npm `@microsoft/powerquery-parser`) | TypeScript, MIT | M lexer/parser with positions (UTF-16 offsets) in a small Node worker |
| [microsoft/powerquery-language-services](https://github.com/microsoft/powerquery-language-services) | TypeScript | Scope-aware M references and rename edits |
| TOM `TmdlSerializer` (NuGet Microsoft.AnalysisServices 19.118.3) | .NET | Already used in pbz-engine for structural validation (not byte-preserving) |
| [Atzingen/tmdl-parser](https://github.com/Atzingen/tmdl-parser) (PyPI) | Python, MIT | Extra test oracle for the TMDL reader |
| [akhilannan/pbir-utils](https://github.com/akhilannan/pbir-utils) (PyPI) | Python, MIT | Filter traversal, measure dependencies, value-free wireframes |
| [NatVanG/fab-inspector](https://github.com/NatVanG/fab-inspector) (ex PBI-InspectorV2) | C#, MIT | Declarative PBIR rules for `pbz validate` (use metadata operators only) |
| [microsoft/Analysis-Services BestPracticeRules](https://github.com/microsoft/Analysis-Services/tree/master/BestPracticeRules) | JSON / Dynamic LINQ, MIT | Model lint rules, run in the broker, report rule ids + object names |
| [microsoft/semantic-link-labs](https://github.com/microsoft/semantic-link-labs) | Python, MIT | PBIR traversal and TOM helper patterns (reference) |
| [microsoft/json-schemas (fabric/item/report)](https://github.com/microsoft/json-schemas/tree/main/fabric/item/report) | JSON Schema | Vendor the pinned PBIR schemas for offline validation |
| [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric) | Markdown, MIT | Power BI task decomposition for SKILL.md (strip live-query and screenshot paths) |
| [data-goblin/power-bi-agentic-development](https://github.com/data-goblin/power-bi-agentic-development) | GPL-3.0 + extra terms | Reference only: PBIR/TMDL validation hooks, reviewer roles (it allows queries/screenshots) |

## Sensitive-value detection

| Project | Lang / license | Use for pbz |
|---|---|---|
| [Presidio](https://github.com/data-privacy-stack/presidio) (`presidio-analyzer`, `presidio-anonymizer`) | Python, MIT | Custom recognizers (wrap the HMAC index), allow lists, context words, reversible mappings |
| [GLiNER](https://github.com/urchade/GLiNER) + NVIDIA GLiNER-PII weights | Python, Apache-2.0 (weights: NVIDIA license) | Optional local person/org detector for unbound free text |
| [spaCy](https://github.com/explosion/spaCy) | Python, MIT | Token boundaries and context ("owner: NAME") |
| [wordfreq](https://github.com/rspeer/wordfreq) | Python, Apache-2.0 (data CC-BY-SA) | Common-word specificity (`zipf_frequency >= 4.5` = too common to match) |

## Agent guards and tests

| Project | Lang / license | Use for pbz |
|---|---|---|
| [openai/codex `codex-rs/hooks`](https://github.com/openai/codex) | Rust, Apache-2.0 | Codex hook protocol fixtures (PreToolUse, apply_patch, shell, MCP) |
| [pchalasani/claude-code-tools `plugins/safety-hooks`](https://github.com/pchalasani/claude-code-tools) | Python, MIT | Hook decision aggregation and file-protection tests |
| [open-policy-agent/opa](https://github.com/open-policy-agent/opa) | Go/Rego, Apache-2.0 | Optional single policy shared by both hook adapters and the broker |
| [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo) | TypeScript, MIT | Canary / adversarial regression harness (local graders only) |

## Design guidance adopted

1. Provenance first: a literal bound to a sensitive column is hidden whatever it looks like.
2. Unbound free text is matched only against distinctive values of person, identifier and title
   columns; never against descriptions, comments, notes, numbers or dates stored as text, and
   never against category labels.
3. Minimum specificity for unbound matches: at least 3 letters and 4 characters, not a common
   word, not a model object name; numeric text is never matched by membership alone.
4. Raw spellings are kept per token (no case-folded collisions), so round trips stay byte-exact.
5. Known caveats: Codex hooks do not cover hosted tools or `write_stdin`; hooks are accident
   prevention, an OS-level identity split is the hard boundary (documented hardening tier).
