from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..auth import require_admin
from ..config import settings
from ..db import get_db
from ..services.context import build_ctx, scores_for, scoring_settings
from ..services.scoring import DEFAULT_THRESHOLD, DEFAULT_WEIGHTS

router = APIRouter(prefix="/api", tags=["overview"])
from ..services.scoring import STATUS_ORDER, UNSCORED  # noqa: E402


def filters(retailer: str = Query(settings.default_retailer), market: str = Query(settings.default_market),
            period: str = Query(settings.default_period)):
    return dict(retailer=retailer, market=market, period=period)


@router.get("/meta")
def meta(db: Session = Depends(get_db)):
    retailers = db.scalars(select(m.Retailer).order_by(m.Retailer.id)
                           .options(selectinload(m.Retailer.markets).selectinload(m.RetailerMarket.market),
                                    selectinload(m.Retailer.markets).selectinload(m.RetailerMarket.assessments))).all()
    dims = db.scalars(select(m.Dimension).order_by(m.Dimension.position)).all()
    levels = db.scalars(select(m.Level).order_by(m.Level.level)).all()
    skus = db.scalars(select(m.Sku).order_by(m.Sku.position, m.Sku.id)).all()
    return dict(
        defaults=dict(retailer=settings.default_retailer, market=settings.default_market, period=settings.default_period),
        retailers=[dict(slug=r.slug, name=r.name, route=r.route, segment=r.segment, hq=r.hq,
                        markets=[dict(code=rm.market.code, name=rm.market.name,
                                      periods=sorted(a.period.strftime("%Y-%m") for a in rm.assessments),
                                      workflow_periods=sorted(a.period.strftime("%Y-%m") for a in rm.assessments if a.data_source == "workflow"))
                                 for rm in r.markets]) for r in retailers],
        dimensions=[dict(code=d.code, name=d.name, short=d.short, color=d.color, summary=d.summary) for d in dims],
        levels=[dict(level=l.level, name=l.name, stage=l.stage) for l in levels],
        skus=[dict(code=s.code, name=s.name) for s in skus],
    )


@router.get("/assessment")
def account_assessment(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    rows = ctx.rows()
    in_run = [r for r in rows if r["in_run"]]
    mat = [r for r in in_run if r["lens"] == "Maturity"]
    status_all = Counter(r["status"] for r in in_run)
    status_mat = Counter(r["status"] for r in mat)
    lens = Counter(r["lens"] for r in in_run)
    conf = Counter(e.confidence for e in ctx.assessment.evidence)
    sev = Counter(fl.severity for fl in ctx.assessment.flags)

    # Trend: the same retailer × market across every month on file
    a = ctx.assessment
    siblings = db.scalars(select(m.Assessment).where(m.Assessment.retailer_market_id == a.retailer_market_id)
                          .order_by(m.Assessment.period).options(selectinload(m.Assessment.results))).all()
    trend = []
    for s in siblings:
        sc = scores_for(db, s, ctx.questions)
        trend.append(dict(period=s.period.strftime("%Y-%m"), data_source=s.data_source,
                          scores={x["dimension"]: x["value"] for x in sc}))

    baseline = ctx.guide.get("baseline_229", {}) if a.data_source == "workflow" else {}
    return dict(
        info=ctx.info(),
        scores=ctx.scores,
        baseline=baseline,
        totals=dict(questions=len(in_run), maturity=len(mat), competitive_vendor=lens.get("Competitive · vendor", 0),
                    competitive_retailer=lens.get("Competitive · retailer", 0), set_size=len(rows)),
        status=[dict(status=s, all=status_all.get(s, 0), maturity=status_mat.get(s, 0)) for s in STATUS_ORDER],
        evidence=dict(records=len(a.evidence), confidence={k: conf.get(k, 0) for k in ["High", "Medium", "Low", "U"]},
                      h_flagged=sum(1 for e in a.evidence if e.data.get("h_flag"))),
        flags=dict(total=len(a.flags), error=sev.get("error", 0), warn=sev.get("warn", 0), info=sev.get("info", 0)),
        trend=trend,
    )


@router.get("/outcomes")
def outcomes(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    rows = [r for r in ctx.rows()]
    by_dim = []
    for sc in ctx.scores:
        d = sc["dimension"]
        drows = [r for r in rows if r["dimension"] == d and r["in_run"]]
        c = Counter(r["status"] for r in drows)
        cm = Counter(r["status"] for r in drows if r["lens"] == "Maturity")
        themes = defaultdict(Counter)
        for r in drows:
            themes[r["sub_theme"]][r["status"]] += 1
        by_dim.append(dict(dimension=d, score=sc, total=len(drows),
                           status={s: c.get(s, 0) for s in STATUS_ORDER},
                           maturity_status={s: cm.get(s, 0) for s in STATUS_ORDER},
                           lens=dict(Counter(r["lens"] for r in drows)),
                           themes=[dict(name=k, total=sum(v.values()), status=dict(v)) for k, v in themes.items()]))
    logged = db.scalars(select(m.Source).where(m.Source.retailer == f["retailer"], m.Source.market == f["market"])).all()
    source_map = {x.code: dict(title=x.title, url=x.url, publisher=x.publisher, date=x.date, source_type=x.source_type) for x in logged}
    return dict(info=ctx.info(), dimensions=by_dim, questions=rows, sources=source_map)


def _short(text: str, n: int = 180) -> str:
    text = (text or "").strip().rstrip(".")
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


@router.get("/dimension-detail")
def dimension_detail(dimension: str = Query(..., pattern="^D[0-9]{1,2}$"), f: dict = Depends(filters), db: Session = Depends(get_db)):
    """Everything behind one dimension's score, laid out like the scorecard."""
    ctx = build_ctx(db, **f)
    sc = next((s for s in ctx.scores if s["dimension"] == dimension), None)
    dim = db.get(m.Dimension, dimension)
    if not sc or not dim:
        raise HTTPException(404, f"Dimension {dimension} not found.")
    cfg = scoring_settings(db)
    threshold = cfg["threshold"]
    defs = {c.level: c for c in db.scalars(select(m.LevelCell).where(m.LevelCell.dimension == dimension))}
    lvmeta = {l.level: l for l in db.scalars(select(m.Level))}
    rows = [r for r in ctx.rows() if r["dimension"] == dimension and r["in_run"]]
    mat = [r for r in rows if r["lens"] == "Maturity"]
    a = ctx.assessment
    old = set(a.old_sources or [])
    lv = sc["level"]

    levels = []
    for c in sc["cells"]:
        L = c["level"]
        qs = [r for r in mat if r["level"] == L]
        cnt = Counter(r["status"] for r in qs)
        d = defs.get(L)
        levels.append(dict(
            level=L, name=(d.name if d else lvmeta[L].name if L in lvmeta else f"Level {L}"),
            stage=(d.stage if d else lvmeta[L].stage if L in lvmeta else ""),
            test=d.test if d else "", threshold=d.threshold if d else "", requirement=d.requirement if d else "",
            result=c["result"], gate=c["gate"], pct=c["pct"], yes=c["yes"], other=c["other"], total=c["total"],
            counts=dict(cnt),
            # What the percentage is made of: weighted points over the questions that count
            counted=c.get("n", 0), weighted=round(c.get("weighted", 0) or 0, 2),
            questions=[dict(qid=r["qid"], text=r["text"], status=r["status"], finding=r["finding"], sources=r["sources"]) for r in qs],
        ))

    # Why this score: highest gate met, the next gate that failed, and H
    why = []
    if lv:
        met = [r for r in mat if r["level"] == lv and r["status"] == "Yes"] or [r for r in mat if r["level"] == lv and r["status"] == "Partial"]
        detail = "; ".join(_short(r["finding"], 120) for r in met[:2])
        why.append(dict(kind="met", level=lv, text=f"Level {lv} met: {levels[lv - 1]['yes']} of {levels[lv - 1]['total']} questions answered Yes"
                        + (f" ({levels[lv - 1]['pct']}% weighted)" if levels[lv - 1]["pct"] is not None else "") + (f". {detail}" if detail else "")))
    else:
        why.append(dict(kind="none", level=0, text="Level 1 is not met yet, so no level is established."))
    if lv < len(levels):
        nxt = levels[lv]
        if nxt["total"]:
            blockers = [r for r in mat if r["level"] == nxt["level"] and r["status"] in ("Not disclosed", "No", "Issue")]
            reason = (f"{nxt['pct']}% weighted, below the {threshold:g}% threshold" if nxt["pct"] is not None else "no answered questions yet")
            ex = _short(blockers[0]["finding"], 140) if blockers else ""
            why.append(dict(kind="fail", level=nxt["level"], text=f"Level {nxt['level']} {(nxt['gate'] or 'not met').lower()}: {reason}" + (f". {ex}" if ex else "")))
        elif nxt["result"] in ("Ceiling", "No questions"):
            why.append(dict(kind="ceiling", level=nxt["level"], text=f"No questions test Level {nxt['level']} in this run, so {lv and f'Level {lv}' or 'this'} is the ceiling."))
    cited_codes = sorted({s for r in mat if r["level"] and r["level"] <= max(lv, 1) for s in r["sources"]})
    stale = sorted(c for c in cited_codes if c in old)
    if sc["h"]:
        why.append(dict(kind="h", level=lv, text=f"H: the evidence for the highest level met is older than 12 months ({', '.join(stale) or 'see sources'})."))

    # Evidence records for this dimension
    ev = sorted((e for e in a.evidence if (e.cell or "").startswith(dimension + "-")), key=lambda e: e.eid)
    evidence = [dict(id=e.id, eid=e.eid, title=(e.data or {}).get("title", ""), cell=e.cell, stage=e.stage, confidence=e.confidence,
                     register_status=e.register_status, approval=e.approval, h_flag=bool((e.data or {}).get("h_flag")),
                     url=(e.data or {}).get("url", ""), publisher=(e.data or {}).get("publisher", ""),
                     claim=(e.data or {}).get("claim", ""), result=(e.data or {}).get("result", ""),
                     # Which questions this record answers, and the sources it rests on
                     questions=list((e.data or {}).get("questions") or []), sources=list((e.data or {}).get("sources") or [])) for e in ev]
    verified = bool(evidence) and all(e["approval"] == "Approved" or e["register_status"] == "Accepted" for e in evidence)

    # Sources: every source cited by this dimension's questions, plus logged sources tagged with it
    cites: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        for code in r["sources"]:
            cites[code].append(r["qid"])
    logged = {s.code: s for s in db.scalars(select(m.Source).where(m.Source.retailer == f["retailer"], m.Source.market == f["market"]))}
    codes = set(cites) | {c for c, s in logged.items() if dimension in (s.dimensions or [])}

    def code_key(c: str):
        digits = "".join(ch for ch in c if ch.isdigit())
        return (-len(cites.get(c, [])), int(digits) if digits else 0)

    sources = []
    groups: Counter = Counter()
    for c in sorted(codes, key=code_key):
        s = logged.get(c)
        group = (s.source_type.split("(")[0].strip() if s and s.source_type else "Not logged")
        if cites.get(c):
            groups[group] += 1
        sources.append(dict(code=c, title=s.title if s else "", publisher=s.publisher if s else "", date=s.date if s else "",
                            source_type=s.source_type if s else "", group=group, url=s.url if s else "",
                            cited_by=sorted(set(cites.get(c, []))), old=c in old))

    basis = " · ".join(f"{g} ×{n}" for g, n in groups.most_common()) or "No sources cited yet"
    baseline = ctx.guide.get("baseline_229", {}).get(dimension) if a.data_source == "workflow" else None
    return dict(
        info=ctx.info(), dimension=dict(code=dim.code, name=dim.name, short=dim.short, summary=dim.summary, color=dim.color),
        score=sc, threshold=threshold, weights=cfg["weights"], levels=levels, why=why,
        facts=dict(score=lv, label=sc["label"], stage=sc["stage"], evidence_status="V" if verified else "P",
                   evidence_status_text="Verified: every evidence record is approved" if verified else "Provisional: public evidence, not yet fully verified",
                   stale=sc["h"], stale_sources=stale, basis=basis, baseline=baseline,
                   questions=len(mat), answered=sum(1 for r in mat if r["status"] not in UNSCORED)),
        evidence=evidence, sources=sources,
    )


@router.get("/competitors")
def competitors(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    rows = [r for r in ctx.rows() if r["in_run"]]
    vendor = [r for r in rows if r["lens"] == "Competitive · vendor"]
    retailer_axis = [r for r in rows if r["lens"] == "Competitive · retailer"]

    def summary(rs):
        c = Counter(r["status"] for r in rs)
        return {s: c.get(s, 0) for s in STATUS_ORDER}

    params = {p["key"]: p["value"] for p in ctx.guide.get("run_parameters", [])}
    # Benchmark: every retailer × market on file for the same month
    period = ctx.assessment.period
    others = db.scalars(select(m.Assessment).where(m.Assessment.period == period)
                        .options(selectinload(m.Assessment.results),
                                 selectinload(m.Assessment.retailer_market).selectinload(m.RetailerMarket.retailer),
                                 selectinload(m.Assessment.retailer_market).selectinload(m.RetailerMarket.market))).all()
    bench = []
    for o in others:
        sc = scores_for(db, o, ctx.questions)
        rm = o.retailer_market
        res = list(o.results)
        comp = [r for r in res if r.qid in {x["qid"] for x in vendor}]
        bench.append(dict(retailer=rm.retailer.name, slug=rm.retailer.slug, market=rm.market.code, market_name=rm.market.name,
                          data_source=o.data_source, current=(o.id == ctx.assessment.id),
                          scores={x["dimension"]: dict(value=x["value"], label=x["label"]) for x in sc},
                          vendor_yes=sum(1 for r in comp if r.status == "Yes"), vendor_answered=sum(1 for r in comp if r.status not in UNSCORED)))
    bench.sort(key=lambda b: (-sum(v["value"] for v in b["scores"].values()), b["retailer"]))
    themes = defaultdict(list)
    for r in vendor:
        themes[r["sub_theme"]].append(r)
    return dict(info=ctx.info(), vendor=params.get("[VENDOR]", "Microsoft"), vendor_skus=params.get("[VENDOR SKUS]", ""),
                competing_vendors=[x.strip() for x in params.get("[COMPETING VENDORS]", "").split(",") if x.strip()],
                peers=ctx.peers,
                vendor_axis=dict(summary=summary(vendor), questions=vendor,
                                 themes=[dict(name=k, total=len(v), yes=sum(1 for x in v if x["status"] == "Yes"),
                                              answered=sum(1 for x in v if x["status"] not in UNSCORED)) for k, v in themes.items()]),
                retailer_axis=dict(summary=summary(retailer_axis), questions=retailer_axis),
                benchmark=bench)


@router.get("/journey", dependencies=[Depends(require_admin)])  # admin view
def journey(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    g = ctx.guide
    route = next(r for r in g["routes"] if r["id"] == ctx.retailer.route)
    score_by = {s["dimension"]: s for s in ctx.scores}
    ev = [e.data for e in ctx.assessment.evidence]
    stages = []
    src = g["journey"]
    for i, st in enumerate(src):
        dims = st["dims"]
        related = [dict(eid=e["eid"], title=e["title"], stage=e["stage"], cell=e["cell"], confidence=e["confidence"])
                   for e in ev if e["cell"][:2] in dims][:4]
        stages.append(dict(position=i + 1, stage=st["stage"], question=st["question"], connection=st["connection"], dims=dims,
                           distributor_stage=g["distributor_journey"][i],
                           readiness=min(score_by[d]["value"] for d in dims),
                           scores=[dict(dimension=d, label=score_by[d]["label"], value=score_by[d]["value"]) for d in dims],
                           evidence=related))
    return dict(info=ctx.info(), route=route, routes=g["routes"], distributor=ctx.retailer.route == 2,
                stages=stages, note=g["journey_note"], distributor_note=g["distributor_note"])


class SimIn(BaseModel):
    retailer: str = settings.default_retailer
    market: str = settings.default_market
    period: str = settings.default_period
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    threshold: float = DEFAULT_THRESHOLD
    infer: bool = True


@router.post("/score/simulate")
def simulate(body: SimIn, db: Session = Depends(get_db)):
    ctx = build_ctx(db, body.retailer, body.market, body.period, weights=body.weights, threshold=body.threshold, infer=body.infer)
    return dict(scores=ctx.scores, weights=body.weights, threshold=body.threshold, infer=body.infer)


@router.get("/reading-guide")
def reading_guide(f: dict = Depends(filters), db: Session = Depends(get_db)):
    """Client-safe notes on how to read the results, plus the view's info (used by the sidebar badge).

    The internal caveats (data provenance, pending checks, records under review) stay on the admin Caveats page.
    """
    ctx = build_ctx(db, **f)
    threshold = scoring_settings(db)["threshold"]
    items = [
        dict(topic="Evidence", text="Scores are based on publicly available evidence. “Not disclosed” means no public information was found either way; it is not a judgement of the retailer’s actual capability."),
        dict(topic="Scoring", text=f"Each dimension scores the highest level met in sequence from Level 1. A level is met when its weighted evidence reaches {threshold:g}%; the decimals show progress toward the next level."),
        dict(topic="Competitive results", text="Competitor and peer comparisons are reported beside the maturity score and are never added to it."),
        dict(topic="Confidence", text="Confidence describes the strength of the evidence and is separate from the maturity level."),
        dict(topic="Freshness", text="H marks a score whose key evidence is older than 12 months. Product, price and availability evidence is refreshed within 30 days of publication."),
        dict(topic="Market", text=f"Results apply to {ctx.market.name} only and do not transfer to other markets."),
    ]
    return dict(info=ctx.info(), items=items)