# Install: live skill folders and guard hooks

One script points Claude Code, Codex, and ChatGPT Codex at this clone. Cursor reads the
Claude and Codex skill folders, so the same links cover Cursor. After that, `git pull`
updates the skill, the reference docs, and the guard script those agents load.

```powershell
cd skills/powerbi-zerodata
python install/install.py --what-if
python install/install.py
```

Trust the new PreToolUse and SessionStart hooks once in each host (Codex: `/hooks`).
Optional MCP registration: `python install/install.py --mcp`. Remove the links and the
pbz hook entries with `python install/install.py --uninstall`. That removes the junctions
only. The clone stays where it is.

## What gets linked

The installer creates a Windows **directory junction** (a folder link, `mklink /J`) from each
agent skill folder to `skills/powerbi-zerodata` in this clone:

| Agent | Folder that receives the junction |
|---|---|
| Shared agent skills | `%USERPROFILE%\.agents\skills\powerbi-zerodata` |
| Claude Code | `%USERPROFILE%\.claude\skills\powerbi-zerodata` |
| Codex and ChatGPT Codex | `%USERPROFILE%\.codex\skills\powerbi-zerodata` |

A junction is a pointer. The files live once, in the clone. Open `SKILL.md` from any of those
folders and you are reading the clone. The next `git pull` in the clone is the update. There
is no copy step and no drag-and-drop into a skills directory.

Cursor loads user skills from the Claude folder and the Codex folder. It does not need a
separate copy under `%USERPROFILE%\.cursor\skills`.

## What the hooks point at

Hook configuration stays in the host's own settings, because those files also hold the rest
of that host's configuration:

| Host | Settings file | Events |
|---|---|---|
| Claude Code | `%USERPROFILE%\.claude\settings.json` | PreToolUse, SessionStart |
| Codex and ChatGPT Codex | `%USERPROFILE%\.codex\hooks.json` | PreToolUse, SessionStart |

Each pbz entry runs:

```text
python -I -S %USERPROFILE%/.agents/skills/powerbi-zerodata/pbz/guard.py --agent claude
```

Codex uses `--agent codex`. That path is the agents junction, so the hook runs the
`pbz/guard.py` from the clone. A pull updates the hook script. Run `install.py` again when
the registered command itself should change, for example after you move the clone or switch
Python. The script is idempotent: an existing junction that already points here is left in
place, and a matching hook entry is left in place. It backs up any settings file it edits.

The installer also writes a short always-on rule between HTML markers in
`%USERPROFILE%\.claude\CLAUDE.md` and `%USERPROFILE%\.codex\AGENTS.md`, and adds Claude
Read-deny rules for Power BI caches.

## If a folder is already a real copy

When the skill folder exists and is a normal directory, the script prints `SKIP` and leaves
it alone. Move that copy aside, then run `install.py` again so the junction can be created.
A junction that points at a different clone is also skipped until you remove it.

## Engine and CLI

The skill links do not build the Desktop broker. From `skills/powerbi-zerodata`:

```powershell
cd tools
npm install
cd ../engine
dotnet publish -c Release -r win-x64 --self-contained false -o bin
```

The CLI on `PATH` after install is `bin/pbz.cmd` inside the skill folder, which is the same
file as `skills/powerbi-zerodata/bin/pbz.cmd` in the clone.
