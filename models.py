from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from contextlib import contextmanager

Base = declarative_base()

_engine       = None
_SessionLocal = None


def init_db(app_or_db_uri):
    global _engine, _SessionLocal

    uri = (app_or_db_uri if isinstance(app_or_db_uri, str)
           else app_or_db_uri.config.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///site.db'))

    connect_args = {"check_same_thread": False} if uri.startswith("sqlite") else {}

    _engine = create_engine(uri, connect_args=connect_args,
                             pool_pre_ping=True, pool_recycle=300)
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    Base.metadata.create_all(_engine)

    # ── safe migration: add new columns if they don't exist yet ──────────
    _run_migrations(_engine)


def _run_migrations(engine):
    """Idempotent column additions so existing DBs are upgraded safely."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    with engine.connect() as conn:

        # ── guests table: add event_id if missing ─────────────────────────
        guest_cols = {c['name'] for c in inspector.get_columns('guests')}
        if 'event_id' not in guest_cols:
            try:
                conn.execute(text(
                    "ALTER TABLE guests ADD COLUMN event_id INTEGER REFERENCES events(id)"
                ))
                conn.commit()
            except Exception:
                conn.rollback()

        # ── events table: add new columns if missing ─────────────────────
        ev_cols = {c['name'] for c in inspector.get_columns('events')}
        for col_def in [
            ("event_type",        "VARCHAR DEFAULT 'Wedding'"),
            ("card_template_url", "VARCHAR"),
        ]:
            if col_def[0] not in ev_cols:
                try:
                    conn.execute(text(
                        f"ALTER TABLE events ADD COLUMN {col_def[0]} {col_def[1]}"
                    ))
                    conn.commit()
                except Exception:
                    conn.rollback()


@contextmanager
def get_db_session():
    if _SessionLocal is None:
        raise Exception("Database not initialized. Call init_db(app) first.")
    session = _SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────────
# Event model
# ──────────────────────────────────────────────────────────────────────────────

class Event(Base):
    __tablename__ = 'events'

    id         = Column(Integer, primary_key=True)
    name       = Column(String, nullable=False)          # e.g. "Paul & Grace Wedding"
    slug       = Column(String, unique=True, nullable=False)  # e.g. "paul-grace-2026"

    # Couple / event details (mirrors old .env vars — now per-event)
    weds_names = Column(String, nullable=True)
    event_day  = Column(String, nullable=True)           # e.g. "Jumamosi"
    event_date = Column(String, nullable=True)           # e.g. "25 Aprili 2026"
    event_venue= Column(String, nullable=True)

    # Per-event WhatsApp config (optional overrides; falls back to global env)
    wa_phone_number_id   = Column(String, nullable=True)
    wa_access_token      = Column(String, nullable=True)
    wa_template_name     = Column(String, nullable=True, default="event_invitation")
    wa_template_language = Column(String, nullable=True, default="sw")

    # Per-event Africa's Talking config (optional overrides)
    at_username  = Column(String, nullable=True)
    at_api_key   = Column(String, nullable=True)
    at_sender_id = Column(String, nullable=True)

    # Per-event Supabase storage prefix (keeps files separate per event)
    storage_prefix     = Column(String, nullable=True)  # e.g. "paul-grace-2026"
    event_type         = Column(String, nullable=True, default="Wedding")  # Wedding, Send-Off, etc.
    card_template_url  = Column(String, nullable=True)  # Supabase URL to per-event card template

    # Status
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, nullable=True)

    # Relationship
    guests = relationship("Guest", back_populates="event",
                          cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Event(id={self.id}, name='{self.name}', slug='{self.slug}')>"


# ──────────────────────────────────────────────────────────────────────────────
# Guest model  (unchanged columns + new event_id FK)
# ──────────────────────────────────────────────────────────────────────────────

class Guest(Base):
    __tablename__ = 'guests'

    id         = Column(Integer, primary_key=True)
    event_id   = Column(Integer, ForeignKey('events.id'), nullable=True, index=True)

    name       = Column(String, nullable=False, default="")
    phone      = Column(String, nullable=False, default="")
    qr_code_id = Column(String, unique=True, nullable=False)
    qr_code_url= Column(String, nullable=True)
    has_entered= Column(Boolean, default=False)
    entry_time = Column(DateTime, nullable=True)
    visual_id  = Column(Integer, nullable=True)
    card_type  = Column(String, default='single', nullable=False)
    group_size = Column(Integer, default=1)
    checked_in_count = Column(Integer, default=0)

    # WhatsApp delivery tracking
    whatsapp_sent      = Column(Boolean, default=False)
    whatsapp_sent_at   = Column(DateTime, nullable=True)
    whatsapp_error     = Column(String, nullable=True)
    has_whatsapp       = Column(Boolean, nullable=True)
    whatsapp_checked_at= Column(DateTime, nullable=True)

    # RSVP
    rsvp_status = Column(String, nullable=True)
    rsvp_at     = Column(DateTime, nullable=True)

    # SMS (legacy)
    sms_sent    = Column(Boolean, default=False)
    sms_sent_at = Column(DateTime, nullable=True)
    sms_error   = Column(String, nullable=True)

    # Africa's Talking SMS
    at_sms_sent    = Column(Boolean, default=False)
    at_sms_error   = Column(String, nullable=True)
    at_sms_sent_at = Column(DateTime, nullable=True)

    # Relationship
    event = relationship("Event", back_populates="guests")

    def __repr__(self):
        return (f"<Guest(id={self.id}, visual_id={self.visual_id}, "
                f"name='{self.name}', event_id={self.event_id})>")

    def save(self, session):
        session.add(self)
        session.commit()
        session.refresh(self)

    def delete(self, session):
        session.delete(self)
        session.commit()


def create_guest(session, **kwargs):
    guest = Guest(**kwargs)
    session.add(guest)
    session.commit()
    session.refresh(guest)
    return guest