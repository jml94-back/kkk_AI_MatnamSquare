from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    else:
        # 라즈베리파이: 연결 수를 작게 유지
        kwargs.update(pool_size=5, max_overflow=5)
    _engine = create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


def create_all():
    from . import models  # noqa: F401

    Base.metadata.create_all(get_engine())
    _add_missing_columns()


# 이미 만들어진 DB에 나중에 추가된 컬럼 (create_all 은 기존 테이블에 컬럼을 추가하지 않는다)
_LATE_COLUMNS = {
    "message": {"wake_depth": "INTEGER NOT NULL DEFAULT 0", "auto_via": "VARCHAR(16)"},
    "wake_job": {"appointment_id": "INTEGER"},
    "wake_config": {"client": "VARCHAR(16)", "last_checked_at": "TIMESTAMP WITH TIME ZONE",
                    "project_label": "VARCHAR(100)"},
}


def _add_missing_columns():
    from sqlalchemy import inspect, text

    eng = get_engine()
    insp = inspect(eng)
    with eng.begin() as conn:
        for table, cols in _LATE_COLUMNS.items():
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db() -> Iterator[Session]:
    with session_scope() as s:
        yield s
