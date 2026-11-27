"""Usage graph of a PBIP project: every model and report object, what it depends on, and where the
reports use it. Built from the sanitized mirror (names and formulas only; tokens are opaque literals).

States (Measure Killer's four plus three):
- used            reached from a report usage (visual field, filter, sort, formatting, bookmark, mobile)
- used_external   reached only from a registered external consumer (`pbz consumers`: thin reports
                  elsewhere, Excel, paginated reports, composite models, query exports, declarations)
- locked          needed by the model itself (relationship keys of kept relationships, sort-by targets of
                  used columns, RLS / OLS, aggregations, marked date table key, refresh policy, roles)
- used_by_unused  not needed, but referenced by other dead objects (remove those first)
- unused          nothing refers to it
- unknown         the analysis is incomplete (a report or registered consumer could not be read, or a
                  consumer's coverage is incomplete): never treated as unused
"Unused" means "unused in the scanned reports and registered consumers": consumers nobody registered
(service reports, Excel files, paginated reports, composite models) are not visible here.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import deps, pbir, tmdl
from .common import PbzError, read_definition_text, read_text, list_names

STATES = ("used", "used_external", "locked", "used_by_unused", "unused", "unknown")
UNSEEN = ["service reports", "Excel / Analyze in Excel", "paginated reports", "composite models",
          "dataflows and notebooks"]

# Edge extensions available to prove a retained table lies on a directed filter path.
# Read from this global at call time; tests patch it.
_PATH_REASON_BUDGET = 50000
_FILTER_PATH_DEFINITE = "active filter path between used tables"
_FILTER_PATH_MANUAL = "possible filter path between used tables: filter direction does not propagate as modeled; manual review before removal"


def key(kind: str, table: str, name: str, report: str = "") -> tuple:
    base = (kind, (table or "").lower(), (name or "") if kind == "m_query" else (name or "").lower())
    if kind == "report_measure":
        return base + ((report or "").lower(),)
    return base


@dataclass
class Obj:
    kind: str     # table column measure hierarchy calc_item relationship role m_query function report_measure
    table: str
    name: str
    file: str = ""
    line: int = 0
    hidden: bool = False
    calculated: bool = False
    private: bool = False
    issues: list = field(default_factory=list)   # unresolved reference names
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple:
        report = self.extra.get("report", "") if self.kind == "report_measure" else ""
        return key(self.kind, self.table, self.name, report)

    def label(self) -> str:
        if self.kind == "table":
            return self.name
        if self.kind in ("column", "hierarchy", "calc_item"):
            return f"{self.table}[{self.name}]" if self.kind == "column" else f"{self.table}.{self.name}"
        if self.kind in ("measure", "report_measure"):
            return f"[{self.name}]"
        if self.kind == "relationship":
            return self.extra.get("label", self.name)
        return self.name


@dataclass
class Edge:
    src: tuple
    dst: tuple
    via: str


@dataclass
class Usage:
    report: str
    page: str
    visual: str
    via: str
    file: str


class Graph:
    def __init__(self):
        self.objects: dict[tuple, Obj] = {}
        self.edges: list[Edge] = []
        self.out: dict[tuple, list[Edge]] = {}
        self.inc: dict[tuple, list[Edge]] = {}
        self.locks: dict[tuple, list[str]] = {}
        self.usages: dict[tuple, list[Usage]] = {}
        self.ext_usages: dict[tuple, list[Usage]] = {}   # registered external consumers (report = consumer id)
        self.consumers: list[dict] = []
        self.reports: list[str] = []
        self.report_objects: list[dict] = []
        self.warnings: list[dict] = []
        self.incomplete = False
        self.state: dict[tuple, str] = {}
        self.blocked_by: dict[tuple, list[tuple]] = {}
        self.model = None

    def add(self, obj: Obj) -> Obj:
        return self.objects.setdefault(obj.key, obj)

    def edge(self, src: tuple, dst: tuple, via: str) -> None:
        if src == dst or dst not in self.objects or src not in self.objects:
            return
        e = Edge(src, dst, via)
        self.edges.append(e)
        self.out.setdefault(src, []).append(e)
        self.inc.setdefault(dst, []).append(e)

    def lock(self, k: tuple, reason: str) -> None:
        if k in self.objects and reason not in self.locks.setdefault(k, []):
            self.locks[k].append(reason)

    def use(self, k: tuple, u: Usage) -> None:
        if k in self.objects:
            self.usages.setdefault(k, []).append(u)

    def use_external(self, k: tuple, u: Usage) -> None:
        if k in self.objects:
            self.ext_usages.setdefault(k, []).append(u)

    def label(self, k: tuple) -> str:
        o = self.objects.get(k)
        return o.label() if o else k[2]

    # ---------------------------------------------------------------- states

    def closure(self, roots) -> set:
        seen, todo = set(), [r for r in roots if r in self.objects]
        while todo:
            k = todo.pop()
            if k in seen:
                continue
            seen.add(k)
            todo.extend(e.dst for e in self.out.get(k, []) if e.dst not in seen)
        return seen

    def compute_states(self) -> None:
        used = self.closure(self.usages)
        ext = self.closure(self.ext_usages) - used
        locked = self.closure(self.locks) - used - ext
        # structural needs that depend on what survives: kept relationships keep their key columns,
        # a kept marked date table keeps its key column
        changed = True
        while changed:
            changed = False
            alive = used | ext | locked
            for k, o in self.objects.items():
                if k in alive:
                    continue
                if o.kind == "relationship" and o.extra.get("active", True):
                    ft, tt = key("table", "", o.extra["from_table"]), key("table", "", o.extra["to_table"])
                    if ft in alive and tt in alive:
                        self.lock(k, "relationship between used tables")
                        locked |= self.closure([k]) - used - ext
                        changed = True
                elif o.kind == "column" and o.extra.get("date_key") and key("table", "", o.table) in alive:
                    self.lock(k, "date table key")
                    locked |= self.closure([k]) - used - ext
                    changed = True
            if _retain_filter_paths(self, alive, locked, used, ext):
                changed = True
        for k, o in self.objects.items():
            if k in used:
                self.state[k] = "used"
            elif k in ext:
                self.state[k] = "used_external"
            elif k in locked:
                self.state[k] = "locked"
            else:
                dead_refs = [e.src for e in self.inc.get(k, []) if e.via not in ("home", "calc_item") and e.src != k
                             and not self._auto_date_rel(e.src)]
                if dead_refs:
                    self.state[k] = "used_by_unused"
                    self.blocked_by[k] = sorted(set(dead_refs))
                else:
                    self.state[k] = "unused"
        # a table goes as a whole: it is removable only when nothing OUTSIDE it (other than its own
        # relationships) still refers to it or to one of its objects
        children: dict[str, list] = {}
        for k, o in self.objects.items():
            if o.kind in ("column", "measure", "hierarchy", "calc_item"):
                children.setdefault(o.table.lower(), []).append(k)
        for k, o in self.objects.items():
            if o.kind != "table" or self.state[k] not in ("unused", "used_by_unused"):
                continue
            tl = o.name.lower()
            inside = set(children.get(tl, [])) | {k}
            for rk, ro in self.objects.items():
                if ro.kind == "relationship" and tl in (ro.extra["from_table"].lower(), ro.extra["to_table"].lower()):
                    inside.add(rk)
            external = set()
            for c in inside:
                external |= {b for b in self.blocked_by.get(c, []) if b not in inside}
            if external:
                self.state[k] = "used_by_unused"
                self.blocked_by[k] = sorted(external)
            else:
                self.state[k] = "unused"
                self.blocked_by.pop(k, None)
        if self.incomplete:
            for k in self.objects:
                if self.state[k] in ("unused", "used_by_unused"):
                    self.state[k] = "unknown"

    def _auto_date_rel(self, k: tuple) -> bool:
        """Auto date/time relationship from a date column to its private LocalDateTable."""
        o = self.objects.get(k)
        if not o or o.kind != "relationship":
            return False
        for side in ("from_table", "to_table"):
            t = self.objects.get(key("table", "", o.extra[side]))
            if t is not None and t.private:
                return True
        return False

    # ---------------------------------------------------------------- lookups

    def find(self, target: str) -> list[tuple]:
        """Resolve a user target: 'T'[C], T[C], [Measure], Table, or a bare name."""
        import re
        target = target.strip()
        m = re.match(r"^'((?:[^']|'')+)'\s*\[(.+)\]$", target) or re.match(r"^([^\[\]']+?)\s*\[(.+)\]$", target)
        if m:
            t, n = m.group(1).replace("''", "'"), m.group(2)
            return [k for k in (key("column", t, n), key("measure", t, n), key("hierarchy", t, n),
                                key("calc_item", t, n)) if k in self.objects] or \
                   [k for k, o in self.objects.items() if o.kind == "measure" and o.name.lower() == n.lower()]
        m = re.match(r"^\[(.+)\]$", target)
        if m:
            n = m.group(1).lower()
            return [k for k, o in self.objects.items() if o.kind in ("measure", "report_measure") and o.name.lower() == n]
        low = target.strip("'").lower()
        hits = [k for k in (key("table", "", low),) if k in self.objects]
        if hits:
            return hits
        return [k for k, o in self.objects.items() if o.name.lower() == low and o.kind != "relationship"]


# ---------------------------------------------------------------- filter-path retention


def _active_relationship_graphs(graph):
    """Undirected and directed table graphs of active relationships.

    Filter flow is `to_table -> from_table`, plus `from_table -> to_table` when `both` is truthy.
    Self-loops are dropped. Parallel edges collapse.
    """
    undirected, directed = {}, {}
    for obj in graph.objects.values():
        if obj.kind != "relationship" or not obj.extra.get("active", True):
            continue
        from_table = key("table", "", obj.extra["from_table"])
        to_table = key("table", "", obj.extra["to_table"])
        if from_table == to_table:
            continue
        undirected.setdefault(from_table, set()).add(to_table)
        undirected.setdefault(to_table, set()).add(from_table)
        directed.setdefault(to_table, set()).add(from_table)
        if obj.extra.get("both"):
            directed.setdefault(from_table, set()).add(to_table)
    return undirected, directed


def _biconnected_blocks(adj):
    """Vertex sets of the biconnected components. `adj` maps a vertex to sorted neighbors."""
    disc, low, parent = {}, {}, {}
    time = 0
    edge_stack = []
    blocks = []
    for start in sorted(adj):
        if start in disc:
            continue
        time += 1
        disc[start] = low[start] = time
        parent[start] = None
        stack = [(start, 0)]
        while stack:
            u, ei = stack[-1]
            neighbors = adj[u]
            if ei < len(neighbors):
                stack[-1] = (u, ei + 1)
                v = neighbors[ei]
                if v not in disc:
                    parent[v] = u
                    time += 1
                    disc[v] = low[v] = time
                    edge_stack.append((u, v))
                    stack.append((v, 0))
                elif v != parent[u] and disc[v] < disc[u]:
                    edge_stack.append((u, v))
                    if disc[v] < low[u]:
                        low[u] = disc[v]
            else:
                stack.pop()
                p = parent[u]
                if p is None:
                    continue
                if low[u] < low[p]:
                    low[p] = low[u]
                if low[u] >= disc[p]:
                    verts = set()
                    while edge_stack:
                        a, b = edge_stack.pop()
                        verts.add(a)
                        verts.add(b)
                        if (a, b) == (p, u):
                            break
                    if verts:
                        blocks.append(verts)
    return blocks


def _tables_on_alive_subtree(blocks, alive_tables):
    """Non-alive vertices of blocks on the block-cut subtree spanning `alive_tables`."""
    if not blocks:
        return set()
    membership = {}
    for index, block in enumerate(blocks):
        for vertex in block:
            membership.setdefault(vertex, []).append(index)
    cuts = {vertex for vertex, indexes in membership.items() if len(indexes) > 1}
    tree = {}
    terminals = set()

    def link(a, b):
        tree.setdefault(a, set()).add(b)
        tree.setdefault(b, set()).add(a)

    for index, block in enumerate(blocks):
        block_node = ("b", index)
        tree.setdefault(block_node, set())
        alive_noncut = False
        for vertex in block:
            if vertex in cuts:
                link(block_node, ("c", vertex))
            elif vertex in alive_tables:
                alive_noncut = True
        if alive_noncut:
            terminals.add(block_node)
    for vertex in cuts:
        if vertex in alive_tables:
            terminals.add(("c", vertex))
    if not terminals:
        return set()
    while True:
        leaves = [node for node, neis in tree.items() if node not in terminals and len(neis) <= 1]
        if not leaves:
            break
        for node in leaves:
            for other in tree[node]:
                tree[other].discard(node)
            del tree[node]
    retained = set()
    for node in tree:
        if node[0] != "b":
            continue
        for vertex in blocks[node[1]]:
            if vertex not in alive_tables:
                retained.add(vertex)
    return retained


def _definite_filter_tables(directed, alive_tables):
    """Tables that sit strictly inside a simple directed path between two alive tables.

    One budget for the whole search, read from `_PATH_REASON_BUDGET` now. An extension is one
    step to a vertex not on the current path. Reaching another alive table marks the interior
    and does not end the path. The search stops once the budget cannot pay for the next extension.
    """
    budget = _PATH_REASON_BUDGET
    adj = {node: sorted(neis) for node, neis in directed.items()}
    definite = set()
    alive = set(alive_tables)
    if budget <= 0:
        return definite
    for start in sorted(alive):
        path = [start]
        on_path = {start}
        stack = [0]
        exhausted = False
        while stack:
            node = path[-1]
            neighbors = adj.get(node, ())
            index = stack[-1]
            if index >= len(neighbors):
                stack.pop()
                on_path.remove(path.pop())
                continue
            stack[-1] = index + 1
            nxt = neighbors[index]
            if nxt in on_path:
                continue
            if budget <= 0:
                exhausted = True
                break
            budget -= 1
            path.append(nxt)
            on_path.add(nxt)
            stack.append(0)
            if nxt in alive and nxt != start:
                definite.update(path[1:-1])
            if budget <= 0:
                exhausted = True
                break
        if exhausted:
            break
    return definite


def _components(undirected):
    seen = set()
    for start in sorted(undirected):
        if start in seen:
            continue
        comp = []
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            comp.append(node)
            for other in undirected[node]:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        yield comp


def _retain_filter_paths(graph, alive, locked, used, ext):
    """Lock non-alive tables on the active-filter block-cut between alive tables.

    `alive` is the fixpoint snapshot from the start of this iteration. A table already in it is
    left untouched, so an earlier reason is never revised. `locked` grows in place.
    """
    snapshot = set(alive)
    alive_tables = {k for k in snapshot if k in graph.objects and graph.objects[k].kind == "table"}
    undirected, directed = _active_relationship_graphs(graph)
    retained = set()
    for comp in _components(undirected):
        alive_here = alive_tables.intersection(comp)
        if len(alive_here) < 2:
            continue
        sub = {node: sorted(undirected[node]) for node in comp}
        retained |= _tables_on_alive_subtree(_biconnected_blocks(sub), alive_here)
    retained = {table for table in retained if table not in snapshot and table in graph.objects
                and graph.objects[table].kind == "table"}
    if not retained:
        return False
    definite = _definite_filter_tables(directed, alive_tables)
    added = False
    for table in sorted(retained):
        reason = _FILTER_PATH_DEFINITE if table in definite else _FILTER_PATH_MANUAL
        previous = list(graph.locks.get(table, ()))
        graph.lock(table, reason)
        if graph.locks.get(table, ()) != previous:
            added = True
    extra_locked = graph.closure(retained) - used - ext
    grew = bool(extra_locked - locked)
    locked |= extra_locked
    return grew or added


# ---------------------------------------------------------------- build


def bound_reports(ws, project, graph: Graph | None = None) -> list[tuple[str, str]]:
    """[(report name, mirror report dir)] for every mirrored report bound to this project's model by path.

    An unreadable or invalid definition.pbir marks the graph incomplete. The report is not treated as unused.
    """
    out = []
    if not os.path.isdir(ws.mirror):
        return out
    want = (project.name + ".SemanticModel").lower()
    for name in sorted(list_names(ws.mirror)):
        rdir = os.path.join(ws.mirror, name)
        if not (name.lower().endswith(".report") and os.path.isdir(rdir)):
            continue
        try:
            ref = json.loads(read_text(os.path.join(rdir, "definition.pbir"))[0])
        except (OSError, ValueError):
            ref = None
        if not isinstance(ref, dict):
            if graph is not None:
                graph.incomplete = True
                graph.warnings.append({"code": "UNREADABLE_REPORT_FILE", "report": name[:-7],
                                       "where": "definition.pbir"})
            continue
        raw_dataset = ref.get("datasetReference")
        if "datasetReference" in ref and not isinstance(raw_dataset, dict):
            if graph is not None:
                graph.incomplete = True
                graph.warnings.append({"code": "UNREADABLE_REPORT_FILE", "report": name[:-7],
                                       "where": "definition.pbir"})
            continue
        dataset = raw_dataset if isinstance(raw_dataset, dict) else {}
        by_path = dataset.get("byPath")
        path = by_path.get("path", "") if isinstance(by_path, dict) else ""
        if "byPath" in dataset and (not isinstance(by_path, dict) or not isinstance(path, str) or not path.strip()):
            if graph is not None:
                graph.incomplete = True
                graph.warnings.append({"code": "UNREADABLE_REPORT_FILE", "report": name[:-7],
                                       "where": "definition.pbir"})
            continue
        if isinstance(path, str) and path and os.path.basename(os.path.normpath(path.replace("\\", "/"))).lower() == want:
            out.append((name[:-7], rdir))
    return out


def build(ws, project, extra_reports=(), model_dir: str | None = None) -> Graph:
    mdef = model_dir or os.path.join(project.mirror_model, "definition")
    if not os.path.isdir(mdef):
        raise PbzError("NO_MIRROR", "run `pbz pull` first")
    model = tmdl.load_model(mdef, root=model_dir or os.path.dirname(project.mirror_model))
    g = Graph()
    g.model = model
    res = deps.Resolver(model)
    _model_objects(g, model)
    _model_edges(g, model, res)
    reports = bound_reports(ws, project, g) if ws is not None else []
    for rdir in extra_reports:
        rdir = os.path.abspath(rdir)
        if not os.path.isdir(os.path.join(rdir, "definition")):
            raise PbzError("BAD_REQUEST", "--report must be a *.Report folder")
        reports.append((os.path.basename(rdir).rsplit(".", 1)[0], rdir))
    for name, rdir in reports:
        g.reports.append(name)
        _report(g, model, res, name, rdir)
    if ws is not None and model_dir is None:
        from . import consumers
        consumers.apply(g, ws, project, model, res)
    g.compute_states()
    return g


def _model_objects(g: Graph, model) -> None:
    for t in model.tables.values():
        tn = t.node
        date_table = bool(tn and tn.props.get("dataCategory", "").strip().lower() == "time")
        g.add(Obj("table", "", t.name, t.file, tn.line if tn else 0, t.hidden, t.calculated, t.private,
                  extra={"date_table": date_table, "calc_group": bool(t.calc_items) or bool(
                      tn and any(c.kind == "calculationGroup" for c in tn.children))}))
        for c in t.columns.values():
            n = c.node
            g.add(Obj("column", t.name, c.name, t.file, n.line if n else 0, c.hidden, c.calculated, t.private,
                      extra={"data_type": c.data_type, "date_key": date_table and bool(n and n.flag("isKey")),
                             "source_column": c.source_column}))
        for m in t.measures.values():
            g.add(Obj("measure", t.name, m.name, t.file, m.node.line if m.node else 0,
                      bool(m.node and m.node.flag("isHidden")), extra={"folder": m.folder}))
        for h in (x for x in (tn.children if tn else []) if x.kind == "hierarchy"):
            g.add(Obj("hierarchy", t.name, h.name, t.file, h.line, h.flag("isHidden"), private=t.private))
        for ci, node in t.calc_items.items():
            g.add(Obj("calc_item", t.name, ci, t.file, node.line))
    for name, node in model.expressions.items():
        text = "".join(model.text_of(r) for r in tmdl.node_regions(model.docs[node.file], node, deep=False))
        g.add(Obj("m_query", "", name, node.file, node.line,
                  extra={"parameter": "isparameterquery" in text.replace(" ", "").lower()}))
    for name, node in model.functions.items():
        g.add(Obj("function", "", name, node.file, node.line))
    for name, node in model.role_nodes.items():
        g.add(Obj("role", "", name, node.file, node.line))
    for r in model.relationships:
        lab = f"{r.from_table}[{r.from_column}] -> {r.to_table}[{r.to_column}]"
        g.add(Obj("relationship", "", r.name, r.node.file if r.node else "", r.node.line if r.node else 0,
                  extra={"label": lab, "active": r.active, "both": r.both, "from_table": r.from_table,
                         "to_table": r.to_table, "from_column": r.from_column, "to_column": r.to_column}))


def _dax_edges(g: Graph, src: tuple, model, res, node, home: str, via: str, row_home: bool = False,
               function: bool = False, lock_reason: str = "", deep: bool = True) -> list[deps.Ref]:
    doc = model.docs[node.file]
    refs = []
    for reg in tmdl.node_regions(doc, node, deep=deep):
        if reg.kind != "dax":
            continue
        d = deps.dax_bindings(doc.text[reg.start:reg.end], home, res, row_home, function, base=reg.start)
        for ref in d.refs:
            k = _ref_key(ref)
            if k is None:
                continue
            g.edge(src, k, via)
            if lock_reason:
                g.lock(k, lock_reason)
        for fn, a, b in d.relationships:
            for r in model.relationships:
                ends = {(r.from_table.lower(), r.from_column.lower()), (r.to_table.lower(), r.to_column.lower())}
                if {(a[0].lower(), a[1].lower()), (b[0].lower(), b[1].lower())} == ends:
                    g.edge(src, key("relationship", "", r.name), fn.lower())
        if src in g.objects:
            g.objects[src].issues += sorted({f"{u.table}[{u.name}]" if u.table else f"[{u.name}]"
                                             for u in d.unresolved} - set(g.objects[src].issues))
        refs += d.refs
    return refs


def _ref_key(ref: deps.Ref) -> tuple | None:
    if ref.kind == "column":
        return key("column", ref.table, ref.name)
    if ref.kind == "measure":
        return key("measure", ref.table, ref.name)
    if ref.kind == "table":
        return key("table", "", ref.name)
    if ref.kind == "function":
        return key("function", "", ref.name)
    if ref.kind == "m_query":
        return key("m_query", "", ref.name)
    return None


def _m_edges(g: Graph, src: tuple, model, res, node, own: str = "", lock_reason: str = "") -> None:
    doc = model.docs[node.file]
    seen = {edge.dst for edge in g.out.get(src, []) if edge.via == "m"}
    for reg in tmdl.node_regions(doc, node, deep=True):
        if reg.kind != "m":
            continue
        for binding in deps.m_bindings(doc.text[reg.start:reg.end], res, own):
            k = _ref_key(binding.ref)
            if k is not None and binding.ref.kind == "m_query" and k not in g.objects:
                alt = key("table", "", binding.ref.name)
                if alt in g.objects:
                    k = alt
            if k is None or k == src:
                continue
            if binding.uncertain:
                g.lock(k, "uncertain M binding: automatic removal disabled")
            if k not in seen:
                g.edge(src, k, "m")
                seen.add(k)
            if lock_reason:
                g.lock(k, lock_reason)


def _model_edges(g: Graph, model, res) -> None:
    mres = deps.Resolver(model)
    mres.expressions.update({name: name for name in model.tables})
    for t in model.tables.values():
        tk = key("table", "", t.name)
        tn = t.node
        for c in t.columns.values():
            ck = key("column", t.name, c.name)
            g.edge(ck, tk, "home")
            if c.node is None:
                continue
            if c.calculated:
                _dax_edges(g, ck, model, res, c.node, t.name, "dax", row_home=True)
            if c.sort_by:
                g.edge(ck, key("column", t.name, c.sort_by), "sortBy")
            for ch in c.node.children:
                if ch.kind == "variation":
                    dh = ch.prop("defaultHierarchy")
                    if dh:
                        g.edge(ck, key("table", "", tmdl.split_dotted(dh)[0]), "variation")
                elif ch.kind == "alternateOf":
                    base = tmdl.split_dotted(ch.prop("baseColumn"))
                    if len(base) == 2:
                        g.edge(ck, key("column", base[0], base[1]), "alternateOf")
                    g.lock(ck, "aggregation (alternateOf)")
                    g.lock(tk, "aggregation table")
        for m in t.measures.values():
            mk = key("measure", t.name, m.name)
            g.edge(mk, tk, "home")
            if m.node is not None:
                _dax_edges(g, mk, model, res, m.node, t.name, "dax")
        for h in (x for x in (tn.children if tn else []) if x.kind == "hierarchy"):
            hk = key("hierarchy", t.name, h.name)
            g.edge(hk, tk, "home")
            for lv in h.children:
                if lv.kind == "level" and lv.prop("column"):
                    g.edge(hk, key("column", t.name, lv.prop("column")), "level")
        for ci, node in t.calc_items.items():
            cik = key("calc_item", t.name, ci)
            g.edge(cik, tk, "home")
            g.edge(tk, cik, "calc_item")
            _dax_edges(g, cik, model, res, node, t.name, "dax")
        for p in t.partition_nodes:
            kind = p.decl_value.strip().lower() or "m"
            if kind == "calculated":
                _dax_edges(g, tk, model, res, p, t.name, "partition")
            else:
                _m_edges(g, tk, model, mres, p)
                src = p.prop("expressionSource")
                if src:
                    g.edge(tk, key("m_query", "", src), "expressionSource")
        for ch in (tn.children if tn else []):
            if ch.kind == "refreshPolicy":
                _m_edges(g, tk, model, mres, ch, lock_reason="incremental refresh policy")
                g.lock(tk, "incremental refresh policy")
            elif ch.kind in ("calendar",):
                for d in ch.descendants():
                    for pv in d.props.values():
                        parts = tmdl.split_dotted(pv.strip())
                        col = parts[-1] if parts else ""
                        if col and res.column(t.name, col):
                            g.lock(key("column", t.name, col), "calendar column group")
    for name, node in model.expressions.items():
        _m_edges(g, key("m_query", "", name), model, mres, node, own=name)
    for name, node in model.functions.items():
        _dax_edges(g, key("function", "", name), model, res, node, "", "dax", function=True)
    for name, node in model.role_nodes.items():
        rk = key("role", "", name)
        g.lock(rk, "security role")
        for tp in (x for x in node.children if x.kind == "tablePermission"):
            tab = res.table(tp.name) or tp.name
            g.edge(rk, key("table", "", tab), "rls")
            _dax_edges(g, rk, model, res, tp, tab, "rls", row_home=True, lock_reason=f"RLS filter in role {name}",
                       deep=False)
            for cp in (x for x in tp.children if x.kind == "columnPermission"):
                g.lock(key("column", tab, cp.name), f"object-level security in role {name}")
    for r in model.relationships:
        rk = key("relationship", "", r.name)
        g.edge(rk, key("column", r.from_table, r.from_column), "relationship")
        g.edge(rk, key("column", r.to_table, r.to_column), "relationship")


# ---------------------------------------------------------------- reports


def _file_kind(rel: str) -> str:
    parts = rel.split("/")
    low = rel.lower()
    if low == "report.json":
        return "report"
    if low == "reportextensions.json":
        return "extensions"
    if parts[0] == "pages" and len(parts) == 3 and parts[2] == "page.json":
        return "page"
    if parts[0] == "pages" and len(parts) == 5 and parts[2] == "visuals":
        return "visual" if parts[4] == "visual.json" else ("mobile" if parts[4] == "mobile.json" else "other")
    if parts[0] == "bookmarks" and low.endswith(".bookmark.json"):
        return "bookmark"
    return "meta"


def _via(kind: str, path: tuple) -> str:
    if kind in ("bookmark", "mobile"):
        return kind
    if "filterConfig" in path or "filters" in path:
        return "filter:" + {"page": "page", "visual": "visual", "report": "report"}.get(kind, kind)
    if "queryState" in path:
        i = path.index("queryState")
        return "projection:" + (path[i + 1] if i + 1 < len(path) else "?")
    if "sortDefinition" in path:
        return "sort"
    if "expansionStates" in path:
        return "slicer_state"
    if "objects" in path or "visualContainerObjects" in path:
        return "formatting"
    return "other"


def _s(node) -> str:
    return node.value if isinstance(node, pbir.JStr) else ""


def _literal_strings(root, keys: set[str]) -> list[str]:
    """Decoded string literals sitting under any of `keys` (navigation targets, bookmark names...)."""
    out = []

    def visit(node, under):
        if isinstance(node, pbir.JArr):
            for v in node.items:
                visit(v, under)
        elif isinstance(node, pbir.JObj):
            lit = node.get("Literal")
            if under and isinstance(lit, pbir.JObj) and isinstance(lit.get("Value"), pbir.JStr):
                val, vtype = pbir.decode_literal(lit.get("Value").value)
                if vtype == "string":
                    out.append(val)
                return
            for k, v in node.pairs:
                visit(v, under or k.value in keys)
        elif isinstance(node, pbir.JStr) and under:
            out.append(node.value)

    visit(root, False)
    return out


def _report(g: Graph, model, res, rname: str, rdir: str) -> None:
    ddir = os.path.join(rdir, "definition")
    files = []
    for path in pbir.report_files(rdir):
        rel = os.path.relpath(path, ddir).replace("\\", "/")
        try:
            files.append((rel, pbir.parse_json(read_definition_text(path)[0])))
        except (OSError, UnicodeDecodeError, pbir.JsonError, IndexError):
            g.warnings.append({"code": "UNREADABLE_REPORT_FILE", "report": rname, "where": rel})
            g.incomplete = True
    pages = {}
    for rel, root in files:
        if _file_kind(rel) == "page":
            pid = rel.split("/")[1]
            disp = _s(root.get("displayName")) or pid
            binding = root.get("pageBinding")
            ptype = _s(binding.get("type")) if isinstance(binding, pbir.JObj) else ""
            pages[pid] = {"kind": "page", "report": rname, "id": pid, "name": disp,
                          "hidden": _s(root.get("visibility")) == "HiddenInViewMode",
                          "type": (ptype or _s(root.get("type")) or "").lower(), "visuals": 0, "file": rel}
    ext_measures = {}
    targets = set()
    nav_all_bookmarks, nav_groups = False, set()
    custom_declared, visual_types = set(), {}
    for rel, root in files:
        kind = _file_kind(rel)
        if kind == "extensions":
            for ent in (root.get("entities").items if isinstance(root.get("entities"), pbir.JArr) else []):
                ename = _s(ent.get("name")) if isinstance(ent, pbir.JObj) else ""
                for mm in (ent.get("measures").items if isinstance(ent, pbir.JObj) and isinstance(ent.get("measures"), pbir.JArr) else []):
                    mname = _s(mm.get("name")) if isinstance(mm, pbir.JObj) else ""
                    if not mname:
                        continue
                    o = g.add(Obj("report_measure", ename, mname, f"{rname}.Report/definition/{rel}",
                                  extra={"report": rname}))
                    ext_measures[mname.lower()] = o.key
                    d = deps.dax(_s(mm.get("expression")), ename, res)
                    for ref in d.refs:
                        k = _ref_key(ref)
                        if k is not None:
                            g.edge(o.key, k, "dax")
                    o.issues += [f"[{u.name}]" for u in d.unresolved if u.name.lower() not in ext_measures]
        elif kind == "report":
            pcv = root.get("publicCustomVisuals")
            custom_declared |= {_s(x) for x in (pcv.items if isinstance(pcv, pbir.JArr) else []) if _s(x)}
    # report measures may reference each other: second pass now that all are known
    for rel, root in files:
        if _file_kind(rel) != "extensions":
            continue
        for ent in (root.get("entities").items if isinstance(root.get("entities"), pbir.JArr) else []):
            for mm in (ent.get("measures").items if isinstance(ent, pbir.JObj) and isinstance(ent.get("measures"), pbir.JArr) else []):
                src = ext_measures.get(_s(mm.get("name")).lower()) if isinstance(mm, pbir.JObj) else None
                if src is None:
                    continue
                for other, ok in ext_measures.items():
                    if ok != src and f"[{other}]" in _s(mm.get("expression")).lower():
                        g.edge(src, ok, "dax")
                g.objects[src].issues = [i for i in g.objects[src].issues if i[1:-1].lower() not in ext_measures]
    for rel, root in files:
        kind = _file_kind(rel)
        parts = rel.split("/")
        page = pages.get(parts[1], {}).get("name", parts[1]) if parts[0] == "pages" and len(parts) > 1 else ""
        visual = parts[3] if kind in ("visual", "mobile") else ""
        if kind == "visual":
            v = root.get("visual")
            vt = _s(v.get("visualType")) if isinstance(v, pbir.JObj) else ("group" if "visualGroup" in root else "")
            if parts[1] in pages:
                pages[parts[1]]["visuals"] += 1
            visual_types[vt] = visual_types.get(vt, 0) + 1
            g.report_objects.append({"kind": "visual", "report": rname, "page": page, "id": visual, "type": vt,
                                     "hidden": _s(root.get("isHidden")) == "true" or (
                                         isinstance(root.get("isHidden"), pbir.JLit) and root.get("isHidden").raw == "true"),
                                     "file": rel})
            if vt == "bookmarkNavigator":
                groups = _literal_strings(root, {"bookmarkGroup"})
                if groups:
                    nav_groups |= set(groups)
                else:
                    nav_all_bookmarks = True
            targets |= set(_literal_strings(root, {"bookmark", "navigationSection", "drillthroughSection",
                                                   "section", "pageName", "destination"}))
        elif kind == "bookmark":
            g.report_objects.append({"kind": "bookmark", "report": rname, "id": _s(root.get("name")) or parts[-1],
                                     "name": _s(root.get("displayName")), "file": rel})
        uses = pbir.walk_fields_with_path(root, model)
        for f, path in uses:
            via = _via(kind, path)
            u = Usage(rname, page, visual, via, rel)
            if f.kind == "measure":
                hit = res.measure(f.name)
                if hit:
                    g.use(key("measure", hit[0], hit[1]), u)
                elif f.name.lower() in ext_measures:
                    g.use(ext_measures[f.name.lower()], u)
                else:
                    g.warnings.append({"code": "UNKNOWN_FIELD", "report": rname, "where": rel,
                                       "field": f"[{f.name}]"})
            elif f.kind == "hierarchy":
                tab = res.table(f.table)
                if tab and f.hierarchy and key("hierarchy", tab, f.hierarchy) in g.objects:
                    g.use(key("hierarchy", tab, f.hierarchy), u)
                if tab and res.column(tab, f.name):
                    g.use(key("column", tab, res.column(tab, f.name)), u)
                elif not tab:
                    g.warnings.append({"code": "UNKNOWN_FIELD", "report": rname, "where": rel, "field": f.table})
            else:
                tab = res.table(f.table)
                col = res.column(tab, f.name) if tab else None
                if col:
                    g.use(key("column", tab, col), u)
                elif res.measure(f.name):
                    mt, mn = res.measure(f.name)
                    g.use(key("measure", mt, mn), u)
                elif f.name.lower() in ext_measures:
                    g.use(ext_measures[f.name.lower()], u)
                else:
                    g.warnings.append({"code": "UNKNOWN_FIELD", "report": rname, "where": rel,
                                       "field": f"{f.table}[{f.name}]"})
    for p in pages.values():
        p["referenced"] = p["id"] in targets or p["name"] in targets
        g.report_objects.append(p)
    in_bookmarks = set()   # visuals a bookmark captures (it may show a hidden visual)
    for rel, root in files:
        if _file_kind(rel) != "bookmark":
            continue
        opts = root.get("options")
        tv = opts.get("targetVisualNames") if isinstance(opts, pbir.JObj) else None
        in_bookmarks |= {_s(x) for x in (tv.items if isinstance(tv, pbir.JArr) else [])}

        def containers(node):
            if isinstance(node, pbir.JObj):
                for k, v in node.pairs:
                    if k.value == "visualContainers" and isinstance(v, pbir.JObj):
                        in_bookmarks.update(kk.value for kk, _ in v.pairs)
                    containers(v)
            elif isinstance(node, pbir.JArr):
                for v in node.items:
                    containers(v)

        containers(root.get("explorationState"))
    for o in g.report_objects:
        if o["kind"] == "visual" and o["report"] == rname:
            o["in_bookmark"] = o["id"] in in_bookmarks
    groups = {}
    for rel, root in files:
        if rel == "bookmarks/bookmarks.json":
            for it in (root.get("items").items if isinstance(root.get("items"), pbir.JArr) else []):
                if isinstance(it, pbir.JObj) and isinstance(it.get("children"), pbir.JArr):
                    groups[_s(it.get("displayName")) or _s(it.get("name"))] = [_s(c) for c in it.get("children").items]
    in_nav_group = {b for gname, kids in groups.items() if gname in nav_groups for b in kids}
    for o in g.report_objects:
        if o["kind"] == "bookmark" and o["report"] == rname:
            o["referenced"] = nav_all_bookmarks or o["id"] in targets or o["id"] in in_nav_group
    for cv in sorted(custom_declared):
        g.report_objects.append({"kind": "custom_visual", "report": rname, "id": cv,
                                 "referenced": visual_types.get(cv, 0) > 0})
