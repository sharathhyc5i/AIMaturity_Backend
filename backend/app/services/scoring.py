"""Level scoring — a direct port of the workflow's Scoring node.

Rule: a dimension scores the highest level met in sequence from Level 1.
A level is met when the weighted evidence of its maturity questions reaches the threshold.
Not yet checked (Check pending), Not assessed – access blocked (Access blocked), N/A and
competitive questions are left out of the denominator.
"""
from dataclasses import dataclass, field
from math import floor

DEFAULT_WEIGHTS = {"Yes": 1.0, "Partial": 0.5, "Issue": 0.5, "No": 0.0, "Not disclosed": 0.0}
DEFAULT_THRESHOLD = 35.0
# Statuses that never count toward a score, whatever the weights say
UNSCORED = ("Check pending", "Access blocked", "N/A")
STATUS_ORDER = ["Yes", "Partial", "Issue", "No", "Not disclosed", "Access blocked", "Check pending", "N/A"]
# D4 merges operating model and measurement: these reported-result questions decide its H flag
D4_RESULT_QIDS = {"D4-15", "D4-16", "D4-17", "D4-18", "D4-20", "D4-23"}


@dataclass
class Q:
    qid: str
    dimension: str
    level: int | None
    lens: str
    status: str
    sources: list[str] = field(default_factory=list)
    # The result's own 0-1 score, when it has one (switches the dimension to the average rule)
    score: float | None = None


def stage_for(level: int) -> str:
    if level >= 4:
        return "Run"
    if level == 3:
        return "Walk"
    if level >= 1:
        return "Crawl"
    return "—"


def gate_word(c: dict) -> str:
    """The scorecard word for one level: Met, Not met, Not disclosed, Unknown, or blank.

    Inferred levels count as Met. Levels with no questions (ceiling) and levels above
    the one a dimension stopped at are left blank, as in the scorecard.
    """
    r = c["result"]
    if r in ("Met", "Inferred"):
        return "Met"
    if r == "Stops here":
        return "Not disclosed" if c["not_disclosed"] >= c["against"] else "Not met"
    if r == "No answered questions":
        return "Unknown"
    return ""


def score_dimension(dim: str, qs: list[Q], weights: dict | None = None, threshold: float = DEFAULT_THRESHOLD,
                    infer: bool = True, old_sources: set[str] | None = None, max_level: int = 5) -> dict:
    W = weights or DEFAULT_WEIGHTS
    old = old_sources or set()
    mat = [q for q in qs if q.dimension == dim and q.lens == "Maturity" and q.level]

    cells = []
    top = 0
    for L in range(1, max_level + 1):
        at = [q for q in mat if q.level == L]
        counted = [q for q in at if q.status in W and q.status not in UNSCORED]
        s = sum(W[q.status] for q in counted)
        tested = bool(at)
        if tested:
            top = L
        cells.append(dict(level=L, tested=tested, n=len(counted), total=len(at), weighted=s,
                          pct=(s / len(counted) if counted else None), inferred=False,
                          yes=sum(q.status == "Yes" for q in at),
                          other=sum(q.status in ("Partial", "Issue", "No") for q in at),
                          not_disclosed=sum(q.status == "Not disclosed" for q in at),
                          against=sum(q.status in ("Partial", "Issue", "No") for q in at),
                          qs=counted))

    lv = 0
    for i, c in enumerate(cells):
        if c["pct"] is None:
            if infer and not c["tested"] and i + 1 < top:
                c["inferred"] = True
                continue
            break
        if c["pct"] * 100 >= threshold:
            lv = i + 1
        else:
            break

    def all_old(q: Q) -> bool:
        return bool(q.sources) and all(s in old for s in q.sources)

    h = False
    if lv:
        ys = [q for q in cells[lv - 1]["qs"] if q.status == "Yes"]
        h = bool(ys) and all(all_old(q) for q in ys)
        if dim == "D4":
            pos = [q for q in mat if q.qid in D4_RESULT_QIDS and q.level <= lv and q.status in ("Yes", "Partial")]
            if pos and all(all_old(q) for q in pos):
                h = True

    nxt = cells[lv] if lv < max_level else None
    frac = 0.0
    if nxt and nxt["tested"] and nxt["pct"] is not None:
        frac = min(floor(nxt["pct"] * 100 / threshold * 100) / 100, 0.99)
    num = f"L{lv + frac:.2f}"
    label = (num + (" H" if h else "")) if lv else "Not established"

    # Result per cell, as the workflow describes it
    for c in cells:
        L = c["level"]
        if not c["tested"]:
            c["result"] = "Inferred" if L <= lv else ("Ceiling" if L > top else "No questions")
        elif c["pct"] is None:
            c["result"] = "No answered questions"
        elif L <= lv:
            c["result"] = "Met"
        elif L == lv + 1:
            c["result"] = "Stops here"
        else:
            c["result"] = "Not reached"
        c["gate"] = gate_word(c)
        c["pct"] = None if c["pct"] is None else round(c["pct"] * 100, 1)
        c.pop("qs")
        c.pop("against")

    return dict(dimension=dim, level=lv, fraction=frac, value=round(lv + frac, 2), number=num, h=h, label=label,
                key=(f"L{lv}" + (" H" if h else "")) if lv else "Not established",
                stage=stage_for(lv) if lv else "Not established", ceiling=top, cells=cells)


def score_dimension_average(dim: str, qs: list[Q], max_level: int = 5) -> dict:
    """The average rule, used when results carry their own 0-1 scores (the 6 Nov 2026 dry run).

    Every question counts, whatever its lens; N/A (no score) is left out. Each level's pct is the
    average of its questions; the dimension scores the average of all its questions × 5.
    """
    inq = [q for q in qs if q.dimension == dim]

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return sum(xs) / len(xs) if xs else None

    a = avg([q.score for q in inq])
    value = round((a or 0) * 5, 2)
    lv = int(floor(value))
    frac = round(value - lv, 2)
    cells = []
    for L in range(1, max_level + 1):
        at = [q for q in inq if q.level == L]
        s = [q.score for q in at if q.score is not None]
        la = avg(s)
        result = "Ceiling" if not at else "No answered questions" if la is None else "Met" if L <= lv else "Not reached"
        cells.append(dict(level=L, tested=bool(at), n=len(s), total=len(at), weighted=sum(s),
                          pct=None if la is None else round(la * 100, 1), inferred=False,
                          yes=sum(1 for x in s if x >= 1), other=sum(1 for x in s if 0 <= x < 1), not_disclosed=0,
                          result=result, gate="Met" if result == "Met" else ""))
    num = f"L{value:.2f}"
    stage = stage_for(lv) if lv else "Crawl"
    return dict(dimension=dim, level=lv, fraction=frac, value=value, number=num, h=False, label=num,
                key=f"L{lv}", stage=stage, ceiling=max(c["level"] for c in cells if c["tested"]) if any(c["tested"] for c in cells) else 0,
                cells=cells, average=None if a is None else round(a, 4))


def score_all(dims: list[str], qs: list[Q], **kw) -> list[dict]:
    # Results with their own scores (the dry run) use the average rule; others use the level rule
    if any(q.score is not None for q in qs):
        return [score_dimension_average(d, qs, kw.get("max_level", 5)) for d in dims]
    return [score_dimension(d, qs, **kw) for d in dims]