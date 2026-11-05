# Third-party notices

pbz contains no copied third-party source code. Its analysis verbs reimplement published rules and
algorithms; the projects below shaped them. Rule entries in `pbz/rules/*.json` carry a `source`
field naming the rule they follow. All listed projects are under the MIT License (text below);
copyright belongs to their respective authors, as stated in each repository's LICENSE file.

| Project | Used for |
|---|---|
| [microsoft/Analysis-Services](https://github.com/microsoft/Analysis-Services) (BestPracticeRules, ALM Toolkit) | BPA rule ids, categories and severities; object comparison semantics for `pbz changes` |
| [microsoft/semantic-link-labs](https://github.com/microsoft/semantic-link-labs) | unused-object rules, model and report BPA rule ideas |
| [TabularEditor/TabularEditor](https://github.com/TabularEditor/TabularEditor) (Tabular Editor 2) | DAX reference resolution rules (FormulaFixup, DaxDependencyHelper), BPA ignore annotation |
| [NatVanG/fab-inspector](https://github.com/NatVanG/fab-inspector) | report rule ideas (Base-rules) |
| [akhilannan/pbir-utils](https://github.com/akhilannan/pbir-utils) | report cleanup and sanitizer ideas |
| [JonathanJihwanKim/pbi-lineage-lenz](https://github.com/JonathanJihwanKim/pbi-lineage-lenz), pbip-lineage-explorer, pbip-documenter | PBIR field-usage walking, impact traversal, documentation layout |
| [ViciusLio/pbi-semantic-doc](https://github.com/ViciusLio/pbi-semantic-doc) | documentation sections |
| [sql-bi/Vpax-Obfuscator](https://github.com/sql-bi/Vpax-Obfuscator), [sql-bi/VertiPaq-Analyzer](https://github.com/sql-bi/VertiPaq-Analyzer) | DAX tokenizer keyword classes; `DISCOVER_CALC_DEPENDENCY` usage |
| [microsoft/powerquery-parser](https://github.com/microsoft/powerquery-parser) | M grammar reference for the let-step analyser |
| [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric) `@microsoft/powerbi-core-visual-schema` (Copyright (c) Microsoft Corporation) | `pbz/rules/visuals.json` and `pbz/rules/visual-objects.json` are data compacted from its `data/capabilities.json`, `vco-capabilities.json` and `instance-selectors.json` (built-in visual data roles, kinds, mapping conditions, formatting objects and properties with value types, container objects, instance states) by `install/build_visual_catalog.py` |

Design references only (documentation, no code used): Microsoft's Power BI report authoring CLI
reference in microsoft/skills-for-fabric (visual role names, custom visual registration and PBIR
pitfalls behind `pbz add` and `pbz visual`), the ECMA-376 SpreadsheetML pivot and slicer parts and the
RDL schema (names read by `pbz consumers`), the Deneb PBIR documentation, the
Microsoft PBIR / TMDL documentation and json-schemas repository (structure checked by `pbz validate`),
and SQLBI's DAX formatting conventions (`pbz format`).

Measure Killer (Brunner BI) informed the feature list, the usage states and the published scoring
formula; no Measure Killer code or assets are used. Its grade labels are available only as an
optional preset (`pbz bpa --labels measure-killer`).

## MIT License

```
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
associated documentation files (the "Software"), to deal in the Software without restriction,
including without limitation the rights to use, copy, modify, merge, publish, distribute,
sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT
NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES
OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
```
