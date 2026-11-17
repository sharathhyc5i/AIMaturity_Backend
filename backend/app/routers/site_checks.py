"""Site checks: six technical AI-search checks, rated on the shared retailer rating scale.

Each check is answered by the D1/D2 questions it maps to, so a check never scores twice:
its rating is worked out from those questions' results for the month in the header.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..auth import current_user
from ..db import get_db
from ..services.access import OPEN, reason_of, site_of
from ..services.context import build_ctx
from .overview import filters

router = APIRouter(prefix="/api", tags=["site checks"], dependencies=[Depends(current_user)])

# Status → rating (the shared retailer rating scale). Not disclosed scores 0 and counts, like No.
RATING = {"Yes": "good", "Partial": "okay", "No": "needs", "Issue": "needs", "Not disclosed": "needs",
          "Access blocked": "blocked", "Check pending": "pending", "N/A": "na"}
POINTS = {"Yes": 1.0, "Partial": 0.5, "No": 0.0, "Issue": 0.0, "Not disclosed": 0.0}

CHECKS = [
    dict(id="sitemap", name="Sitemap", what="The site's XML sitemap loads correctly and has no errors.", qids=["D1-04", "D1-53"],
         fix="Fix the errors the sitemap check lists, keep only live, indexable URLs in it, and keep lastmod dates current. "
             "Reference the sitemap in robots.txt."),
    dict(id="response", name="Response codes", what="Pages return the right status (200 OK) and can be indexed.", qids=["D1-01", "D1-54"],
         fix="Return 200 for live product pages, 301 for moved ones and 404/410 for removed ones; remove noindex and "
             "X-Robots-Tag blocks from pages that should be found."),
    dict(id="canonicals", name="Canonicals", what="Each page points to the correct master version, avoiding duplicate-content confusion.",
         qids=["D1-06", "D1-01"],
         fix="Give each product one canonical URL; point variants, tracking and sort URLs at it with rel=canonical."),
    dict(id="schema", name="Schema", what="Structured data (markup that helps search and answer engines understand the page).",
         qids=["D2-06", "D1-12"],
         fix="Add schema.org Product markup with Offer (price, availability), GTIN/MPN and AggregateRating, and fix the "
             "validation errors listed."),
    dict(id="metadata", name="Metadata", what="Page titles and meta descriptions, with recommended rewrites.", qids=["D1-52"],
         fix="Write a unique title of about 60 characters (product, key spec, brand) and a meta description of 150–160 "
             "characters for each product page. The check lists suggested rewrites for the pages that fall short."),
    dict(id="robots", name="Robots.txt", what="Which crawlers, including AI bots, are allowed in, with a ready-to-paste robots.txt block.",
         qids=["D1-03"], fix="Paste the block below into robots.txt, keeping any existing rules for private areas."),
]

AI_AGENTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "PerplexityBot", "Google-Extended", "Applebot-Extended",
             "Amazonbot", "CCBot"]


def robots_block(site: str) -> str:
    host = site if "." in site and " " not in site else "www.example.com"
    lines = ["# Allow AI search and assistant crawlers to read public product pages"]
    for a in AI_AGENTS:
        lines += [f"User-agent: {a}", "Allow: /", ""]
    lines += [f"Sitemap: https://{host}/sitemap.xml"]
    return "\n".join(lines)


def rate(statuses: list[str]) -> str:
    """One rating for a check from its questions' statuses."""
    if any(s == "Access blocked" for s in statuses):
        return "blocked"
    scored = [POINTS[s] for s in statuses if s in POINTS]
    if not scored:
        return "pending" if any(s == "Check pending" for s in statuses) else "na"
    avg = sum(scored) / len(scored)
    return "good" if avg >= 0.999 else "okay" if avg >= 0.5 else "needs"


@router.get("/site-checks")
def site_checks(f: dict = Depends(filters), db: Session = Depends(get_db)):
    ctx = build_ctx(db, **f)
    rows = {r["qid"]: r for r in ctx.rows()}
    rm = ctx.assessment.retailer_market
    site = site_of(db, ctx.retailer, ctx.market)
    req = db.scalar(select(m.AccessRequest).where(m.AccessRequest.retailer_market_id == rm.id)
                    .order_by(m.AccessRequest.status.in_(OPEN).desc(), m.AccessRequest.id.desc()))
    out = []
    for i, c in enumerate(CHECKS, start=1):
        rs = [rows[q] for q in c["qids"] if q in rows]
        statuses = [r["status"] for r in rs]
        rating = rate(statuses)
        if rating == "blocked":
            summary = reason_of(next(r["finding"] for r in rs if r["status"] == "Access blocked"))
        elif rating == "pending":
            summary = "Queued: this check runs in the next cycle."
        elif rating == "na":
            summary = "Does not apply to this retailer or market."
        else:
            worst = sorted(rs, key=lambda r: POINTS.get(r["status"], 2))
            summary = worst[0]["finding"] if worst else ""
        pending = sum(1 for s in statuses if s == "Check pending")
        out.append(dict(
            n=i, id=c["id"], name=c["name"], what=c["what"], rating=rating, summary=summary, pending=pending,
            questions=[dict(qid=r["qid"], text=r["text"], level=r["level"], status=r["status"], rating=RATING.get(r["status"], "na"),
                            finding=r["finding"], sources=r["sources"]) for r in rs],
            fix=c["fix"], robots=robots_block(site) if c["id"] == "robots" else None,
        ))
    counts: dict[str, int] = {}
    for c in out:
        counts[c["rating"]] = counts.get(c["rating"], 0) + 1
    return dict(info=ctx.info(), site=site, checks=out, counts=counts,
                access=dict(id=req.id, status=req.status, reason=req.reason, updated_at=req.updated_at.isoformat() if req.updated_at else None)
                if req else None)