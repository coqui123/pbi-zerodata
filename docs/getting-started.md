# Getting started

Windows-focused. Run commands from the folder that holds your `.pbip` (the workspace root).

## 1. Install

```powershell
cd skills/powerbi-zerodata/tools
npm install   # optional: Microsoft PBIR + Desktop bridge CLIs

cd ../engine
dotnet publish -c Release -r win-x64 --self-contained false -o bin

cd ..
python install/install.py --what-if
python install/install.py
```

The installer junctions `%USERPROFILE%\.claude\skills\powerbi-zerodata`,
`%USERPROFILE%\.codex\skills\powerbi-zerodata`, and
`%USERPROFILE%\.agents\skills\powerbi-zerodata` at this clone. Codex and ChatGPT Codex use the
`.codex` folder. Cursor reads the Claude and Codex folders. Hook commands run `pbz/guard.py`
through the agents junction, so `git pull` updates the skill and the hook script together.
Trust those hooks once. Details: [install.md](install.md). Uninstall with
`python install/install.py --uninstall`.

Requirements: Python 3.11+, .NET 10 for the engine, Power BI Desktop for live checks.

## 2. Initialize a workspace

```powershell
pbz init          # creates .pbz/policy.json, scans data-bearing files, first pull
pbz status        # mirror, Desktop, engine, guard
pbz doctor        # read-only OS isolation checklist (changes nothing)
pbz capabilities  # what pbz can and cannot certify
```

After `init`, edit only under `pbz-workspace/`. Never open real `*.SemanticModel` / `*.Report`
folders in the agent.

## 3. Orient on the model (metadata only)

```powershell
pbz describe
pbz describe Sales
pbz describe "[Total Sales]"
pbz values 'Date'[Year]     # shape / tokenized samples from the mirror — not live query grids
pbz unused
pbz lineage "[Total Sales]"
```

## 4. Make a safe change

```powershell
# Example: rename a measure and fix references in the mirror
pbz rename measure "[Old Name]" "[New Name]"

pbz validate
pbz preflight          # full staged validation without writing real files
pbz diff
pbz push               # backup + validate + write real files
# or: pbz push --dry-run
```

If Desktop already has the report open for a report-only change:

```powershell
pbz push --reload
```

## 5. Verify without leaking values

```powershell
pbz desktop open
pbz health
pbz check checks.json
pbz snapshot save baseline --spec queries.json
pbz snapshot compare baseline
pbz synth build
pbz synth-test save my-case --spec case.json
pbz synth-test run my-case
pbz smoke
```

All of these print closed verdicts or synthetic results — never company cell values.

## 6. Hand off to a human

```powershell
pbz handoff
pbz review
# In your own terminal (not the agent):
pbz review --accept
```

For external files the agent needs (a requirements workbook, brand asset):

```powershell
pbz consent request C:\path\to\file.xlsx --purpose requirements
# Human inspects the file, then:
pbz consent approve <id> --attested
pbz consent read <id>
```

## 7. Day-to-day pull / push

```powershell
pbz pull                 # refresh mirror from real files
pbz pull --rebase        # keep unpushed mirror edits
pbz revert --object "measure 'Sales'[X]"
pbz recover              # interrupted push recovery (Desktop closed)
```

## Where to go next

- Full verb list: [cli-catalog.md](cli-catalog.md)
- Concepts: [overview.md](overview.md)
- Agent rules: [../skills/powerbi-zerodata/SKILL.md](../skills/powerbi-zerodata/SKILL.md)
- Workspace verbs detail: [../skills/powerbi-zerodata/reference/workspace.md](../skills/powerbi-zerodata/reference/workspace.md)
