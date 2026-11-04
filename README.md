# pbi-zerodata

Skill + CLI bundle that lets coding agents (Claude Code, Codex) develop Power BI PBIP projects
without company data entering the agent's context.

- `skills/powerbi-zerodata/SKILL.md`: the workflow agents follow.
- `skills/powerbi-zerodata/pbz/`: the `pbz` CLI (Python, stdlib + jsonschema) and `guard.py`, the
  PreToolUse / SessionStart hook shared by Claude Code and Codex.
- `skills/powerbi-zerodata/engine/`: `pbz-engine`, the C# declassification broker (TOM + ADOMD.NET)
  that runs checks against Power BI Desktop's local engine and prints verdicts only.
- `skills/powerbi-zerodata/tools/`: Microsoft's `powerbi-report-author` and `powerbi-desktop`
  CLIs (npm, local install; `screenshot` / `preview` are blocked by the guard).

## Install

```
cd skills/powerbi-zerodata/tools && npm install          # Microsoft PBIR + Desktop bridge CLIs
cd ../engine && dotnet publish -c Release -r win-x64 --self-contained false -o bin
python ../install/install.py --what-if                    # preview
python ../install/install.py                              # junctions, hooks, rule blocks
```

Then trust the hooks once in Codex (`/hooks`). Uninstall: `python install/install.py --uninstall`.

## Tests

```
python skills/powerbi-zerodata/tests/test_guard.py     # hook decision table (Claude + Codex shapes)
python skills/powerbi-zerodata/tests/test_mirror.py    # tokenizer, round trip, push, rename, conflicts
python skills/powerbi-zerodata/tests/test_engine.py    # engine verbs + canary scan (needs Desktop)
```

This repo is marked synthetic (`.pbz/policy.json`), so the guard treats its fixtures as fake data.
Develop the skill from a session started here, not from a real Power BI workspace.
