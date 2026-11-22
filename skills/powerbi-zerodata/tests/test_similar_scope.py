"""Offline checks that scoped canonical keys cannot authorize unsafe merges.

Nested iterator references must preserve their row-table binding, and unresolved
references must block automatic consolidation even when canonical keys match.

Run: python tests/test_similar_scope.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import common, consolidate, deps, similar, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SALES = (
    "table Sales\n\n"
    "\tcolumn Price\n\t\tdataType: int64\n\t\tsourceColumn: Price\n\n"
    "\tmeasure IterA = SUMX(Sales, SUMX(Other, [Price]))\n\n"
    "\tmeasure IterB = SUMX(Sales, SUMX(Other, Sales[Price]))\n\n"
    "\tmeasure IterC = SUMX(Sales, [Price] + SUMX(Other, [Price]))\n\n"
    "\tmeasure IterD = SUMX(Sales, [Price] + SUMX(Other, Sales[Price]))\n\n"
    "\tmeasure UnresA = SUMX(Sales, [Nope])\n\n"
    "\tmeasure UnresB = SUMX(Sales, [Nope])\n\n"
    "\tmeasure CtrlA = SUM(Sales[Price])\n\n"
    "\tmeasure CtrlB = SUM(Sales[Price])\n\n"
    "\tmeasure VarA = VAR v = Other RETURN SUMX(v, [Price])\n\n"
    "\tmeasure VarB = VAR v = Other RETURN SUMX(v, Sales[Price])\n\n"
    "\tmeasure AliasUnres = 'Missing'[CtrlA]\n\n"
    "\tmeasure AliasOk = [CtrlA]\n\n"
    "\tmeasure WrapA = VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure WrapB = VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure TabA = COUNTROWS('Missing')\n\n"
    "\tmeasure TabB = COUNTROWS('Missing')\n\n"
    "\tmeasure TabCtrlA = COUNTROWS(Sales)\n\n"
    "\tmeasure TabCtrlB = COUNTROWS(Sales)\n\n"
    "\tmeasure TopA = VAR v = Other RETURN SUMX(TOPN(MAX(Sales[Price]), v), [Price])\n\n"
    "\tmeasure TopB = VAR v = Other RETURN SUMX(TOPN(MAX(Sales[Price]), v), Sales[Price])\n\n"
    "\tmeasure ScalarA = VAR n = 0 RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ScalarB = VAR n = 0 RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ExprA = VAR v = FILTER(Other, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure ExprB = VAR v = FILTER(Other, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure ChainA = VAR a = Other VAR v = a RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure ChainB = VAR a = Other VAR v = a RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure ValuesA = VAR v = VALUES(Other[Price]) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure ValuesB = VAR v = VALUES(Other[Price]) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure DerivedA = VAR a = Other VAR v = FILTER(a, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure DerivedB = VAR a = Other VAR v = FILTER(a, TRUE()) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure ScalarQA = VAR n = MAX('Sales'[Price]) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ScalarQB = VAR n = MAX('Sales'[Price]) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure CrossA = VAR src = Other VAR v = CROSSJOIN(src, { 1 }) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure CrossB = VAR src = Other VAR v = CROSSJOIN(src, { 1 }) RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure CtorA = VAR v = { 1, 2 } RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])\n\n"
    "\tmeasure CtorB = VAR v = { 1, 2 } RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])\n\n"
    "\tmeasure CountA = VAR n = COUNTROWS(Other) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure CountB = VAR n = COUNTROWS(Other) RETURN SUMX(FILTER(Sales, Sales[Price] > n), [Price])\n\n"
    "\tmeasure ScopeA = (VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), [Price])) + (VAR v = 1 RETURN v)\n\n"
    "\tmeasure ScopeB = (VAR v = Other RETURN SUMX(CALCULATETABLE(v, Sales[Price] > 0), Sales[Price])) + (VAR v = 1 RETURN v)\n\n"
    "\tmeasure InlineA = SUMX(TOPN(MAX(Sales[Price]), Other), [Price])\n\n"
    "\tmeasure InlineB = SUMX(TOPN(MAX(Sales[Price]), Other), Sales[Price])\n\n"
    "\tmeasure QualA = SUMX(Sales, SUMX(TOPN(MAX(Sales[Price]), VALUES(Other[Price])), [Price]))\n\n"
    "\tmeasure QualB = SUMX(Sales, SUMX(TOPN(MAX(Sales[Price]), VALUES(Other[Price])), Sales[Price]))\n\n"
    "\tmeasure GenA = SUMX(Sales, SUMX(TOPN(MAX(Sales[Price]), GENERATESERIES(1, 5)), [Price]))\n\n"
    "\tmeasure GenB = SUMX(Sales, SUMX(TOPN(MAX(Sales[Price]), GENERATESERIES(1, 5)), Sales[Price]))\n\n"
    "\tmeasure TopNCtrlA = SUMX(TOPN(MAX(Sales[Price]), FILTER(Sales, Sales[Price] > 0)), [Price])\n\n"
    "\tmeasure TopNCtrlB = SUMX(TOPN(MAX(Sales[Price]), FILTER(Sales, Sales[Price] > 0)), [Price])\n\n"
    "\tmeasure SumzA = SUMX(SUMMARIZE(Sales, Other[Price]), [Price])\n\n"
    "\tmeasure SumzB = SUMX(SUMMARIZE(Sales, Other[Price]), Sales[Price])\n\n"
    "\tmeasure Gen2A = SUMX(GENERATESERIES(1, MAX(Sales[Price])), [Price])\n\n"
    "\tmeasure Gen2B = SUMX(GENERATESERIES(1, MAX(Sales[Price])), Sales[Price])\n\n"
    "\tmeasure NestCtrlA = SUMX(CALCULATETABLE(FILTER(Sales, Sales[Price] > MAX(Other[Price]))), [Price])\n\n"
    "\tmeasure NestCtrlB = SUMX(CALCULATETABLE(FILTER(Sales, Sales[Price] > MAX(Other[Price]))), [Price])\n\n"
    "\tmeasure Gen3A = SUMX(GENERATESERIES(MAX(Sales[Price]), 3), [Price])\n\n"
    "\tmeasure Gen3B = SUMX(GENERATESERIES(MAX(Sales[Price]), 3), Sales[Price])\n\n"
    "\tmeasure ConsA = SUMX(TOPN(MAX(Sales[Price]), { 1, 2 }), [Price])\n\n"
    "\tmeasure ConsB = SUMX(TOPN(MAX(Sales[Price]), { 1, 2 }), Sales[Price])\n\n"
    "\tmeasure IfCtrlA = SUMX(FILTER(Sales, IF(Sales[Price] > 0, TRUE(), FALSE())), [Price])\n\n"
    "\tmeasure IfCtrlB = SUMX(FILTER(Sales, IF(Sales[Price] > 0, TRUE(), FALSE())), [Price])\n\n"
    "\tmeasure CalcA = SUMX(CALCULATETABLE({ 1, 2 }, Other), [Price])\n\n"
    "\tmeasure CalcB = SUMX(CALCULATETABLE({ 1, 2 }, Other), Other[Price])\n\n"
    "\tmeasure AllCountA = SUMX(GENERATESERIES(1, COUNTROWS(ALL(Sales))), [Price])\n\n"
    "\tmeasure AllCountB = SUMX(GENERATESERIES(1, COUNTROWS(ALL(Sales))), Sales[Price])\n\n"
    "\tmeasure FiltCountA = SUMX(GENERATESERIES(1, COUNTROWS(FILTER(Other, TRUE()))), [Price])\n\n"
    "\tmeasure FiltCountB = SUMX(GENERATESERIES(1, COUNTROWS(FILTER(Other, TRUE()))), Sales[Price])\n\n"
    "\tmeasure IfPredCtrlA = SUMX(FILTER(Sales, IF(Sales[Price] > MAX(Other[Price]), TRUE(), FALSE())), [Price])\n\n"
    "\tmeasure IfPredCtrlB = SUMX(FILTER(Sales, IF(Sales[Price] > MAX(Other[Price]), TRUE(), FALSE())), [Price])\n\n"
    "\tmeasure ContainsA = SUMX(GENERATESERIES(1, IF(CONTAINS(Sales, Sales[Price], 1), 2, 3)), [Price])\n\n"
    "\tmeasure ContainsB = SUMX(GENERATESERIES(1, IF(CONTAINS(Sales, Sales[Price], 1), 2, 3)), Sales[Price])\n\n"
    "\tmeasure PredCountCtrlA = SUMX(FILTER(Sales, COUNTROWS(ALL(Other)) > 0), [Price])\n\n"
    "\tmeasure PredCountCtrlB = SUMX(FILTER(Sales, COUNTROWS(ALL(Other)) > 0), [Price])\n\n"
    "\tmeasure MaxxCtrlA = SUMX(FILTER(Sales, Sales[Price] > MAXX(ALL(Sales), Sales[Price])), [Price])\n\n"
    "\tmeasure MaxxCtrlB = SUMX(FILTER(Sales, Sales[Price] > MAXX(ALL(Sales), Sales[Price])), [Price])\n\n"
    "\tmeasure PredMaxxA = SUMX(FILTER(Sales, Sales[Price] > MAXX(Other, Other[Price])), [Price])\n\n"
    "\tmeasure PredMaxxB = SUMX(FILTER(Sales, Sales[Price] > MAXX(Other, Other[Price])), [Price])\n\n"
    "\tmeasure PredValuesA = SUMX(FILTER(Sales, COUNTROWS(VALUES(Other[Price])) > 0), [Price])\n\n"
    "\tmeasure PredValuesB = SUMX(FILTER(Sales, COUNTROWS(VALUES(Other[Price])) > 0), [Price])\n\n"
    "\tmeasure PredVarA = VAR v = Other RETURN SUMX(FILTER(Sales, COUNTROWS(v) > 0), [Price])\n\n"
    "\tmeasure PredVarB = VAR v = Other RETURN SUMX(FILTER(Sales, COUNTROWS(v) > 0), [Price])\n\n"
    "\tmeasure JoinA = VAR v = Other RETURN SUMX(NATURALLEFTOUTERJOIN(Sales, v), [Price])\n\n"
    "\tmeasure JoinB = VAR v = Other RETURN SUMX(NATURALLEFTOUTERJOIN(Sales, v), Sales[Price])\n\n"
    "\tmeasure JoinInlA = SUMX(NATURALLEFTOUTERJOIN(Sales, Other), [Price])\n\n"
    "\tmeasure JoinInlB = SUMX(NATURALLEFTOUTERJOIN(Sales, Other), Sales[Price])\n\n"
    "\tmeasure TopnCountCtrlA = VAR v = Other RETURN SUMX(TOPN(COUNTX(v, 1), Sales, Sales[Price], DESC), [Price])\n\n"
    "\tmeasure TopnCountCtrlB = VAR v = Other RETURN SUMX(TOPN(COUNTX(v, 1), Sales, Sales[Price], DESC), [Price])\n\n"
    "\tmeasure TopnSortCtrlA = SUMX(TOPN(1, Sales, Sales[Price] + MAX(Other[Price]), DESC), [Price])\n\n"
    "\tmeasure TopnSortCtrlB = SUMX(TOPN(1, Sales, Sales[Price] + MAX(Other[Price]), DESC), [Price])\n\n"
    "\tmeasure TopnBareA = SUMX(Sales, SUMX(TOPN(1, Other, [Price]), Other[Price]))\n\n"
    "\tmeasure TopnBareB = SUMX(Sales, SUMX(TOPN(1, Other, Sales[Price]), Other[Price]))\n\n"
    "\tmeasure TopnCountCtorCtrlA = SUMX(TOPN(COUNTROWS({ 1, 2 }), Sales, Sales[Price], DESC), [Price])\n\n"
    "\tmeasure TopnCountCtorCtrlB = SUMX(TOPN(COUNTROWS({ 1, 2 }), Sales, Sales[Price], DESC), [Price])\n\n"
    "\tmeasure TopnCntA = SUMX(Sales, SUMX(TOPN(Sales[Price], Other, [Price]), Other[Price]))\n\n"
    "\tmeasure TopnCntB = SUMX(Sales, SUMX(TOPN(Sales[Price], Other, Sales[Price]), Other[Price]))\n\n"
    "\tmeasure RankBareA = SUMX(Sales, RANK(DENSE, Other, ORDERBY([Price])))\n\n"
    "\tmeasure RankBareB = SUMX(Sales, RANK(DENSE, Other, ORDERBY(Sales[Price])))\n\n"
    "\tmeasure FailA = SUMX(Sales, SUMX(TOPN(1, SUMMARIZE(Sales, Other[Price]), [Price]), Other[Price]))\n\n"
    "\tmeasure FailB = SUMX(Sales, SUMX(TOPN(1, SUMMARIZE(Sales, Other[Price]), Sales[Price]), Other[Price]))\n\n"
    "\tmeasure ExcA = VAR v = Other RETURN SUMX(Sales, COUNTROWS(TOPN(1, EXCEPT(v, Sales), [Price], ASC)))\n\n"
    "\tmeasure ExcB = VAR v = Other RETURN SUMX(Sales, COUNTROWS(TOPN(1, EXCEPT(v, Sales), Sales[Price], ASC)))\n\n"
    "\tmeasure MissingA = COUNTROWS(Missing)\n\n"
    "\tmeasure MissingB = COUNTROWS(Missing)\n\n"
    "\tmeasure PoisA = SUMX(Sales, SUMX(EXCEPT(GENERATESERIES(1, MAX(Other[Price])), Other), [Price]))\n\n"
    "\tmeasure PoisB = SUMX(Sales, SUMX(EXCEPT(GENERATESERIES(1, MAX(Other[Price])), Other), Other[Price]))\n\n"
    "\tmeasure UnionRowA = SUMX(Sales, SUMX(UNION(ROW(\"Price\", Sales[Price] + 1), Sales), [Price]))\n\n"
    "\tmeasure UnionRowB = SUMX(Sales, SUMX(UNION(ROW(\"Price\", Sales[Price] + 1), Sales), Sales[Price]))\n\n"
    "\tmeasure WinOptA = SUMX(Sales, COUNTROWS(WINDOW(1, ABS, 3, Other, ORDERBY(Sales[Price], [Price]))))\n\n"
    "\tmeasure WinOptB = SUMX(Sales, COUNTROWS(WINDOW(1, ABS, 3, Other, ORDERBY(Sales[Price], Sales[Price]))))\n\n"
    "\tmeasure UnionWrapA = SUMX(Sales, SUMX(UNION(ROW(\"Price\", Sales[Price] + 1), FILTER(Sales, TRUE())), [Price]))\n\n"
    "\tmeasure UnionWrapB = SUMX(Sales, SUMX(UNION(ROW(\"Price\", Sales[Price] + 1), FILTER(Sales, TRUE())), Sales[Price]))\n\n"
    "\tmeasure UnionAllA = SUMX(Sales, SUMX(UNION(ROW(\"Price\", Sales[Price] + 1), ALL(Sales)), [Price]))\n\n"
    "\tmeasure UnionAllB = SUMX(Sales, SUMX(UNION(ROW(\"Price\", Sales[Price] + 1), ALL(Sales)), Sales[Price]))\n\n"
    "\tmeasure YearCtrlA = CALCULATE(SUM(Sales[Price]), DATEADD(Sales[Price], -1, YEAR))\n\n"
    "\tmeasure YearCtrlB = CALCULATE(SUM(Sales[Price]), DATEADD(Sales[Price], -1, YEAR))\n\n"
    "\tmeasure TopnExcA = SUMX(Sales, SUMX(TOPN(MAX(Sales[Price]), EXCEPT(ROW(\"Price\", 1), Sales)), [Price]))\n\n"
    "\tmeasure TopnExcB = SUMX(Sales, SUMX(TOPN(MAX(Sales[Price]), EXCEPT(ROW(\"Price\", 1), Sales)), Sales[Price]))\n\n"
    "\tmeasure NestUnionA = SUMX(Sales, SUMX(UNION(Sales, FILTER(UNION(ROW(\"Price\", Sales[Price] + 1), ALL(Sales)), TRUE())), [Price]))\n\n"
    "\tmeasure NestUnionB = SUMX(Sales, SUMX(UNION(Sales, FILTER(UNION(ROW(\"Price\", Sales[Price] + 1), ALL(Sales)), TRUE())), Sales[Price]))\n\n"
    "\tmeasure SelColA = SUMX(Sales, SUMX(UNION(Sales, SELECTCOLUMNS(Sales, \"Adjusted\", Sales[Price] + 1)), [Price]))\n\n"
    "\tmeasure SelColB = SUMX(Sales, SUMX(UNION(Sales, SELECTCOLUMNS(Sales, \"Adjusted\", Sales[Price] + 1)), Sales[Price]))\n\n"
    "\tmeasure AllExcA = SUMX(Sales, SUMX(UNION(VALUES(Sales[Price]), ALLEXCEPT(Sales, Sales[Price])), [Price]))\n\n"
    "\tmeasure AllExcB = SUMX(Sales, SUMX(UNION(VALUES(Sales[Price]), ALLEXCEPT(Sales, Sales[Price])), Sales[Price]))\n\n"
    "\tmeasure TreatA = SUMX(FILTER(Sales, Sales[Price] = 1), SUMX(UNION(Sales, TREATAS(Sales, Sales[Price])), [Price]))\n\n"
    "\tmeasure TreatB = SUMX(FILTER(Sales, Sales[Price] = 1), SUMX(UNION(Sales, TREATAS(Sales, Sales[Price])), Sales[Price]))\n\n"
    "\tmeasure TreatFirstA = SUMX(FILTER(Sales, Sales[Price] = 1), SUMX(UNION(TREATAS(Sales, Sales[Price]), Sales), [Price]))\n\n"
    "\tmeasure TreatFirstB = SUMX(FILTER(Sales, Sales[Price] = 1), SUMX(UNION(TREATAS(Sales, Sales[Price]), Sales), Sales[Price]))\n\n"
    "\tmeasure RankxA = SUMX(Other, SUMX(Sales, RANKX(Other, Other[Price], [Price])))\n\n"
    "\tmeasure RankxB = SUMX(Other, SUMX(Sales, RANKX(Other, Other[Price], Other[Price])))\n\n"
    "\tmeasure PercxA = SUMX(Other, SUMX(Sales, PERCENTILEX.INC(Other, Other[Price], [Price] / 100)))\n\n"
    "\tmeasure PercxB = SUMX(Other, SUMX(Sales, PERCENTILEX.INC(Other, Other[Price], Other[Price] / 100)))\n\n"
    "\tmeasure ConcatxA = SUMX(Other, SUMX(Sales, LEN(CONCATENATEX(Other, Other[Price], [Price] & \"\"))))\n\n"
    "\tmeasure ConcatxB = SUMX(Other, SUMX(Sales, LEN(CONCATENATEX(Other, Other[Price], Other[Price] & \"\"))))\n\n"
    "\tmeasure XnpvA = SUMX(Sales, XNPV(Other, [Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvB = SUMX(Sales, XNPV(Other, Sales[Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvTopA = SUMX(Sales, XNPV(TOPN(MAX(Sales[Price]), Other), [Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvTopB = SUMX(Sales, XNPV(TOPN(MAX(Sales[Price]), Other), Sales[Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvNestCtrlA = XNPV(Other, SUMX(Sales, [Price]), DATE(2026, 1, 1), 0)\n\n"
    "\tmeasure XnpvNestCtrlB = XNPV(Other, SUMX(Sales, Sales[Price]), DATE(2026, 1, 1), 0)\n\n"
    "\tmeasure XnpvExcA = VAR v = Other RETURN SUMX(Sales, XNPV(EXCEPT(v, Sales), [Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvExcB = VAR v = Other RETURN SUMX(Sales, XNPV(EXCEPT(v, Sales), Sales[Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvSelA = SUMX(Sales, XNPV(SELECTCOLUMNS(Sales, \"Price\", Sales[Price] + 1), [Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvSelB = SUMX(Sales, XNPV(SELECTCOLUMNS(Sales, \"Price\", Sales[Price] + 1), Sales[Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvRankA = SUMX(Sales, XNPV(Other, RANKX(Sales, Sales[Price], [Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvRankB = SUMX(Sales, XNPV(Other, RANKX(Sales, Sales[Price], Sales[Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvRateCtrlA = SUMX(Sales, XNPV(Other, Other[Price], DATE(2026, 1, 1), [Price]))\n\n"
    "\tmeasure XnpvRateCtrlB = SUMX(Sales, XNPV(Other, Other[Price], DATE(2026, 1, 1), Sales[Price]))\n\n"
    "\tmeasure XnpvDateA = SUMX(Sales, XNPV(Other, Other[Price], [Price], 0))\n\n"
    "\tmeasure XnpvDateB = SUMX(Sales, XNPV(Other, Other[Price], Sales[Price], 0))\n\n"
    "\tmeasure XnpvThirdA = SUMX(Sales, XNPV(Other, SUMX(Dates, [Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvThirdB = SUMX(Sales, XNPV(Other, SUMX(Dates, Sales[Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvUnionA = SUMX(Sales, XNPV(UNION(VALUES(Sales[Price]), VALUES(Sales[Price])), [Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvUnionB = SUMX(Sales, XNPV(UNION(VALUES(Sales[Price]), VALUES(Sales[Price])), Sales[Price], DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvProjA = SUMX(Sales, XNPV(Other, SUMX(SELECTCOLUMNS(Sales, \"K\", 1), [Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure XnpvProjB = SUMX(Sales, XNPV(Other, SUMX(SELECTCOLUMNS(Sales, \"K\", 1), Sales[Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure WrapProjA = SUMX(Sales, XNPV(Other, SUMX(SELECTCOLUMNS(FILTER(Sales, TRUE()), \"K\", 1), [Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure WrapProjB = SUMX(Sales, XNPV(Other, SUMX(SELECTCOLUMNS(FILTER(Sales, TRUE()), \"K\", 1), Sales[Price]), DATE(2026, 1, 1), 0))\n\n"
    "\tmeasure PlainProjA = SUMX(Sales, SUMX(Other, SUMX(SELECTCOLUMNS(Sales, \"K\", 1), [Price])))\n\n"
    "\tmeasure PlainProjB = SUMX(Sales, SUMX(Other, SUMX(SELECTCOLUMNS(Sales, \"K\", 1), Sales[Price])))\n\n"
    "\tmeasure ValuesColA = SUMX(Sales, SUMX(VALUES(Wide[Qty]), [Price]))\n\n"
    "\tmeasure ValuesColB = SUMX(Sales, SUMX(VALUES(Wide[Qty]), Wide[Price]))\n\n"
    "\tmeasure AddColA = SUMX(Sales, SUMX(ADDCOLUMNS(Dates, \"Price\", 1), [Price]))\n\n"
    "\tmeasure AddColB = SUMX(Sales, SUMX(ADDCOLUMNS(Dates, \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure ProjCapA = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, \"Price\", 1), [Price]))\n\n"
    "\tmeasure ProjCapB = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure SumzCapA = SUMX(Sales, SUMX(SUMMARIZE(Dates, Dates[Day], \"Price\", 1), [Price]))\n\n"
    "\tmeasure SumzCapB = SUMX(Sales, SUMX(SUMMARIZE(Dates, Dates[Day], \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure SumzColA = SUMX(Sales, SUMX(SUMMARIZECOLUMNS(Dates[Day], Dates, \"Price\", 1), [Price]))\n\n"
    "\tmeasure SumzColB = SUMX(Sales, SUMX(SUMMARIZECOLUMNS(Dates[Day], Dates, \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure SelOptA = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, Dates[Day], \"Price\", 1), [Price]))\n\n"
    "\tmeasure SelOptB = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, Dates[Day], \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure RollupA = SUMX(Sales, SUMX(SUMMARIZECOLUMNS(Dates[Day], ROLLUPADDISSUBTOTAL(Dates[Day], \"Price\")), [Price] + 0))\n\n"
    "\tmeasure RollupB = SUMX(Sales, SUMX(SUMMARIZECOLUMNS(Dates[Day], ROLLUPADDISSUBTOTAL(Dates[Day], \"Price\")), Sales[Price] + 0))\n\n"
    "\tmeasure SelMidA = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, \"K\", 1, Dates[Day], \"Price\", 1), [Price]))\n\n"
    "\tmeasure SelMidB = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, \"K\", 1, Dates[Day], \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure SelStrA = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, Dates[Day] & \"\", \"Price\", 1), [Price]))\n\n"
    "\tmeasure SelStrB = SUMX(Sales, SUMX(SELECTCOLUMNS(Dates, Dates[Day] & \"\", \"Price\", 1), Sales[Price]))\n\n"
    "\tmeasure ShadowA = SUMX(ADDCOLUMNS(Dates, \"CtrlA\", 1), [CtrlA])\n\n"
    "\tmeasure ShadowB = SUMX(ADDCOLUMNS(Dates, \"CtrlA\", 1), Sales[CtrlA])\n\n"
    "\tmeasure TopnCapA = COUNTROWS(TOPN(1, Sales, IF(Sales[Price] = 10, COUNTROWS(TOPN(1, SELECTCOLUMNS(Sales, \"Price\", Sales[Price] + 1), [Price], DESC)), 1), DESC))\n\n"
    "\tmeasure TopnCapB = COUNTROWS(TOPN(1, Sales, IF(Sales[Price] = 10, COUNTROWS(TOPN(1, SELECTCOLUMNS(Sales, \"Price\", Sales[Price] + 1), Sales[Price], DESC)), 1), DESC))\n\n"
    "\tmeasure XnpvShadowA = XNPV(ADDCOLUMNS(Dates, \"CtrlA\", 1), [CtrlA], DATE(2026, 1, 1), 0)\n\n"
    "\tmeasure XnpvShadowB = XNPV(ADDCOLUMNS(Dates, \"CtrlA\", 1), Sales[CtrlA], DATE(2026, 1, 1), 0)\n\n"
    "\tmeasure RankCapA = RANK(DENSE, ADDCOLUMNS(Sales, \"CtrlA\", 1), ORDERBY([CtrlA]))\n\n"
    "\tmeasure RankCapB = RANK(DENSE, ADDCOLUMNS(Sales, \"CtrlA\", 1), ORDERBY(Sales[CtrlA]))\n\n"

    "\tmeasure ConcatxSortCtrlA = SUMX(Other, SUMX(Sales, LEN(CONCATENATEX(Other, Other[Price], \",\", [Price], ASC))))\n\n"
    "\tmeasure ConcatxSortCtrlB = SUMX(Other, SUMX(Sales, LEN(CONCATENATEX(Other, Other[Price], \",\", Other[Price], ASC))))\n\n"
)
OTHER = (
    "table Other\n\n"
    "\tcolumn Price\n\t\tdataType: int64\n\t\tsourceColumn: Price\n\n"
)
WIDE = (
    "table Wide\n\n"
    "\tcolumn Price\n\t\tdataType: int64\n\t\tsourceColumn: Price\n\n"
    "\tcolumn Qty\n\t\tdataType: int64\n\t\tsourceColumn: Qty\n\n"
)
DATES = (
    "table Dates\n\n"
    "\tcolumn Day\n\t\tdataType: dateTime\n\t\tsourceColumn: Day\n\n"
)
MODEL = "model Model\n\tculture: en-US\n\nref table Sales\nref table Other\nref table Dates\nref table Wide\n"
DATABASE = "database\n\tcompatibilityLevel: 1400\n"


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class SimilarScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = tmp.name
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(root, "private")
        os.makedirs(self.ws.private)
        _write(os.path.join(root, "Mini.pbip"), "{}")
        self.project = common.Project(self.ws, "Mini")
        mdef = os.path.join(self.project.mirror_model, "definition")
        _write(os.path.join(mdef, "tables", "Sales.tmdl"), SALES)
        _write(os.path.join(mdef, "tables", "Other.tmdl"), OTHER)
        _write(os.path.join(mdef, "tables", "Dates.tmdl"), DATES)
        _write(os.path.join(mdef, "tables", "Wide.tmdl"), WIDE)
        _write(os.path.join(mdef, "model.tmdl"), MODEL)
        _write(os.path.join(mdef, "database.tmdl"), DATABASE)
        os.makedirs(self.project.model_dir, exist_ok=True)
        self.model = tmdl.load_model(mdef)
        self.resolver = deps.Resolver(self.model)
        self.assertEqual(set(self.model.tables), {"Sales", "Other", "Dates", "Wide"})
        for table in ("Sales", "Other"):
            self.assertIsNotNone(self.model.column(table, "Price"))
            self.assertIsNone(self.model.column(table, "Nope"))
        self.assertIsNone(self.model.measure("Price"))
        self.assertIsNone(self.model.measure("Nope"))
        self.assertEqual(
            {(m.table, m.name) for m in self.model.all_measures()},
            {("Sales", name) for name in
             ("IterA", "IterB", "IterC", "IterD", "UnresA", "UnresB", "CtrlA", "CtrlB",
              "VarA", "VarB", "AliasUnres", "AliasOk", "WrapA", "WrapB",
              "TabA", "TabB", "TabCtrlA", "TabCtrlB", "TopA", "TopB", "ScalarA", "ScalarB",
              "ExprA", "ExprB", "ChainA", "ChainB", "ValuesA", "ValuesB",
              "DerivedA", "DerivedB", "ScalarQA", "ScalarQB", "CrossA", "CrossB",
              "CtorA", "CtorB", "CountA", "CountB", "ScopeA", "ScopeB", "InlineA", "InlineB",
              "QualA", "QualB", "GenA", "GenB", "TopNCtrlA", "TopNCtrlB",
              "SumzA", "SumzB", "Gen2A", "Gen2B", "NestCtrlA", "NestCtrlB",
              "Gen3A", "Gen3B", "ConsA", "ConsB", "IfCtrlA", "IfCtrlB",
              "CalcA", "CalcB", "AllCountA", "AllCountB", "FiltCountA", "FiltCountB",
              "IfPredCtrlA", "IfPredCtrlB", "ContainsA", "ContainsB",
              "PredCountCtrlA", "PredCountCtrlB", "MaxxCtrlA", "MaxxCtrlB",
              "PredMaxxA", "PredMaxxB", "PredValuesA", "PredValuesB", "PredVarA", "PredVarB",
              "JoinA", "JoinB", "JoinInlA", "JoinInlB",
              "TopnCountCtrlA", "TopnCountCtrlB", "TopnSortCtrlA", "TopnSortCtrlB",
              "TopnBareA", "TopnBareB", "TopnCountCtorCtrlA", "TopnCountCtorCtrlB",
              "TopnCntA", "TopnCntB", "RankBareA", "RankBareB",
              "FailA", "FailB", "ExcA", "ExcB", "MissingA", "MissingB",
              "PoisA", "PoisB", "UnionRowA", "UnionRowB", "WinOptA", "WinOptB",
              "UnionWrapA", "UnionWrapB", "UnionAllA", "UnionAllB", "YearCtrlA", "YearCtrlB",
              "TopnExcA", "TopnExcB", "NestUnionA", "NestUnionB", "SelColA", "SelColB",
              "AllExcA", "AllExcB", "TreatA", "TreatB", "TreatFirstA", "TreatFirstB",
              "RankxA", "RankxB", "PercxA", "PercxB", "ConcatxA", "ConcatxB",
              "XnpvA", "XnpvB", "ConcatxSortCtrlA", "ConcatxSortCtrlB",
              "XnpvTopA", "XnpvTopB", "XnpvNestCtrlA", "XnpvNestCtrlB",
              "XnpvExcA", "XnpvExcB", "XnpvSelA", "XnpvSelB", "XnpvRankA", "XnpvRankB",
              "XnpvRateCtrlA", "XnpvRateCtrlB", "XnpvDateA", "XnpvDateB",
              "XnpvThirdA", "XnpvThirdB", "XnpvUnionA", "XnpvUnionB",
              "XnpvProjA", "XnpvProjB", "WrapProjA", "WrapProjB",
              "PlainProjA", "PlainProjB", "ValuesColA", "ValuesColB",
              "AddColA", "AddColB", "ProjCapA", "ProjCapB",
              "SumzCapA", "SumzCapB", "SumzColA", "SumzColB",
              "SelOptA", "SelOptB", "RollupA", "RollupB", "SelMidA", "SelMidB",
              "SelStrA", "SelStrB", "ShadowA", "ShadowB", "TopnCapA", "TopnCapB",
              "XnpvShadowA", "XnpvShadowB", "RankCapA", "RankCapB")},
        )

    def _canon(self, name):
        measure = self.model.measure(name)
        self.assertIsNotNone(measure)
        return similar.canon(measure.expression, "Sales", self.resolver)

    def _key(self, name):
        return self._canon(name).words("hash")

    def test_nested_iterator_occurrences_get_different_keys(self):
        key_a, key_b = self._key("IterA"), self._key("IterB")
        self.assertNotEqual(key_a, key_b)
        self.assertIn("other[price]", key_a)
        self.assertIn("sales[price]", key_b)
        self.assertEqual(
            key_a,
            similar.canon("SUMX(Sales, SUMX(Other, Other[Price]))",
                          "Sales", self.resolver).words("hash"),
        )

    def test_each_occurrence_binds_its_own_row_context(self):
        key_c, key_d = self._key("IterC"), self._key("IterD")
        self.assertNotEqual(key_c, key_d)
        self.assertIn("sales[price]", key_c)
        self.assertIn("other[price]", key_c)
        self.assertEqual(
            key_c,
            similar.canon("SUMX(Sales, Sales[Price] + SUMX(Other, Other[Price]))",
                          "Sales", self.resolver).words("hash"),
        )

    def test_merge_preview_refuses_scoped_pair_without_force(self):
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "IterB", "IterA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unresolved_occurrence_blocks_automatic_merge(self):
        self.assertEqual(self._key("UnresA"), self._key("UnresB"))
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "UnresB", "UnresA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_identical_control_still_previews_identical(self):
        result = consolidate.merge(self.ws, self.project, "CtrlB", "CtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")
        self.assertEqual(result["result"], "DRY_RUN")

    def test_var_table_iterator_occurrence_is_uncertain(self):
        self.assertEqual(self._key("VarA"), self._key("VarB"))
        self.assertTrue(self._canon("VarA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "VarB", "VarA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_uncertain_alias_is_refused(self):
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "AliasUnres", "CtrlA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_resolved_alias_still_previews_alias(self):
        result = consolidate.merge(self.ws, self.project, "AliasOk", "CtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "alias")
        self.assertEqual(result["result"], "DRY_RUN")

    def test_wrapped_var_source_stays_uncertain(self):
        self.assertEqual(self._key("WrapA"), self._key("WrapB"))
        self.assertTrue(self._canon("WrapA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "WrapB", "WrapA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unresolved_table_token_blocks_identical_merge(self):
        self.assertEqual(self._key("TabA"), self._key("TabB"))
        self.assertTrue(self._canon("TabA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "TabB", "TabA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")
        result = consolidate.merge(self.ws, self.project, "TabCtrlB", "TabCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_table_var_after_reference_still_poisons(self):
        self.assertEqual(self._key("TopA"), self._key("TopB"))
        self.assertTrue(self._canon("TopA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "TopB", "TopA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_expression_defined_table_var_still_poisons(self):
        for dup, into in (("ExprB", "ExprA"), ("ChainB", "ChainA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_column_fed_and_derived_table_vars_still_poison(self):
        for dup, into in (("ValuesB", "ValuesA"), ("DerivedB", "DerivedA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unlisted_table_forms_still_poison(self):
        for dup, into in (("CrossB", "CrossA"), ("CtorB", "CtorA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_sibling_scope_and_inline_source_still_poison(self):
        for dup, into in (("ScopeB", "ScopeA"), ("InlineB", "InlineA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_qualified_and_generated_sources_still_poison(self):
        for dup, into in (("QualB", "QualA"), ("GenB", "GenA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_projected_and_generated_sources_still_poison(self):
        for dup, into in (("SumzB", "SumzA"), ("Gen2B", "Gen2A")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_scalar_bound_and_constructor_sources_still_poison(self):
        for dup, into in (("Gen3B", "Gen3A"), ("ConsB", "ConsA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_silent_source_and_scalar_wrapped_teachings_still_poison(self):
        for dup, into in (("CalcB", "CalcA"), ("AllCountB", "AllCountA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")
        self.assertTrue(self._canon("FiltCountA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "FiltCountB", "FiltCountA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_natural_join_second_table_still_poisons(self):
        for dup, into in (("JoinB", "JoinA"), ("JoinInlB", "JoinInlA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_xnpv_rate_argument_stays_certain(self):
        self.assertFalse(self._canon("XnpvRateCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "XnpvRateCtrlB", "XnpvRateCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_xnpv_nested_iterator_stays_certain(self):
        self.assertFalse(self._canon("XnpvNestCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "XnpvNestCtrlB", "XnpvNestCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_concatx_sort_argument_stays_certain(self):
        self.assertFalse(self._canon("ConcatxSortCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "ConcatxSortCtrlB", "ConcatxSortCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_interval_keyword_argument_stays_certain(self):
        self.assertFalse(self._canon("YearCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "YearCtrlB", "YearCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_topn_scalar_arguments_stay_certain(self):
        for name, dup in (("TopnCountCtrlA", "TopnCountCtrlB"), ("TopnSortCtrlA", "TopnSortCtrlB"),
                          ("TopnCountCtorCtrlA", "TopnCountCtorCtrlB")):
            with self.subTest(name=name):
                self.assertFalse(self._canon(name).uncertain)
                result = consolidate.merge(self.ws, self.project, dup, name, dry_run=True)
                self.assertEqual(result["equivalence"], "identical")

    def test_bare_column_in_topn_sort_is_uncertain(self):
        self.assertEqual(self._key("TopnBareA"), self._key("TopnBareB"))
        self.assertTrue(self._canon("TopnBareA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "TopnBareB", "TopnBareA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_count_taught_and_clause_brackets_are_uncertain(self):
        for dup, into in (("TopnCntB", "TopnCntA"), ("RankBareB", "RankBareA"),
                          ("FailB", "FailA"), ("ExcB", "ExcA"), ("MissingB", "MissingA"),
                          ("PoisB", "PoisA"), ("UnionRowB", "UnionRowA"), ("WinOptB", "WinOptA"),
                          ("UnionWrapB", "UnionWrapA"), ("UnionAllB", "UnionAllA"),
                          ("TopnExcB", "TopnExcA"), ("NestUnionB", "NestUnionA"),
                          ("SelColB", "SelColA"), ("AllExcB", "AllExcA"),
                          ("TreatB", "TreatA"), ("TreatFirstB", "TreatFirstA"),
                          ("RankxB", "RankxA"), ("PercxB", "PercxA"), ("ConcatxB", "ConcatxA"),
                          ("XnpvB", "XnpvA"), ("XnpvTopB", "XnpvTopA"),
                          ("XnpvExcB", "XnpvExcA"), ("XnpvSelB", "XnpvSelA"),
                          ("XnpvRankB", "XnpvRankA"), ("XnpvDateB", "XnpvDateA"),
                          ("XnpvThirdB", "XnpvThirdA"), ("XnpvUnionB", "XnpvUnionA"),
                          ("XnpvProjB", "XnpvProjA"), ("WrapProjB", "WrapProjA"),
                          ("PlainProjB", "PlainProjA"), ("ValuesColB", "ValuesColA"),
                          ("AddColB", "AddColA"), ("ProjCapB", "ProjCapA"),
                          ("SumzCapB", "SumzCapA"), ("SumzColB", "SumzColA"),
                          ("SelOptB", "SelOptA"), ("RollupB", "RollupA"),
                          ("SelMidB", "SelMidA"), ("SelStrB", "SelStrA"),
                          ("ShadowB", "ShadowA"), ("TopnCapB", "TopnCapA"),
                          ("XnpvShadowB", "XnpvShadowA"), ("RankCapB", "RankCapA")):
            with self.subTest(into=into):
                self.assertEqual(self._key(dup), self._key(into))
                self.assertTrue(self._canon(into).uncertain)
                with self.assertRaises(PbzError) as caught:
                    consolidate.merge(self.ws, self.project, dup, into, dry_run=True)
                self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_unlisted_scalar_wrapper_still_poisons(self):
        self.assertEqual(self._key("ContainsA"), self._key("ContainsB"))
        self.assertTrue(self._canon("ContainsA").uncertain)
        with self.assertRaises(PbzError) as caught:
            consolidate.merge(self.ws, self.project, "ContainsB", "ContainsA", dry_run=True)
        self.assertEqual(caught.exception.code, "NOT_EQUIVALENT")

    def test_predicate_table_calls_stay_certain(self):
        for name, dup in (("PredCountCtrlA", "PredCountCtrlB"), ("MaxxCtrlA", "MaxxCtrlB"),
                          ("PredMaxxA", "PredMaxxB"), ("PredValuesA", "PredValuesB"),
                          ("PredVarA", "PredVarB")):
            with self.subTest(name=name):
                self.assertFalse(self._canon(name).uncertain)
                result = consolidate.merge(self.ws, self.project, dup, name, dry_run=True)
                self.assertEqual(result["equivalence"], "identical")

    def test_conditional_predicate_with_lookup_stays_certain(self):
        self.assertFalse(self._canon("IfPredCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "IfPredCtrlB", "IfPredCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_conditional_predicate_source_stays_certain(self):
        self.assertFalse(self._canon("IfCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "IfCtrlB", "IfCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_nested_predicate_source_stays_certain(self):
        self.assertFalse(self._canon("NestCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "NestCtrlB", "NestCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_filtered_same_table_source_stays_certain(self):
        self.assertFalse(self._canon("TopNCtrlA").uncertain)
        result = consolidate.merge(self.ws, self.project, "TopNCtrlB", "TopNCtrlA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_table_consuming_scalar_var_does_not_poison(self):
        self.assertFalse(self._canon("CountA").uncertain)
        result = consolidate.merge(self.ws, self.project, "CountB", "CountA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_quoted_qualified_scalar_var_does_not_poison(self):
        self.assertFalse(self._canon("ScalarQA").uncertain)
        result = consolidate.merge(self.ws, self.project, "ScalarQB", "ScalarQA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_scalar_var_does_not_block_learned_row_table(self):
        self.assertFalse(self._canon("ScalarA").uncertain)
        result = consolidate.merge(self.ws, self.project, "ScalarB", "ScalarA", dry_run=True)
        self.assertEqual(result["equivalence"], "identical")

    def test_similar_run_does_not_group_scoped_pair(self):
        groups = similar.run(self.ws, self.project)["identical"]
        self.assertTrue(any({"[CtrlA]", "[CtrlB]"} <= set(group["measures"])
                            for group in groups))
        for group in groups:
            self.assertFalse({"[IterA]", "[IterB]"} <= set(group["measures"]))


if __name__ == "__main__":
    unittest.main()
