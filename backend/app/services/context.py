"""Load one assessment (retailer × market × month) with its questions, results and scores."""
from dataclasses import dataclass
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from .scoring import DEFAULT_THRESHOLD, DEFAULT_WEIGHTS, Q, score_all

DIMS = ["D1", "D2", "D3", "D4"]  # seed order; live lists come from dims_of()


def dims_of(db: Session) -> list[str]:
    return list(db.scalars(select(m.Dimension.code).order_by(m.Dimension.position, m.Dimension.code)))


def max_level(db: Session) -> int:
    return db.scalar(select(func.max(m.Level.level))) or 5


def scoring_settings(db: Session) -> dict:
    g = db.get(m.GuideContent, "scoring")
    c = (g.content if g else {}) or {}
    return dict(weights=c.get("weights") or dict(DEFAULT_WEIGHTS), threshold=float(c.get("threshold", DEFAULT_THRESHOLD)),
                infer=bool(c.get("infer", True)))


def parse_period(p: str) -> date:
    try:
        y, mo = p.split("-")[:2]
        return date(int(y), int(mo), 1)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "period must look like YYYY-MM") from exc


@dataclass
class Ctx:
    assessment: m.Assessment
    retailer: m.Retailer
    market: m.Market
    peers: list[str]
    questions: list[m.Question]
    results: dict[str, m.QuestionResult]
    scores: list[dict]
    guide: dict

    def rows(self) -> list[dict]:
        out = []
        for q in self.questions:
            r = self.results.get(q.qid)
            out.append(dict(qid=q.qid, dimension=q.dimension, sub_theme=q.sub_theme, text=q.text, lens=q.lens, level=q.level,
                            change=q.change, evidence_type=q.evidence_type, how=q.how, answer_type=q.answer_type, cadence=q.cadence,
                            # The retailer's own score where its result has one (N/A has none), else the question's
                            origin=q.origin, score=r.score if r and (r.score is not None or r.status == "N/A") else q.score,
                            in_run=bool(r),
                            status=r.status if r else "Not in run", finding=r.finding if r else "Not in this run.",
                            sources=r.sources if r else [], flags=r.flags if r else []))
        return out

    def info(self) -> dict:
        a = self.assessment
        return dict(id=a.id, retailer=dict(slug=self.retailer.slug, name=self.retailer.name, route=self.retailer.route,
                                           segment=self.retailer.segment),
                    market=dict(code=self.market.code, name=self.market.name, currency=self.market.currency),
                    period=a.period.strftime("%Y-%m"), run_date=a.run_date.isoformat(), data_source=a.data_source,
                    notes=a.notes, peers=self.peers)


def load_guide(db: Session) -> dict:
    return {g.key: g.content for g in db.scalars(select(m.GuideContent))}


def find_assessment(db: Session, retailer: str, market: str, period: str) -> tuple[m.Assessment, m.RetailerMarket]:
    rm = db.scalar(select(m.RetailerMarket).join(m.Retailer).join(m.Market)
                   .where(m.Retailer.slug == retailer, m.Market.code == market))
    if not rm:
        raise HTTPException(404, f"{retailer} is not assessed in {market}")
    a = db.scalar(select(m.Assessment).where(m.Assessment.retailer_market_id == rm.id, m.Assessment.period == parse_period(period))
                  .options(selectinload(m.Assessment.results), selectinload(m.Assessment.evidence), selectinload(m.Assessment.flags)))
    if not a:
        raise HTTPException(404, f"No assessment for {retailer} {market} in {period}")
    return a, rm


def build_ctx(db: Session, retailer: str, market: str, period: str, weights=None, threshold=None, infer=None) -> Ctx:
    a, rm = find_assessment(db, retailer, market, period)
    cfg = scoring_settings(db)
    weights = weights if weights is not None else cfg["weights"]
    threshold = threshold if threshold is not None else cfg["threshold"]
    infer = infer if infer is not None else cfg["infer"]
    questions = list(db.scalars(select(m.Question).order_by(m.Question.position)))
    results = {r.qid: r for r in a.results}
    qs = [Q(q.qid, q.dimension, q.level, q.lens, results[q.qid].status, results[q.qid].sources, results[q.qid].score)
          for q in questions if q.qid in results]
    scores = score_all(dims_of(db), qs, weights=weights, threshold=threshold, infer=infer,
                       old_sources=set(a.old_sources or []), max_level=max_level(db))
    return Ctx(a, rm.retailer, rm.market, rm.peers or [], questions, results, scores, load_guide(db))


def scores_for(db: Session, a: m.Assessment, questions: list[m.Question]) -> list[dict]:
    results = {r.qid: r for r in a.results}
    qs = [Q(q.qid, q.dimension, q.level, q.lens, results[q.qid].status, results[q.qid].sources, results[q.qid].score)
          for q in questions if q.qid in results]
    cfg = scoring_settings(db)
    return score_all(dims_of(db), qs, weights=cfg["weights"], threshold=cfg["threshold"], infer=cfg["infer"],
                     old_sources=set(a.old_sources or []), max_level=max_level(db))