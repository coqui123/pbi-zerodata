"""`pbz audit`: efficiency and consolidation findings on the synthetic parity project with planted patterns,
bucketed evidence (index buckets, memory bands), baselines, waivers and the CI gate.

Run: python tests/test_audit.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import audit, common, mirror  # noqa: E402
from pbz.common import PbzError  # noqa: E402

T = "\t"
SALES_EXTRA = """
\tmeasure 'Iter Call' = SUMX(Sales, [Total Sales])
\t\tlineageTag: 20000000-0000-0000-0000-000000000001

\tmeasure 'Inner X' = SUMX(Sales, Sales[Amount] * 2)
\t\tlineageTag: 20000000-0000-0000-0000-000000000002

\tmeasure 'Nested Iter' = SUMX(Sales, [Inner X])
\t\tlineageTag: 20000000-0000-0000-0000-000000000003

\tmeasure 'Summ Ext' = SUMX(SUMMARIZE(Sales, Sales[CustomerKey], "Amt", SUM(Sales[Amount])), [Amt])
\t\tlineageTag: 20000000-0000-0000-0000-000000000004

\tmeasure 'Line Total' = SUM(Sales[Line Amount])
\t\tlineageTag: 20000000-0000-0000-0000-000000000005

\tmeasure 'Seg A' = CALCULATE([Total Sales], Customer[Segment] = "A")
\t\tlineageTag: 20000000-0000-0000-0000-000000000006

\tmeasure 'Seg B' = CALCULATE([Total Sales], Customer[Segment] = "B")
\t\tlineageTag: 20000000-0000-0000-0000-000000000007

\tmeasure 'Seg C' = CALCULATE([Total Sales], Customer[Segment] = "C")
\t\tlineageTag: 20000000-0000-0000-0000-000000000008

\tmeasure 'Big Cust' = CALCULATE(DISTINCTCOUNT(Sales[CustomerKey]), Sales[Amount] > 100)
\t\tlineageTag: 20000000-0000-0000-0000-000000000009

\tmeasure 'Big Dates' = CALCULATE(DISTINCTCOUNT(Sales[DateKey]), Sales[Amount] > 100)
\t\tlineageTag: 20000000-0000-0000-0000-000000000010

\tmeasure 'Big Ship' = CALCULATE(DISTINCTCOUNT(Sales[ShipDateKey]), Sales[Amount] > 100)
\t\tlineageTag: 20000000-0000-0000-0000-000000000011

\tmeasure 'Share 1' = DIVIDE(CALCULATE(SUM(Sales[Amount]), Customer[Segment] = "A"), CALCULATE(SUM(Sales[Amount]), ALL(Customer))) * 100
\t\tlineageTag: 20000000-0000-0000-0000-000000000012

\tmeasure 'Share 2' = DIVIDE(CALCULATE(SUM(Sales[Amount]), Customer[Segment] = "A"), CALCULATE(SUM(Sales[Amount]), ALL(Customer))) + 1
\t\tlineageTag: 20000000-0000-0000-0000-000000000013

\tmeasure 'Share 3' = IF(DIVIDE(CALCULATE(SUM(Sales[Amount]), Customer[Segment] = "A"), CALCULATE(SUM(Sales[Amount]), ALL(Customer))) > 0.5, 1, 0)
\t\tlineageTag: 20000000-0000-0000-0000-000000000014

\tmeasure 'Total Sales YTD' = CALCULATE([Total Sales], DATESYTD('Date'[Date]))
\t\tlineageTag: 20000000-0000-0000-0000-000000000030

\tmeasure 'Inner X YTD' = CALCULATE([Inner X], DATESYTD('Date'[Date]))
\t\tlineageTag: 20000000-0000-0000-0000-000000000031

\tmeasure 'Line Total YTD' = CALCULATE([Line Total], DATESYTD('Date'[Date]))
\t\tlineageTag: 20000000-0000-0000-0000-000000000032

\tmeasure 'Total Sales PY' = CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[Date]))
\t\tlineageTag: 20000000-0000-0000-0000-000000000033

\tmeasure 'Inner X PY' = CALCULATE([Inner X], SAMEPERIODLASTYEAR('Date'[Date]))
\t\tlineageTag: 20000000-0000-0000-0000-000000000034

\tmeasure 'Line Total PY' = CALCULATE([Line Total], SAMEPERIODLASTYEAR('Date'[Date]))
\t\tlineageTag: 20000000-0000-0000-0000-000000000035

\tcolumn 'Line Amount' = Sales[Amount] * 2
\t\tdataType: double
\t\tlineageTag: 20000000-0000-0000-0000-000000000020
\t\tsummarizeBy: sum

\tcolumn 'Cust Segment' = RELATED(Customer[Segment])
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000021
\t\tsummarizeBy: none

\tcolumn 'Amount Copy' = [Amount]
\t\tdataType: double
\t\tlineageTag: 20000000-0000-0000-0000-000000000022
\t\tsummarizeBy: sum

\tcolumn 'Region Upper' = UPPER([Region])
\t\tdataType: string
\t\tlineageTag: 20000000-0000-0000-0000-000000000023
\t\tsummarizeBy: none
"""


def m_table(name: str, tag: str, steps: str, mode: str = "import", col: str = "Region", dtype: str = "string") -> str:
    body = "\n".join("\t\t\t\t" + ln for ln in steps.strip("\n").splitlines())
    return (f"table '{name}'\n\tlineageTag: {tag}\n\n\tcolumn {col}\n\t\tdataType: {dtype}\n"
            f"\t\tlineageTag: {tag[:-2]}99\n\t\tsummarizeBy: none\n\t\tsourceColumn: {col}\n\n"
            f"\tpartition '{name}' = m\n\t\tmode: {mode}\n\t\tsource =\n{body}\n")


REGION_STEPS = """let
    Source = Csv.Document(File.Contents("C:/data/regions.csv")),
    Promoted = Table.PromoteHeaders(Source),
    Typed = Table.TransformColumnTypes(Promoted, {{"Region", type text}})
in
    Typed"""
BUFFER_STEPS = """let
    Source = Csv.Document(File.Contents("C:/data/other.csv")),
    Buffered = Table.Buffer(Source),
    Filtered = Table.SelectRows(Buffered, each [Region] <> null)
in
    Filtered"""
DQ_STEPS = """let
    Source = Sql.Database("srv", "db"),
    Facts = Source{[Schema="dbo",Item="Facts"]}[Data]
in
    Facts"""
SHIP_CAL = """table 'Ship Calendar'
\tlineageTag: 20000000-0000-0000-0000-000000000300

\tcolumn Date
\t\tdataType: dateTime
\t\tlineageTag: 20000000-0000-0000-0000-000000000301
\t\tsummarizeBy: none
\t\tsourceColumn: [Date]

\tpartition 'Ship Calendar' = calculated
\t\tmode: import
\t\tsource = CALENDAR(DATE(2024, 1, 1), DATE(2024, 12, 31))
"""


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzaudit-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    ws = common.Workspace(root)
    ws.private = os.path.join(tmp, "private")
    project = ws.project()
    mirror.pull(ws, project)
    mdef = os.path.join(project.mirror_model, "definition")
    tdir = os.path.join(mdef, "tables")

    def append(rel, text):
        path = os.path.join(mdef, *rel.split("/"))
        old, bom = common.read_text(path) if os.path.exists(path) else ("", b"")
        common.write_text(path, old.rstrip("\n") + "\n" + text, bom)

    append("tables/Sales.tmdl", SALES_EXTRA)
    common.write_text(os.path.join(tdir, "Region A.tmdl"), m_table("Region A", "20000000-0000-0000-0000-000000000100", REGION_STEPS), b"")
    common.write_text(os.path.join(tdir, "Region B.tmdl"), m_table("Region B", "20000000-0000-0000-0000-000000000200", REGION_STEPS), b"")
    common.write_text(os.path.join(tdir, "Buffered.tmdl"), m_table("Buffered", "20000000-0000-0000-0000-000000000400", BUFFER_STEPS), b"")
    common.write_text(os.path.join(tdir, "DQ Facts.tmdl"), m_table("DQ Facts", "20000000-0000-0000-0000-000000000500", DQ_STEPS,
                                                                   mode="directQuery", col="CustomerKey", dtype="int64"), b"")
    common.write_text(os.path.join(tdir, "Ship Calendar.tmdl"), SHIP_CAL, b"")
    rel_path = os.path.join(mdef, "relationships.tmdl")
    rels, bom = common.read_text(rel_path)
    rels = rels.replace("relationship r1\n", "relationship r1\n\tcrossFilteringBehavior: bothDirections\n", 1)
    rels = rels.rstrip("\n") + "\n\nrelationship r9\n\tfromColumn: 'DQ Facts'.CustomerKey\n\ttoColumn: Customer.CustomerKey\n"
    common.write_text(rel_path, rels, bom)

    def rules_of(res):
        return {(f["rule"], f["object"]) for f in res["findings"]}

    def find(res, rule, obj=None):
        return [f for f in res["findings"] if f["rule"] == rule and (obj is None or obj in f["object"])]

    # ---------------------------------------------------------------- static only
    r = audit.run(ws, project)
    got = rules_of(r)
    for rule, obj in [("ITERATOR_MEASURE_CALL", "[Iter Call]"), ("NESTED_ITERATOR", "[Nested Iter]"),
                      ("SUMMARIZE_EXTENSION_COLUMNS", "[Summ Ext]"), ("CALC_COLUMN_AS_MEASURE", "'Sales'[Line Amount]"),
                      ("RELATED_COPY_COLUMN", "'Sales'[Cust Segment]"), ("COPY_COLUMN", "'Sales'[Amount Copy]"),
                      ("CALC_COLUMN_UPSTREAM", "'Sales'[Region Upper]"), ("MIXED_STORAGE_DIMENSION", "Customer"),
                      ("M_BUFFER_BEFORE_FILTER", "Buffered"), ("DUPLICATE_TABLE", "Region A, Region B"),
                      ("M_SHARED_STEPS", "Region A, Region B"), ("DUPLICATE_MEASURE", "[Total Sales], [Sales Copy]")]:
        check((rule, obj) in got, f"{rule} on {obj} missing: {sorted(x for x in got if x[0] == rule)}")
    check(find(r, "FILTERED_FAMILY") and set(find(r, "FILTERED_FAMILY")[0]["members"]) == {"[Seg A]", "[Seg B]", "[Seg C]"},
          f"filtered family: {find(r, 'FILTERED_FAMILY')}")
    check(find(r, "PARAMETERIZED_FAMILY") and set(find(r, "PARAMETERIZED_FAMILY")[0]["members"])
          == {"[Big Cust]", "[Big Dates]", "[Big Ship]"}, f"parameterized family: {find(r, 'PARAMETERIZED_FAMILY')}")
    sub = find(r, "REPEATED_SUBEXPRESSION")
    check(any(set(f["members"]) == {"[Share 1]", "[Share 2]", "[Share 3]"} for f in sub)
          and len([f for f in sub if set(f["members"]) == {"[Share 1]", "[Share 2]", "[Share 3]"}]) == 1,
          f"repeated subexpression reported once at its largest span: {sub}")
    check(any("helper measure is safe" in f["message"] for f in sub if "[Share 1]" in f["members"]),
          "top-level subexpression: helper measure is safe")
    fam = find(r, "CALC_GROUP_FAMILY")
    check(len(fam) == 1 and len(fam[0]["members"]) == 6 and fam[0]["severity"] == "medium"
          and fam[0]["confidence"] == "high" and "--family " in fam[0]["fix"],
          f"calculation-group family (YTD, PY over three bases): {fam}")
    check(not any(set(f["members"]) & {"[Total Sales YTD]", "[Inner X PY]"} for f in find(r, "PARAMETERIZED_FAMILY")),
          "family members are not reported again as parameterized")
    check(find(r, "UNFOLDERED_MEASURES", "Sales") and not find(r, "MEASURES_SCATTERED"),
          "design: Sales has more than 10 loose measures; measures live in too few tables to be scattered")
    dates = find(r, "MULTIPLE_DATE_TABLES")
    check(dates and set(dates[0]["members"]) == {"Date", "Ship Calendar"}, f"date tables: {dates}")
    check(not find(r, "HIGH_CARDINALITY_BIDIRECTIONAL") and not find(r, "UNUSED_HIGH_CARDINALITY")
          and not find(r, "MEMORY_HOTSPOT"), "bucket rules stay silent without evidence")
    cov = {c["rule"]: c["status"] for c in r.get("coverage", [])}
    check(cov.get("MEMORY_HOTSPOT", "").startswith("skipped") and "pbz index" in cov.get("HIGH_CARDINALITY_BIDIRECTIONAL", ""),
          f"coverage names the missing evidence: {cov}")
    check(r["evidence"] == {"static": True, "index": False, "memory": "none"}, f"evidence: {r['evidence']}")
    it = find(r, "ITERATOR_MEASURE_CALL", "[Iter Call]")[0]
    check(it["severity"] == "low" and "rows unknown" in it["evidence"][0], f"no buckets, low severity: {it}")
    nest = find(r, "NESTED_ITERATOR", "[Nested Iter]")[0]
    check("via [Inner X]" in nest["message"], f"nested through a measure: {nest}")
    calc = find(r, "CALC_COLUMN_AS_MEASURE")[0]
    check(calc["members"] == ["[Line Total]"] and "SUMX('Sales'" in calc["message"], f"calc column as measure: {calc}")
    check(all(f["id"].startswith("a") and len(f["id"]) == 12 for f in r["findings"]), "fingerprints")
    check(len({f["id"] for f in r["findings"]}) == len(r["findings"]), "fingerprints unique")
    blob = json.dumps(r)
    check("Jane Doe" not in blob and "regions.csv" not in blob and "srv" not in blob,
          "audit output carries names only, no literals")
    only = audit.run(ws, project, category="consolidation")
    check(only["findings"] and all(f["category"] == "consolidation" for f in only["findings"]), "category filter")
    one = audit.run(ws, project, rules=["COPY_COLUMN"])
    check({f["rule"] for f in one["findings"]} == {"COPY_COLUMN"}, "rule filter")
    try:
        audit.run(ws, project, rules=["NOPE"])
        check(False, "unknown rule should fail")
    except PbzError as e:
        check(e.code == "BAD_REQUEST", f"unknown rule: {e.code}")
    full = audit.run(ws, project, coverage=True)
    check(len(full["coverage"]) == len(audit.RULES) and all("source" in c for c in full["coverage"]), "full coverage list")

    # ---------------------------------------------------------------- index buckets
    os.makedirs(ws.private, exist_ok=True)
    with open(os.path.join(ws.private, "index-summary.json"), "w", encoding="utf-8") as f:
        json.dump({"tables": [{"table": "Sales", "rows_bucket": "10k+",
                               "columns": [{"column": "Amount Copy", "card_bucket": "10k+"}]},
                              {"table": "Customer", "rows_bucket": "1k-9k",
                               "columns": [{"column": "CustomerKey", "card_bucket": "10k+"}]}]}, f)
    r = audit.run(ws, project)
    it = find(r, "ITERATOR_MEASURE_CALL", "[Iter Call]")[0]
    check(it["severity"] == "medium" and it["evidence"] == ["rows: 10k+ (index)"], f"row bucket raises severity: {it}")
    check(find(r, "HIGH_CARDINALITY_BIDIRECTIONAL"), "bidirectional relationship on a 10k+ key")
    check(find(r, "UNUSED_HIGH_CARDINALITY", "'Sales'[Amount Copy]"), "unused high-cardinality column")
    check(r["evidence"]["index"] is True, "index evidence flagged")

    # ---------------------------------------------------------------- memory bands
    mem = {"tables": [{"name": "Sales", "size_band": "100-1000 MiB", "share_band": ">=20%", "columns": [
        {"name": "Comment", "size_band": "100-1000 MiB", "share_band": ">=20%", "dictionary_heavy": True, "long_text": True},
        {"name": "DueDateKey", "size_band": "10-100 MiB", "share_band": "5-20%", "time_part": True},
        {"name": "Amount", "size_band": "1-10 MiB", "share_band": "1-5%", "fractional": False},
        {"name": "Region", "size_band": "10-100 MiB", "share_band": "1-5%", "dictionary_heavy": True}]}]}
    real_memory = audit._memory
    audit._memory = lambda ws_, project_: dict(mem, stale=False)
    try:
        r = audit.run(ws, project)
    finally:
        audit._memory = real_memory
    for rule, obj in [("MEMORY_HOTSPOT", "'Sales'[Comment]"), ("LONG_TEXT", "'Sales'[Comment]"),
                      ("MEMORY_HOTSPOT", "'Sales'[DueDateKey]"), ("DATETIME_TIME_PART", "'Sales'[DueDateKey]"),
                      ("DOUBLE_WITHOUT_FRACTIONS", "'Sales'[Amount]"), ("DICTIONARY_HEAVY", "'Sales'[Region]")]:
        check((rule, obj) in rules_of(r), f"memory rule {rule} on {obj}")
    check(not find(r, "DICTIONARY_HEAVY", "'Sales'[Comment]"), "hotspot not repeated as dictionary-heavy")
    check(r["evidence"]["memory"] == "fresh" and r["findings"][0]["severity"] == "high", "memory evidence sorts first")
    # a tiny model: shares say nothing worth fixing; strict disclosure: no bands, predicates still count
    for variant, want, unwanted in [
            ({"model": {"size_band": "1-10 MiB"}, "tables": [{"name": "Sales", "columns": [
                {"name": "Comment", "size_band": "1-10 MiB", "share_band": ">=20%", "dictionary_heavy": True}]}]},
             [], ["MEMORY_HOTSPOT", "UNUSED_HIGH_CARDINALITY"]),
            ({"tables": [{"name": "Sales", "columns": [{"name": "DueDateKey", "dictionary_heavy": False, "time_part": True}]}]},
             ["DATETIME_TIME_PART"], ["MEMORY_HOTSPOT"])]:
        audit._memory = lambda ws_, project_, v=variant: dict(v, stale=False)
        try:
            rv = audit.run(ws, project)
        finally:
            audit._memory = real_memory
        check(all(find(rv, w) for w in want) and not any(find(rv, u, "Comment") for u in unwanted),
              f"memory variant {sorted(variant)}: {[(f['rule'], f['object']) for f in rv['findings'] if 'memory' in str(f['evidence'])]}")
        if want:
            tp = find(rv, "DATETIME_TIME_PART")[0]
            check(tp["severity"] == "low" and "withheld" in tp["evidence"][0], f"strict bands withheld: {tp}")
    # the saved file of `pbz audit memory`: fresh until the mirror changes
    from pbz import evidence
    saved = {"verb": "memory", "project": project.name, "saved_at": "2026-11-05T00:00:00",
             "mirror_rev": evidence.mirror_rev(ws, project), "disclosure": "shape",
             "model": {"size_band": "100-1000 MiB"}, "tables": [],
             "relationships": [{"name": "r1", "from": "Sales[CustomerKey]", "to": "Customer[CustomerKey]",
                                "size_band": "10-100 MiB"}],
             "auto_datetime": {"size_band": "10-100 MiB", "share_band": "5-20%"}, "coverage": "FULL", "predicates": "DONE"}
    mem_file = os.path.join(ws.pbz_dir, "memory.json")
    os.makedirs(ws.pbz_dir, exist_ok=True)
    with open(mem_file, "w", encoding="utf-8") as f:
        json.dump(saved, f)
    r = audit.run(ws, project)
    check(r["evidence"]["memory"] == "fresh" and find(r, "LARGE_RELATIONSHIP", "Sales[CustomerKey]")
          and find(r, "AUTO_DATETIME_STORAGE") and find(r, "AUTO_DATETIME_STORAGE")[0]["severity"] == "medium",
          f"saved memory bands: {r['evidence']} {[f['rule'] for f in r['findings'] if 'memory' in str(f['evidence'])]}")
    append("tables/Sales.tmdl", "\n\tmeasure 'Touch' = 1\n\t\tlineageTag: 20000000-0000-0000-0000-000000000041\n")
    r = audit.run(ws, project)
    check(r["evidence"]["memory"] == "stale" and any("stale" in c["status"] for c in r.get("coverage", [])),
          f"mirror edit marks bands stale: {r['evidence']}")
    os.remove(mem_file)

    # ---------------------------------------------------------------- baselines, waivers, gate
    base = audit.run(ws, project, save_baseline="b1")
    check(base.get("baseline_saved") == "b1" and os.path.isfile(os.path.join(ws.pbz_dir, "audit", "baselines", "b1.json")),
          "baseline saved")
    saved = open(os.path.join(ws.pbz_dir, "audit", "baselines", "b1.json"), encoding="utf-8").read()
    check("Jane Doe" not in saved and "SUMX" not in saved, "baseline holds fingerprints and names only")
    append("tables/Sales.tmdl", "\n\tmeasure 'Iter Call 2' = SUMX(Sales, [Total Sales] + 1)\n"
                                "\t\tlineageTag: 20000000-0000-0000-0000-000000000040\n")
    r = audit.run(ws, project, since="b1", fail_on="new")
    new = [f for f in r["findings"] if f.get("status") == "new"]
    check(r["gate"] == "FAIL" and r["baseline"]["new"] == 1 and len(new) == 1 and new[0]["object"] == "[Iter Call 2]",
          f"one new finding fails the gate: {r.get('baseline')} {[f['object'] for f in new]}")
    w = audit.waive(ws, project, new[0]["id"], "accepted until the rewrite", expires="2099-01-01")
    check(w["result"] == "WAIVED", f"waive: {w}")
    r = audit.run(ws, project, since="b1", fail_on="new")
    check(r["gate"] == "PASS" and r["summary"]["waived"] == 1 and r["waived"][0]["id"] == new[0]["id"],
          f"waived finding passes the gate: {r.get('gate')} {r.get('waived')}")
    for args, code in [((new[0]["id"], ""), "BAD_REQUEST"), (("a00000000000", "x"), "NOT_FOUND")]:
        try:
            audit.waive(ws, project, *args)
            check(False, f"waive {args} should fail")
        except PbzError as e:
            check(e.code == code, f"waive {args}: {e.code}")
    try:
        audit.waive(ws, project, new[0]["id"], "x", expires="next week")
        check(False, "bad expiry should fail")
    except PbzError as e:
        check(e.code == "BAD_REQUEST", f"bad expiry: {e.code}")
    path = os.path.join(mdef, "tables", "Sales.tmdl")
    text, bom = common.read_text(path)
    text = text.replace("\tmeasure 'Iter Call' = SUMX(Sales, [Total Sales])\n\t\tlineageTag: 20000000-0000-0000-0000-000000000001\n", "")
    common.write_text(path, text, bom)
    r = audit.run(ws, project, since="b1")
    check(any(x["rule"] == "ITERATOR_MEASURE_CALL" and x["object"] == "[Iter Call]" for x in r["baseline"]["resolved"]),
          f"resolved finding listed: {r['baseline']['resolved']}")
    check(audit.waive(ws, project, new[0]["id"], None, remove=True)["result"] == "REMOVED", "unwaive")
    try:
        audit.run(ws, project, fail_on="new")
        check(False, "--fail-on new needs --since")
    except PbzError as e:
        check(e.code == "BAD_REQUEST", f"fail-on new without since: {e.code}")
    g = audit.run(ws, project, fail_on="high")
    check(g["gate"] == "PASS", f"no high findings without memory evidence: {g.get('gate_findings')}")

    if fails:
        print("FAIL")
        for f in fails:
            print(" -", f)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
