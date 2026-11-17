"""Actionable decision pages: everything here is derived from the assessment, not hard-coded."""
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import require_admin
from ..db import get_db
from ..services.context import Ctx, build_ctx, scoring_settings
from .overview import filters

router = APIRouter(prefix="/api/actions", tags=["actions"])
# Evidence gaps, next actions, unpublished questions and caveats are admin (internal) views
Admin = [Depends(require_admin)]


def _cells(db: Session) -> dict[str, m.LevelCell]:
    return {f"{c.dimension}-L{c.level}": c for c in db.scalars(select(m.LevelCell))}


def _qtext(ctx: Ctx) -> dict[str, str]:
    return {q.qid: q.text for q in ctx.questions}


def _reason(ctx: Ctx, s: dict, threshold: float = 35) -> str:
    real = ctx.assessment.data_source == "workflow"
    if real and s["dimension"] in ctx.guide.get("stop_reasons", {}):
        return ctx.guide["stop_reasons"][s["dimension"]]
    L = s["level"] + 1
    if L > len(s["cells"]):
        return "Every level is met."
    c = s["cells"][L - 1]
    if not c["tested"]:
        return f"Level {L} is not tested: the question set has no maturity question there, so L{s['level']} is the ceiling."
    if c["pct"] is None:
        return f"Level {L} has no answered questions yet: its public-source checks are still pending."
    return f"Level {L} not met: weighted evidence {round(c['pct'])}% is below the {threshold:.0f}% threshold."


@router.get("/capabilities")
def capabilities(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    rows = [r for r in ctx.rows() if r["in_run"]]
    ev = [e.data for e in ctx.assessment.evidence]
    live = [e for e in ev if e["stage"] in ("Live", "Pilot", "Measured") and e["result"] == "Pass"]
    proven = []
    for d in [sc['dimension'] for sc in ctx.scores]:
        yes = [r for r in rows if r["dimension"] == d and r["status"] == "Yes"]
        partial = [r for r in rows if r["dimension"] == d and r["status"] == "Partial"]
        themes = defaultdict(lambda: dict(yes=0, partial=0, qids=[]))
        for r in yes + partial:
            t = themes[r["sub_theme"]]
            t["yes" if r["status"] == "Yes" else "partial"] += 1
            t["qids"].append(r["qid"])
        proven.append(dict(dimension=d, yes=len(yes), partial=len(partial),
                           themes=sorted([dict(name=k, **v) for k, v in themes.items()], key=lambda x: (-x["yes"], -x["partial"])),
                           highlights=[dict(qid=r["qid"], text=r["text"], finding=r["finding"], level=r["level"], lens=r["lens"], sources=r["sources"])
                                       for r in sorted(yes, key=lambda r: -(r["level"] or 0))[:5]]))
    return dict(info=ctx.info(), live=[dict(eid=e["eid"], title=e["title"], claim=e["claim"], stage=e["stage"], cell=e["cell"],
                                            channel=e["channel"], use_case=e["use_case"], confidence=e["confidence"], h_flag=e["h_flag"],
                                            sources=e.get("sources", []), limits=e["limits"]) for e in live],
                by_dimension=proven, scores=ctx.scores)


@router.get("/evidence-gaps", dependencies=Admin)
def evidence_gaps(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    qt = _qtext(ctx)
    cells = _cells(db)
    flags = [dict(area=fl.area, kind=fl.kind, severity=fl.severity, text=fl.text,
                  questions=[dict(qid=q, text=qt.get(q, "")) for q in fl.qids]) for fl in ctx.assessment.flags]
    # Missing evidence at the level where each dimension stopped (the human-review queue)
    queue = []
    for s in ctx.scores:
        L = s["level"] + 1
        if L > len(s["cells"]):
            continue
        cell = cells.get(f"{s['dimension']}-L{L}")
        if not cell:
            continue
        c = s["cells"][L - 1]
        missing = [dict(qid=q.qid, text=q.text, status=ctx.results[q.qid].status)
                   for q in ctx.questions if q.dimension == s["dimension"] and q.level == L and q.lens == "Maturity"
                   and q.qid in ctx.results and ctx.results[q.qid].status in ("Not disclosed", "Check pending", "Access blocked")]
        queue.append(dict(dimension=s["dimension"], level=L, cell=f"{s['dimension']}-L{L}", name=cell.name, score=s["label"],
                          testable=c["tested"], pct=c["pct"], threshold=scoring_settings(db)["threshold"], requirement=cell.threshold, missing=missing))
    register = [dict(eid=e.eid, title=e.data["title"], status=e.register_status, note=e.data["register_note"], freshness=e.data["freshness"],
                     confidence=e.confidence, limits=e.data["limits"])
                for e in ctx.assessment.evidence if e.register_status not in ("Accepted",)]
    answered = [r for r in ctx.rows() if r["in_run"] and r["status"] in ("Yes", "Partial", "Issue", "No")]
    return dict(info=ctx.info(), flags=flags, queue=queue, register=register,
                stats=dict(flags=len(flags), unsourced=sum(1 for r in answered if not r["sources"]),
                           pending=sum(1 for r in ctx.rows() if r["status"] == "Check pending"),
                           blocked=sum(1 for r in ctx.rows() if r["status"] == "Access blocked"),
                           missing_at_stop=sum(len(q["missing"]) for q in queue)))


@router.get("/next-actions", dependencies=Admin)
def next_actions(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    a = ctx.assessment
    cells = _cells(db)
    actions = []
    for e in a.evidence:
        d = e.data
        if d["val_disposition"] != "Accepted":
            actions.append(dict(kind="Validate evidence", priority="High" if e.register_status in ("Adjudication", "Needs refresh") else "Medium",
                                title=d["val_proof"], detail=f"{d['eid']} · {d['title']}", cell=e.cell, owner=d["val_owner"],
                                reviewer=d["val_reviewer"], due=d["val_due"], status=d["val_disposition"], qids=d.get("questions", [])))
    for fl in a.flags:
        if fl.severity == "error":
            actions.append(dict(kind="Fix a validation flag", priority="High", title=fl.text, detail=fl.kind, cell=None, owner="Assessor",
                                reviewer="QA reviewer", due=(a.run_date + timedelta(days=7)).isoformat(), status="Open", qids=fl.qids))
    for d in [sc['dimension'] for sc in ctx.scores]:
        pend = [r.qid for q, r in ((q, ctx.results.get(q.qid)) for q in ctx.questions) if r and q.dimension == d and r.status == "Check pending"]
        # (Not yet checked: runs in the next cycle)
        if pend:
            actions.append(dict(kind="Run pending checks", priority="Medium", rating="pending", title=f"Run {len(pend)} pending public-source checks in {d}",
                                detail="Queued for the next agent run on the v4 set.", cell=None, owner="Agent run",
                                reviewer="QA reviewer", due=(a.run_date + timedelta(days=30)).isoformat(), status="Queued", qids=pend))
    # Ratings → actions (the retailer rating scale)
    rm = a.retailer_market
    plans = {p.qid: p for p in db.scalars(select(m.ActionPlan).where(m.ActionPlan.retailer_market_id == rm.id))}
    mat = [r for r in ctx.rows() if r["in_run"] and r["lens"] == "Maturity"]
    for r in mat:
        if r["status"] in ("No", "Issue", "Not disclosed"):
            p = plans.get(r["qid"])
            actions.append(dict(kind="Fix for the agency", priority="High" if r["status"] == "Issue" else "Medium",
                                title=r["text"], detail=r["finding"], cell=f"{r['dimension']}-L{r['level']}" if r["level"] else None,
                                owner=(p.owner if p and p.owner else ""), reviewer="Account team", due=None,
                                status=(p.status if p else "Open"), qids=[r["qid"]], rating="needs", c5i_status=r["status"],
                                effort=(p.effort if p else ""), editable=True))
    for d in [sc["dimension"] for sc in ctx.scores]:
        okay = [r["qid"] for r in mat if r["dimension"] == d and r["status"] == "Partial"]
        if okay:
            actions.append(dict(kind="Optional improvement", priority="Low", title=f"Firm up {len(okay)} Okay results in {d}",
                                detail="Meets best practice with minor gaps or on part of the sample. Optional, owned by the agency.",
                                cell=None, owner="Agency", reviewer="Account team", due=None, status="Optional", qids=okay, rating="okay"))
    blocked = [r["qid"] for r in ctx.rows() if r["status"] == "Access blocked"]
    if blocked:
        req = db.scalar(select(m.AccessRequest).where(m.AccessRequest.retailer_market_id == rm.id).order_by(m.AccessRequest.id.desc()))
        actions.append(dict(kind="Allowlisting request", priority="High",
                            title=f"Ask {ctx.retailer.name} to allowlist our crawler{f' on {req.site}' if req else ''}, then re-run",
                            detail=f"{len(blocked)} checks are Not assessed – access blocked and sit outside the score."
                                   + (f" {req.reason}" if req else ""),
                            cell=None, owner="Account team", reviewer="Assessor", due=None,
                            status=(req.status if req else "To request"), qids=blocked, rating="blocked", link="/admin/access-requests"))
    for s in ctx.scores:
        L = s["level"] + 1
        if L <= len(s["cells"]) and f"{s['dimension']}-L{L}" in cells:
            c = cells[f"{s['dimension']}-L{L}"]
            actions.append(dict(kind="Move up a level", priority="Low" if not s["cells"][L - 1]["tested"] else "Medium",
                                title=f"Reach {s['dimension']}-L{L} {c.name}: {c.test}", detail=c.threshold, cell=f"{s['dimension']}-L{L}",
                                owner="Account team", reviewer="Retailer sponsor", due=None,
                                status="Not testable in this set" if not s["cells"][L - 1]["tested"] else "Open", qids=[]))
    order = {"High": 0, "Medium": 1, "Low": 2}
    actions.sort(key=lambda x: (order[x["priority"]], x["due"] or "9999"))
    for i, x in enumerate(actions):
        x["id"] = i + 1
    return dict(info=ctx.info(), actions=actions)


@router.get("/stops")
def stops(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    cells = _cells(db)
    base = ctx.guide.get("baseline_229", {}) if ctx.assessment.data_source == "workflow" else {}
    out = []
    for s in ctx.scores:
        L = s["level"] + 1
        nxt = cells.get(f"{s['dimension']}-L{L}")
        out.append(dict(score=s, reason=_reason(ctx, s, scoring_settings(db)["threshold"]), baseline=base.get(s["dimension"]),
                        next=dict(cell=f"{s['dimension']}-L{L}", name=nxt.name, stage=nxt.stage, test=nxt.test, threshold=nxt.threshold,
                                  boundary=nxt.boundary) if nxt else None))
    return dict(info=ctx.info(), dimensions=out)


@router.get("/unpublished", dependencies=Admin)
def unpublished(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    rows = [r for r in ctx.rows() if r["status"] == "Not disclosed"]
    groups = []
    for d in [sc['dimension'] for sc in ctx.scores]:
        rs = [r for r in rows if r["dimension"] == d]
        groups.append(dict(dimension=d, items=rs))
    return dict(info=ctx.info(), total=len(rows), maturity=sum(1 for r in rows if r["lens"] == "Maturity"), groups=groups)


@router.get("/caveats", dependencies=Admin)
def caveats(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    a = ctx.assessment
    cfg = scoring_settings(db)
    rows = ctx.rows()
    pend = sum(1 for r in rows if r["status"] == "Check pending")
    blocked = sum(1 for r in rows if r["status"] == "Access blocked")
    ceil = [s for s in ctx.scores if s["level"] < len(s["cells"]) and not s["cells"][s["level"]]["tested"]]
    items = [
        dict(topic="Method", text="All scores are public-evidence assessments (P). 'Not disclosed' is a statement about the public record, not about the retailer's capability."),
        dict(topic="Method", text="Competitive questions (vendor and retailer axes) are reported alongside the maturity score and never added to it."),
        dict(topic="Method", text="Weights (" + " · ".join(f"{k} {v:g}" for k, v in cfg["weights"].items()) + f") and the {cfg['threshold']:g}% level threshold are set in Settings. They started as proposed defaults, not supplied in the inputs."),
        dict(topic="Coverage", text=f"{pend} questions are still Check pending (not yet checked) and sit outside every score until they are run."),
        *([dict(topic="Coverage", text=f"{blocked} checks are Not assessed – access blocked: the site blocked our crawler. They sit outside every score until the retailer allowlists the crawler and the checks are re-run (Admin › Access requests).")] if blocked else []),
        dict(topic="Coverage", text="Score ceiling: " + (", ".join(f"{s['dimension']} {s['label']}" for s in ceil) if ceil else "no dimension is held by the question-set ceiling") + ". No maturity question tests Levels 4–5 in D1–D3 or Level 5 in D4."),
        dict(topic="Freshness", text="Dynamic product, price and availability evidence must be refreshed within 30 days of client publication; deployment and measurement evidence is revalidated after 12 months and otherwise marked H."),
        dict(topic="Confidence", text="Confidence describes evidence strength and is separate from maturity. A supplier repeating a company announcement does not independently verify outcomes."),
        dict(topic="Market", text=f"Single market per run ({ctx.market.name}). No rating transfers across markets."),
    ]
    if a.data_source == "illustrative":
        items.insert(0, dict(topic="Data", text="Scores for this retailer are placeholders until its assessment results are loaded; they will be replaced by the real values."))
    else:
        items.insert(0, dict(topic="Data", text="Best Buy results are carried from the previous run through each question's previous ID; the final 158-question set has not yet been run end to end, and scores will shift once it is."))
        items.append(dict(topic="Ownership", text="CEO changes 1 Nov 2026; AI ownership after the transition is not yet published (EV-010). Recheck after that date."))
        items.append(dict(topic="Evidence", text="One record is held for adjudication (EV-002 snapshot vs live conflict) and one needs refresh before publication (EV-008)."))
    return dict(info=ctx.info(), items=items, notes=a.notes)