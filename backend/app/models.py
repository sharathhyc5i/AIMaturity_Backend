from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# JSONB on Postgres, plain JSON elsewhere (handy for tests)
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Retailer(Base):
    __tablename__ = "retailers"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    hq: Mapped[str] = mapped_column(String(4))
    route: Mapped[int] = mapped_column(Integer)  # 1 customer-facing, 2 distributor, 3 strategic partner
    segment: Mapped[str] = mapped_column(String(40))
    markets: Mapped[list["RetailerMarket"]] = relationship(back_populates="retailer", cascade="all, delete-orphan")


class Market(Base):
    __tablename__ = "markets"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(4), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(4))
    market_maker_entries: Mapped[int] = mapped_column(Integer, default=0)
    strategic_entries: Mapped[int] = mapped_column(Integer, default=0)


class Sku(Base):
    """Product family shown in the header SKU dropdown (managed in Admin > Settings)."""
    __tablename__ = "skus"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)


class Source(Base):
    """One link consulted for a retailer x country run (Source register; managed in Admin > Settings)."""
    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("retailer", "market", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    retailer: Mapped[str] = mapped_column(String(40), index=True)   # retailer slug
    market: Mapped[str] = mapped_column(String(4), index=True)      # country code
    code: Mapped[str] = mapped_column(String(12))                   # S01, S02 ... (what question results cite)
    title: Mapped[str] = mapped_column(String(300))
    publisher: Mapped[str] = mapped_column(String(200), default="")
    date: Mapped[str] = mapped_column(String(60), default="")       # as logged: "27 Aug 2026", "Undated (live page)"
    source_type: Mapped[str] = mapped_column(String(80), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    dimensions: Mapped[list] = mapped_column(JSONType, default=list)
    used_for: Mapped[str] = mapped_column(Text, default="")
    citing_rows: Mapped[int] = mapped_column(Integer, default=0)


class RetailerMarket(Base):
    __tablename__ = "retailer_markets"
    __table_args__ = (UniqueConstraint("retailer_id", "market_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_id: Mapped[int] = mapped_column(ForeignKey("retailers.id", ondelete="CASCADE"))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"))
    peers: Mapped[list] = mapped_column(JSONType, default=list)
    retailer: Mapped[Retailer] = relationship(back_populates="markets")
    market: Mapped[Market] = relationship()
    assessments: Mapped[list["Assessment"]] = relationship(back_populates="retailer_market", cascade="all, delete-orphan")


class Dimension(Base):
    __tablename__ = "dimensions"
    code: Mapped[str] = mapped_column(String(4), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    short: Mapped[str] = mapped_column(String(80))
    color: Mapped[str] = mapped_column(String(9))
    summary: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)


class Level(Base):
    __tablename__ = "levels"
    level: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    stage: Mapped[str] = mapped_column(String(10))
    meaning: Mapped[str] = mapped_column(Text)
    min_evidence: Mapped[str] = mapped_column(Text)


class LevelCell(Base):
    """One dimension × level: its test, threshold and definition."""
    __tablename__ = "level_cells"
    __table_args__ = (UniqueConstraint("dimension", "level"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dimension: Mapped[str] = mapped_column(ForeignKey("dimensions.code"))
    level: Mapped[int] = mapped_column(ForeignKey("levels.level"))
    name: Mapped[str] = mapped_column(String(40))
    stage: Mapped[str] = mapped_column(String(10))
    test: Mapped[str] = mapped_column(Text)
    threshold: Mapped[str] = mapped_column(Text)
    requirement: Mapped[str] = mapped_column(Text)
    anchor: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    boundary: Mapped[str] = mapped_column(Text)


class Question(Base):
    __tablename__ = "questions"
    qid: Mapped[str] = mapped_column(String(10), primary_key=True)
    dimension: Mapped[str] = mapped_column(ForeignKey("dimensions.code"), index=True)
    sub_theme: Mapped[str] = mapped_column(String(120))
    text: Mapped[str] = mapped_column(Text)
    lens: Mapped[str] = mapped_column(String(40), index=True)
    level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    change: Mapped[str] = mapped_column(String(20))
    prev_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    evidence_type: Mapped[str] = mapped_column(Text)
    how: Mapped[str] = mapped_column(Text)
    answer_type: Mapped[str] = mapped_column(String(120))
    cadence: Mapped[str] = mapped_column(String(40))
    in_run: Mapped[bool] = mapped_column(Boolean, default=True)
    origin: Mapped[str | None] = mapped_column(String(80), nullable=True)
    position: Mapped[int] = mapped_column(Integer)
    # Static 0-1 score for the question; a dimension's average times 5 is its score
    score: Mapped[float | None] = mapped_column(Float, nullable=True)


class Assessment(Base):
    __tablename__ = "assessments"
    __table_args__ = (UniqueConstraint("retailer_market_id", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_market_id: Mapped[int] = mapped_column(ForeignKey("retailer_markets.id", ondelete="CASCADE"), index=True)
    period: Mapped[date] = mapped_column(Date, index=True)  # first day of the month
    run_date: Mapped[date] = mapped_column(Date)
    data_source: Mapped[str] = mapped_column(String(20))  # "workflow" | "illustrative"
    old_sources: Mapped[list] = mapped_column(JSONType, default=list)  # source IDs older than 12 months
    notes: Mapped[list] = mapped_column(JSONType, default=list)
    retailer_market: Mapped[RetailerMarket] = relationship(back_populates="assessments")
    results: Mapped[list["QuestionResult"]] = relationship(back_populates="assessment", cascade="all, delete-orphan")
    evidence: Mapped[list["EvidenceRecord"]] = relationship(back_populates="assessment", cascade="all, delete-orphan")
    flags: Mapped[list["ValidationFlag"]] = relationship(back_populates="assessment", cascade="all, delete-orphan")


class QuestionResult(Base):
    __tablename__ = "question_results"
    __table_args__ = (UniqueConstraint("assessment_id", "qid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    qid: Mapped[str] = mapped_column(ForeignKey("questions.qid"))
    status: Mapped[str] = mapped_column(String(20), index=True)
    finding: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONType, default=list)
    flags: Mapped[list] = mapped_column(JSONType, default=list)
    # This retailer's 0-1 score for the question (1, 0.5 or 0; None = N/A or not scored).
    # When an assessment's results carry scores, its dimensions score as their average × 5.
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    assessment: Mapped[Assessment] = relationship(back_populates="results")


class EvidenceRecord(Base):
    __tablename__ = "evidence_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    eid: Mapped[str] = mapped_column(String(20))
    cell: Mapped[str] = mapped_column(String(10))
    stage: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[str] = mapped_column(String(10))
    register_status: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONType)  # full evidence-record fields
    # Manual review (Evidence approval page)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    approval: Mapped[str] = mapped_column(String(30), default="Approved")  # Pending review | Approved | Rejected | Changes requested
    reviewed_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    history: Mapped[list] = mapped_column(JSONType, default=list)
    assessment: Mapped[Assessment] = relationship(back_populates="evidence")


class ValidationFlag(Base):
    __tablename__ = "validation_flags"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    area: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10))
    text: Mapped[str] = mapped_column(Text)
    qids: Mapped[list] = mapped_column(JSONType, default=list)
    assessment: Mapped[Assessment] = relationship(back_populates="flags")


class GuideContent(Base):
    """Reference content for the Guide pages (policies, rules, removed questions...)."""
    __tablename__ = "guide_content"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    content: Mapped[dict | list] = mapped_column(JSONType)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(10), default="user")  # admin | user
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | removed
    source: Mapped[str] = mapped_column(String(20), default="domain")  # bootstrap | domain | invited
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScoreSnapshot(Base):
    """A saved, versioned set of dimension scores for one assessment (Score register)."""
    __tablename__ = "score_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    weights: Mapped[dict] = mapped_column(JSONType, default=dict)
    threshold: Mapped[float] = mapped_column(Float, default=35)
    scores: Mapped[list] = mapped_column(JSONType, default=list)  # [{dimension, label, value, level, cells:[pct]}]
    notes: Mapped[list] = mapped_column(JSONType, default=list)


class VendorScore(Base):
    """How one vendor's products are treated inside a retailer, by theme (0–100)."""
    __tablename__ = "vendor_scores"
    __table_args__ = (UniqueConstraint("assessment_id", "vendor", "theme"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    assessment_id: Mapped[int] = mapped_column(ForeignKey("assessments.id", ondelete="CASCADE"), index=True)
    vendor: Mapped[str] = mapped_column(String(60), index=True)
    theme: Mapped[str] = mapped_column(String(80))
    score: Mapped[float] = mapped_column(Float)


class AccessRequest(Base):
    """A request to the retailer to allowlist our crawler, raised when a site blocked a check.

    Status: To request → Requested → Allowlisted. Marking it Allowlisted moves the blocked checks
    back to Check pending (Not yet checked) so they run in the next cycle.
    """
    __tablename__ = "access_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_market_id: Mapped[int] = mapped_column(ForeignKey("retailer_markets.id", ondelete="CASCADE"), index=True)
    period: Mapped[date] = mapped_column(Date)  # month the block was seen
    site: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str] = mapped_column(String(200))
    qids: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(20), default="To request", index=True)
    contact: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    history: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    retailer_market: Mapped[RetailerMarket] = relationship()


class ActionPlan(Base):
    """Owner and effort an admin sets for a gap the agency fixes (a Needs Optimization question)."""
    __tablename__ = "action_plans"
    __table_args__ = (UniqueConstraint("retailer_market_id", "qid"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    retailer_market_id: Mapped[int] = mapped_column(ForeignKey("retailer_markets.id", ondelete="CASCADE"), index=True)
    qid: Mapped[str] = mapped_column(String(10))
    owner: Mapped[str] = mapped_column(String(200), default="")
    effort: Mapped[str] = mapped_column(String(40), default="")
    status: Mapped[str] = mapped_column(String(20), default="Open")
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())