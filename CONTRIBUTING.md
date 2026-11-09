# Contributing

Thanks for helping improve `pbi-zerodata`. This project is a **zero-data** skill and CLI: agents
edit Power BI metadata without seeing company values. Keep that boundary intact in every change.

## Development setup (Windows)

Requirements: Python 3.11+, .NET 10 SDK (for the engine), Node.js (optional Microsoft CLIs),
Power BI Desktop for live suites.

```powershell
cd skills/powerbi-zerodata/tools
npm install

cd ../engine
dotnet publish -c Release -r win-x64 --self-contained false -o bin

cd ..
python install/install.py --what-if
python install/install.py
```

That junctions the Claude, Codex / ChatGPT, and shared agent skill folders at this clone.
See [docs/install.md](docs/install.md). Trust hooks once in your agent host (for example Codex `/hooks`).

## Tests

Offline CI suites must stay green. From `skills/powerbi-zerodata`:

```powershell
python tests/test_guard.py
python tests/test_ci_registration.py
# Prefer the full list in .github/workflows/tests.yml before opening a PR.
```

Live Desktop / engine tests (`test_engine.py` without `--offline`, `test_e2e.py`) stay local and
must use synthetic or fixture models only.

## Documentation

- Public landing and onboarding: root [`README.md`](README.md) and [`docs/`](docs/INDEX.md).
- Agent contract: [`skills/powerbi-zerodata/SKILL.md`](skills/powerbi-zerodata/SKILL.md).
- Deep topic docs: [`skills/powerbi-zerodata/reference/`](skills/powerbi-zerodata/reference/).
- CLI inventory: [`docs/cli-catalog.md`](docs/cli-catalog.md).

When you add or rename a `pbz` verb:

1. Wire it in `pbz/cli.py` (and MCP parity if applicable).
2. Document it in `docs/cli-catalog.md` and the matching `reference/*.md` topic.
3. Register offline coverage in `.github/workflows/tests.yml` when you add a suite
   (`test_ci_registration.py` enforces this).
4. Note the change in [`CHANGELOG.md`](CHANGELOG.md).

## Safety rules for PRs

- No real company data, PBIX/PBIP production files, screenshots of populated visuals, or query
  result dumps.
- Prefer invented fixtures; never relax the guard, mirror tokenizer, or engine closed vocabulary
  “just for convenience.”
- Fail closed on unusable policy and aliased mirror/state paths.
- Do not commit `node_modules/`, `engine/bin/`, `.pytest_cache/`, or live `pbz-workspace/` trees.

## Pull requests

- Keep changes focused; separate refactors from behavior changes when practical.
- Describe the threat-model impact if you touch `guard.py`, `mirror.py`, `policy.py`, `evidence.py`,
  `synthtest.py`, `recipes.py`, or the engine contract.
- Link related docs updates in the same PR.
