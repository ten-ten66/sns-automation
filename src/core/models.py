"""Database models for the SNS automation system."""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    JSON,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Platform(str, Enum):
    X = "x"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"


class ContentType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    CAROUSEL = "carousel"
    SHORT_VIDEO = "short_video"
    STORY = "story"


class PostStatus(str, Enum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DEAD = "dead"  # Max retries exceeded — will not be retried again


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        Index("ix_posts_platform_status", "platform", "status"),
        Index("ix_posts_scheduled_at", "scheduled_at"),
        Index("ix_posts_published_at", "published_at"),
        Index("ix_posts_status_scheduled", "status", "scheduled_at"),
        Index("ix_posts_ab_test_group", "ab_test_group"),
        Index("ix_posts_platform_status_scheduled", "platform", "status", "scheduled_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False, index=True)
    content_type = Column(String(20), nullable=False, default=ContentType.TEXT)
    text_content = Column(Text, nullable=True)
    media_urls = Column(JSON, nullable=True)
    hashtags = Column(JSON, nullable=True)
    keywords = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default=PostStatus.DRAFT)
    scheduled_at = Column(DateTime, nullable=True)
    published_at = Column(DateTime, nullable=True)
    platform_post_id = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0, server_default="0")
    viral_score = Column(Float, nullable=True)
    ab_test_group = Column(String(10), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    metrics = relationship("PostMetric", back_populates="post", cascade="all, delete-orphan")
    revenues = relationship("Revenue", back_populates="post", cascade="all, delete-orphan")


class PostMetric(Base):
    __tablename__ = "post_metrics"
    __table_args__ = (
        Index("ix_metrics_post_recorded", "post_id", "recorded_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    impressions = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    saves = Column(Integer, default=0)
    clicks = Column(Integer, default=0)
    followers_gained = Column(Integer, default=0)
    engagement_rate = Column(Float, default=0.0)
    ctr = Column(Float, default=0.0)  # click-through rate = clicks / impressions
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    post = relationship("Post", back_populates="metrics")


class TrendTopic(Base):
    __tablename__ = "trend_topics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    topic = Column(String(500), nullable=False)
    category = Column(String(100), nullable=True)
    volume = Column(Integer, nullable=True)
    velocity = Column(Float, nullable=True)  # growth rate
    sentiment = Column(Float, nullable=True)  # -1 to 1
    sample_posts = Column(JSON, nullable=True)
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(DateTime, nullable=True)


class ViralPost(Base):
    __tablename__ = "viral_posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    platform_post_id = Column(String(255), nullable=False)
    author = Column(String(255), nullable=True)
    content = Column(Text, nullable=True)
    media_type = Column(String(20), nullable=True)
    likes = Column(Integer, default=0)
    shares = Column(Integer, default=0)
    comments = Column(Integer, default=0)
    engagement_rate = Column(Float, default=0.0)
    analysis = Column(JSON, nullable=True)  # AI analysis of why it went viral
    discovered_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Competitor(Base):
    __tablename__ = "competitors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    username = Column(String(255), nullable=False)
    display_name = Column(String(255), nullable=True)
    follower_count = Column(Integer, default=0)
    posting_frequency = Column(Float, nullable=True)  # posts per day
    avg_engagement_rate = Column(Float, nullable=True)
    top_hashtags = Column(JSON, nullable=True)
    content_strategy = Column(Text, nullable=True)  # AI-generated analysis
    last_analyzed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class FollowerSnapshot(Base):
    __tablename__ = "follower_snapshots"
    __table_args__ = (
        Index("ix_follower_platform_date", "platform", "recorded_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(String(20), nullable=False)
    follower_count = Column(Integer, nullable=False)
    following_count = Column(Integer, default=0)
    daily_gain = Column(Integer, default=0)
    demographics = Column(JSON, nullable=True)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Revenue(Base):
    __tablename__ = "revenues"
    __table_args__ = (
        Index("ix_revenue_recorded_at", "recorded_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=True)
    platform = Column(String(20), default="")
    source = Column(String(100), nullable=False)  # affiliate, ad, sponsorship, etc.
    network = Column(String(100), nullable=True)  # a8, amazon, rakuten, etc.
    amount = Column(Numeric(precision=12, scale=2), nullable=False, default=0.0)
    currency = Column(String(10), default="JPY")
    clicks = Column(Integer, default=0)
    conversions = Column(Integer, default=0)
    recorded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    post = relationship("Post", back_populates="revenues")


class SEOKeyword(Base):
    __tablename__ = "seo_keywords"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(255), nullable=False)
    search_volume = Column(Integer, nullable=True)
    competition = Column(Float, nullable=True)  # 0-1
    relevance_score = Column(Float, nullable=True)
    platform = Column(String(20), nullable=True)
    last_updated = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ContentTemplate(Base):
    __tablename__ = "content_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    platform = Column(String(20), nullable=False)
    content_type = Column(String(20), nullable=False)
    template_text = Column(Text, nullable=False)
    variables = Column(JSON, nullable=True)
    performance_score = Column(Float, nullable=True)
    use_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AffiliateProduct(Base):
    """Affiliate product catalog for automated promotion."""
    __tablename__ = "affiliate_products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_name = Column(String(200), nullable=False)
    product_url = Column(String(500), nullable=False)  # Base product page URL
    affiliate_link = Column(String(500), nullable=False)  # Affiliate-tagged link
    network = Column(String(50), nullable=False)  # a8, afb, amazon, rakuten, qoo10
    category = Column(String(100), default="cosmetics")  # cosmetics, skincare, haircare, etc.
    commission_rate = Column(Float, default=0.0)  # Percentage
    price_jpy = Column(Integer, default=0)
    description = Column(Text, default="")
    is_active = Column(Boolean, default=True, index=True)
    times_promoted = Column(Integer, default=0)
    last_promoted_at = Column(DateTime, nullable=True)
    total_clicks = Column(Integer, default=0)
    total_conversions = Column(Integer, default=0)
    total_revenue_jpy = Column(Float, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# HoshimiLive fortune service — customers, payments, readings, agent ops
# ---------------------------------------------------------------------------


class PaymentProvider(str, Enum):
    PADDLE = "paddle"
    LEMON_SQUEEZY = "lemon_squeezy"


class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    REFUNDED = "refunded"
    FAILED = "failed"
    DISPUTED = "disputed"


class SubscriptionStatus(str, Enum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    EXPIRED = "expired"


class FortuneType(str, Enum):
    TAROT = "tarot"
    HOROSCOPE = "horoscope"
    NUMEROLOGY = "numerology"
    LOVE = "love"
    GUARDIAN_ANGEL = "guardian_angel"


class AgentDepartment(str, Enum):
    SECRETARIAT = "secretariat"
    PM = "pm"
    RESEARCH = "research"
    MARKETING = "marketing"
    FINANCE = "finance"


class AgentTaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELED = "canceled"


class Customer(Base):
    """HoshimiLive paying / registered customer.

    Created when a payment webhook arrives or (future) when a user registers.
    `external_id` is the Paddle / Lemon Squeezy customer id.
    """
    __tablename__ = "customers"
    __table_args__ = (
        Index("ix_customers_provider_external", "provider", "external_id", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    external_id = Column(String(128), nullable=True, index=True)  # provider customer id
    provider = Column(String(32), nullable=True)  # paddle / lemon_squeezy
    country = Column(String(2), nullable=True)  # ISO-3166 alpha-2
    locale = Column(String(16), nullable=True)  # en, ja, en-US, ...
    display_name = Column(String(120), nullable=True)
    extra_data = Column(JSON, nullable=True)  # provider-specific raw payload
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    orders = relationship("Order", back_populates="customer", cascade="all, delete-orphan")
    subscriptions = relationship(
        "Subscription", back_populates="customer", cascade="all, delete-orphan"
    )
    readings = relationship(
        "FortuneReading", back_populates="customer", cascade="all, delete-orphan"
    )


class Order(Base):
    """One-time purchase (single reading, reading pack, etc.)."""
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_customer_created", "customer_id", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_provider_external", "provider", "external_id", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    external_id = Column(String(128), nullable=True)  # provider order/transaction id
    provider = Column(String(32), nullable=False)  # paddle / lemon_squeezy
    product_sku = Column(String(64), nullable=False)  # our internal product code
    amount = Column(Numeric(precision=12, scale=2), nullable=False, default=0)
    currency = Column(String(10), nullable=False, default="USD")
    status = Column(String(20), nullable=False, default=OrderStatus.PENDING)
    paid_at = Column(DateTime, nullable=True)
    refunded_at = Column(DateTime, nullable=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    customer = relationship("Customer", back_populates="orders")


class Subscription(Base):
    """Recurring subscription (Oracle Premium monthly/yearly, etc.)."""
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subs_customer_status", "customer_id", "status"),
        Index("ix_subs_current_period_end", "current_period_end"),
        Index("ix_subs_provider_external", "provider", "external_id", unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    external_id = Column(String(128), nullable=True)  # provider subscription id
    provider = Column(String(32), nullable=False)
    plan_sku = Column(String(64), nullable=False)  # e.g. "oracle_premium_monthly"
    status = Column(String(20), nullable=False, default=SubscriptionStatus.ACTIVE)
    amount = Column(Numeric(precision=12, scale=2), nullable=False, default=0)
    currency = Column(String(10), nullable=False, default="USD")
    interval = Column(String(20), nullable=False, default="month")  # month / year
    current_period_start = Column(DateTime, nullable=True)
    current_period_end = Column(DateTime, nullable=True)
    cancel_at_period_end = Column(Boolean, default=False)
    canceled_at = Column(DateTime, nullable=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    customer = relationship("Customer", back_populates="subscriptions")


class FortuneReading(Base):
    """Audit log of every AI fortune reading served.

    `customer_id` is nullable because free-sample readings on the LP are
    anonymous. `ip_hash` is a sha256 of the client IP for rate limiting +
    analytics without storing raw IP.
    """
    __tablename__ = "fortune_readings"
    __table_args__ = (
        Index("ix_readings_created_at", "created_at"),
        Index("ix_readings_type_created", "fortune_type", "created_at"),
        Index("ix_readings_customer_created", "customer_id", "created_at"),
        Index("ix_readings_ip_created", "ip_hash", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    ip_hash = Column(String(64), nullable=True)  # sha256 hex
    fortune_type = Column(String(32), nullable=False)
    theme_or_input = Column(String(500), nullable=True)  # sanitized user input
    card_name = Column(String(120), nullable=True)  # for tarot
    text = Column(Text, nullable=True)
    cta = Column(String(500), nullable=True)
    model_used = Column(String(100), nullable=True)
    cost_usd = Column(Numeric(precision=10, scale=6), default=0)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    customer = relationship("Customer", back_populates="readings")


class AgentLog(Base):
    """Audit log of every 5-department agent dispatch.

    One row per call to Orchestrator.dispatch(). Keeps inputs, outputs, timing,
    and errors for observability + debugging. Does NOT persist task state —
    see AgentTask for that.
    """
    __tablename__ = "agent_logs"
    __table_args__ = (
        Index("ix_agent_logs_created_at", "created_at"),
        Index("ix_agent_logs_dept_task_created", "department", "task", "created_at"),
        Index("ix_agent_logs_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    department = Column(String(32), nullable=False)
    task = Column(String(120), nullable=False)
    status = Column(String(20), nullable=False, default="ok")  # ok / error
    duration_ms = Column(Integer, default=0)
    input = Column(JSON, nullable=True)  # kwargs passed to dispatch
    output = Column(JSON, nullable=True)  # AgentResult.data
    error = Column(Text, nullable=True)
    triggered_by = Column(String(100), nullable=True)  # scheduler / api / cli
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class AgentTask(Base):
    """Persistent task item owned by an agent department.

    Distinct from AgentLog: AgentLog is a per-dispatch audit trail, AgentTask
    is a long-lived todo/ticket that survives across dispatches.
    """
    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_agent_tasks_dept_status", "department", "status"),
        Index("ix_agent_tasks_status_due", "status", "due_at"),
        Index("ix_agent_tasks_parent", "parent_task_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    department = Column(String(32), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default=AgentTaskStatus.PENDING)
    priority = Column(Integer, default=50)  # 0 (low) .. 100 (critical)
    assigned_to = Column(String(100), nullable=True)
    due_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    parent_task_id = Column(Integer, ForeignKey("agent_tasks.id"), nullable=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    parent = relationship("AgentTask", remote_side="AgentTask.id", backref="subtasks")
