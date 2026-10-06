"""
FastAPI application for Production Data Entry & Planner.

Provides REST API endpoints for:
  - Creating, reading, updating, deleting production orders
  - Processing orders through the LangGraph workflow
  - Filtering orders by date and machine number

Uses the existing database module and deployment configuration.
The built-in interface is available at /planner.
Set CORS_ORIGINS to a comma-separated allowlist for cross-origin frontends.
"""

from contextlib import asynccontextmanager
from datetime import date
import logging
import os
import re
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, Query, Path
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError, IntegrityError
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

# Cross-origin access is opt-in. The built-in planner uses same-origin requests.
_origins = [item.strip() for item in os.getenv("CORS_ORIGINS", "").split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=bool(_origins) and "*" not in _origins,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)


# ── Helper: map feeders dict → model columns ──────────────────────────────
def _feeders_to_columns(feeders: dict) -> dict:
    """Convert {'FEEDER 1': 'val', ...} → {feeder_1: 'val', ...}"""
    return {f"feeder_{key.rsplit(' ', 1)[1]}": value
            for key, value in feeders.items()}


async def _commit(session):
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        raise


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    # Exclude raw inputs/context: NaN/Infinity and exception objects are not JSON-safe.
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]})


@app.exception_handler(SQLAlchemyError)
async def database_error_handler(request, exc):
    logging.getLogger(__name__).error("Database operation failed", exc_info=exc)
    if isinstance(exc, IntegrityError):
        return JSONResponse(status_code=409, content={"detail": "This change conflicts with an existing record. Refresh and review the order."})
    return JSONResponse(status_code=503, content={"detail": "The database is unavailable. Refresh the order list before retrying a save."})



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
    await _commit(session)
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

    query = query.order_by(ProductionOrder.order_date.desc(), ProductionOrder.mc_no, ProductionOrder.design_no, ProductionOrder.id)

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
    order_id: int = Path(..., ge=1),
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
    payload: ProductionOrderUpdate,
    order_id: int = Path(..., ge=1),
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
        for number in range(1, 9):
            column = f"feeder_{number}"
            setattr(order, column, feeder_cols.get(column, ""))

    # Set remaining scalar fields
    for field, value in update_data.items():
        setattr(order, field, value)

    await _commit(session)
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
    order_id: int = Path(..., ge=1),
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(
        select(ProductionOrder).where(ProductionOrder.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    await session.delete(order)
    await _commit(session)
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
    await _commit(session)
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

    query = query.order_by(ProductionOrder.order_date, ProductionOrder.id)
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
    try:
        result = await wf.ainvoke(state)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Schedule contains invalid stored data: {exc}") from exc
    processed_df = result["processed_data"]

    # Convert DataFrame → response list
    schedule = []
    for _, row in processed_df.iterrows():
        feeders = {
            k: str(v) for k, v in row.items()
            if re.fullmatch(r"FEEDER [1-8]", str(k)) and pd.notna(v) and str(v).strip()
        }
        schedule.append(ProcessedOrderResponse(
            mc_no=str(row.get("M/C No.", "")),
            design_no=str(row.get("DESIGN NO", "")),
            beam=str(row.get("BEAM", "")) if pd.notna(row.get("BEAM")) else None,
            piece_divided_by_3=float(row.get("PIECE (Divided by 3)", 0)),
            feeders=feeders,
        ))

    return schedule


# Kept inline so deployment still uses the same three application files.
# The original JSON health endpoint remains at /; open /planner for the UI.
PLANNER_HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Running Order · Production planner</title>
<style>
:root{font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#152b31;background:#f2f5f4;--brand:#126557;--line:#dce5e2;--muted:#62746f}*{box-sizing:border-box}body{margin:0}button,input,select{font:inherit}button{cursor:pointer;border:1px solid var(--line);border-radius:9px;background:white;padding:10px 16px;color:inherit;font-weight:600}button:hover{background:#eaf3ef}button:disabled{opacity:.5;cursor:wait}button.primary{background:var(--brand);color:white;border-color:var(--brand)}button.danger{color:#ac3232}button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:3px solid #84bdae;outline-offset:2px}header{background:#fff;border-bottom:1px solid var(--line);padding:22px max(24px,calc((100vw - 1360px)/2));display:flex;justify-content:space-between;align-items:center;gap:18px}h1{font-size:24px;margin:2px 0}h2{font-size:18px;margin:0 0 18px}p{margin:6px 0}.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:2px;color:var(--brand);font-weight:750}.subtle{color:var(--muted);font-size:13px}main{max-width:1408px;margin:auto;padding:24px;display:grid;grid-template-columns:350px minmax(0,1fr);gap:22px;align-items:start}.panel{border:1px solid var(--line);border-radius:16px;background:white;padding:22px;box-shadow:0 4px 16px #17392d04}.stack{display:grid;gap:18px}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.between{justify-content:space-between}.fields{display:grid;grid-template-columns:1fr 1fr;gap:14px}.full{grid-column:1/-1}label{display:block;font-size:12px;font-weight:650;margin-bottom:6px}input,select{width:100%;min-width:0;border:1px solid #cbd9d3;border-radius:8px;background:#fff;padding:10px 11px;color:#152b31}input::placeholder{color:#96a49f;font-weight:400}fieldset{border:0;padding:0;margin:18px 0}legend{font-size:12px;font-weight:650;margin-bottom:8px}.choices{display:flex;flex-wrap:wrap;gap:6px}.choices button{min-width:31px;padding:6px 8px}.choices button[aria-pressed=true]{background:#e0f0e9;border-color:var(--brand);color:var(--brand)}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1}.filters{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.filters>div{flex:1;min-width:135px}.tabs{display:flex;gap:8px;border-bottom:1px solid var(--line);margin-bottom:18px;padding-bottom:14px}.tabs button[aria-pressed=true]{background:var(--brand);color:white}.summary{display:flex;gap:24px;padding:16px 0;color:var(--muted);font-size:12px}.summary strong{font-size:22px;color:#183b31;display:block}.table-wrap{overflow:auto;max-height:65vh}table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;background:#f2f6f4;color:#5a7066;font-size:11px;letter-spacing:.03em;text-transform:uppercase;white-space:nowrap;position:sticky;top:0}th,td{padding:12px 10px;border-bottom:1px solid #e5ece8;vertical-align:top}td{overflow-wrap:anywhere}td button{padding:5px 9px;font-size:12px;margin:2px}.num{text-align:right;font-variant-numeric:tabular-nums}.tag{display:inline-block;background:#edf3f0;padding:2px 6px;border-radius:5px;margin:2px;font-size:11px}.empty{text-align:center;padding:50px 15px;color:var(--muted)}#status{max-width:1360px;margin:16px auto 0;padding:12px 18px;border-radius:9px;background:#e3f2eb}#status[data-error=true]{color:#942d2d;background:#ffeded}#status:empty{display:none}[hidden]{display:none!important}.print-hint{margin-top:12px}.print-root{position:absolute;left:-10000px;top:0;width:4in;color:#000;background:white}.print-page{width:4in;height:6in;padding:.14in;overflow:visible;display:flex;flex-direction:column;font:9pt/1.2 Arial,sans-serif;break-after:page;page-break-after:always}.print-page:last-child{break-after:auto;page-break-after:auto}.print-heading{font:bold 12pt Arial;margin-bottom:4px}.print-meta{font-size:8pt;margin-bottom:8px;overflow-wrap:anywhere}.print-content{flex:1;min-height:0}.print-page table{table-layout:fixed;font:inherit;width:100%;color:#000}.print-page th,.print-page td{padding:4px;border:1px solid #444;position:static;font:inherit;color:#000;background:white;white-space:normal;overflow-wrap:anywhere}.print-page th{font-weight:bold}.print-page tbody{break-inside:avoid;page-break-inside:avoid}.print-page .feeder-cell{font-size:.93em}.print-footer{font:8pt Arial;padding-top:7px;min-height:20px}.print-root .tag{background:none}dialog{border:1px solid var(--line);border-radius:16px;padding:26px;max-width:420px;width:calc(100% - 36px)}dialog::backdrop{background:#102c2755}dialog .row{justify-content:flex-end;margin-top:20px}@media(max-width:1000px){main{grid-template-columns:300px minmax(0,1fr);padding:16px;gap:14px}.panel{padding:16px}}@media(max-width:760px){main{grid-template-columns:1fr}header{padding:18px;align-items:start}h1{font-size:21px}.table-wrap{max-height:none}.fields{gap:12px}#status{margin:12px 16px 0}}@page{size:4in 6in;margin:0}@media print{html,body{margin:0!important;padding:0!important;background:white!important}body> :not(#print-root){display:none!important}.print-root{position:static!important;width:4in;margin:0}.print-page{box-shadow:none;border-radius:0}thead{display:table-header-group}}
</style></head><body>
<header><div><div class="eyebrow">Production workspace</div><h1>Running Order</h1><p class="subtle">Daily entries. Clear schedules. Ready for the floor.</p></div><button id="new-order" type="button">＋ New order</button></header>
<div id="status" role="status" aria-live="polite"></div>
<main><section class="panel"><h2 id="form-title">New production order</h2><form id="order-form"><div class="fields">
<div class="full"><label for="order-date">Order date *</label><input id="order-date" type="date" required></div>
<div><label for="machine">Machine no. *</label><input id="machine" maxlength="80" placeholder="e.g. M-01" required></div>
<div><label for="design">Design no. *</label><input id="design" maxlength="120" placeholder="e.g. KT-0049" required></div>
<div class="full"><label for="beam">Beam</label><input id="beam" maxlength="160" placeholder="Enter beam reference"></div>
<div><label for="rate">Rate</label><input id="rate" type="number" min="0" step="any" placeholder="Enter rate" inputmode="decimal"></div>
<div><label for="piece">Pieces</label><input id="piece" type="number" min="0" step="any" placeholder="Enter pieces" inputmode="decimal"></div>
</div><fieldset><legend>How many feeders?</legend><div class="choices" id="feeder-count" role="group" aria-label="Feeder count"></div></fieldset><div id="feeder-inputs" class="fields"></div><p class="subtle" style="margin-top:12px">Blank rate and pieces are saved as 0.</p><div class="actions"><button type="submit" class="primary" id="save-order">Save order</button><button type="button" id="cancel-edit" hidden>Cancel</button></div></form></section>
<section class="panel"><div class="tabs" role="group" aria-label="View"><button type="button" id="orders-tab" aria-pressed="true">Orders</button><button type="button" id="schedule-tab" aria-pressed="false">Schedule</button></div><div class="filters"><div><label for="filter-date">Filter by date</label><input id="filter-date" type="date"></div><div><label for="filter-machine">Machine</label><input id="filter-machine" maxlength="80" placeholder="All machines"></div><button id="apply-filter">Apply</button><button id="clear-filter">Clear</button><button id="refresh">Refresh</button></div><div class="summary"><div><strong id="record-count">—</strong><span id="count-label">orders</span></div><div><strong id="piece-total">—</strong><span id="total-label">total pieces</span></div></div><div class="row between"><h2 id="view-title">Production orders</h2><button class="primary" id="print-schedule" hidden>Print 4 × 6</button></div><div id="table-container" class="table-wrap" aria-busy="false"></div><p id="print-hint" class="subtle print-hint" hidden>4 × 6-inch portrait paper · 100% scale · headers and footers off. Large schedules continue on additional labels. Schedule quantities are pieces ÷ 3.</p></section></main>
<div id="print-root" class="print-root" aria-hidden="true"></div>
<dialog id="confirm-dialog" aria-labelledby="confirm-title"><h2 id="confirm-title">Delete this order?</h2><p id="confirm-description"></p><div class="row"><button id="keep-order">Keep order</button><button id="confirm-delete" class="danger">Delete order</button></div></dialog>
<script>
'use strict';
const $=id=>document.getElementById(id), API=new URL('.',location.href).pathname.replace(/\/$/,'');
let orders=[],schedule=[],editingId=null,feederCount=3,view='orders',generation=0,loading=false,saving=false,deleting=false,dirty=false;
let activeFilters={date:'',machine:''};
const formatter=new Intl.NumberFormat(undefined,{maximumFractionDigits:3});
const number=value=>formatter.format(Number(value)||0);
const today=()=>{const d=new Date();return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`};
function announce(message,error=false){$('status').textContent=message;$('status').dataset.error=String(error)}
function element(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node}
async function request(path,options={}){const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);try{const response=await fetch(API+path,{...options,signal:controller.signal,headers:{'Accept':'application/json',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});const data=await response.json().catch(()=>null);if(!response.ok){const detail=data?.detail;throw new Error(Array.isArray(detail)?detail.map(e=>`${e.loc.slice(1).join(' ')}: ${e.msg}`).join('; '):typeof detail==='string'?detail:`Request failed (${response.status}).`)}if(data===null)throw new Error('The server returned an invalid response. Refresh before retrying.');return data}catch(error){if(error.name==='AbortError')throw new Error('The request timed out. Refresh the list before retrying a save.');if(error instanceof TypeError)throw new Error('Connection failed. Refresh the list before retrying a save.');throw error}finally{clearTimeout(timer)}}
function setFeeders(count,values=null){const current=values||Object.fromEntries(Array.from($('feeder-inputs').querySelectorAll('input')).map(n=>[n.dataset.key,n.value]));feederCount=count;$('feeder-inputs').replaceChildren();for(let i=1;i<=count;i++){const key=`FEEDER ${i}`,wrap=element('div'),label=element('label',`Feeder ${i}`),input=element('input');label.htmlFor=`feeder-${i}`;input.id=label.htmlFor;input.dataset.key=key;input.maxLength=100;input.placeholder='Colour / yarn';input.value=current[key]||'';wrap.append(label,input);$('feeder-inputs').append(wrap)}for(const button of $('feeder-count').children)button.setAttribute('aria-pressed',String(Number(button.dataset.count)===count))}
for(let i=0;i<=8;i++){const button=element('button',String(i));button.type='button';button.dataset.count=i;button.setAttribute('aria-label',`${i} feeders`);button.onclick=()=>{if(i<feederCount&&Array.from($('feeder-inputs').querySelectorAll('input')).slice(i).some(n=>n.value.trim())&&!window.confirm('Remove the values in the extra feeders?'))return;dirty=true;setFeeders(i)};$('feeder-count').append(button)}
function resetForm(){dirty=false;editingId=null;$('order-form').reset();$('order-date').value=today();$('form-title').textContent='New production order';$('save-order').textContent='Save order';$('cancel-edit').hidden=true;setFeeders(3,{})}
function editOrder(order){if(saving)return;if(dirty&&!window.confirm('Discard your unsaved changes?'))return;dirty=false;editingId=order.id;$('order-date').value=(order.order_date||'').slice(0,10);$('machine').value=order.mc_no;$('design').value=order.design_no;$('beam').value=order.beam||'';$('rate').value=order.rate;$('piece').value=order.piece;const feeders=order.feeders||{},keys=Object.keys(feeders).filter(k=>feeders[k]);setFeeders(Math.max(0,...keys.map(k=>Number(k.match(/[1-8]$/)?.[0])||0)),feeders);$('form-title').textContent='Edit production order';$('save-order').textContent='Save changes';$('cancel-edit').hidden=false;$('order-form').scrollIntoView({behavior:'smooth',block:'start'});$('machine').focus({preventScroll:true})}
function query(){const params=new URLSearchParams();if(activeFilters.date)params.set('order_date',activeFilters.date);if(activeFilters.machine)params.set('mc_no',activeFilters.machine);return params.size?'?'+params.toString():''}
function updateBusy(){for(const id of ['new-order','cancel-edit','save-order'])$(id).disabled=saving;$('print-schedule').disabled=loading||!schedule.length;$('table-container').setAttribute('aria-busy',String(loading))}
async function load(){const token=++generation;loading=true;updateBusy();$('table-container').replaceChildren(element('div','Loading…','empty'));try{const data=await request((view==='orders'?'/orders':'/orders/process/schedule')+query());if(token!==generation)return;if(!Array.isArray(data))throw new Error('The server returned an invalid list.');if(view==='orders')orders=data;else schedule=data;if($('status').dataset.error==='true')announce('');render()}catch(error){if(token!==generation)return;if(view==='orders')orders=[];else schedule=[];$('record-count').textContent='—';$('piece-total').textContent='—';$('table-container').replaceChildren(element('div','Unable to load. Use Refresh to try again.','empty'));announce(error.message,true)}finally{if(token===generation){loading=false;updateBusy()}}}
function render(){const data=view==='orders'?orders:schedule;const isSchedule=view==='schedule';$('record-count').textContent=data.length;$('piece-total').textContent=number(data.reduce((sum,o)=>sum+Number(isSchedule?o.piece_divided_by_3:o.piece),0));$('total-label').textContent=isSchedule?'total pieces ÷ 3':'total pieces';$('count-label').textContent=isSchedule?'schedule entries':'orders';const host=$('table-container');host.replaceChildren();if(!data.length){host.append(element('div','No orders match these filters. Add an order or clear the filters.','empty'));return}const table=element('table'),head=element('thead'),tr=element('tr');for(const title of isSchedule?['Machine','Design','Beam','Pieces ÷ 3','Feeders']:['Date','Machine / design','Beam','Rate','Pieces','Actions']){const th=element('th',title);th.scope='col';tr.append(th)}head.append(tr);table.append(head);const body=element('tbody');for(const order of data){const row=element('tr');if(isSchedule){for(const key of ['mc_no','design_no','beam'])row.append(element('td',order[key]||'—'));row.append(element('td',number(order.piece_divided_by_3),'num'));const cell=element('td');for(const [key,val] of Object.entries(order.feeders||{}))if(val)cell.append(element('span',`${key.replace('FEEDER ','F')}: ${val}`,'tag'));row.append(cell)}else{row.append(element('td',(order.order_date||'').slice(0,10)));const machine=element('td');machine.append(element('strong',order.mc_no),element('div',order.design_no,'subtle'));row.append(machine,element('td',order.beam||'—'),element('td',number(order.rate),'num'),element('td',number(order.piece),'num'));const actions=element('td'),edit=element('button','Edit'),del=element('button','Delete','danger');edit.setAttribute('aria-label',`Edit ${order.mc_no} ${order.design_no}`);del.setAttribute('aria-label',`Delete ${order.mc_no} ${order.design_no}`);edit.onclick=()=>editOrder(order);del.onclick=()=>confirmDelete(order);actions.append(edit,del);row.append(actions)}body.append(row)}table.append(body);host.append(table)}
function switchView(next){view=next;$('orders-tab').setAttribute('aria-pressed',String(view==='orders'));$('schedule-tab').setAttribute('aria-pressed',String(view==='schedule'));$('view-title').textContent=view==='orders'?'Production orders':'Production schedule';$('print-schedule').hidden=view!=='schedule';$('print-hint').hidden=view!=='schedule';$('print-root').replaceChildren();load()}
$('order-form').onsubmit=async event=>{event.preventDefault();if(saving)return;const form=$('order-form');if(!form.reportValidity())return;const payload={order_date:$('order-date').value,mc_no:$('machine').value.trim(),design_no:$('design').value.trim(),beam:$('beam').value.trim()||null,rate:$('rate').value===''?0:Number($('rate').value),piece:$('piece').value===''?0:Number($('piece').value),feeders:{}};if(!payload.mc_no||!payload.design_no){announce('Enter a machine number and design number.',true);return}if(!Number.isFinite(payload.rate)||!Number.isFinite(payload.piece)){announce('Rate and pieces must be finite numbers.',true);return}for(const input of $('feeder-inputs').querySelectorAll('input'))payload.feeders[input.dataset.key]=input.value.trim();saving=true;updateBusy();const id=editingId;for(const input of form.querySelectorAll('input,button'))input.disabled=true;try{await request(id===null?'/orders':`/orders/${id}`,{method:id===null?'POST':'PATCH',body:JSON.stringify(payload)});resetForm();announce(id===null?'Order saved.':'Changes saved.');await load()}catch(error){announce(error.message,true)}finally{saving=false;for(const input of form.querySelectorAll('input,button'))input.disabled=false;updateBusy()}};
let pendingDelete=null;
function confirmDelete(order){if(deleting||saving)return;pendingDelete=order;$('confirm-description').textContent=`${order.mc_no} · ${order.design_no}. This will permanently remove the order.`;$('confirm-dialog').showModal();$('keep-order').focus()}
$('keep-order').onclick=()=>$('confirm-dialog').close();$('confirm-dialog').addEventListener('cancel',event=>{if(deleting)event.preventDefault()});
$('confirm-delete').onclick=async()=>{if(!pendingDelete||deleting)return;deleting=true;$('confirm-delete').disabled=true;$('keep-order').disabled=true;try{await request(`/orders/${pendingDelete.id}`,{method:'DELETE'});if(editingId===pendingDelete.id)resetForm();$('confirm-dialog').close();announce('Order deleted.');await load()}catch(error){$('confirm-dialog').close();announce(error.message,true)}finally{deleting=false;$('confirm-delete').disabled=false;$('keep-order').disabled=false;pendingDelete=null}};
$('orders-tab').onclick=()=>switchView('orders');$('schedule-tab').onclick=()=>switchView('schedule');$('apply-filter').onclick=()=>{activeFilters={date:$('filter-date').value,machine:$('filter-machine').value.trim()};load()};$('clear-filter').onclick=()=>{$('filter-date').value='';$('filter-machine').value='';activeFilters={date:'',machine:''};load()};$('refresh').onclick=()=>load();for(const id of ['filter-date','filter-machine'])$(id).onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();$('apply-filter').click()}};function startNew(){if(saving)return;if(dirty&&!window.confirm('Discard your unsaved changes?'))return;resetForm();$('machine').focus()};$('new-order').onclick=startNew;$('cancel-edit').onclick=startNew;
function createPrintPage(){const page=element('section',undefined,'print-page');const heading=element('div','Production schedule','print-heading');const meta=element('div',`${activeFilters.date||'All dates'} · ${activeFilters.machine||'All machines'} · Pieces ÷ 3`,'print-meta');const content=element('div',undefined,'print-content'),table=element('table'),head=element('thead'),row=element('tr');for(const text of ['Machine','Design','Beam','Pieces ÷ 3']){const th=element('th',text);th.scope='col';row.append(th)}head.append(row);table.append(head);content.append(table);const footer=element('div',undefined,'print-footer');page.append(heading,meta,content,footer);$('print-root').append(page);return {page,content,table,footer}}
function printGroup(order){const body=element('tbody'),row=element('tr');for(const key of ['mc_no','design_no','beam'])row.append(element('td',order[key]||'—'));row.append(element('td',number(order.piece_divided_by_3)));body.append(row);const entries=Object.entries(order.feeders||{}).filter(([,value])=>value);for(let i=0;i<entries.length;i+=2){const feederRow=element('tr');for(let j=0;j<2;j++){const entry=entries[i+j],cell=element('td',entry?`${entry[0]}: ${entry[1]}`:'','feeder-cell');cell.colSpan=2;feederRow.append(cell)}body.append(feederRow)}return body}
function preparePrint(){if(view!=='schedule'||loading||!schedule.length)return false;const root=$('print-root');root.replaceChildren();let current=createPrintPage();const pages=[current];for(const order of schedule){const group=printGroup(order);current.table.append(group);if(current.table.getBoundingClientRect().height>current.content.getBoundingClientRect().height){if(current.table.tBodies.length>1){group.remove();current=createPrintPage();pages.push(current);current.table.append(group)}let size=9;while(current.table.getBoundingClientRect().height>current.content.getBoundingClientRect().height&&size>7){size-=.25;current.table.style.fontSize=`${size}pt`}if(current.table.getBoundingClientRect().height>current.content.getBoundingClientRect().height){root.replaceChildren();announce('An entry is too long for a 4 × 6 label. Shorten its beam, design or feeder text before printing.',true);return false}}}pages.forEach((item,index)=>{item.footer.textContent=`${index+1} / ${pages.length} · ${schedule.length} entries`});return true}
$('print-schedule').onclick=()=>{if(preparePrint())window.print()};window.addEventListener('beforeprint',()=>{if(view==='schedule'){preparePrint();return}const previous=schedule;schedule=orders.map(o=>({...o,piece_divided_by_3:Number(o.piece)/3}));view='schedule';preparePrint();view='orders';schedule=previous});
$('order-form').addEventListener('input',()=>{dirty=true});
window.addEventListener('beforeunload',event=>{if(saving||dirty){event.preventDefault();event.returnValue=''}});
resetForm();load();
</script></body></html>'''


@app.get("/planner", response_class=HTMLResponse, include_in_schema=False)
async def planner():
    """Same-origin UI, including measured 4 × 6-inch schedule pagination."""
    return HTMLResponse(PLANNER_HTML, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
