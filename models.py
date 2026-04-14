from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from contextlib import contextmanager

Base = declarative_base()

_engine = None
_SessionLocal = None


def init_db(app_or_db_uri):
    global _engine, _SessionLocal

    if isinstance(app_or_db_uri, str):
        uri = app_or_db_uri
    else:
        uri = app_or_db_uri.config.get('SQLALCHEMY_DATABASE_URI', 'sqlite:///site.db')

    connect_args = {"check_same_thread": False} if uri.startswith("sqlite") else {}

    _engine = create_engine(
        uri,
        connect_args=connect_args,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    Base.metadata.create_all(_engine)


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


class Guest(Base):
    __tablename__ = 'guests'

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, default="")
    phone = Column(String, nullable=False, default="")
    qr_code_id = Column(String, unique=True, nullable=False)
    qr_code_url = Column(String, nullable=True)
    has_entered = Column(Boolean, default=False)
    entry_time = Column(DateTime, nullable=True)
    visual_id = Column(Integer, unique=True, nullable=True)
    card_type = Column(String, default='single', nullable=False)
    group_size = Column(Integer, default=1)
    checked_in_count = Column(Integer, default=0)

    # WhatsApp delivery tracking
    whatsapp_sent = Column(Boolean, default=False)
    whatsapp_sent_at = Column(DateTime, nullable=True)
    whatsapp_error = Column(String, nullable=True)

    # RSVP tracking
    rsvp_status = Column(String, nullable=True)   # 'attending' | 'not_attending' | None
    rsvp_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return (
            f"<Guest(id={self.id}, visual_id={self.visual_id}, name='{self.name}', "
            f"card_type='{self.card_type}', whatsapp_sent={self.whatsapp_sent}, "
            f"rsvp={self.rsvp_status})>"
        )

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