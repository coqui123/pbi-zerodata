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

Every tool schema includes `workspace` and `project`. `project` selects the `.pbip` when that
folder holds several. A `workspace` value on the tool call does not change the folder: the
command always uses the server's `--workspace`, or the server's working folder when that flag
was omitted. Other arguments:

A star marks a required argument. Names are the tool fields, not the CLI flags.

| Tool | Arguments |
|---|---|
| `preflight` | `only` |
| `recover` | `transaction` |
| `preview` | `page`, `spec` |
| `check_schema` | none |
| `interactions` | `action*`, `spec`, `mode` |
| `freshness_capture` | `max_age_hours` |
| `freshness_compare` | `spec*` |
| `requirements_import` | `spec*` |
| `requirements_status` | none |
| `requirements_verify` | `id*` |
| `consent_request` | `path*`, `purpose` |
| `consent_status` | `id*` |
| `consent_read` | `id*` |
| `status`, `doctor`, `capabilities`, `init` | none |
| `scan` | `dry_run` |
| `pull` | `force`, `rebase` |
| `diff` | `patch` |
| `push` | `reload`, `dry_run`, `only` |
| `revert` | `paths`, `object` |
| `style` | `page`, `visual`, `set`, `unset`, `type`, `all`, `series`, `state`, `show`, `list`, `object`, `canvas` |
| `image` | `action*`, `target`, `name` |
| `visual` | `action*`, `target`, `as`, `roles`, `replace` |
| `consumers` | `action*`, `target`, `register`, `depth`, `kind`, `label`, `objects`, `model_names` |
| `culture` | `action*`, `code` |
| `translate` | `code`, `target`, `caption`, `description`, `folder`, `export`, `import_path` |
| `perspective` | `action*`, `name*`, `targets` |
| `role` | `action*`, `name*`, `args`, `permission` |
| `refresh_policy` | `action*`, `table`, `date`, `store`, `store_unit`, `refresh`, `refresh_unit`, `detect`, `complete`, `hybrid` |
| `validate` | `fail_on`, `since`, `ci`, `format`, `out`, `save_baseline` |
| `watch` | `once` (the server always passes one pass) |
| `describe` | `target` |
| `values` | `column*` |
| `find` | `pattern*`, `kind` |
| `replace` | `pattern*`, `repl*`, `kind`, `apply` |
| `rename` | `kind*`, `old*`, `new*`, `table`, `match`, `to`, `dry_run` |
| `move_measure` | `name*`, `to*` |
| `unused` | `all`, `kinds`, `report`, `impact`, `remove`, `dry_run`, `cascade`, `only`, `m_step`, `restore`, `verify_engine`, `what_if` |
| `lineage` | `target*`, `up`, `depth`, `report` |
| `lineage_source` | `target`, `up`, `depth`, `report`, `from_source`, `native_sql`, `format`, `out` |
| `impact` | `drop_source_column`, `rename_source_column`, `to` |
| `suggest` | `report` |
| `bpa` | `scope`, `rules`, `opinion`, `labels`, `report`, `fix`, `dry_run`, `import_rules`, `fail_on`, `since`, `ci`, `format`, `out`, `save_baseline` |
| `similar` | `threshold`, `max`, `models`, `against` |
| `audit` | `action`, `id`, `category`, `rule`, `min_severity`, `since`, `save_baseline`, `fail_on`, `coverage`, `max`, `reason`, `expires`, `remove`, `no_predicates` |
| `consolidate` | `action*`, `target`, `into`, `keep_alias`, `force`, `family`, `measure`, `name`, `column`, `precedence`, `dry_run` |
| `changes` | `from`, `to`, `list`, `against` |
| `docs` | `format`, `dax` |
| `m_lineage` | `table` |
| `layout` | `page`, `no_map` |
| `format` | `target`, `apply` |
| `repair` | `dry_run`, `fields`, `lineage` |
| `new` | `kind*`, `name*`, `start`, `end`, `relate`, `date`, `items`, `precedence`, `fields`, `labels` |
| `relationship` | `action*`, `from_col*`, `to_col*`, `inactive`, `active`, `both`, `single`, `many_to_many`, `force`, `dry_run` |
| `relationship_paths` | `left*`, `right*` |
| `add_page` | `name*`, `width`, `height`, `hidden`, `after`, `tooltip`, `drillthrough` |
| `link` | `page*`, `visual*`, `to_page`, `back`, `bookmark`, `drillthrough`, `url`, `tooltip`, `clear` |
| `filter` | `action*`, `target`, `report`, `page`, `visual`, `values_in`, `values_out`, `where`, `top`, `by`, `bottom`, `dry_run` |
| `bookmark` | `action*`, `name`, `page`, `hide`, `show`, `only` |
| `interaction` | `page*`, `source*`, `target*`, `mode*`, `dry_run` |
| `add_visual` | `type*`, `page*`, `fields`, `pos`, `title`, `text`, `image`, `fit`, `alt` |
| `health`, `index` | none |
| `check` | `spec*`, `validate_only`, `diagnostic` |
| `probe` | `table*`, `columns` |
| `snapshot` | `action*`, `name*`, `spec` |
| `smoke` | `page` |
| `perf` | `dax*`, `runs`, `baseline`, `action` |
| `perf_import` | `file*` |
| `handoff` | `all` |
| `evidence` | none (this is the `pbz review` packet; acceptance stays on the CLI) |
| `set_props` | `items*`, `kind`, `table`, `match`, `folder`, `unset`, `dry_run` |
| `get_props` | `items`, `kind`, `table`, `match`, `folder` |
| `copy` | `kind*`, `page*`, `visual`, `as`, `after`, `independent_slicers`, `to_page`, `pos` |
| `a11y` | `page`, `fix_tab_order`, `min_font` |
| `mobile` | `page*`, `visual*`, `pos`, `remove` |
| `hierarchy` | `action*`, `table*`, `name*`, `levels`, `names`, `force` |
| `calendar` | `table*`, `name*`, `column*`, `unit` |
| `kpi` | `measure*`, `target`, `status`, `trend` |
| `format_expr` | `measure*`, `dax`, `remove`, `upgrade_compat` |
| `rewrite` | `action*`, `target*`, `accept_semantic`, `format`, `description` |
| `expand` | `target*` |
| `function` | `action*`, `name`, `params`, `body`, `to`, `force`, `upgrade_compat` |
| `recipe` | `action*`, `spec*`, `params`, `dry_run` |
| `script` | `action*`, `path*` |
| `desktop` | `action*`, `wait` |
| `refresh` | `tables` |
| `synth` | `action*`, `dax`, `edge`, `spec`, `rows`, `seed`, `wait` |
| `synth_test` | `action*`, `name`, `spec`, `measures`, `by`, `replace`, `tol` |
| `memscrub` | `dir*` |

`memscrub` returns replacement counts only. `--show` and `--apply` are not MCP arguments.
Listing stored cross-filter modes is `pbz interaction <page> --show` on the CLI, not an MCP argument.

Each call runs `python -I pbz/cli.py --workspace <ws> <verb> ...` in a child process. `<ws>` is
the server workspace above, not a workspace field from the tool call. The call returns the
verb's JSON as one text block; `isError` is true when the verb exits non-zero (pbz errors keep their
code, for example `NOT_FOUND` or `DESKTOP_OPEN`). Bad arguments (unknown name, wrong type, missing
required value, a value starting with `-`) are JSON-RPC errors `-32602`; unknown methods `-32601`;
malformed JSON `-32700`; batches `-32600`.

## Safety

- The tools are the CLI: same mirror-only editing, same staged validation on push, same
  closed-vocabulary outputs. No tool returns real rows, totals, or query grids. `describe` and
  `values` return category labels. `synth query` returns rows from the fake copy only. None bypasses pbz.
- `pbz admin` (human only) and `memscrub --apply` are not exposed.
- stdout carries protocol messages only. stderr carries two lines (`ready`, `stdin closed`) and never
  tool arguments, outputs, error text or tracebacks: when a verb fails without JSON output, the tool
  returns `BAD_ARGUMENTS` or `INTERNAL_ERROR` instead of forwarding stderr.
- Tool names avoid words the guard treats as file access (read, open, file, view) and never contain
  "powerbi", so the guard hook lets `mcp__pbz__*` through while still denying other Power BI MCP
  servers.
