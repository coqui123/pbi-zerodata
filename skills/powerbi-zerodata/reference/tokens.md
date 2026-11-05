# Mirror tokens

`pbz pull` writes `pbz-workspace\<Project>.SemanticModel\` and `.Report\` as exact copies of the
real files, except that sensitive literals are replaced in place by tokens. Formatting, names,
DAX, M and JSON structure are untouched, so diffs stay minimal.

## Grammar

| Token | Replaces |
|---|---|
| `«Table[Column]#n»` | a value of a sensitive column (people, titles, IDs, free text), bound by context: comparisons, `IN {}`, `TREATAS`, `SWITCH`, `LOOKUPVALUE`, `CONTAINSSTRING`, DATATABLE / `#table` rows, PBIR filters, slicer selections, bookmarks, series selectors |
| `«lit#n»` | a sensitive value not tied to a column (known value from the index, or a person-like name) |
| `«pii#n»` | an email / UPN / role member |
| `«num#n»` | a figure typed into free text (descriptions, comments, text boxes) |
| `«payload:<where>#n»` | embedded data blobs (Enter Data base64, inline Deneb / Vega `values`) |
| `«Table[Column]@g#n»` | a group key reported by a check, known only by hash (not usable in `where`) |

Tokens are stable across pulls: the same value of the same column always gets the same token.
Inside DAX/M the token sits inside the original quotes (`= "«Person[Name]#4»"`); in PBIR it
replaces the whole literal (`"Value": "«Person[Name]#4»"`).

## Classification

- With an index (`pbz index`, Desktop open): text columns with at most `category_max` distinct
  values and a non-personal name are **category** (labels shown); others are **sensitive**.
- Provenance first: a literal bound to a sensitive column (comparison, IN list, filter, slicer,
  embedded row) is hidden whatever it looks like. Small whole numbers (up to 3 digits) in embedded
  config tables stay readable (sort orders, weights).
- Unbound text (comments, descriptions, titles, free literals) is matched only against distinctive
  values of sensitive **person, identifier and title** columns, never against category labels,
  free-text columns (description, comment, notes) or numbers / dates stored as text. A match needs
  3+ letters and 4+ characters and may not be only common English words (local Hunspell
  dictionaries, `PBZ_DICT` to override) or model object names. Lone name parts must be
  capitalized and either not an English word or a known proper noun. Research notes:
  `docs/reference-projects.md` in the repo.
- Without an index (**heuristic** mode, shown in `MIRROR.md` and `pbz status`): column-name rules
  plus a person-name pattern. It can miss a lone first name typed into a formula; run `pbz index`
  before deep work.
- Personal-name columns (Name, Owner, Manager, PM, Leader, Supervisor, Resource, Person,
  Employee, Email, Title, Description, ...) are always sensitive. IDs are sensitive.
- The user can force a column readable: `pbz admin category "Table[Column]"` (their terminal).

## Push rules

- Each token is restored to the exact raw text it replaced in that context, so an untouched
  file round-trips byte for byte. A token copied into a new place is encoded for that place.
- A token that is not in the map (typo, invented) fails the push with `UNKNOWN_TOKEN`.
- Files that could not round-trip at pull time are marked `lossy` and are not pushed; edit those
  objects in Desktop.
- Plain literals you write are pushed as typed.

## Culture files

Culture files are mirrored. Their Q&A linguistic schema becomes one opaque token
`«payload:linguisticMetadata#n»` (keep it untouched); translated captions, descriptions and display
folders are visible and screened as free text.

## Strict literal policy

`"literals": "strict"` in `.pbz/policy.json` switches the classifier to hide by default: every string
literal in DAX, M and PBIR becomes a token unless it is a known category label of the bound column
(index mode), a model object or source-column name, a format code (`"MMM"`, `"#,0"`, ...) or one or
two characters; numbers bound to non-date columns are tokenized unless they are small ordinals. The
mirror is less readable and fully reversible. Adding the setting is allowed; removing it counts as
loosening the policy, which only the user can do (`pbz admin`).
