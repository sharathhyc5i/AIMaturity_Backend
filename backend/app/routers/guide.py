from collections import Counter

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..db import get_db
from ..services.context import dims_of, load_guide, scores_for

router = APIRouter(prefix="/api/guide", tags=["guide"])


@router.get("/question-set")
def question_set(db: Session = Depends(get_db)):
    g = load_guide(db)
    qs = db.scalars(select(m.Question).order_by(m.Question.position)).all()
    rows = [dict(qid=q.qid, dimension=q.dimension, sub_theme=q.sub_theme, text=q.text, lens=q.lens, level=q.level, change=q.change,
                 prev_id=q.prev_id, evidence_type=q.evidence_type, how=q.how, answer_type=q.answer_type, cadence=q.cadence, score=q.score,
                 in_run=q.in_run)
            for q in qs]
    summary = []
    for d in dims_of(db):
        dr = [r for r in rows if r["dimension"] == d]
        c, ch = Counter(r["lens"] for r in dr), Counter(r["change"] for r in dr)
        summary.append(dict(dimension=d, total=len(dr), new=ch.get("New", 0), modified=ch.get("Modified", 0), unchanged=ch.get("Unchanged", 0),
                            maturity=c.get("Maturity", 0), vendor=c.get("Competitive · vendor", 0), retailer=c.get("Competitive · retailer", 0)))
    return dict(questions=rows, summary=summary, removed=g["removed"], not_adopted=g["not_adopted"], readme=g["readme"])


@router.get("/accounts")
def accounts(db: Session = Depends(get_db)):
    g = load_guide(db)
    questions = db.scalars(select(m.Question)).all()
    routes = {r["id"]: r["name"] for r in g["routes"]}
    out = []
    for r in db.scalars(select(m.Retailer).order_by(m.Retailer.id)
                        .options(selectinload(m.Retailer.markets).selectinload(m.RetailerMarket.market),
                                 selectinload(m.Retailer.markets).selectinload(m.RetailerMarket.assessments).selectinload(m.Assessment.results))):
        for rm in r.markets:
            latest = max(rm.assessments, key=lambda a: a.period)
            sc = scores_for(db, latest, list(questions))
            out.append(dict(retailer=r.name, slug=r.slug, segment=r.segment, route=routes[r.route], market=rm.market.name, market_code=rm.market.code,
                            peers=rm.peers, periods=len(rm.assessments), latest=latest.period.strftime("%Y-%m"), data_source=latest.data_source,
                            scores={s["dimension"]: s["label"] for s in sc}))
    return dict(accounts=out, run_parameters=g["run_parameters"])


@router.get("/dm8")
def dm8(db: Session = Depends(get_db)):
    mk = db.scalars(select(m.Market).order_by(m.Market.id)).all()
    covered = {}
    for rm in db.scalars(select(m.RetailerMarket).options(selectinload(m.RetailerMarket.retailer), selectinload(m.RetailerMarket.market))):
        covered.setdefault(rm.market.code, []).append(rm.retailer.name)
    rows = [dict(code=x.code, name=x.name, currency=x.currency, market_maker=x.market_maker_entries, strategic=x.strategic_entries,
                 total=x.market_maker_entries + x.strategic_entries, assessed=covered.get(x.code, [])) for x in mk]
    return dict(markets=rows, totals=dict(market_maker=sum(r["market_maker"] for r in rows), strategic=sum(r["strategic"] for r in rows),
                                          total=sum(r["total"] for r in rows), markets=len(rows)))


@router.get("/dimensions")
def dimensions(db: Session = Depends(get_db)):
    dims = db.scalars(select(m.Dimension).order_by(m.Dimension.position)).all()
    cells = db.scalars(select(m.LevelCell).order_by(m.LevelCell.dimension, m.LevelCell.level)).all()
    return dict(dimensions=[dict(code=d.code, name=d.name, short=d.short, color=d.color, summary=d.summary,
                                 levels=[dict(level=c.level, name=c.name, stage=c.stage, anchor=c.anchor, evidence=c.evidence, boundary=c.boundary)
                                         for c in cells if c.dimension == d.code]) for d in dims])


@router.get("/levels")
def levels(db: Session = Depends(get_db)):
    g = load_guide(db)
    lv = db.scalars(select(m.Level).order_by(m.Level.level)).all()
    cells = db.scalars(select(m.LevelCell).order_by(m.LevelCell.dimension, m.LevelCell.level)).all()
    return dict(levels=[dict(level=l.level, name=l.name, stage=l.stage, meaning=l.meaning, min_evidence=l.min_evidence) for l in lv],
                requirements=[dict(dimension=c.dimension, level=c.level, requirement=c.requirement) for c in cells],
                scoring=g["scoring"], status_definitions=g["status_definitions"])


@router.get("/framework")
def framework(db: Session = Depends(get_db)):
    g = load_guide(db)
    cells = db.scalars(select(m.LevelCell).order_by(m.LevelCell.dimension, m.LevelCell.level)).all()
    qs = db.scalars(select(m.Question).where(m.Question.lens == "Maturity", m.Question.in_run.is_(True))).all()
    count = Counter((q.dimension, q.level) for q in qs)
    return dict(scoring=g["scoring"], cells=[dict(dimension=c.dimension, level=c.level, name=c.name, stage=c.stage, test=c.test,
                                                  threshold=c.threshold, questions=count.get((c.dimension, c.level), 0)) for c in cells],
                status_definitions=g["status_definitions"])


@router.get("/evidence-quality")
def evidence_quality(db: Session = Depends(get_db)):
    g = load_guide(db)
    return dict(fields=g["evidence_fields"], confidence=g["confidence"], conflicts=g["conflicts"], status_definitions=g["status_definitions"])


@router.get("/freshness")
def freshness(db: Session = Depends(get_db)):
    g = load_guide(db)
    return dict(policy=g["freshness"], confidence=g["confidence"])
