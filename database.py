"""
Database module for connecting to Neon PostgreSQL.
Uses SQLAlchemy async with asyncpg driver for high-performance async queries.
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, func, Text
)
from dotenv import load_dotenv

load_dotenv()

# ── Neon PostgreSQL Connection ─────────────────────────────────────────────
# Set your Neon connection string in .env as:
#   DATABASE_URL=postgresql+asyncpg://user:password@ep-xxxxx.us-east-2.aws.neon.tech/neondb?sslmode=require
#
# You can find this in the Neon dashboard → Connection Details → select "asyncpg" driver.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL environment variable is not set. "
        "Please add it to your .env file. Example:\n"
        "DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>/<dbname>?sslmode=require"
    )

# Create async engine with Neon-friendly pool settings
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,         # Neon free tier allows limited connections
    max_overflow=2,
    pool_pre_ping=True,  # Handles Neon's serverless cold starts gracefully
    pool_recycle=300,     # Recycle connections every 5 min (Neon may drop idle ones)
)

# Session factory
async_session = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


# ── Base Model ──────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ── Production Order Table ──────────────────────────────────────────────────
class ProductionOrder(Base):
    """
    Stores each production entry with machine info, design details,
    and up to 8 feeders stored as individual columns.
    """
    __tablename__ = "production_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_date = Column(Date, nullable=False, index=True)
    mc_no = Column(String(50), nullable=False, index=True)
    design_no = Column(String(100), nullable=False)
    beam = Column(String(100), nullable=True)
    rate = Column(Float, default=0)
    piece = Column(Float, default=0)

    # Feeders 1–8 (nullable to support variable feeder count)
    feeder_1 = Column(String(200), nullable=True)
    feeder_2 = Column(String(200), nullable=True)
    feeder_3 = Column(String(200), nullable=True)
    feeder_4 = Column(String(200), nullable=True)
    feeder_5 = Column(String(200), nullable=True)
    feeder_6 = Column(String(200), nullable=True)
    feeder_7 = Column(String(200), nullable=True)
    feeder_8 = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def to_dict(self):
        """Convert model instance to a flat dictionary."""
        feeders = {}
        for i in range(1, 9):
            val = getattr(self, f"feeder_{i}")
            if val is not None and val != "":
                feeders[f"FEEDER {i}"] = val

        return {
            "id": self.id,
            "order_date": str(self.order_date) if self.order_date else None,
            "mc_no": self.mc_no,
            "design_no": self.design_no,
            "beam": self.beam,
            "rate": self.rate,
            "piece": self.piece,
            "feeders": feeders,
            "created_at": str(self.created_at) if self.created_at else None,
        }


# ── Lifecycle Helpers ───────────────────────────────────────────────────────
async def init_db():
    """Create all tables if they don't exist (safe for first-run)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncSession:
    """Dependency: yields an async session for FastAPI routes."""
    async with async_session() as session:
        yield session
