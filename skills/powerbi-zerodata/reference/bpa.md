# pbz bpa: best practice analysis and scores

```
pbz bpa                      # model and report rules, scores and grades
pbz bpa model | report       # one scope
pbz bpa --opinion            # also the convention rules (descriptions, format strings, casing, alt text)
pbz bpa --labels measure-killer
pbz bpa --rules overlay.json # disable / enable / re-weight rules, change thresholds
pbz bpa --report <folder>    # also scan a thin report bound by connection
```

Everything runs on the sanitized mirror. Findings name objects (tables, columns, measures,
relationships, pages, visual ids, bookmarks) and count things (fields per visual, visuals per page).
They never quote a literal value from DAX, M or PBIR.

## Output

- `model` / `report`: `score`, `grade`, `per100` (score per 100 objects), `rate` (0 to 100, the
  severity-weighted share of checks passed, comparable across model sizes), `objects`, `findings`,
  `by_rule` (findings per rule id).
- `findings`: `rule`, `severity` (1 info, 2 warning, 3 error), `category`, `scope`, `kind`, `object`,
  `message`, `fix`, plus `table`, `id` (page, visual or bookmark id) and `excess` (threshold rules).
- `suppressed`: findings silenced by a Tabular Editor ignore annotation, with `by`.
- `coverage`: rules `evaluated`, `applicable` (rules that found something to check), `disabled`
  (opinion rules, overlay), `skipped_data_rules` (need data, never run).
- `ruleset`: hash of the effective rules (ids, severities, on/off, thresholds). Compare scores only
  between runs with the same hash.

## Scores and grades

- Model: `sum(severity x violations) x log10(tables + columns + measures + relationships)`.
  Grades below 75, 300, 600, 900, 1200, and above.
- Report: `sum(severity x (1 + excess))`, where `excess` is how far a threshold rule is over its
  limit (pages, visuals per page, fields per visual, filters per page, page height). Grades below
  50, 100, 150, 250, 300, and above.
- Labels: `letters` (A to F, default) or `measure-killer` ("Power BI Pro", "Good", "Ok", "Poor",
  "Awful", "Power BI Criminal") for side-by-side comparison with that tool.
- Auto date/time tables count as findings (`REMOVE_AUTO-DATE_TABLE`) but not as objects.

## Suppression and overlay

- Tabular Editor's annotation is honored: `annotation BestPracticeAnalyzer_IgnoreRules =
  {"RuleIDs":["RULE_ID"]}` on an object ignores the rule for that object; on the model (model.tmdl)
  it ignores the rule everywhere. Write it in the mirror like any other edit.
- Overlay file (`--rules`): `{"disable": [ids], "enable": [ids], "severity": {"ID": 3},
  "thresholds": {"REDUCE_PAGES": 10}}`. Rule ids match microsoft/Analysis-Services BPARules.json
  where one exists, so existing ignore lists carry over.

## Model rules

| Id | Sev | Default | Checks |
|---|---|---|---|
| `REMOVE_AUTO-DATE_TABLE` | 2 | on | auto date/time tables exist |
| `DATE/CALENDAR_TABLES_SHOULD_BE_MARKED_AS_A_DATE_TABLE` | 2 | on | table named with the word date or calendar, has a date column, not marked |
| `MODEL_SHOULD_HAVE_A_DATE_TABLE` | 2 | on | date columns but no marked date table |
| `AVOID_FLOATING_POINT_DATA_TYPES` | 2 | on | `dataType: double` |
| `NUMERIC_COLUMN_SUMMARIZE_BY` | 1 | on | visible numeric column that summarizes (an omitted summarizeBy is the summarizing default) |
| `HIDE_FOREIGN_KEYS` | 2 | on | visible many-side relationship key (fromCardinality defaults to many) |
| `MARK_PRIMARY_KEYS` | 1 | opinion | one-side key without isKey |
| `RELATIONSHIP_COLUMNS_SHOULD_BE_OF_INTEGER_DATA_TYPE` | 1 | on | key is neither int64 nor dateTime |
| `RELATIONSHIP_COLUMNS_SAME_DATA_TYPE` | 3 | on | key types differ |
| `AVOID_EXCESSIVE_BI-DIRECTIONAL_OR_MANY-TO-MANY_RELATIONSHIPS` | 2 | on, share > 0.3 | bi-directional plus many-to-many relationships over all relationships |
| `MANY-TO-MANY_RELATIONSHIPS_SHOULD_BE_SINGLE-DIRECTION` | 2 | on | many-to-many and bi-directional |
| `INACTIVE_RELATIONSHIPS_THAT_ARE_NEVER_ACTIVATED` | 2 | on | no USERELATIONSHIP or CROSSFILTER names it |
| `ENSURE_TABLES_HAVE_RELATIONSHIPS` | 1 | on | isolated table; measure tables, field parameters, what-if parameters, calculation groups and auto date tables are exempt |
| `SNOWFLAKE_SCHEMA_ARCHITECTURE` | 1 | on | table on the one side of one relationship and the many side of another |
| `UNNECESSARY_COLUMNS` / `_MEASURES` | 2 | on | hidden and unused by reports, model and security (`pbz unused` states) |
| `REDUCE_USAGE_OF_CALCULATED_COLUMNS_THAT_USE_THE_RELATED_FUNCTION` | 2 | on | calculated column calls RELATED |
| `REDUCE_USAGE_OF_CALCULATED_TABLES` | 1 | on | calculated table that is not a field parameter, what-if parameter, calculation group or auto date table |
| `USE_THE_DIVIDE_FUNCTION_FOR_DIVISION` | 2 | on | `/` operator, except division by a non-zero constant |
| `AVOID_USING_THE_IFERROR_FUNCTION` | 2 | on | IFERROR |
| `USE_THE_TREATAS_FUNCTION_INSTEAD_OF_INTERSECT` | 2 | on | INTERSECT |
| `EVALUATEANDLOG_SHOULD_NOT_BE_USED_IN_PRODUCTION_MODELS` | 1 | on | EVALUATEANDLOG |
| `DAX_COLUMNS_FULLY_QUALIFIED` | 2 | on | bare `[Column]` |
| `DAX_MEASURES_UNQUALIFIED` | 2 | on | `'Table'[Measure]` |
| `AVOID_DUPLICATE_MEASURES` | 2 | on | same formula fingerprint (case, spacing and comments ignored) |
| `MEASURES_SHOULD_NOT_BE_DIRECT_REFERENCES_OF_OTHER_MEASURES` | 2 | on | formula is only `[Other]` |
| `FILTER_COLUMN_VALUES` | 2 | on | `FILTER('Table', column test)` |
| `FILTER_MEASURE_VALUES_BY_COLUMNS` | 2 | on | `FILTER('Table', [Measure] test)` |
| `AVOID_ADDING_0_TO_A_MEASURE` | 2 | on | `+ 0` |
| `AVOID_USING_'1-(X/Y)'_SYNTAX` | 2 | on | `1 - (x / y)` |
| `AVOID_AGGREGATING_TEXT_COLUMNS` | 3 | on | SUM, AVERAGE, MEDIAN, PRODUCT, STDEV or VAR of a text column |
| `LIMIT_ROW_LEVEL_SECURITY_(RLS)_LOGIC` | 2 | on | RLS filter calls RIGHT, LEFT, UPPER, LOWER or FIND |
| `AVOID_THE_USERELATIONSHIP_FUNCTION_AND_RLS_AGAINST_THE_SAME_TABLE` | 3 | on | USERELATIONSHIP on a relationship touching a secured table |
| `ISAVAILABLEINMDX_FALSE_NONATTRIBUTE_COLUMNS` | 2 | on | hidden column, not a sort-by target, hierarchy level or variation (an omitted isAvailableInMdx is true) |
| `MONTH_(AS_A_STRING)_MUST_BE_SORTED` | 2 | on | text column with the word month and no sortByColumn |
| `PROVIDE_FORMAT_STRING_FOR_MEASURES` | 2 | on | visible measure without format string; text measures and dynamic format strings are exempt |
| `OBJECTS_WITH_NO_DESCRIPTION` | 1 | opinion | visible measure or table without `///` description |
| `OBJECTS_SHOULD_NOT_START_OR_END_WITH_A_SPACE` | 3 | on | leading or trailing space in a name |
| `AVOID_INVALID_NAME_CHARACTERS` | 3 | on | control characters in a name |
| `EXPRESSIONS_WITH_UNRESOLVED_REFERENCES` | 3 | on | reference to an object that does not exist |
| `DATA_COLUMNS_MUST_HAVE_A_SOURCE_COLUMN` | 3 | on | data column without sourceColumn |
| `CALCULATION_GROUPS_WITH_NO_CALCULATION_ITEMS` | 2 | on | empty calculation group |
| `PERSPECTIVES_WITH_NO_OBJECTS` | 1 | on | empty perspective |
| `ADD_DATA_CATEGORY_FOR_COLUMNS` | 1 | on | text column named with a geographic word and no dataCategory |
| `INTEGER_FORMATTING` / `PERCENTAGE_FORMATTING` / `DATECOLUMN_FORMATSTRING` | 1 | opinion | format conventions |
| `FIRST_LETTER_OF_OBJECTS_MUST_BE_CAPITALIZED` | 1 | opinion | name starts lowercase |
| `SPLIT_DATE_AND_TIME`, `LARGE_TABLES_SHOULD_BE_PARTITIONED`, `AVOID_BI-DIRECTIONAL_RELATIONSHIPS_AGAINST_HIGH-CARDINALITY_COLUMNS`, `REDUCE_USAGE_OF_LONG-LENGTH_COLUMNS_WITH_HIGH_CARDINALITY`, `FIX_REFERENTIAL_INTEGRITY_VIOLATIONS` | 2 | data | need data; listed, never run |

Name tests match whole words, split at spaces, underscores and case changes: `DimDate` contains
the word date, `Updates` does not; `Capacity Band` does not contain city.

## Report rules

| Id | Sev | Default | Checks |
|---|---|---|---|
| `REDUCE_PAGES` | 1 | > 15 per report | pages |
| `REDUCE_BOOKMARKS` | 1 | > 25 per report | bookmarks |
| `REDUCE_VISUALS_ON_PAGE` | 2 | > 20 per page | visible visuals (groups not counted) |
| `REDUCE_OBJECTS_WITHIN_VISUALS` | 1 | > 6 per visual | bound fields; tables and matrices exempt |
| `REDUCE_TOPN_FILTERS` | 2 | > 2 per page | TopN filters (page and visual level) |
| `REDUCE_ADVANCED_FILTERS` | 1 | > 3 per page | advanced filters |
| `REDUCE_MEASURE_FILTERS` | 2 | > 2 per page | filters on measures |
| `AVOID_SHOW_ITEMS_WITH_NO_DATA` | 2 | on | "Show items with no data" |
| `HIDE_TOOLTIP_DRILLTHROUGH_PAGES` | 1 | on | visible tooltip or drillthrough page |
| `AVOID_TALL_PAGES` | 1 | > 1080 px | page height |
| `REMOVE_UNUSED_CUSTOM_VISUALS` | 1 | on | declared custom visual with no visual of that type |
| `AVOID_HARD_CODED_COLORS` | 1 | on | hex color literals in formatting |
| `AVOID_IMPLICIT_MEASURES` | 1 | on | implicit aggregations of columns |
| `FIX_INVALID_REFERENCES` | 3 | on | field not in the model or the report extensions |
| `AVOID_REPORT_LEVEL_MEASURES` | 1 | on | report-level measures |
| `REMOVE_INVALID_BOOKMARKS` | 2 | on | bookmark on a missing page or visual |
| `REMOVE_HIDDEN_VISUALS_NEVER_SHOWN` | 1 | on | hidden visual no bookmark mentions |
| `REMOVE_UNREFERENCED_HIDDEN_PAGES` | 1 | on | hidden page without navigation, tooltip or drillthrough |
| `AVOID_PIE_CHARTS` | 1 | on | pie and donut charts |
| `GIVE_VISIBLE_PAGES_MEANINGFUL_NAMES` | 1 | on | "Page 1", "Duplicate of ..." |
| `ENSURE_ALTTEXT` | 1 | opinion | data visual without alt text |

Sources: microsoft/Analysis-Services BestPracticeRules (MIT) for the model rule ids and logic;
NatVanG/fab-inspector Base-rules, akhilannan/pbir-utils and microsoft/semantic-link-labs (all MIT) for
the report rule concepts; Measure Killer for the scoring formula and grade cutoffs (concept only).
Each catalog entry in `pbz/rules/*.json` names its source.
