"""Scan-time normalization core.

Each scan record carries a naive local time plus an IANA timezone. According to
tz rules a local time maps to:

* exactly one UTC instant  - the common case;
* two distinct UTC instants - the local time is repeated when DST falls back;
* no UTC instant at all     - the local time is skipped when DST springs forward.

Transfer links constrain the UTC duration between two records to a closed
interval ``[min_seconds, max_seconds]`` and all links must hold simultaneously.
Because every record owns at most two candidates, joint feasibility is a 2-SAT
instance: a boolean variable per ambiguous record and one clause per forbidden
candidate combination. That lets us answer exactly - not heuristically - which
candidates of a record can appear in at least one globally consistent
interpretation, without ever picking one arbitrarily.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Union
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utc_candidates(local: datetime, tz_name: str) -> list[datetime]:
    """Sorted, distinct UTC instants whose reading in ``tz_name`` equals ``local``.

    ``fold=0/1`` covers the fall-back duplicate; the round-trip check discards
    imaginary spring-forward times, so the result has 0, 1 or 2 entries.
    """
    tz = ZoneInfo(tz_name)
    found: set[datetime] = set()
    for fold in (0, 1):
        aware = local.replace(tzinfo=tz, fold=fold)
        instant = aware.astimezone(UTC)
        if instant.astimezone(tz).replace(tzinfo=None) == local:
            found.add(instant)
    return sorted(found)


def _tarjan_scc(graph: list[list[int]]) -> list[int]:
    """Iterative Tarjan SCC; returns the component id of every node."""
    n = len(graph)
    index = [-1] * n
    lowlink = [0] * n
    on_stack = [False] * n
    comp = [-1] * n
    stack: list[int] = []
    counter = 0
    ncomp = 0

    for root in range(n):
        if index[root] != -1:
            continue
        work = [(root, 0)]
        while work:
            v, child_i = work[-1]
            if child_i == 0:
                index[v] = lowlink[v] = counter
                counter += 1
                stack.append(v)
                on_stack[v] = True
            if child_i < len(graph[v]):
                work[-1] = (v, child_i + 1)
                w = graph[v][child_i]
                if index[w] == -1:
                    work.append((w, 0))
                elif on_stack[w]:
                    lowlink[v] = min(lowlink[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
                if lowlink[v] == index[v]:
                    while True:
                        w = stack.pop()
                        on_stack[w] = False
                        comp[w] = ncomp
                        if w == v:
                            break
                    ncomp += 1
    return comp


class _TwoSAT:
    """2-SAT checker. Literal encoding: ``lit = 2 * var + value``; ``lit ^ 1`` negates."""

    def __init__(self, num_vars: int, clauses: list[tuple[int, int]]):
        self.num_vars = num_vars
        self.graph: list[list[int]] = [[] for _ in range(2 * num_vars)]
        for a, b in clauses:
            self.graph[a ^ 1].append(b)
            self.graph[b ^ 1].append(a)

    def satisfiable(self, assumptions: tuple[int, ...] = ()) -> bool:
        if assumptions:
            graph = [edges[:] for edges in self.graph]
            for lit in assumptions:  # unit clause (lit OR lit)
                graph[lit ^ 1].append(lit)
        else:
            graph = self.graph
        comp = _tarjan_scc(graph)
        return all(comp[2 * v] != comp[2 * v + 1] for v in range(self.num_vars))


@dataclass(frozen=True)
class ScanInput:
    id: str
    local_time: datetime  # naive, whole-second precision
    timezone: str


@dataclass(frozen=True)
class LinkInput:
    from_id: str
    to_id: str
    min_seconds: int
    max_seconds: int


@dataclass
class RecordResult:
    id: str
    status: str  # "determined" | "needs_review"
    candidates: list[datetime]  # UTC instants that appear in some global solution


@dataclass
class Ok:
    results: list[RecordResult]


@dataclass
class NoLegalCandidate:
    """Records whose local time has no legal UTC reading (DST gap)."""

    records: list[ScanInput]


@dataclass
class NoGlobalSolution:
    """Constraints are mutually inconsistent; holds an irreducible link subset."""

    link_indices: list[int]
    record_ids: list[str]


Outcome = Union[Ok, NoLegalCandidate, NoGlobalSolution]


def normalize(records: list[ScanInput], links: list[LinkInput]) -> Outcome:
    """Resolve every record's feasible UTC candidates under all links at once."""
    candidates: dict[str, list[datetime]] = {}
    no_candidate: list[ScanInput] = []
    for r in records:
        cands = utc_candidates(r.local_time, r.timezone)
        candidates[r.id] = cands
        if not cands:
            no_candidate.append(r)
    if no_candidate:
        return NoLegalCandidate(no_candidate)

    # One boolean variable per ambiguous (two-candidate) record.
    var_of: dict[str, int] = {}
    for r in records:
        if len(candidates[r.id]) == 2:
            var_of[r.id] = len(var_of)

    # Translate every link into clauses forbidding out-of-interval combinations.
    per_link_clauses: list[list[tuple[int, int]]] = []
    self_unsat: set[int] = set()  # links violated by two fixed records
    for idx, link in enumerate(links):
        clauses: list[tuple[int, int]] = []
        for i, t_from in enumerate(candidates[link.from_id]):
            for j, t_to in enumerate(candidates[link.to_id]):
                delta = (t_to - t_from).total_seconds()
                if link.min_seconds <= delta <= link.max_seconds:
                    continue
                a_var = var_of.get(link.from_id)
                b_var = var_of.get(link.to_id)
                if a_var is not None and b_var is not None:
                    clauses.append(((2 * a_var + i) ^ 1, (2 * b_var + j) ^ 1))
                elif a_var is not None:
                    unit = (2 * a_var + i) ^ 1
                    clauses.append((unit, unit))
                elif b_var is not None:
                    unit = (2 * b_var + j) ^ 1
                    clauses.append((unit, unit))
                else:
                    self_unsat.add(idx)
        per_link_clauses.append(clauses)

    def subset_satisfiable(link_indices: list[int]) -> bool:
        clauses: list[tuple[int, int]] = []
        for i in link_indices:
            if i in self_unsat:
                return False
            clauses.extend(per_link_clauses[i])
        return _TwoSAT(len(var_of), clauses).satisfiable()

    all_indices = list(range(len(links)))
    if not subset_satisfiable(all_indices):
        # Greedy deletion -> irreducible inconsistent subset, for a precise reason.
        core = all_indices
        for i in all_indices:
            trial = [x for x in core if x != i]
            if not subset_satisfiable(trial):
                core = trial
        involved = sorted({links[i].from_id for i in core} | {links[i].to_id for i in core})
        return NoGlobalSolution(link_indices=core, record_ids=involved)

    engine = _TwoSAT(len(var_of), [c for cls in per_link_clauses for c in cls])
    results: list[RecordResult] = []
    for r in records:
        cands = candidates[r.id]
        if len(cands) == 1:
            feasible = cands  # overall SAT, so the sole candidate is reachable
        else:
            v = var_of[r.id]
            feasible = [
                c for value, c in enumerate(cands)
                if engine.satisfiable((2 * v + value,))
            ]
        results.append(
            RecordResult(
                id=r.id,
                status="determined" if len(feasible) == 1 else "needs_review",
                candidates=feasible,
            )
        )
    return Ok(results)
