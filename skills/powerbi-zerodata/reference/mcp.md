# pbz as an MCP server

`pbz mcp` serves the pbz verbs as typed Model Context Protocol tools over stdio (newline-delimited
JSON-RPC 2.0, protocol versions 2025-06-18, 2025-03-26 and 2024-11-05). It needs nothing beyond
Python. Hosts show the tools as `mcp__pbz__<tool>`.

## Running it

```
python -I <skill>/pbz/cli.py mcp                      # workspace = the server's working folder
python -I <skill>/pbz/cli.py --workspace <ws> mcp      # fixed default workspace
```

Register it once for Claude Code (user scope) and Codex with the installer:

```
python install/install.py --mcp --what-if    # preview
python install/install.py --mcp              # mcpServers.pbz in ~/.claude.json, [mcp_servers.pbz] in ~/.codex/config.toml
```

`python install/install.py --uninstall` removes both entries again. Claude Code users can instead
run `claude mcp add --scope user pbz -- <python> -I <skill>/pbz/cli.py mcp`. Long tools (push,
smoke, index, desktop open) can take minutes; raise the host's MCP tool timeout if it cuts them off
(Claude Code: the `MCP_TOOL_TIMEOUT` environment variable, in milliseconds).

## Tools

Every tool takes optional `workspace` (default: the server's `--workspace`, else its working folder)
and `project` (when the workspace holds several `.pbip` files), plus its own arguments:

| Tool | Arguments |
|---|---|
| `status`, `init`, `validate`, `health`, `index` | none |
| `scan` | `dry_run` |
| `pull` | `force`, `rebase` |
| `diff` | `patch` |
| `push` | `reload`, `dry_run`, `only` |
| `revert` | `paths`, `object` |
| `style` | `page`, `visual`, `set`, `unset`, `type`, `all`, `series`, `state`, `show`, `list`, `object` |
| `link` | `page`, `visual` (required), `to_page`, `back`, `bookmark`, `drillthrough`, `url`, `tooltip`, `clear` |
| `filter` | `action` (add / remove / list, required), `target`, `report`, `page`, `visual`, `values_in`, `values_out`, `where`, `top`, `by`, `bottom` |
| `interaction` | `page`, `source`, `target`, `mode` (all required) |
| `bookmark` | `action` (add / remove / list, required), `name`, `page`, `hide`, `show`, `only` |
| `evidence` (the `pbz review` packet) | none |
| `set_props` / `get_props` | `items` (object and prop=value pairs), `kind`, `table`, `match`, `folder`, `unset`, `dry_run` |
| `copy` | `kind` (page / visual), `page` (required), `visual`, `as`, `after`, `independent_slicers`, `to_page`, `pos` |
| `a11y` | `page`, `fix_tab_order`, `min_font` |
| `mobile` | `page`, `visual` (required), `pos`, `remove` |
| `hierarchy` | `action`, `table`, `name` (required), `levels`, `names`, `force` |
| `format_expr` | `measure` (required), `dax`, `remove`, `upgrade_compat` |
| `function` | `action` (required), `name`, `params`, `body`, `to`, `force`, `upgrade_compat` |
| `recipe` | `action` (run / check), `spec` (required), `params` |
| `synth_test` | `action` (save / run / list / remove, required), `name`, `spec`, `measures`, `by`, `replace`, `tol` |
| `visual` | `action` (list / roles / import / register, required), `target`, `as`, `roles`, `replace` |
| `consumers` | `action` (list / add / scan / show / discover, required), `target`, `register`, `depth`, `kind`, `label`, `objects`, `model_names` |
| `culture`, `translate`, `perspective`, `role`, `refresh_policy` | see `pbz <verb> --help` (same arguments) |
| `describe` | `target` |
| `values` | `column` (required) |
| `rename` | `kind` (measure / column / table), `old`, `new` (required), `table` |
| `move_measure` | `name`, `to` (required) |
| `unused` | `all`, `kinds`, `report`, `impact`, `remove`, `dry_run`, `cascade`, `only`, `m_step`, `restore`, `verify_engine` |
| `lineage` | `target` (required), `up`, `depth`, `report` |
| `suggest` | `report` |
| `bpa` | `scope`, `rules`, `opinion`, `labels`, `report`, `fix`, `dry_run` |
| `similar` | `threshold`, `max` |
| `changes` | `from`, `to`, `list` |
| `docs` | `format`, `dax` |
| `m_lineage` | `table` |
| `layout` | `page`, `no_map` |
| `format` | `target`, `apply` |
| `repair` | `dry_run`, `fields`, `lineage` |
| `new` | `kind`, `name` (required), `start`, `end`, `relate`, `date`, `items`, `precedence`, `fields`, `labels` |
| `relationship` | `action`, `from_col`, `to_col` (required), `inactive`, `active`, `both`, `single`, `many_to_many`, `force` |
| `add_page` | `name` (required), `width`, `height`, `hidden`, `after`, `tooltip`, `drillthrough` |
| `add_visual` | `type`, `page` (required), `fields`, `pos`, `title`, `text` |
| `check` | `spec` (required, the checks object) |
| `probe` | `table` (required), `columns` |
| `snapshot` | `action`, `name` (required), `spec` |
| `smoke` | `page` |
| `handoff` | `all` |
| `desktop` | `action` (required), `wait` |
| `refresh` | `tables` |
| `synth` | `action` (required), `dax`, `rows`, `seed`, `wait` |
| `memscrub` | `dir` (required), `show` (preview only) |

Each call runs `python -I pbz/cli.py --workspace <ws> <verb> ...` in a child process and returns the
verb's JSON as one text block; `isError` is true when the verb exits non-zero (pbz errors keep their
code, for example `NOT_FOUND` or `DESKTOP_OPEN`). Bad arguments (unknown name, wrong type, missing
required value, a value starting with `-`) are JSON-RPC errors `-32602`; unknown methods `-32601`;
malformed JSON `-32700`; batches `-32600`.

## Safety

- The tools are the CLI: same mirror-only editing, same staged validation on push, same
  closed-vocabulary outputs. No tool returns data values, and none bypasses pbz.
- `pbz admin` (human only) and `memscrub --apply` are not exposed.
- stdout carries protocol messages only. stderr carries two lines (`ready`, `stdin closed`) and never
  tool arguments, outputs, error text or tracebacks: when a verb fails without JSON output, the tool
  returns `BAD_ARGUMENTS` or `INTERNAL_ERROR` instead of forwarding stderr.
- Tool names avoid words the guard treats as file access (read, open, file, view) and never contain
  "powerbi", so the guard hook lets `mcp__pbz__*` through while still denying other Power BI MCP
  servers.
