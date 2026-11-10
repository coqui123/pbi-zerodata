"""Follow-up checks for expand, unused-var rewrite, scripts, native SQL and CI lines.

Run: python tests/test_followup_author.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import ciout, common, expand, rewrite, scripts, sourceline  # noqa: E402
from pbz.common import PbzError  # noqa: E402

LEAK = "DO_NOT_LEAK_EXPR"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _bytes(root: str) -> dict[str, bytes]:
    found = {}
    for dirpath, dirnames, names in os.walk(root):
        dirnames.sort()
        for name in sorted(names):
            path = os.path.join(dirpath, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = handle.read()
    return found


class FollowupAuthorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pbzfollow-")
        self.addCleanup(self.tmp.cleanup)
        private = os.path.join(self.tmp.name, "private")
        os.environ["PBZ_PRIVATE_ROOT"] = private
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        self.ws = common.Workspace(self.root)
        self.ws.private = private
        _write(os.path.join(self.root, "Mini.pbip"), "{}\n")
        self.project = self.ws.project()

    def _model(self, table: str, functions: str = "") -> str:
        mdef = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), "model Model\n\tculture: en-US\n\nref table Sales\n")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), table)
        if functions:
            _write(os.path.join(mdef, "functions.tmdl"), functions)
        return mdef

    def test_expand_walks_functions_and_keeps_dax(self):
        self._model(
            "table Sales\n"
            "\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
            "\tmeasure Leaf = 1\n"
            "\tmeasure HiddenM = [Leaf] + 0\n"
            "\tmeasure Root = sCaLe([Total Sales])\n"
            "\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n",
            "function Scale = (x : int64) => innerfn(x) + [HiddenM]\n"
            "\tlineageTag: f1\n\n"
            "function InnerFn = (y : int64) => y + [Leaf]\n"
            "\tlineageTag: f2\n",
        )
        out = expand.run(self.ws, self.project, "[Root]")
        self.assertIn("dax", out)
        self.assertIsInstance(out["dax"], str)
        self.assertIn("DEFINE", out["dax"])
        self.assertEqual(set(out["measures"]), {"Leaf", "HiddenM", "Total Sales", "Root"})
        self.assertEqual(set(out["functions"]), {"InnerFn", "Scale"})
        self.assertEqual(len(out["functions"]), 2)
        self.assertIn("FUNCTION Scale = (x : int64) => innerfn(x) + [HiddenM]", out["dax"])
        self.assertIn("FUNCTION InnerFn = (y : int64) => y + [Leaf]", out["dax"])
        self.assertIn("MEASURE 'Sales'[HiddenM]", out["dax"])
        self.assertIn("MEASURE 'Sales'[Total Sales]", out["dax"])
        self.assertNotIn("FUNCTION sCaLe =", out["dax"])
        self.assertNotIn("FUNCTION innerfn =", out["dax"])
        for line in out["dax"].splitlines():
            if line.startswith("FUNCTION"):
                self.assertFalse(line.rstrip().endswith("="), line)
                self.assertIn("=>", line)

    def test_expand_omits_unresolved_functions_and_stops_on_cycles(self):
        self._model(
            "table Sales\n"
            "\tmeasure Leaf = 1\n"
            "\tmeasure Root = Ping(1) + MissingFn([Leaf])\n"
            "\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n",
            "function Ping = (x : int64) => Pong(x)\n\tlineageTag: f1\n\n"
            "function Pong = (x : int64) => Ping(x) + [Leaf]\n\tlineageTag: f2\n",
        )
        out = expand.run(self.ws, self.project, "[Root]")
        self.assertIn("dax", out)
        self.assertEqual(set(out["functions"]), {"Ping", "Pong"})
        self.assertEqual(out["functions"].count("Ping"), 1)
        self.assertEqual(out["functions"].count("Pong"), 1)
        self.assertIn("Leaf", out["measures"])
        self.assertIn("FUNCTION Ping = (x : int64) => Pong(x)", out["dax"])
        self.assertIn("FUNCTION Pong = (x : int64) => Ping(x) + [Leaf]", out["dax"])
        self.assertNotIn("FUNCTION MissingFn", out["dax"])
        for line in out["dax"].splitlines():
            if line.startswith("FUNCTION"):
                self.assertNotEqual(line.split("=", 1)[1].strip(), "")

    def test_unused_keeps_return_of_a_remaining_var(self):
        expr = "VAR keep = [Stay]\nVAR drop = [Gone]\nRETURN keep"
        out = rewrite._unused(expr)
        self.assertIsNotNone(out)
        self.assertIn("RETURN", out.upper())
        self.assertIn("[Stay]", out)
        self.assertIn("keep", out)
        self.assertNotIn("drop", out)
        self.assertNotIn("[Gone]", out)

        with self.assertRaises(PbzError) as nested:
            rewrite._unused("VAR keep =\n    VAR drop = [Gone]\n    RETURN [Stay]\nRETURN keep")
        self.assertEqual(nested.exception.code, "REFUSED")
        self.assertEqual(nested.exception.detail, "unused-var rewrite did not keep the rest of the expression")
        still = rewrite._unused("VAR u = 0\nVAR a = 1\nRETURN a +\nVAR b = [Stay]\nRETURN b")
        self.assertIsNotNone(still)
        self.assertIn("[Stay]", still)
        self.assertNotIn("VAR u", still)
        self.assertEqual(still.upper().count("RETURN"), 2)
        table = rewrite._unused("VAR u = 0\nVAR a = {1, 2}\nRETURN a")
        followed = rewrite._unused("VAR z = 0\nVAR t = {1}\nRETURN VAR u = [Stay]\nRETURN u + COUNTROWS(t)")
        self.assertIsNotNone(followed)
        self.assertIn("[Stay]", followed)
        self.assertEqual(followed.upper().count("RETURN"), 2)
        self.assertNotIn("VAR z", followed)
        self.assertIsNotNone(table)
        self.assertIn("{1, 2}", table)
        self.assertIn("RETURN", table.upper())
        self.assertNotIn("VAR u", table)

        with self.assertRaises(PbzError) as leading:
            rewrite._unused("[Stay] + [End] + VAR drop = [Gone]\nRETURN 0")
        self.assertEqual(leading.exception.code, "REFUSED")
        self.assertEqual(leading.exception.detail, "unused-var rewrite did not keep the rest of the expression")

    def test_unused_refuses_a_leading_return_left_by_a_nested_var(self):
        expr = "VAR x = 1 RETURN SUMX(T, VAR y = [a] RETURN y)"
        with self.assertRaises(PbzError) as caught:
            rewrite._unused(expr)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "unused-var rewrite did not keep the rest of the expression")
        plain = rewrite._unused("VAR x = 1\nRETURN [Stay]")
        self.assertIsNotNone(plain)
        self.assertNotIn("RETURN", plain.upper())
        self.assertIn("[Stay]", plain)
        self.assertNotIn("VAR", plain.upper())
        with self.assertRaises(PbzError) as nested_call:
            rewrite._unused("SUMX(T, VAR x = 1 RETURN [M])")
        self.assertEqual(nested_call.exception.code, "REFUSED")
        self.assertEqual(nested_call.exception.detail, "unused-var rewrite did not keep the rest of the expression")
        for expr in (
            "IF(x, VAR a = 1 RETURN a, VAR u = 1 RETURN [M])",
            "VAR a = 1 RETURN VAR u = 1 RETURN a",
            "VAR t = {1} VAR u = 2 RETURN VAR unused = 1 RETURN COUNTROWS(t) + u",
        ):
            with self.assertRaises(PbzError) as borrowed:
                rewrite._unused(expr)
            self.assertEqual(borrowed.exception.code, "REFUSED")
            self.assertEqual(borrowed.exception.detail, "unused-var rewrite did not keep the rest of the expression")

    def test_unused_refuses_when_an_outside_bracket_disappears(self):
        expr = "[Stay] + VAR drop = [Gone]\nRETURN [End]"
        spans = [(expr.index("VAR drop"), expr.index("RETURN"))]
        with self.assertRaises(PbzError) as caught:
            rewrite._require_outside_brackets(expr, spans, "[Stay] + RETURN ")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn("rewrite", caught.exception.detail)
        with self.assertRaises(PbzError) as unused:
            rewrite._unused(expr)
        self.assertEqual(unused.exception.code, "REFUSED")
        self.assertEqual(unused.exception.detail, "unused-var rewrite did not keep the rest of the expression")

    def test_script_export_then_apply_is_nothing_to_change(self):
        self._model(
            "table Sales\n"
            "\tmeasure 'Total Sales' = SUM(Sales[Amount])\n"
            "\tmeasure Padded =\n"
            "\t\t\tSUM(Sales[Amount]) + [Total Sales]\n"
            "\tpartition Sales = m\n\t\tmode: import\n\t\tsource = let x = 1 in x\n",
        )
        path = os.path.join(self.root, "measures.json")
        before = _bytes(self.project.mirror_model)
        exported = scripts.export(self.ws, self.project, path)
        self.assertEqual(exported["action"], "export")
        applied = scripts.apply(self.ws, self.project, path)
        self.assertEqual(applied["result"], "NOTHING_TO_CHANGE")
        self.assertEqual(applied["measures"], [])
        self.assertEqual(_bytes(self.project.mirror_model), before)

        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        padded = next(item for item in doc["measures"] if item["name"] == "Padded")
        self.assertFalse(padded["expression"].startswith("\t"))
        self.assertIn("SUM(Sales[Amount])", padded["expression"])
        doc["measures"].append(dict(padded))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        with self.assertRaises(PbzError) as caught:
            scripts.apply(self.ws, self.project, path)
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertIn("duplicate", caught.exception.detail)
        self.assertEqual(_bytes(self.project.mirror_model), before)

        doc["measures"].pop()
        changed = next(item for item in doc["measures"] if item["name"] == "Total Sales")
        changed["expression"] = "SUM(Sales[Amount]) + 0"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        updated = scripts.apply(self.ws, self.project, path)
        self.assertEqual(updated["result"], "CHANGED")
        self.assertEqual(updated["measures"], ["Total Sales"])
        again = scripts.export(self.ws, self.project, path)
        self.assertGreater(again["measures"], 0)
        quiet = scripts.apply(self.ws, self.project, path)
        self.assertEqual(quiet["result"], "NOTHING_TO_CHANGE")

    def test_native_sql_balances_database_call(self):
        secret_server = "ServerSecretXYZ"
        secret_db = "DatabaseSecretXYZ"
        queries = {
            "Parsed": (
                f'let Source = Value.NativeQuery(Sql.Database("{secret_server}", "{secret_db}"), '
                '"SELECT Alpha, Beta FROM Fact", null, [EnableFolding=true]) in Source'
            ),
            "Nested": (
                'let Source = Value.NativeQuery(Sql.Database("ser,ver(1)", "db"), '
                '"SELECT Kept FROM Fact") in Source'
            ),
            "Quoted": (
                'let Source = Value.NativeQuery(Sql.Database(ServerName, "db"), '
                '"SELECT \'Alpha\' AS Name FROM Fact") in Source'
            ),
            "Comment": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"SELECT Alpha -- note FROM Fact") in Source'
            ),
            "Block": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"SELECT Alpha /* note */ FROM Fact") in Source'
            ),
            "Tokenized": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"SELECT «lit#1», Alpha FROM Fact") in Source'
            ),
            "NonSelect": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"UPDATE Fact SET Alpha = 1") in Source'
            ),
            "InsertSelect": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"INSERT INTO dst SELECT Amount FROM Sales") in Source'
            ),
            "Tripled": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"""SELECT Amount FROM Sales""") in Source'
            ),
            "EscapedQuote": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"SELECT #(0022)Amount#(0022) FROM Sales") in Source'
            ),
            "Concat": (
                'let Source = Value.NativeQuery(Sql.Database("s", "d"), '
                '"SELECT Amount FROM Sales" & «lit#1») in Source'
            ),
            "Plain": (
                'let Source = Sql.Database("s", "d") in Source'
            ),
        }
        blocks = []
        for name, source in queries.items():
            blocks.append(
                f"table {name}\n"
                f"\tpartition {name} = m\n"
                f"\t\tmode: import\n"
                f"\t\tsource =\n\t\t\t\t{source}\n"
            )
        mdef = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), "model Model\n")
        for block in blocks:
            first = block.splitlines()[0].split()[1]
            _write(os.path.join(mdef, "tables", first + ".tmdl"), block)
        report = sourceline.native_sql(self.ws, self.project)
        by_table = {item["table"]: item for item in report["native_sql"]}
        blob = json.dumps(report)
        self.assertNotIn(secret_server, blob)
        self.assertNotIn(secret_db, blob)
        parsed = by_table["Parsed"]
        self.assertEqual(parsed["status"], "PARSED")
        self.assertEqual(parsed["columns"], ["Alpha", "Beta"])
        nested = by_table["Nested"]
        self.assertEqual(nested["status"], "PARSED")
        self.assertEqual(nested["columns"], ["Kept"])
        self.assertEqual(by_table["Quoted"]["status"], "PARSED")
        self.assertEqual(by_table["Quoted"]["columns"], [])
        self.assertEqual(by_table["Comment"]["status"], "PARSED")
        self.assertEqual(by_table["Comment"]["columns"], [])
        self.assertEqual(by_table["Block"]["status"], "PARSED")
        self.assertEqual(by_table["Block"]["columns"], [])
        self.assertEqual(by_table["Tokenized"]["status"], "OPAQUE")
        self.assertNotIn("columns", by_table["Tokenized"])
        self.assertNotIn("«lit#1»", blob)
        self.assertEqual(by_table["NonSelect"]["status"], "OPAQUE")
        self.assertEqual(by_table["InsertSelect"]["status"], "OPAQUE")
        self.assertNotIn("columns", by_table["InsertSelect"])
        self.assertNotIn("Amount", json.dumps(by_table["InsertSelect"]))
        self.assertEqual(by_table["Tripled"]["status"], "OPAQUE")
        self.assertNotIn("columns", by_table["Tripled"])
        self.assertNotIn("Amount", json.dumps(by_table["Tripled"]))
        self.assertEqual(by_table["EscapedQuote"]["status"], "OPAQUE")
        self.assertNotIn("columns", by_table["EscapedQuote"])
        self.assertNotIn("Amount", json.dumps(by_table["EscapedQuote"]))
        self.assertEqual(by_table["Concat"]["status"], "OPAQUE")
        self.assertNotIn("columns", by_table["Concat"])
        self.assertNotIn("Amount", json.dumps(by_table["Concat"]))
        self.assertEqual(by_table["Plain"]["status"], "OPAQUE")

    def test_native_sql_record_lookup_is_one_argument(self):
        source = (
            'let Source = Value.NativeQuery(Source{[Schema="x",Item="y"]}[Data], '
            '"select Amount from Sales") in Source'
        )
        block = (
            "table Lookup\n"
            "\tpartition Lookup = m\n"
            "\t\tmode: import\n"
            f"\t\tsource =\n\t\t\t\t{source}\n"
        )
        mdef = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(mdef, "model.tmdl"), "model Model\n")
        _write(os.path.join(mdef, "tables", "Lookup.tmdl"), block)
        report = sourceline.native_sql(self.ws, self.project)
        blob = json.dumps(report)
        self.assertNotIn("select Amount from Sales", blob)
        self.assertNotIn('Schema="x"', blob)
        self.assertNotIn('Item="y"', blob)
        self.assertEqual(len(report["native_sql"]), 1)
        item = report["native_sql"][0]
        self.assertEqual(item["status"], "PARSED")
        self.assertEqual(item["columns"], ["Amount"])
        for name in ("Schema", "Item", "y"):
            self.assertNotIn(name, item["columns"])
        self.assertNotIn("server", item)
        self.assertNotIn("database", item)

    def test_ci_lines_escape_github_and_azdo_separately(self):
        finding = {
            "rule": "RULE",
            "object": "A,B:C;D]E%\nF\rG",
            "where": "a;b]c,d:e%",
            "severity": "error",
            "message": LEAK,
            "expression": LEAK + " SUM(Sales[Amount])",
        }
        github = ciout._ci_lines("github", [finding])
        azdo = ciout._ci_lines("azdo", [finding])
        self.assertEqual(len(github), 1)
        self.assertEqual(len(azdo), 1)
        gline, aline = github[0], azdo[0]
        self.assertNotIn(LEAK, gline)
        self.assertNotIn(LEAK, aline)
        self.assertNotIn("SUM(Sales[Amount])", gline + aline)
        self.assertTrue(gline.startswith("::error file="))
        g_props, g_message = gline.split("::", 2)[1:]
        self.assertEqual(g_message, "RULE A,B:C;D]E%25%0AF%0DG")
        self.assertIn("file=a;b]c%2Cd%3Ae%25", g_props)
        self.assertIn("title=RULE", g_props)
        self.assertNotIn("%3B", gline)
        self.assertNotIn("%5D", gline)
        self.assertTrue(aline.startswith("##vso[task.logissue type=error;sourcepath="))
        a_message = aline.split("]", 1)[1]
        self.assertEqual(a_message, "RULE A,B:C%3BD%5DE%25%0AF%0DG")
        self.assertIn("sourcepath=a%3Bb%5Dc,d:e%25]", aline)
        self.assertNotIn("%3A", aline)
        self.assertNotIn("%2C", aline)
        literal = {"rule": "PCT", "object": "%0A", "where": "p", "severity": "error",
                   "expression": LEAK}
        pct = ciout._ci_lines("github", [literal])[0]
        self.assertEqual(pct.split("::", 2)[2], "PCT %250A")
        self.assertNotIn(LEAK, pct)


if __name__ == "__main__":
    unittest.main()
