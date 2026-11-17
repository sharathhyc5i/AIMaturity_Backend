"""Allowlisting requests: raised when a retailer's site blocked our crawler.

A check that could not run because the site blocked us is stored as "Access blocked"
(shown as "Not assessed – access blocked"). It is left out of the score, like Check pending.
Each retailer × market with blocked checks in its latest month gets one open request.
"""
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import models as m
from ..config import settings

PREFIX = "Not assessed – access blocked: "
OPEN = ("To request", "Requested")
STATUSES = ("To request", "Requested", "Allowlisted")


def reason_of(finding: str) -> str:
    return (finding or "").replace(PREFIX, "").strip() or "The site blocked our crawler."


# Main public sites of the accounts on file (used when no logged source gives the address)
WEBSITES = {
    ("best-buy", "US"): "www.bestbuy.com", ("best-buy", "CA"): "www.bestbuy.ca", ("currys", "GB"): "www.currys.co.uk",
    ("yodobashi", "JP"): "www.yodobashi.com", ("jb-hi-fi", "AU"): "www.jbhifi.com.au",
    ("ingram-micro", "US"): "www.ingrammicro.com", ("ingram-micro", "CA"): "ca.ingrammicro.com",
}


def site_of(db: Session, retailer: m.Retailer, market: m.Market) -> str:
    """The retailer's main web address, from its logged sources (most cited host)."""
    if (retailer.slug, market.code) in WEBSITES:
        return WEBSITES[(retailer.slug, market.code)]
    hosts = Counter()
    for s in db.scalars(select(m.Source).where(m.Source.retailer == retailer.slug, m.Source.market == market.code)):
        host = urlparse(s.url or "").hostname or ""
        if host and retailer.slug.replace("-", "") in host.replace("-", ""):
            hosts[host] += 1
    if hosts:
        return hosts.most_common(1)[0][0]
    return WEBSITES.get((retailer.slug, market.code), f"{retailer.name} website ({market.code})")


def sync_access_requests(db: Session) -> int:
    """Open a request for every account whose latest month has blocked checks and no open request."""
    made = 0
    rms = db.scalars(select(m.RetailerMarket).options(selectinload(m.RetailerMarket.assessments).selectinload(m.Assessment.results),
                                                      selectinload(m.RetailerMarket.retailer), selectinload(m.RetailerMarket.market))).all()
    for rm in rms:
        if not rm.assessments:
            continue
        latest = max(rm.assessments, key=lambda a: a.period)
        blocked = [r for r in latest.results if r.status == "Access blocked"]
        if not blocked:
            continue
        open_req = db.scalar(select(m.AccessRequest).where(m.AccessRequest.retailer_market_id == rm.id, m.AccessRequest.status.in_(OPEN)))
        qids = sorted(r.qid for r in blocked)
        if open_req:
            if open_req.qids != qids:
                open_req.qids = qids
            continue
        db.add(m.AccessRequest(retailer_market_id=rm.id, period=latest.period, site=site_of(db, rm.retailer, rm.market),
                               reason=reason_of(blocked[0].finding), user_agent=settings.crawler_user_agent, qids=qids,
                               status="To request", history=[dict(at=datetime.now(timezone.utc).isoformat(), by="System",
                                                                  status="To request", note=f"{len(qids)} checks blocked in {latest.period:%B %Y}.")]))
        made += 1
    db.commit()
    return made


def email_draft(req: m.AccessRequest) -> dict:
    rm = req.retailer_market
    subject = f"Request to allowlist our crawler on {req.site}"
    body = "\n".join([
        f"Hello {rm.retailer.name} web team,",
        "",
        f"We run a monthly public-site review of how well {rm.retailer.name} ({rm.market.name}) product pages work with AI search and "
        "shopping assistants. This month our crawler could not read the pages it needs, so these checks could not be assessed:",
        "",
        f"  What happened: {req.reason}",
        f"  Checks affected: {', '.join(req.qids)}",
        "",
        "Could you please allowlist our crawler on the public product and category pages?",
        "",
        f"  User agent: {req.user_agent}",
        "  Volume: about 20 product pages plus robots.txt and the XML sitemap, once a month, at a low request rate",
        "  Scope: public pages only; no sign-in, checkout or account pages",
        "",
        "Once it is allowlisted we will re-run the checks in the next cycle.",
        "",
        "Thank you,",
        settings.crawler_contact,
    ])
    return dict(subject=subject, body=body)


def mark(db: Session, req: m.AccessRequest, status: str, by: str, note: str = "", contact: str | None = None) -> None:
    """Move a request on. Allowlisted puts the blocked checks back in the queue for the next run."""
    req.status = status
    if contact is not None:
        req.contact = contact
    if note:
        req.note = note
    req.history = [*(req.history or []), dict(at=datetime.now(timezone.utc).isoformat(), by=by, status=status, note=note)]
    if status == "Allowlisted":
        latest = max(req.retailer_market.assessments, key=lambda a: a.period)
        stamp = datetime.now(timezone.utc).strftime("%d %b %Y")
        for r in latest.results:
            if r.status == "Access blocked" and r.qid in req.qids:
                r.status = "Check pending"
                r.finding = f"Not yet checked: crawler allowlisted on {stamp}; queued for the next run."
    db.commit()