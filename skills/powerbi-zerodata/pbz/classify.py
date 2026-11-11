"""Column sensitivity and literal decisions.

A literal is hidden (tokenized) when it is a value of a sensitive column, matches a known
sensitive value (HMAC membership from the engine's value index), or looks like PII. Category
labels, dates and authored constants stay readable.
"""
from __future__ import annotations

import json
import os
import re

from . import dpapi
from .common import PbzError

PII_NAME = re.compile(
    r"(?i)(name|first|last|full|e-?mail|upn|user|login|owner|manager|\bpm\b|leader|\blead\b|supervisor|"
    r"resource|person|employee|staff|member|assignee|requester|approver|sponsor|contact|phone|address|"
    r"comment|note|description|title|summary|vendor|customer|client|account)")
ID_NAME = re.compile(r"(?i)(\bid\b|id$|_id\b|\bids\b|number|\bno\b|\bnum\b|code|\bkey\b|guid|ssn|account)")
CATEGORY_NAME = re.compile(
    r"(?i)(status|type|category|class|phase|stage|state|flag|label|bucket|group|kind|priority|level|tier|"
    r"year|quarter|month|day|week|period|season|region|department|division|function|portfolio|program|"
    r"work ?type|\brole\b|grade|band|size|scope|source|method|mode|unit|health|risk|rag|exception|"
    r"active|current|is |has )")
PII_WORDS = {"name", "names", "first", "last", "fullname", "email", "mail", "upn", "user", "username", "login",
             "owner", "manager", "pm", "leader", "lead", "supervisor", "resource", "person", "employee", "staff",
             "member", "assignee", "requester", "approver", "sponsor", "contact", "phone", "address", "comment",
             "comments", "note", "notes", "description", "title", "summary", "vendor", "customer", "client", "account"}
CATEGORY_WORDS = {"type", "types", "status", "state", "category", "class", "phase", "stage", "flag", "label",
                  "bucket", "group", "kind", "priority", "level", "tier", "year", "quarter", "month", "day", "week",
                  "period", "season", "region", "department", "division", "function", "portfolio", "program", "role",
                  "grade", "band", "size", "scope", "source", "method", "mode", "unit", "health", "risk", "rag",
                  "exception", "active", "current", "is", "has", "count", "pct", "percent", "series", "grain", "sort"}
ID_WORDS = {"id", "ids", "number", "no", "num", "code", "key", "guid", "ssn"}
# Which sensitive columns may be used to recognize UNBOUND text (comments, titles, free literals).
# Provenance first: bound literals are hidden whatever they look like; unbound text is matched only
# against distinctive person, identifier and title values (research notes: docs/reference-projects.md).
PERSON_WORDS = {"name", "names", "fullname", "email", "mail", "upn", "user", "username", "login", "owner",
                "manager", "pm", "leader", "lead", "supervisor", "resource", "person", "employee", "staff",
                "member", "assignee", "requester", "approver", "sponsor", "contact", "vendor", "customer", "client"}
TITLE_WORDS = {"title", "project", "subject", "initiative", "car", "cer"}
FREE_TEXT_WORDS = {"description", "desc", "comment", "comments", "note", "notes", "summary", "remarks", "remark",
                   "text", "details", "detail", "reason", "justification", "narrative", "body", "message"}
VALUE_TEXT_WORDS = {"hours", "hrs", "hour", "date", "dates", "time", "datetime", "amount", "cost", "costs", "budget",
                    "value", "values", "count", "qty", "quantity", "pct", "percent", "row", "rows", "days",
                    "workdays", "elapsed", "duration", "total", "sum", "rate", "score", "index", "seq", "order"}


def match_kind(column: str) -> str | None:
    """person | identifier | title when the column's values may be matched in unbound text, else None."""
    w = name_words(column)
    if w & (FREE_TEXT_WORDS | VALUE_TEXT_WORDS | CATEGORY_WORDS):
        return None
    if w & PERSON_WORDS:
        return "person"
    if is_id_column(column):
        return "identifier"
    if w & TITLE_WORDS:
        return "title"
    return None


def name_words(column: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", column)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w}


def is_pii_column(column: str) -> bool:
    w = name_words(column)
    return bool(w & PII_WORDS) and not (w & CATEGORY_WORDS)


def _digit_run(value: str) -> str | None:
    """Strip a separator-or-digit string. None when it is empty, a decimal, or a date.

    Bare digit runs are accepted. Separators and a leading plus are optional.
    """
    s = value.strip()
    if not s or not _PHONE_CHARS.fullmatch(s) or _DECIMAL_VALUE.fullmatch(s) or _ISO_DATE.fullmatch(s) \
            or _NUM_DATE.fullmatch(s):
        return None
    return "".join(ch for ch in s if ch.isdigit())


def looks_like_phone(value: str) -> bool:
    """A phone-shaped string: 10 to 15 digits, bare or with separators / a leading plus.

    An ISO date, a numeric date, and a single decimal are not phones. 16+ digit
    card or account runs are looks_like_long_digits, not phones.
    """
    digits = _digit_run(value)
    return digits is not None and 10 <= len(digits) <= 15


def looks_like_long_digits(value: str) -> bool:
    """Separator-or-digit string with 16 or more digits that is not an ISO or numeric date."""
    digits = _digit_run(value)
    return digits is not None and len(digits) >= 16


def _pii_number_spans(text: str) -> list[tuple[int, int]]:
    """In-text spans accepted by looks_like_phone or looks_like_long_digits.

    Dates and decimals fail those checks and stay visible. A run is bounded so a paragraph
    of ordinary numbers is not swallowed whole.
    """
    out: list[tuple[int, int]] = []
    for m in _PII_NUM_RUN.finditer(text):
        raw = m.group()
        lead = len(raw) - len(raw.lstrip())
        core = raw.strip()
        while core and not core[-1].isdigit() and not (
                looks_like_phone(core) or looks_like_long_digits(core)):
            core = core[:-1]
        if core and (looks_like_phone(core) or looks_like_long_digits(core)):
            start = m.start() + lead
            out.append((start, start + len(core)))
    return out


def _nonlatin(text: str) -> bool:
    return any(ch.isalpha() and not ch.isascii() for ch in text)


def is_id_column(column: str) -> bool:
    # "No" counts only as a trailing separate word ("Project No"), not inside "YesorNo"
    return bool(name_words(column) & (ID_WORDS - {"no"})) or bool(re.search(r"(?i)(?:^|[\s_.#-])no\.?$", column))


_EMAIL_VALUE = re.compile(r"(?i)^[\w.+'-]+@[\w-]+(\.[\w-]+)+$")
_PHONE_CHARS = re.compile(r"^\+?[\d\s().-]+$")
_DECIMAL_VALUE = re.compile(r"^[+-]?\d+\.\d+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?$")
_NUM_DATE = re.compile(r"^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")
# Letters of any script, for exact index matches. Latin WORD_RE stays the heuristic tokenizer.
INDEX_WORD = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")
# format patterns stay readable under the strict literal policy (FORMAT() codes, number formats)
FORMATISH = re.compile(r"(?i)^(?:(?:yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|nn|ss|s|q|am/pm|a/p|tt)[\s\-/.:,]*)+$"
                       r"|^[#0.,%\s$€£¥+()\-]+$|^(?:general|long|short) (?:date|time)$|^(?:true|false)$")
PII_IN_TEXT = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# Bounded phone / long-digit candidates. The classifiers decide; this only limits the scan.
_PII_NUM_RUN = re.compile(r"(?<![\w])\+?[\d\s().-]{10,64}(?!\d)")
NAME_LIKE = re.compile(r"^[A-Z][a-z'À-ɏ-]+(?: [A-Z][a-z'À-ɏ.-]+){1,3}$")
WORD_RE = re.compile(r"[A-Za-z0-9À-ɏ][\w'&.À-ɏ-]*")
NUMERIC_TYPES = {"int64", "double", "decimal", "currency", "number"}
# Heuristic-mode person-name detector for free text: 2-3 capitalized words in a row.
CAP_RUN = re.compile(r"\b[A-Z][a-zÀ-ɏ'-]+(?: [A-Z][a-zÀ-ɏ'-]+){1,2}\b")
COMMON_WORDS = set("""
the a an and or of for to in on at by with from per vs via all any new old
total sum count average avg min max distinct current previous prior last next year years quarter month week
day date dates time period ytd mtd qtd yoy mom fiscal calendar today
project projects portfolio program status active closed open complete completed planned actual actuals
demand capacity hours hour resource resources role roles team teams work type types category
report page table column measure value values overview summary detail details dashboard home
kpi kpis trend trends top bottom rank risk exception exceptions governance capital budget cost costs spend
owner owners manager managers leader leaders person people user users employee staff
showing select clear filter filters reset back next view details click here more less
high medium low none other unknown yes no true false not blank
north south east west region division department business it operations admin bau pto
""".split())


_DICT_WORDS: set[str] | None = None
_PROPER_NOUNS: set[str] = set()
_DICT_FILES = [os.environ.get("PBZ_DICT", "")] + [
    os.path.join(os.environ.get(v, ""), "LibreOffice", "share", "extensions", "dict-en", f)
    for v in ("ProgramFiles", "ProgramFiles(x86)") for f in ("en_US.dic", "en_GB.dic")]


def common_english() -> set[str]:
    """Lowercase entries of local Hunspell dictionaries (proper nouns are capitalized there, so names stay out)."""
    global _DICT_WORDS
    if _DICT_WORDS is None:
        words: set[str] = set()
        for path in _DICT_FILES:
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    next(f, None)
                    for line in f:
                        w = line.split("/", 1)[0].strip()
                        if len(w) > 1 and w.isalpha():
                            if w[0].islower():
                                words.add(w)
                            elif w[1:].islower():
                                _PROPER_NOUNS.add(w.lower())
            except OSError:
                continue
        _DICT_WORDS = words
    return _DICT_WORDS


def is_proper_noun(word: str) -> bool:
    common_english()
    return word.lower() in _PROPER_NOUNS


def is_common_word(word: str) -> bool:
    w = word.lower().strip(".'-")
    if w in COMMON_WORDS:
        return True
    d = common_english()
    if w in d:
        return True
    for suf in ("ing", "ers", "ed", "es", "er", "ly", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and w[:-len(suf)] in d:
            return True
    return False


class Classifier:
    def __init__(self, model, private_dir: str | None, policy: dict):
        self.model = model
        self.policy = policy or {}
        self.index = None
        self.categories: dict = {}
        self.key = None
        self.hmac_to_col: dict[str, str] = {}
        self.word_hmac_to_col: dict[str, str] = {}
        if private_dir:
            index_path = os.path.join(private_dir, "index.json")
            missing = False
            try:
                with open(index_path, encoding="utf-8") as f:
                    loaded = json.load(f)
            except FileNotFoundError:
                missing = True
            except (OSError, ValueError):
                raise PbzError("REFUSED", "private index could not be read") from None
            if not missing:
                if not isinstance(loaded, dict):
                    raise PbzError("REFUSED", "private index could not be read")
                self.index = loaded
                try:
                    self.key = dpapi.load_key(private_dir, create=False)
                except (OSError, ValueError):
                    raise PbzError("REFUSED", "private index could not be read") from None
            raw = dpapi.read_protected(os.path.join(private_dir, "categories.bin")) if self.index is not None else None
            if raw:
                try:
                    self.categories = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self.categories = {}
        self.mode = "index" if self.index is not None and self.key else "heuristic"
        self.sensitive_cols = {c.lower() for c in self.policy.get("sensitive_columns", [])}
        self.category_cols = {c.lower() for c in self.policy.get("category_columns", [])}
        self.model_words: set[str] = set()
        self.model_names: set[str] = set()
        # policy "literals": "strict" hides by default: every literal is tokenized unless it is a known
        # category label, a model object / source column name, a format pattern or one or two characters
        self.strict = self.policy.get("literals") == "strict"
        if model is not None:
            for name in model.object_names():
                self.model_names.add(name.strip().lower())
                self.model_words |= {w.lower() for w in WORD_RE.findall(name)}
            for c in model.all_columns():
                if c.source_column:
                    self.model_names.add(c.source_column.strip().lower())
        # Unbound-text recognizers: only sensitive person / identifier / title columns qualify.
        # Category labels, free-text columns and numbers or dates stored as text never do.
        self.col_kind: dict[str, str] = {}
        if self.index:
            for col, info in (self.index.get("columns") or {}).items():
                if "[" not in col or not col.endswith("]"):
                    continue
                table, cname = col[:col.index("[")], col[col.index("[") + 1:-1]
                kind = match_kind(cname)
                if not kind or self.column_class(table, cname) != "sensitive":
                    continue
                self.col_kind[col] = kind
                for h in info.get("hmacs", []) or []:
                    self.hmac_to_col.setdefault(h, col)
                if kind == "person":
                    for h in info.get("word_hmacs", []) or []:
                        self.word_hmac_to_col.setdefault(h, col)

    # -------------------------------------------------------------- columns

    @staticmethod
    def label(table: str, column: str) -> str:
        return f"{table}[{column}]"

    def column_class(self, table: str, column: str) -> str:
        """category | sensitive | numeric | date | other | unknown"""
        lab = self.label(table, column)
        if lab.lower() in self.sensitive_cols:
            return "sensitive"
        if lab.lower() in self.category_cols:
            return "category"
        col = self.model.column(table, column) if self.model else None
        dtype = (col.data_type if col else "").lower()
        if col is None and not self.index:
            return "unknown"
        info = (self.index or {}).get("columns", {}).get(lab) if self.index else None
        if info and not dtype:
            dtype = str(info.get("type", "")).lower()
        if dtype in NUMERIC_TYPES:
            return "sensitive" if is_id_column(column) else "numeric"
        if dtype.startswith("date"):
            return "date"
        if dtype == "boolean":
            return "other"
        if is_pii_column(column) or is_id_column(column):
            return "sensitive"
        if info:
            cls = info.get("class")
            if cls == "category" or cls == "empty":
                return "category"
            if cls == "high":
                return "sensitive"
        return "category" if name_words(column) & CATEGORY_WORDS else "sensitive"

    def category_values(self, table: str, column: str) -> list | None:
        if self.column_class(table, column) != "category":
            return None
        return self.categories.get(self.label(table, column))

    # -------------------------------------------------------------- literals

    def _specific(self, text: str) -> bool:
        """Worth matching at all: 3+ letters, 4+ chars, not only common or model words, not a model name."""
        s = text.strip()
        if len(s) < 4 or sum(ch.isalpha() for ch in s) < 3:
            return False
        if s.lower() in self.model_names:
            return False
        words = [w.lower() for w in WORD_RE.findall(s)]
        return bool(words) and not all(is_common_word(w) or w in self.model_words for w in words)

    def _fits_kind(self, text: str, kind: str) -> bool:
        s = text.strip()
        nwords = len(WORD_RE.findall(s))
        if _nonlatin(s):
            nwords = max(nwords, len(INDEX_WORD.findall(s)))
        if kind == "title":
            return nwords >= 2 and len(s) >= 8
        if kind == "identifier":
            return len(s) >= 6
        return len(s) >= (4 if nwords == 1 else 6)  # person

    def hmac_column(self, value) -> str | None:
        """Column whose distinctive value equals this unbound text (precision-first), else None.

        Multi-word text that exactly equals a person / identifier / title value is strong evidence and
        is hidden even when its words are common ("Mark Price"); single words must be specific."""
        if not self.key or value is None or not isinstance(value, str):
            return None
        s = value.strip()
        nonlatin = _nonlatin(s)
        # Latin length and word-shape gates reject short indexed non-Latin names (2–4 letters).
        # An exact HMAC hit is the only reason a non-Latin value is hidden; nothing is guessed.
        min_len = 2 if nonlatin else 4
        min_alpha = 2 if nonlatin else 3
        if len(s) < min_len or sum(ch.isalpha() for ch in s) < min_alpha or s.lower() in self.model_names:
            return None
        # Latin single words still need the specific/proper-noun gate. Non-Latin and uncased
        # scripts have no title case to test; an exact index hit is the only reason to hide them.
        if not nonlatin and len(WORD_RE.findall(s)) < 2 and not self._specific(s) \
                and not (s[:1].isupper() and is_proper_noun(s)):
            return None
        col = self.hmac_to_col.get(dpapi.hmac_hex(self.key, value))
        if not col:
            return None
        if nonlatin:
            return col
        if self._fits_kind(value, self.col_kind.get(col, "person")):
            return col
        return None

    def literal(self, value, vtype: str, column: tuple | None) -> str | None:
        """Token label for a literal that must be hidden, or None to keep it."""
        if vtype == "string" and isinstance(value, str) and (
                _EMAIL_VALUE.match(value.strip()) or looks_like_phone(value) or looks_like_long_digits(value)):
            return "pii"
        if self.strict:
            return self._strict_literal(value, vtype, column)
        if column:
            cls = self.column_class(*column)
            if cls == "sensitive":
                return self.label(*column)
            if cls in ("category", "numeric", "date", "other"):
                return None
        if vtype == "string" and isinstance(value, str) and value.strip():
            hit = self.hmac_column(value)
            if hit:
                return hit
            if self.mode == "heuristic" and self._person_like(value):
                return "lit"
        return None

    def _strict_literal(self, value, vtype: str, column: tuple | None) -> str | None:
        cls = self.column_class(*column) if column else None
        if cls == "sensitive":
            return self.label(*column)
        if vtype == "string" and isinstance(value, str):
            s = value.strip()
            if not s or len(s) <= 2 or s.lower() in self.model_names or FORMATISH.match(s):
                return None
            if column and cls == "category" and self.mode == "index":
                vals = self.category_values(*column) or []
                if s in vals:
                    return None  # a label the engine index has seen as a category value
            return self.label(*column) if column else "lit"
        if vtype == "number" and column and cls not in ("date",):
            try:
                small = float(value) == int(float(value)) and 0 <= float(value) <= 31
            except (TypeError, ValueError):
                small = False
            return None if small else self.label(*column)
        return None

    def _person_like(self, value: str) -> bool:
        """Two to four capitalized words, at least one not a common or model word."""
        v = value.strip()
        if not NAME_LIKE.match(v):
            return False
        return any(w.lower() not in COMMON_WORDS and w.lower() not in self.model_words for w in v.split())

    def free_text(self, text: str, numbers: bool = False) -> list[tuple[int, int, str]]:
        """Spans of free text (descriptions, titles, text boxes, comments) to hide.

        CJK and other scripts without word separators are matched only as a whole indexed token.
        A name glued to neighboring letters (no boundary) is not split out; that is not guessed.
        """
        spans: list[tuple[int, int, str]] = []
        for m in PII_IN_TEXT.finditer(text):
            spans.append((m.start(), m.end(), "pii"))
        for s, e in _pii_number_spans(text):
            if not any(s < b and e > a for a, b, _lab in spans):
                spans.append((s, e, "pii"))
        if self.key:
            words = list(WORD_RE.finditer(text))
            taken = [(m.start(), m.end()) for m in words]
            extra = [m for m in INDEX_WORD.finditer(text)
                     if _nonlatin(m.group()) and not any(m.start() < b and m.end() > a for a, b in taken)]
            words = sorted(words + extra, key=lambda m: m.start())
            # Indexed titles have no four-word limit. Bound work without leaking long prose:
            # an unusually large data-derived description is safer as an opaque token.
            if self.hmac_to_col and len(words) > 256:
                return [(0, len(text), "lit")]
            i = 0
            while i < len(words):
                hit = None
                for k in range(len(words) - i, 0, -1):
                    s, e = words[i].start(), words[i + k - 1].end()
                    col = self.hmac_column(text[s:e])
                    if not col and k == 1 and self.word_hmac_to_col:
                        # a lone name part: capitalized, 4+ letters, and either not an English word
                        # or a known first/last name ("Patty"), never a model word
                        core = dpapi.word_core(text[s:e])
                        titled = core[:1].isupper() or _nonlatin(core)
                        need = 2 if _nonlatin(core) else 4
                        if sum(ch.isalpha() for ch in core) >= need and titled \
                                and core.lower() not in self.model_words and core.lower() not in self.model_names \
                                and (not is_common_word(core) or is_proper_noun(core) or _nonlatin(core)):
                            col = self.word_hmac_to_col.get(dpapi.hmac_hex(self.key, core))
                    if col:
                        hit = (s, e, col, k)
                        break
                if hit:
                    spans.append(hit[:3])
                    i += hit[3]
                else:
                    i += 1
        if self.mode == "heuristic":
            for m in CAP_RUN.finditer(text):
                # "Owner Jane Doe": a common capitalized word must not shield the name next to it, so
                # look at every run of two or more uncommon words inside the match
                run, start = [], None
                for wm in re.finditer(r"\S+", m.group(0)):
                    w = wm.group(0)
                    if w.lower() in COMMON_WORDS or w.lower() in self.model_words:
                        if len(run) >= 2:
                            spans.append((m.start() + start, m.start() + run[-1].end(), "lit"))
                        run, start = [], None
                    else:
                        run.append(wm)
                        start = wm.start() if start is None else start
                if len(run) >= 2:
                    spans.append((m.start() + start, m.start() + run[-1].end(), "lit"))
        if numbers:
            # Counts in descriptions/comments are data regardless of their magnitude. This
            # path does not tokenize executable authored DAX/M constants or schema ordinals.
            for m in re.finditer(r"(?<!\w)[$€£]?\s?[+-]?\d[\d,]*(?:\.\d+)?(?:[eE][+-]?\d+)?\s?[%KMB]?(?!\w)", text):
                spans.append((m.start() + (len(m.group(0)) - len(m.group(0).lstrip())), m.end(), "num"))
        spans.sort()
        merged: list[tuple[int, int, str]] = []
        for s, e, lab in spans:
            # A longer indexed value that starts where a shorter number does covers that number.
            if merged and merged[-1][0] == s and e > merged[-1][1]:
                merged[-1] = (s, e, lab)
                continue
            if merged and s < merged[-1][1]:
                continue
            merged.append((s, e, lab))
        return merged
