"""
FastAPI application for Production Data Entry & Planner.

Provides REST API endpoints for:
  - Creating, reading, updating, deleting production orders
  - Processing orders through the LangGraph workflow
  - Filtering orders by date and machine number

Connects to Neon PostgreSQL and is deployable on Vercel.
"""

from contextlib import asynccontextmanager
from datetime import date
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete as sql_delete

from database import ProductionOrder, init_db, get_session
from schemas import (
    ProductionOrderCreate,
    ProductionOrderUpdate,
    ProductionOrderResponse,
    ProcessedOrderResponse,
    MessageResponse,
)


# ── App Lifecycle ───────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB tables on startup."""
    await init_db()
    yield


app = FastAPI(
    title="Running Order — Production Planner API",
    description=(
        "Backend API for managing daily production entries, "
        "processing schedules via LangGraph, and querying by date/machine."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS (allow Streamlit / any frontend) ──────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Helper: map feeders dict → model columns ──────────────────────────────
def _feeders_to_columns(feeders: dict) -> dict:
    """Convert {'FEEDER 1': 'val', ...} → {feeder_1: 'val', ...}"""
    cols = {}
    for key, val in feeders.items():
        # Normalize "FEEDER 1", "Feeder 1", "feeder_1", etc.
        num = "".join(filter(str.isdigit, str(key)))
        if num and 1 <= int(num) <= 8:
            cols[f"feeder_{num}"] = val
    return cols


# ═══════════════════════════════════════════════════════════════════════════
#   CRUD ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/", response_model=MessageResponse, tags=["Health"])
async def root():
    """Health check endpoint."""
    return {"message": "Running Order API is live 🚀", "count": None}


# ── CREATE ──────────────────────────────────────────────────────────────────
@app.post(
    "/orders",
    response_model=ProductionOrderResponse,
    status_code=201,
    tags=["Orders"],
    summary="Create a new production order",
)
async def create_order(
    payload: ProductionOrderCreate,
    session: AsyncSession = Depends(get_session),
):
    feeder_cols = _feeders_to_columns(payload.feeders)

    order = ProductionOrder(
        order_date=payload.order_date,
        mc_no=payload.mc_no,
        design_no=payload.design_no,
        beam=payload.beam,
        rate=payload.rate,
        piece=payload.piece,
        **feeder_cols,
    )
    session.add(order)
    await session.commit()
    await session.refresh(order)
    return order.to_dict()


# ── READ ALL / FILTERED ────────────────────────────────────────────────────
@app.get(
    "/orders",
    response_model=List[ProductionOrderResponse],
    tags=["Orders"],
    summary="List orders (optional filters: date, machine)",
)
async def list_orders(
    order_date: Optional[date] = Query(None, description="Filter by order date (YYYY-MM-DD)"),
    mc_no: Optional[str] = Query(None, description="Filter by machine number"),
    session: AsyncSession = Depends(get_session),
):
    query = select(ProductionOrder)

    if order_date:
        query = query.where(ProductionOrder.order_date == order_date)
    if mc_no:
        query = query.where(ProductionOrder.mc_no == mc_no.strip())

    query = query.order_by(ProductionOrder.mc_no, ProductionOrder.design_no)

    result = await session.execute(query)
    orders = result.scalars().all()
    return [o.to_dict() for o in orders]


# ── READ ONE ────────────────────────────────────────────────────────────────
@app.get(
    "/orders/{order_id}",
    response_model=ProductionOrderResponse,
    tags=["Orders"],
    summary="Get a single order by ID",
)
async def get_order(
    order_id: int,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ProductionOrder).where(ProductionOrder.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order.to_dict()


# ── UPDATE ──────────────────────────────────────────────────────────────────
@app.patch(
    "/orders/{order_id}",
    response_model=ProductionOrderResponse,
    tags=["Orders"],
    summary="Partially update an order",
)
async def update_order(
    order_id: int,
    payload: ProductionOrderUpdate,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ProductionOrder).where(ProductionOrder.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    update_data = payload.model_dump(exclude_unset=True)

    # Handle feeders separately
    feeders = update_data.pop("feeders", None)
    if feeders is not None:
        feeder_cols = _feeders_to_columns(feeders)
        for k, v in feeder_cols.items():
            setattr(order, k, v)

    # Set remaining scalar fields
    for field, value in update_data.items():
        setattr(order, field, value)

    await session.commit()
    await session.refresh(order)
    return order.to_dict()


# ── DELETE ONE ──────────────────────────────────────────────────────────────
@app.delete(
    "/orders/{order_id}",
    response_model=MessageResponse,
    tags=["Orders"],
    summary="Delete an order by ID",
)
async def delete_order(
    order_id: int,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ProductionOrder).where(ProductionOrder.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    await session.delete(order)
    await session.commit()
    return {"message": f"Order {order_id} deleted successfully"}


# ── DELETE ALL ──────────────────────────────────────────────────────────────
@app.delete(
    "/orders",
    response_model=MessageResponse,
    tags=["Orders"],
    summary="Delete all orders (use with caution)",
)
async def delete_all_orders(
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(sql_delete(ProductionOrder))
    await session.commit()
    return {"message": "All orders deleted", "count": result.rowcount}


# ═══════════════════════════════════════════════════════════════════════════
#   WORKFLOW / PROCESSING ENDPOINT
# ═══════════════════════════════════════════════════════════════════════════

@app.get(
    "/orders/process/schedule",
    response_model=List[ProcessedOrderResponse],
    tags=["Workflow"],
    summary="Process orders through the LangGraph workflow and return the schedule",
)
async def process_schedule(
    order_date: Optional[date] = Query(None),
    mc_no: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """
    Fetches orders (with optional filters), runs them through the
    LangGraph workflow, and returns the processed production schedule
    with piece values divided by 3.
    """
    query = select(ProductionOrder)
    if order_date:
        query = query.where(ProductionOrder.order_date == order_date)
    if mc_no:
        query = query.where(ProductionOrder.mc_no == mc_no.strip())

    result = await session.execute(query)
    orders = result.scalars().all()

    if not orders:
        return []

    # Convert DB rows → dicts compatible with the LangGraph workflow
    input_data = []
    for o in orders:
        d = o.to_dict()
        input_data.append({
            "Order Date": d["order_date"],
            "M/C No.": d["mc_no"],
            "Design No": d["design_no"],
            "Beam": d["beam"],
            "Rate": d["rate"],
            "Piece": d["piece"],
            "Feeders": d["feeders"],
        })

    # Run the LangGraph workflow
    from workflow import build_workflow
    import pandas as pd

    wf = build_workflow()
    state = {"input_data": input_data, "processed_data": pd.DataFrame()}
    result = wf.invoke(state)
    processed_df = result["processed_data"]

    # Convert DataFrame → response list
    schedule = []
    for _, row in processed_df.iterrows():
        feeders = {
            k: str(v) for k, v in row.items()
            if "FEEDER" in str(k).upper() and pd.notna(v) and str(v).strip()
        }
        schedule.append(ProcessedOrderResponse(
            order_date=str(row.get("ORDER DATE", "")),
            mc_no=str(row.get("M/C No.", "")),
            design_no=str(row.get("DESIGN NO", "")),
            beam=str(row.get("BEAM", "")) if pd.notna(row.get("BEAM")) else None,
            rate=float(row.get("RATE", 0)),
            piece_divided_by_3=float(row.get("PIECE (Divided by 3)", 0)),
            feeders=feeders,
        ))

    return schedule
