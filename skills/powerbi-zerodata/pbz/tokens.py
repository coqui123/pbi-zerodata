"""Reversible tokenization of sensitive literals in TMDL and PBIR text.

Tokens look like «Project Info[Owner]#4» (column-bound), «lit#3», «pii#1», «num#2» or
«payload:Table#1». The token map (DPAPI-protected, private store) records, per token, the exact
raw text it replaced in each context, so detokenize(tokenize(x)) == x byte for byte.
"""
from __future__ import annotations

import json
import os
import re

from . import dpapi, lexers, pbir, tmdl
from .common import PbzError, sha

TOKEN_RE = re.compile(r"«[^«»\r\n]{1,300}»")
FULL_JSON_TOKEN_RE = re.compile(r'"(«[^«»"\r\n]{1,300}»)"')
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]{200,}$")
# Embedded files (images in themes, buttons, image URLs; base64 blobs inside longer strings) are opaque
# payloads: an image can show data, and its bytes are useless to read. A whole string that is a data: URI
# becomes one token; a base64 data: URI inside a longer string (HTML, a DAX SVG template) is cut out.
DATA_URI_RE = re.compile(r"(?i)^\s*data:(?:[a-z0-9.+-]+/[a-z0-9.+-]+)?(?:;[a-z0-9=.+_-]+)*,")  # not "Data: Q1"
# A token written back by a JSON tool that escapes non-ASCII (\u00ab ... \u00bb): still a token.
ESC_TOKEN_RE = re.compile(r"\\u00[aA][bB]((?:[^\"\\\r\n]|\\u[0-9a-fA-F]{4}){1,300}?#\d{1,7})\\u00[bB][bB]")
EMBEDDED_B64_URI_RE = re.compile(r"(?i)data:[a-z0-9.+/-]*(?:;[a-z0-9.+=_-]+)*;base64,[A-Za-z0-9+/=]{16,}")

COLUMN_VALUE_FUNCS = {"SELECTEDVALUE", "VALUES", "DISTINCT", "MAX", "MIN", "MAXA", "MINA", "FIRSTNONBLANK",
                      "LASTNONBLANK", "ALLSELECTED", "FILTERS"}
CMP_OPS = {"=", "==", "<>", "<", ">", "<=", ">="}
SUBSTRING_FUNCS = {"CONTAINSSTRING", "CONTAINSSTRINGEXACT", "SEARCH", "FIND", "TEXT.CONTAINS",
                   "TEXT.STARTSWITH", "TEXT.ENDSWITH", "STARTSWITH", "ENDSWITH"}
STRUCTURAL_KEYS = {"$schema", "Entity", "Property", "Name", "Source", "visualType", "name", "id", "queryRef",
                   "nativeQueryRef", "Hierarchy", "Level", "type", "pageBinding", "parent", "activeSectionIndex",
                   "objectName", "dataRole", "role", "fieldType", "howCreated", "layoutOptimization",
                   "displayOption", "version", "versionMetadata", "path", "url", "resourcePackages", "items",
                   "themeCollection", "baseTheme", "reportVersionAtImport", "pageOrder", "activePageName"}


# ---------------------------------------------------------------- token map

class TokenMap:
    def __init__(self, private_dir: str):
        self.path = os.path.join(private_dir, "tokens.bin")
        raw = dpapi.read_protected(self.path)
        self.data = json.loads(raw.decode("utf-8")) if raw else {"version": 1, "next": {}, "tokens": {}, "keys": {}}
        self.dirty = False

    def save(self) -> None:
        if self.dirty:
            dpapi.write_protected(self.path, json.dumps(self.data, ensure_ascii=False).encode("utf-8"))
            self.dirty = False

    def _new(self, label: str, key: str, rec: dict) -> str:
        existing = self.data["keys"].get(key)
        if existing:
            return existing
        n = self.data["next"].get(label, 0) + 1
        self.data["next"][label] = n
        tok = f"«{label}#{n}»"
        rec["label"] = label
        self.data["tokens"][tok] = rec
        self.data["keys"][key] = tok
        self.dirty = True
        return tok

    def lit(self, label: str, value, vtype: str, ctx: str, raw: str) -> str:
        # exact value, not the case-folded HMAC form: spelling variants keep their own raw text
        key = f"lit\0{label}\0{vtype}\0{json.dumps(value, ensure_ascii=False)}"
        tok = self._new(label, key, {"kind": "lit", "value": value, "vtype": vtype, "raws": {}})
        raws = self.data["tokens"][tok].setdefault("raws", {})
        if ctx not in raws:
            raws[ctx] = raw
            self.dirty = True
        return tok

    def raw(self, label: str, raw: str, ctx: str, value: str | None = None) -> str:
        key = f"raw\0{label}\0{sha(raw)}\0{ctx}"
        tok = self._new(label, key, {"kind": "raw", "value": value if value is not None else raw, "raws": {}})
        raws = self.data["tokens"][tok].setdefault("raws", {})
        if ctx not in raws:
            raws[ctx] = raw
            self.dirty = True
        return tok

    def get(self, tok: str) -> dict | None:
        return self.data["tokens"].get(tok)

    def known(self) -> set[str]:
        return set(self.data["tokens"])

    def find_by_hmac(self, key: bytes, label: str, digest: str) -> str | None:
        for tok, rec in self.data["tokens"].items():
            if rec.get("label") == label and rec.get("kind") == "lit" and dpapi.hmac_hex(key, rec.get("value")) == digest:
                return tok
        return None

    def opaque(self, label: str, digest: str) -> str:
        """Token for a group key known only by HMAC (from engine output)."""
        return self._new(label + "@g", f"hmac\0{label}\0{digest}", {"kind": "hmac", "hmac": digest, "column": label, "raws": {}})


def data_uri_label(uri: str) -> str:
    return "payload:image" if uri.strip()[5:11].lower() == "image/" else "payload:data"


def _overlaps(s: int, e: int, spans: list[tuple[int, int, str]]) -> bool:
    return any(s < ue and e > us for us, ue, _r in spans)


def apply_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    out, pos = [], 0
    for s, e, rep in sorted(edits, key=lambda x: x[0]):
        if s < pos:
            continue  # overlapping edit: keep the first
        out.append(text[pos:s])
        out.append(rep)
        pos = e
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------- DAX / M binding

def _match_close(sig, i, open_="(", close=")"):
    depth = 0
    for j in range(i, len(sig)):
        if sig[j].kind == "op" and sig[j].text == open_:
            depth += 1
        elif sig[j].kind == "op" and sig[j].text == close:
            depth -= 1
            if depth == 0:
                return j
    return len(sig) - 1


def _match_open(sig, j, open_="(", close=")"):
    depth = 0
    for i in range(j, -1, -1):
        if sig[i].kind == "op" and sig[i].text == close:
            depth += 1
        elif sig[i].kind == "op" and sig[i].text == open_:
            depth -= 1
            if depth == 0:
                return i
    return 0


def _split_args(sig, open_idx, close_idx):
    """Top-level argument index ranges inside a call's parentheses."""
    args, depth, start = [], 0, open_idx + 1
    for k in range(open_idx + 1, close_idx):
        t = sig[k]
        if t.kind == "op" and t.text in ("(", "{", "["):
            depth += 1
        elif t.kind == "op" and t.text in (")", "}", "]"):
            depth -= 1
        elif t.kind == "op" and t.text == "," and depth == 0:
            args.append((start, k))
            start = k + 1
    args.append((start, close_idx))
    return [(a, b) for a, b in args if a < b]


def bind_literals(sig: list, lang: str, cur_table: str | None, model) -> dict:
    """Map index in sig -> (table, column) or 'skip' (column names in DATATABLE / #table headers)."""
    binding: dict[int, object] = {}
    colref: dict[int, tuple] = {}   # index of the token that *ends* a column ref -> (table, col)
    start_of: dict[int, int] = {}   # end index -> start index of that ref
    tables = model.tables if model else {}

    def is_col(table, col):
        return bool(model and model.column(table, col))

    for i, t in enumerate(sig):
        if t.kind != "bracket":
            continue
        prev = sig[i - 1] if i > 0 else None
        if prev is not None and prev.end == t.start and prev.kind in ("table", "ident") and lang == "dax":
            colref[i] = (prev.value, t.value)
            start_of[i] = i - 1
        elif cur_table and is_col(cur_table, t.value):
            colref[i] = (cur_table, t.value)
            start_of[i] = i
        elif lang == "m" and cur_table:
            colref[i] = (cur_table, t.value)
            start_of[i] = i

    var_col: dict[str, tuple] = {}

    def operand_col(j):
        """Column constrained by the operand ending at index j."""
        if j < 0:
            return None
        if j in colref:
            return colref[j]
        t = sig[j]
        if t.kind == "op" and t.text == ")":
            o = _match_open(sig, j)
            if o > 0 and sig[o - 1].kind == "ident" and sig[o - 1].text.upper() in COLUMN_VALUE_FUNCS:
                for k in range(o + 1, j):
                    if k in colref:
                        return colref[k]
            return None
        if t.kind == "ident" and t.text in var_col:
            return var_col[t.text]
        return None

    def bind(k, col):
        if col and k not in binding:
            binding[k] = col

    lits = {i for i, t in enumerate(sig) if t.kind in ("str", "num")}

    for i, t in enumerate(sig):
        up = t.text.upper() if t.kind == "ident" else ""
        # VAR x = SELECTEDVALUE('T'[C])  /  VAR x = 'T'[C]
        if up == "VAR" and i + 2 < len(sig) and sig[i + 1].kind == "ident" and sig[i + 2].text == "=":
            k = i + 3
            if k < len(sig) and sig[k].kind == "ident" and sig[k].text.upper() in COLUMN_VALUE_FUNCS \
                    and k + 1 < len(sig) and sig[k + 1].text == "(":
                close = _match_close(sig, k + 1)
                for m in range(k + 2, close):
                    if m in colref:
                        var_col[sig[i + 1].text] = colref[m]
                        break
            else:
                for m in range(k, min(k + 3, len(sig))):
                    if m in colref:
                        var_col[sig[i + 1].text] = colref[m]
                        break
        # DATATABLE("A", STRING, "B", INTEGER, {{...}})
        if up == "DATATABLE" and i + 1 < len(sig) and sig[i + 1].text == "(":
            close = _match_close(sig, i + 1)
            names, k = [], i + 2
            while k < close and sig[k].text != "{":
                if sig[k].kind == "str":
                    names.append(sig[k].value)
                    binding[k] = "skip"
                k += 1
            row, pos = 0, 0
            depth = 0
            for m in range(k, close):
                tm_ = sig[m]
                if tm_.text == "{":
                    depth += 1
                    if depth == 2:
                        pos = 0
                elif tm_.text == "}":
                    depth -= 1
                elif tm_.text == "," and depth == 2:
                    pos += 1
                elif m in lits and depth >= 2 and cur_table and pos < len(names):
                    bind(m, (cur_table, names[pos], "row"))
        # #table(type table [A = text], {...}) or #table({"A","B"}, {...})
        if lang == "m" and t.kind == "op" and t.text == "#" and i + 2 < len(sig) and sig[i + 1].text == "table" \
                and sig[i + 2].text == "(":
            close = _match_close(sig, i + 2)
            args = _split_args(sig, i + 2, close)
            names = []
            if args:
                a0, b0 = args[0]
                for m in range(a0, b0):
                    if sig[m].kind == "str":
                        names.append(sig[m].value)
                        binding[m] = "skip"
                    elif sig[m].kind == "bracket" and sig[m].value:
                        names.append(sig[m].value.split("=")[0].strip())
                    elif sig[m].kind == "ident" and m + 1 < b0 and sig[m + 1].text == "=" and sig[m - 1].text in ("[", ","):
                        names.append(sig[m].text)
            if len(args) > 1:
                a1, b1 = args[1]
                depth, pos = 0, 0
                for m in range(a1, b1):
                    tm_ = sig[m]
                    if tm_.text == "{":
                        depth += 1
                        if depth == 2:
                            pos = 0
                    elif tm_.text == "}":
                        depth -= 1
                    elif tm_.text == "," and depth == 2:
                        pos += 1
                    elif m in lits and depth >= 2 and cur_table and pos < len(names):
                        bind(m, (cur_table, names[pos], "row"))
        # IN { ... }
        if up == "IN" and i + 1 < len(sig) and sig[i + 1].text == "{":
            col = operand_col(i - 1)
            close = _match_close(sig, i + 1, "{", "}")
            for m in range(i + 2, close):
                if m in lits:
                    bind(m, col)
        # TREATAS({...}, 'T'[C], ...)
        if up == "TREATAS" and i + 2 < len(sig) and sig[i + 1].text == "(" and sig[i + 2].text == "{":
            close = _match_close(sig, i + 1)
            bclose = _match_close(sig, i + 2, "{", "}")
            cols = [colref[m] for m in range(bclose, close) if m in colref]
            if cols:
                depth, pos = 0, 0
                for m in range(i + 2, bclose):
                    tm_ = sig[m]
                    if tm_.text == "(":
                        depth += 1
                        pos = 0
                    elif tm_.text == ")":
                        depth -= 1
                    elif tm_.text == "," and depth == 1:
                        pos += 1
                    elif m in lits:
                        bind(m, cols[min(pos, len(cols) - 1)] if depth >= 1 else cols[0])
        # List.Contains({...}, [C])
        if lang == "m" and up in ("LIST.CONTAINS", "LIST.CONTAINSANY") and i + 2 < len(sig) and sig[i + 1].text == "(":
            close = _match_close(sig, i + 1)
            cols = [colref[m] for m in range(i + 2, close) if m in colref]
            if cols:
                for m in range(i + 2, close):
                    if m in lits:
                        bind(m, cols[0])
        # CONTAINSSTRING([C], "x"), SEARCH("x", [C]), Text.Contains([C], "x")
        if up in SUBSTRING_FUNCS and i + 1 < len(sig) and sig[i + 1].text == "(":
            close = _match_close(sig, i + 1)
            cols = [colref[m] for m in range(i + 2, close) if m in colref]
            if cols:
                for m in range(i + 2, close):
                    if m in lits:
                        bind(m, cols[0])
        # LOOKUPVALUE(result, search1, value1, ...)
        if up == "LOOKUPVALUE" and i + 1 < len(sig) and sig[i + 1].text == "(":
            close = _match_close(sig, i + 1)
            args = _split_args(sig, i + 1, close)
            for a in range(1, len(args) - 1, 2):
                sa, sb = args[a]
                va, vb = args[a + 1]
                col = next((colref[m] for m in range(sa, sb) if m in colref), None)
                for m in range(va, vb):
                    if m in lits:
                        bind(m, col)
        # SWITCH('T'[C], "a", ..., "b", ...)
        if up == "SWITCH" and i + 1 < len(sig) and sig[i + 1].text == "(":
            close = _match_close(sig, i + 1)
            args = _split_args(sig, i + 1, close)
            if args:
                a0, b0 = args[0]
                col = operand_col(b0 - 1)
                for a in range(1, len(args) - 1, 2):
                    va, vb = args[a]
                    if vb - va == 1 and va in lits:
                        bind(va, col)

    # comparisons: col op lit  /  lit op col
    for k in lits:
        if k in binding:
            continue
        p = k - 1
        if p >= 0 and sig[p].kind == "op" and sig[p].text == "-" and p - 1 >= 0 and sig[p - 1].kind == "op":
            p -= 1
        if p >= 0 and sig[p].kind == "op" and sig[p].text in CMP_OPS:
            col = operand_col(p - 1)
            if col:
                bind(k, col)
                continue
        nx = k + 1
        if nx + 1 < len(sig) and sig[nx].kind == "op" and sig[nx].text in CMP_OPS:
            j = nx + 1
            end = next((e for e, s in start_of.items() if s == j), None)
            if end is not None:
                bind(k, colref[end])
    return binding


# ---------------------------------------------------------------- TMDL

def _table_of(node) -> str | None:
    n = node
    while n is not None:
        if n.kind in ("table", "tablePermission"):  # an RLS filter's row context is the table it filters
            return n.name
        n = n.parent
    return None


def _dq_map(inner: str) -> tuple[str, list[int]]:
    """Decode doubled double-quotes inside a DAX/M string body; positions map decoded -> raw."""
    out, pos, i = [], [], 0
    while i < len(inner):
        out.append(inner[i])
        pos.append(i)
        i += 2 if inner[i] == '"' and i + 1 < len(inner) and inner[i + 1] == '"' else 1
    pos.append(len(inner))
    return "".join(out), pos


def _num_value(text: str):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


TRANSLATED_PROPS = ("caption", "description", "displayFolder")
# Colon properties whose values are types, refs, enums or ids. Anything else that looks like free
# text is tokenized whole: heuristic free-text screening must not leave an unknown property visible.
STRUCTURAL_COLON_PROPS = TRANSLATED_PROPS + (
    "dataType", "formatString", "displayFolder", "sourceColumn", "sortByColumn", "dataCategory",
    "summarizeBy", "lineageTag", "sourceLineageTag", "alignment", "isAvailableInMdx", "isHidden",
    "isKey", "isUnique", "isNullable", "fromColumn", "toColumn", "crossFilteringBehavior",
    "joinOnDateBehavior", "fromCardinality", "toCardinality", "securityFilteringBehavior",
    "relyOnReferentialIntegrity", "isActive", "mode", "column", "ordinal", "compatibilityLevel",
    "culture", "defaultMode", "discourageImplicitMeasures", "queryGroup", "permission",
    "modelPermission", "metadataPermission", "relationship", "modifiedTime", "state",
    "defaultPowerBIDataSourceVersion", "sourceQueryCulture", "groupByColumn",
)
_ENUM_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_NUM_VALUE = re.compile(r"^-?\d+(?:\.\d+)?$")
_GUID_VALUE = re.compile(r"^[0-9a-fA-F-]{32,}$")
# report / page / visualContainer schemas this build knows how to screen. Others are fail-closed.
# Full URI prefix is required so a spoofed host with the same suffix cannot pass.
PBIR_SCHEMA_PREFIX = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
PBIR_SCHEMA_RE = re.compile(
    r"^https://developer\.microsoft\.com/json-schemas/fabric/item/report/definition/"
    r"(report|page|visualContainer)/(\d+\.\d+\.\d+)/schema\.json$")
KNOWN_PBIR_SCHEMA = {
    "report": frozenset({"3.3.0"}),
    "page": frozenset({"2.1.0"}),
    "visualContainer": frozenset({"2.4.0", "2.10.0"}),
}


def _literal_bearing(val: str) -> bool:
    """Free text or a quoted literal, not an enum token, number or guid."""
    v = val.strip()
    if len(v) < 3 or v.lower() in ("true", "false", "none", "null"):
        return False
    if _NUM_VALUE.match(v) or _GUID_VALUE.match(v) or _ENUM_VALUE.match(v):
        return False
    return True


def _pbir_schema_known(relpath: str, root) -> bool | None:
    """True/False for report.json, page.json and visual.json; None for every other file."""
    name = relpath.replace("\\", "/").rsplit("/", 1)[-1].lower()
    kind = {"report.json": "report", "page.json": "page", "visual.json": "visualContainer"}.get(name)
    if kind is None or not isinstance(root, pbir.JObj):
        return None
    node = root.get("$schema")
    sch = node.value.strip() if isinstance(node, pbir.JStr) else ""
    m = PBIR_SCHEMA_RE.fullmatch(sch)
    return bool(m and m.group(1) == kind and m.group(2) in KNOWN_PBIR_SCHEMA[kind])


def tokenize_tmdl(text: str, relpath: str, model, clf, tm: TokenMap) -> str:
    doc = tmdl.parse(relpath, text)
    edits: list[tuple[int, int, str]] = []
    if relpath.lower().startswith("cultures/"):
        # Q&A linguistic schema (synonyms, phrasings, examples) can be data-derived: one opaque payload.
        # Translated captions, descriptions and folders are names, still screened as free text.
        lines = doc.text.splitlines(keepends=True)
        pos = 0
        for raw_line in lines:
            line = raw_line.rstrip("\r\n")
            m = tmdl.PROP_RE.match(line)
            if m and m.group("prop") in TRANSLATED_PROPS:
                vs = pos + m.start("val")
                val = m.group("val")
                for s, e, lab in clf.free_text(val, numbers=m.group("prop") == "description"):
                    edits.append((vs + s, vs + e, tm.raw(lab, val[s:e], "tmdl")))
            pos += len(raw_line)
    for reg in doc.regions:
        seg = text[reg.start:reg.end]
        if reg.node.kind == "linguisticMetadata":
            lead = len(seg) - len(seg.lstrip(" \t"))
            if seg[lead:].strip():
                edits.append((reg.start + lead, reg.end, tm.raw("payload:linguisticMetadata", seg[lead:], "tmdl")))
            continue
        if reg.kind in ("dax", "m"):
            toks = (lexers.lex_dax if reg.kind == "dax" else lexers.lex_m)(seg, reg.start)
            sig = lexers.significant(toks)
            cur = _table_of(reg.node)
            binding = bind_literals(sig, reg.kind, cur, model)
            for k, t in enumerate(sig):
                b = binding.get(k)
                if b == "skip":
                    continue
                col = b[:2] if isinstance(b, tuple) else None
                in_row = isinstance(b, tuple) and len(b) == 3  # embedded table rows are data
                # Embedded cells are data, even when their magnitude resembles a flag or ordinal.
                if t.kind == "num" and in_row:
                    lab = clf.label(*col)
                    edits.append((t.start, t.end, tm.lit(lab, _num_value(t.text), "number", "tmdl", t.text)))
                    continue
                if t.kind == "str":
                    inner = t.text[1:-1] if t.text.endswith('"') and len(t.text) >= 2 else t.text[1:]
                    if reg.kind == "m" and BASE64_RE.match(t.value or ""):
                        edits.append((t.start + 1, t.start + 1 + len(inner),
                                      tm.raw(f"payload:{cur or 'blob'}", inner, "tmdl")))
                        continue
                    label = clf.literal(t.value, "string", col)
                    if not label and in_row and clf.mode == "heuristic" and clf.column_class(*col) == "unknown" \
                            and clf._specific(t.value):
                        label = clf.label(*col)  # embedded row under a header the model does not know: fail closed
                    if label:
                        edits.append((t.start + 1, t.start + 1 + len(inner),
                                      tm.lit(label, t.value, "string", "tmdl", inner)))
                    else:
                        decoded, pos = _dq_map(inner)
                        # an embedded image (a logo inside an SVG measure) is cut out; the template stays readable
                        uris = [(t.start + 1 + pos[m.start()], t.start + 1 + pos[m.end()],
                                 tm.raw(data_uri_label(m.group(0)), inner[pos[m.start()]:pos[m.end()]], "tmdl"))
                                for m in EMBEDDED_B64_URI_RE.finditer(decoded)]
                        edits += uris
                        for s, e, lab in clf.free_text(decoded):
                            rs, re_ = pos[s], pos[e]
                            if not _overlaps(t.start + 1 + rs, t.start + 1 + re_, uris):
                                edits.append((t.start + 1 + rs, t.start + 1 + re_, tm.raw(lab, inner[rs:re_], "tmdl")))
                elif t.kind == "num" and col:
                    label = clf.literal(_num_value(t.text), "number", col)
                    if label:
                        edits.append((t.start, t.end, tm.lit(label, _num_value(t.text), "number", "tmdl", t.text)))
            for t in toks:
                if t.kind == "comment":
                    for s, e, lab in clf.free_text(t.text, numbers=True):
                        edits.append((t.start + s, t.start + e, tm.raw(lab, t.text[s:e], "tmdl")))
        elif reg.kind == "text":
            for s, e, lab in clf.free_text(seg, numbers=reg.prop == "description"):
                edits.append((reg.start + s, reg.start + e, tm.raw(lab, seg[s:e], "tmdl")))
        elif reg.kind == "member":
            edits.append((reg.start, reg.end, tm.raw("pii", seg, "tmdl")))
    # Unknown `prop: value` lines are not expression regions. A heuristic miss must not leave them visible.
    pos = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        pm = tmdl.PROP_RE.match(line)
        # Unknown colon props: fail closed — tokenize any non-empty value (no short/word/number bypass).
        # Schema enums and refs stay in STRUCTURAL_COLON_PROPS so the mirror remains valid TMDL.
        if pm and pm.group("prop") not in STRUCTURAL_COLON_PROPS and pm.group("val").strip():
            vs = pos + pm.start("val")
            ve = vs + len(pm.group("val"))
            if not _overlaps(vs, ve, edits):
                edits.append((vs, ve, tm.raw("lit", pm.group("val"), "tmdl")))
        # Captions, descriptions and folders are names, but a phone or email inside them is not.
        elif pm and pm.group("prop") in TRANSLATED_PROPS and pm.group("val"):
            vs = pos + pm.start("val")
            val = pm.group("val")
            for s, e, lab in clf.free_text(val, numbers=pm.group("prop") == "description"):
                if not _overlaps(vs + s, vs + e, edits):
                    edits.append((vs + s, vs + e, tm.raw(lab, val[s:e], "tmdl")))
        pos += len(raw_line)
    return apply_edits(text, edits)


def detokenize_tmdl(text: str, tm: TokenMap) -> tuple[str, list[str]]:
    unknown: list[str] = []

    def repl(m):
        rec = tm.get(m.group(0))
        if not rec:
            unknown.append(m.group(0))
            return m.group(0)
        raw = rec.get("raws", {}).get("tmdl")
        if raw is not None:
            return raw
        if rec.get("kind") == "hmac":
            unknown.append(m.group(0))
            return m.group(0)
        v = rec.get("value")
        if rec.get("vtype") in ("number", "int", "double"):
            return str(v)
        return str(v).replace('"', '""')

    return TOKEN_RE.sub(repl, text), unknown


# ---------------------------------------------------------------- PBIR

def _decode_map(raw_inner: str) -> tuple[str, list[int]]:
    """Decode the inside of a JSON string; positions[k] = raw offset of decoded char k."""
    out, pos = [], []
    i, n = 0, len(raw_inner)
    while i < n:
        c = raw_inner[i]
        if c == "\\" and i + 1 < n:
            e = raw_inner[i + 1]
            if e == "u" and i + 5 < n:
                cp = int(raw_inner[i + 2:i + 6], 16)
                step = 6
                if 0xD800 <= cp < 0xDC00 and raw_inner[i + 6:i + 8] == "\\u":
                    lo = int(raw_inner[i + 8:i + 12], 16)
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)
                    step = 12
                out.append(chr(cp))
                pos.append(i)
                i += step
                continue
            out.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}.get(e, e))
            pos.append(i)
            i += 2
            continue
        out.append(c)
        pos.append(i)
        i += 1
    pos.append(n)
    return "".join(out), pos


def _json_escape(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)[1:-1]


class _DenebMalformed:
    """Embedded Vega/Deneb text that is not valid JSON. The whole literal stays opaque."""


_DENEB_MALFORMED = _DenebMalformed()


def _deneb_values_spans(decoded: str):
    """Spans of inline data arrays inside a Vega/Deneb spec, or a malformed marker.

    None means this literal is not an embedded spec. A malformed spec is not treated as
    ordinary text: the caller opaques the whole literal instead of publishing its values.
    """
    if not (decoded.startswith("'") and decoded.endswith("'")):
        return None
    quoted_obj = decoded[1:].lstrip().startswith("{")
    if '"$schema"' not in decoded and '"data"' not in decoded and not quoted_obj:
        return None
    inner_pos, inner = [], []
    i = 1
    while i < len(decoded) - 1:
        if decoded[i] == "'" and decoded[i + 1] == "'":
            inner.append("'")
            inner_pos.append(i)
            i += 2
            continue
        inner.append(decoded[i])
        inner_pos.append(i)
        i += 1
    inner_pos.append(len(decoded) - 1)
    spec = "".join(inner)
    try:
        root = pbir.parse_json(spec)
    except pbir.JsonError:
        return _DENEB_MALFORMED
    spans = []

    def visit(node):
        if isinstance(node, pbir.JObj):
            for k, v in node.pairs:
                if k.value == "values" and isinstance(v, pbir.JArr) and v.items:
                    spans.append((inner_pos[v.start], inner_pos[v.end]))
                else:
                    visit(v)
        elif isinstance(node, pbir.JArr):
            for v in node.items:
                visit(v)

    visit(root)
    return spans


def _opaque_unknown_object(text: str, root: pbir.JObj, tm: TokenMap) -> str:
    """Keep a known Fabric `$schema`. Every other top-level key and value is one raw token.

    Numeric and boolean canaries are opaque, and detokenize restores the original slices.
    """
    edits = []

    def tok(node) -> str:
        raw = text[node.start:node.end]
        return '"' + tm.raw("payload:schema", raw, "pbir_full") + '"'

    schema_kept = False
    for key, value in root.pairs:
        recognized = key.value == "$schema" and isinstance(value, pbir.JStr) and \
            PBIR_SCHEMA_RE.fullmatch(value.value.strip()) is not None
        if recognized:
            schema_kept = True
            continue
        if key.value != "$schema":
            edits.append((key.start, key.end, tok(key)))
        edits.append((value.start, value.end, tok(value)))
    if not schema_kept and not edits:
        return text
    return apply_edits(text, edits)


def tokenize_pbir(text: str, relpath: str, model, clf, tm: TokenMap) -> str:
    try:
        root = pbir.parse_json(text)
    except pbir.JsonError:
        raise PbzError("REFUSED", "malformed report JSON") from None
    name = relpath.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in ("report.json", "page.json", "visual.json") and not isinstance(root, pbir.JObj):
        raise PbzError("REFUSED", "malformed report JSON")
    # Unknown report/page/visual object: fail closed. `$schema` stays when it is a Fabric URI.
    if _pbir_schema_known(relpath, root) is False:
        return _opaque_unknown_object(text, root, tm)
    edits: list[tuple[int, int, str]] = []
    handled: set[int] = set()

    def whole_uri(node) -> None:  # the whole JSON string is one data: URI (theme image, image URL literal)
        raw_full = text[node.start:node.end]
        edits.append((node.start, node.end, '"' + tm.raw(data_uri_label(node.value.strip("'")), raw_full, "pbir_full") + '"'))

    for site in pbir.walk_literals(root, model):
        handled.add(site.node.start)
        value, vtype = pbir.decode_literal(site.raw)
        raw_full = text[site.node.start:site.node.end]
        if vtype == "string" and isinstance(value, str):
            if DATA_URI_RE.match(value):
                whole_uri(site.node)
                continue
            spans = _deneb_values_spans(site.raw)
            if spans is _DENEB_MALFORMED:
                edits.append((site.node.start, site.node.end,
                              '"' + tm.raw("payload:deneb", raw_full, "pbir_full") + '"'))
                continue
            if spans:
                inner_raw = raw_full[1:-1]
                decoded, pos = _decode_map(inner_raw)
                deneb = []
                for s, e in spans:
                    rs, re_ = pos[s] + 1, pos[e] + 1
                    deneb.append((site.node.start + rs, site.node.start + re_,
                                  tm.raw("payload:deneb", raw_full[rs:re_], "pbir_inner")))
                edits.extend(deneb)
                edits.extend(u for u in _uri_edits(text, site.node, tm) if not _overlaps(u[0], u[1], deneb))
                continue
        label = None
        if vtype in ("string", "int", "double"):
            label = clf.literal(value, "string" if vtype == "string" else "number", site.column)
        if label:
            edits.append((site.node.start, site.node.end, '"' + tm.lit(label, value, vtype, "pbir_full", raw_full) + '"'))
        elif vtype == "string" and isinstance(value, str):
            uris = _uri_edits(text, site.node, tm)
            edits.extend(uris)
            if site.column is None:
                _free_text_edits(text, site.node, value, clf, tm, edits, numbers=False, avoid=uris)
    for key, node in pbir.string_nodes(root):
        if node.start in handled:
            continue
        if DATA_URI_RE.match(node.value):
            whole_uri(node)
            continue
        uris = _uri_edits(text, node, tm)
        edits.extend(uris)
        if key in STRUCTURAL_KEYS:
            continue
        numbers = key == "value" and '"textRuns"' in text
        _free_text_edits(text, node, node.value, clf, tm, edits, numbers=numbers, avoid=uris)
    return apply_edits(text, edits)


def _uri_edits(text, node, tm) -> list[tuple[int, int, str]]:
    """Base64 data: URIs inside one JSON string (HTML, a Vega spec, an SVG) as inner payload tokens."""
    inner_raw = text[node.start + 1:node.end - 1]
    if "base64," not in inner_raw.lower():
        return []
    decoded, pos = _decode_map(inner_raw)
    out = []
    for m in EMBEDDED_B64_URI_RE.finditer(decoded):
        rs, re_ = pos[m.start()], pos[m.end()]
        out.append((node.start + 1 + rs, node.start + 1 + re_,
                    tm.raw(data_uri_label(m.group(0)), inner_raw[rs:re_], "pbir_inner")))
    return out


def _sq_unquote(decoded: str) -> tuple[str, list[int]] | None:
    """PBIR single-quoted literal -> (unquoted text, decoded index of each unquoted char).

    Doubled single quotes collapse to one apostrophe. The last map entry is the exclusive end
    (the closing quote). None when `decoded` is not one quoted literal.
    """
    if len(decoded) < 2 or decoded[0] != "'" or decoded[-1] != "'":
        return None
    out, mp = [], []
    i, end = 1, len(decoded) - 1
    while i < end:
        if decoded[i] == "'" and i + 1 < end and decoded[i + 1] == "'":
            out.append("'")
            mp.append(i)
            i += 2
            continue
        if decoded[i] == "'":
            return None
        out.append(decoded[i])
        mp.append(i)
        i += 1
    mp.append(end)
    return "".join(out), mp


def _value_to_decoded(decoded: str, value: str) -> list[int] | None:
    """Map indexes in the unquoted literal `value` onto `decoded` (JSON-unescaped) text."""
    if decoded == value:
        return list(range(len(decoded) + 1))
    unq = _sq_unquote(decoded)
    if unq is not None and unq[0] == value:
        return unq[1]
    return None


def _free_text_edits(text, node, value, clf, tm, edits, numbers, avoid=()):
    """Hide free-text spans inside one JSON string.

    `value` is the unquoted literal (PBIR decode) or the JSON string itself. Spans are mapped
    through doubled single quotes and JSON escapes so an email, phone or name is not dropped
    when the raw text is longer than the screened value.
    """
    if not isinstance(value, str) or not value:
        return
    inner_raw = text[node.start + 1:node.end - 1]
    decoded, pos = _decode_map(inner_raw)
    mapping = _value_to_decoded(decoded, value)
    if mapping is None:
        return
    spans = clf.free_text(value, numbers=numbers)
    if not spans:
        return
    for s, e, lab in spans:
        if s < 0 or e >= len(mapping):
            continue
        ds, de = mapping[s], mapping[e]
        if ds < 0 or de >= len(pos):
            continue
        rs, re_ = pos[ds], pos[de]
        if avoid and _overlaps(node.start + 1 + rs, node.start + 1 + re_, list(avoid)):
            continue
        edits.append((node.start + 1 + rs, node.start + 1 + re_,
                      tm.raw(lab, inner_raw[rs:re_], "pbir_inner", value[s:e])))


def detokenize_pbir(text: str, tm: TokenMap) -> tuple[str, list[str]]:
    unknown: list[str] = []

    def esc(m):  # «...» escaped as «...» by a JSON writer: the same token
        try:
            tok = "«" + json.loads('"' + m.group(1) + '"') + "»"
        except ValueError:
            return m.group(0)
        if tm.get(tok) is None:
            unknown.append(tok)
            return m.group(0)
        return tok

    text = ESC_TOKEN_RE.sub(esc, text)

    def full(m):
        tok = m.group(1)
        rec = tm.get(tok)
        if not rec or rec.get("kind") == "hmac":
            unknown.append(tok)
            return m.group(0)
        raws = rec.get("raws", {})
        if "pbir_full" in raws:
            return raws["pbir_full"]
        if rec.get("kind") == "lit":
            vtype = rec.get("vtype", "string")
            vtype = {"number": "double"}.get(vtype, vtype)
            if vtype == "double" and isinstance(rec.get("value"), int):
                vtype = "int"
            return json.dumps(pbir.encode_literal(rec.get("value"), vtype), ensure_ascii=False)
        return '"' + raws.get("pbir_inner", _json_escape(str(rec.get("value", "")))) + '"'

    def inner(m):
        tok = m.group(0)
        rec = tm.get(tok)
        if not rec or rec.get("kind") == "hmac":
            unknown.append(tok)
            return tok
        raws = rec.get("raws", {})
        if "pbir_inner" in raws:
            return raws["pbir_inner"]
        return _json_escape(str(rec.get("value", "")))

    text = FULL_JSON_TOKEN_RE.sub(full, text)
    return TOKEN_RE.sub(inner, text), unknown


def detokenize_dax(expr: str, tm: TokenMap) -> tuple[str, list[str]]:
    """For agent-written DAX (check specs): tokens inside strings -> escaped value, else literal."""
    return detokenize_tmdl(expr, tm)
