"""Production Studio — run with: streamlit run order_ui.py.

Uses Neon PostgreSQL to store data and uses API key required. 
PRODUCTION_DATA_FILE can set an absolute path on a persistent LOCAL disk.
CSV writes are atomic and locked between sessions/processes on the same host.
Separate hosts must not write independent copies of this CSV; use the shared
backend for a multi-host deployment. APP_TIMEZONE defaults to Asia/Kolkata.
"""
from collections import Counter
from contextlib import contextmanager
import copy
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import streamlit as st

BASE_COLUMNS = ["Order Date", "M/C No.", "Design No", "Beam", "Rate", "Piece"]
FEEDER_COLUMNS = [f"FEEDER {i}" for i in range(1, 9)]
COLUMNS = BASE_COLUMNS + FEEDER_COLUMNS
ID_COLUMN = "_entry_id"
LOG = logging.getLogger(__name__)
STRETCH = {"width": "stretch"} if "width" in inspect.signature(st.button).parameters else {"use_container_width": True}


def default_data_file():
    """Prefer the app directory; retain an existing legacy working-directory CSV."""
    configured = os.getenv("PRODUCTION_DATA_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    local = Path(__file__).resolve().parent / "backend_raw_data.csv"
    legacy = Path.cwd() / "backend_raw_data.csv"
    return local if local.exists() or not legacy.exists() else legacy.resolve()


DATA_FILE = default_data_file()


def local_today():
    try:
        return dt.datetime.now(ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))).date()
    except ZoneInfoNotFoundError:
        return dt.date.today()


@st.cache_resource
def data_lock():
    return threading.RLock()


@contextmanager
def storage_lock(path):
    """Stable sidecar lock survives atomic CSV replacement; bounded wait."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with data_lock():
        with open(path.with_name(path.name + ".lock"), "a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                def acquire():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                def release():
                    fcntl.flock(handle, fcntl.LOCK_UN)
            deadline = time.monotonic() + 10
            while True:
                try:
                    acquire()
                    break
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Saved entries are busy. Please try again shortly.")
                    time.sleep(.05)
            try:
                yield
            finally:
                release()


def read_records(path=DATA_FILE):
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS + [ID_COLUMN])
    # Validate before pandas can silently rename duplicate column headings.
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, strict=True)
        header = next(reader, [])
        if len(header) != len(set(header)):
            raise ValueError("The saved file has duplicate column headings.")
        missing = set(BASE_COLUMNS) - set(header)
        if missing:
            raise ValueError("Saved data is missing columns: " + ", ".join(sorted(missing)))
        for line, row in enumerate(reader, 2):
            if row and len(row) != len(header):
                raise ValueError(f"Saved CSV row {line} has the wrong number of columns.")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    frame = frame.reindex(columns=list(dict.fromkeys(COLUMNS + [ID_COLUMN] + list(frame.columns))), fill_value="")
    # Deterministic IDs for legacy records preserve identical but distinct rows.
    occurrences = Counter()
    for index, row in frame.iterrows():
        if not str(row[ID_COLUMN]).strip():
            raw = json.dumps([str(row[col]) for col in COLUMNS], ensure_ascii=False)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            occurrences[digest] += 1
            frame.at[index, ID_COLUMN] = f"legacy-{digest}-{occurrences[digest]}"
    if frame[ID_COLUMN].duplicated().any():
        raise ValueError("The saved file has duplicate entry IDs. Resolve them before saving.")
    return frame


def validate_entry(entry):
    """Shared validation for new submissions and stored rows; never silently coerce bad data."""
    value = copy.deepcopy(entry)
    try:
        raw_date = value.get("Order Date")
        if isinstance(raw_date, dt.datetime):
            parsed_date = raw_date.date()
        elif isinstance(raw_date, dt.date):
            parsed_date = raw_date
        else:
            parsed_date = dt.date.fromisoformat(str(raw_date).strip())
    except (TypeError, ValueError):
        raise ValueError("Enter a valid order date (YYYY-MM-DD).") from None
    value["Order Date"] = parsed_date
    for name, limit in [("M/C No.", 80), ("Design No", 120), ("Beam", 160)]:
        text = str(value.get(name) or "").strip()
        if name != "Beam" and not text:
            raise ValueError(f"{name} is required.")
        if len(text) > limit:
            raise ValueError(f"{name} must be at most {limit} characters.")
        value[name] = text
    for name in ("Rate", "Piece"):
        raw = value.get(name)
        if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
            raise ValueError(f"{name} is required.")
        try:
            number = Decimal(str(raw).strip())
        except InvalidOperation:
            raise ValueError(f"{name} must be a valid number.") from None
        if not number.is_finite() or number < 0 or number > Decimal("9007199254740991"):
            raise ValueError(f"{name} must be a finite, non-negative number within the supported range.")
        if name == "Piece" and (number < 1 or number != number.to_integral_value()):
            raise ValueError("Pieces must be a whole number of at least 1.")
        value[name] = int(number) if name == "Piece" else float(number)
    feeders = value.get("Feeders") or {}
    if not isinstance(feeders, dict) or any(key not in FEEDER_COLUMNS for key in feeders):
        raise ValueError("Use feeder numbers 1 through 8.")
    value["Feeders"] = {}
    for key, raw in feeders.items():
        if not isinstance(raw, str):
            raise ValueError(f"{key} must contain text.")
        text = raw.strip()
        if len(text) > 100:
            raise ValueError(f"{key} must be at most 100 characters.")
        if text:
            value["Feeders"][key] = text
    return value


def save_entry(entry, path=DATA_FILE, entry_id=None):
    """Append once per submission token, with process lock and atomic replacement."""
    path = Path(path)
    entry = validate_entry(entry)
    entry_id = entry_id or str(uuid.uuid4())
    row = {key: entry[key] for key in BASE_COLUMNS}
    row.update(entry["Feeders"])
    row[ID_COLUMN] = entry_id
    temporary = None
    with storage_lock(path):
        try:
            existing = read_records(path)
            if entry_id in existing[ID_COLUMN].values:
                stored = records_to_entries(existing.loc[existing[ID_COLUMN] == entry_id])[0]
                if entry_fingerprint(stored) != entry_fingerprint(entry):
                    raise ValueError("This submission was already saved with different values. Reload saved entries before starting a new entry.")
                return entry_id
            new_row = pd.DataFrame([{column: str(row.get(column, "")) for column in existing.columns}])
            updated = pd.concat([existing, new_row], ignore_index=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent,
                                             prefix=".production-", suffix=".csv", delete=False) as handle:
                temporary = handle.name
                updated.to_csv(handle, index=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return entry_id


def records_to_entries(frame):
    entries = []
    for row_number, rec in enumerate(frame.to_dict("records"), 2):
        try:
            value = validate_entry({**{key: rec[key] for key in BASE_COLUMNS},
                                    "Feeders": {key: rec.get(key, "") for key in FEEDER_COLUMNS}})
        except ValueError as exc:
            raise ValueError(f"Saved record {row_number - 1}: {exc}") from exc
        if rec.get(ID_COLUMN):
            value[ID_COLUMN] = rec[ID_COLUMN]
        entries.append(value)
    return entries


def entry_fingerprint(entry):
    value = validate_entry(entry)
    return tuple(str(value[key]) for key in BASE_COLUMNS) + tuple(value["Feeders"].get(key, "") for key in FEEDER_COLUMNS)


def merge_entries(current, loaded):
    """Linear-time merging, while retaining separately saved identical orders."""
    output = list(current)
    ids = {row[ID_COLUMN] for row in output if row.get(ID_COLUMN)}
    # Match old session data (without IDs) to saved records one occurrence at a time.
    legacy = Counter(entry_fingerprint(row) for row in output if not row.get(ID_COLUMN))
    occurrences = Counter()
    totals = Counter(entry_fingerprint(row) for row in output)
    added = 0
    for row in loaded:
        fingerprint = entry_fingerprint(row)
        identity = row.get(ID_COLUMN)
        if identity:
            if identity in ids:
                continue
            ids.add(identity)
            if legacy[fingerprint]:
                legacy[fingerprint] -= 1
                continue
        else:
            occurrences[fingerprint] += 1
            if totals[fingerprint] >= occurrences[fingerprint]:
                continue
            totals[fingerprint] += 1
        output.append(row)
        added += 1
    return output, added


def reset_entry():
    for key in ("mc_no", "design_no", "beam"):
        st.session_state[key] = ""
    st.session_state.rate = None
    st.session_state.piece = None
    st.session_state.feeder_drafts = {}
    for i in range(1, 9):
        st.session_state[f"feeder_{i}"] = ""
    st.session_state.submission_token = str(uuid.uuid4())


def remember_feeder(key):
    st.session_state.setdefault("feeder_drafts", {})[key] = st.session_state.get(key, "")


def keep_feeder_selection():
    """A selected segment cannot be cleared into an invalid None feeder count."""
    selected = st.session_state.get("feeder_count")
    if selected is None:
        st.session_state.feeder_count = st.session_state.get("last_feeder_count", 6)
    else:
        st.session_state.last_feeder_count = selected


def invalidate_schedule():
    st.session_state.pop("processed_schedule", None)


@st.cache_resource
def get_workflow():
    from workflow import build_workflow
    return build_workflow()


def format_number(value):
    return f"{float(value):,.3f}".rstrip("0").rstrip(".") if float(value) % 1 else f"{int(value):,}"


def safe_csv(frame):
    """Mitigate spreadsheet formula execution in downloaded text fields."""
    output = frame.copy()
    for column in output.columns:
        output[column] = output[column].map(
            lambda value: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value)
    return output.to_csv(index=False).encode("utf-8-sig")


def make_print_html(processed, dates, entries_per_page=2):
    """Physical 4×6 portrait labels; keep the existing attributes-down table layout."""
    columns = [name for name in ["M/C No.", "DESIGN NO", "BEAM", "PIECE"] if name in processed.columns]
    columns += [name for name in FEEDER_COLUMNS if name in processed.columns and processed[name].fillna("").astype(str).str.strip().ne("").any()]
    rows = []
    for record in processed.to_dict("records"):
        rows.append({key: ("" if pd.isna(record.get(key)) else format_number(record[key]) if key == "PIECE)" else str(record[key])) for key in columns})
    payload = json.dumps({"rows": rows, "columns": columns, "dates": dates, "perPage": int(entries_per_page)}, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>
*{box-sizing:border-box}body{margin:0;background:#eef2f0;color:#172c26;font:13px/1.4 system-ui,sans-serif}.toolbar{position:sticky;top:0;background:#fff;padding:12px 14px;z-index:2;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #d6e1db}button{background:#176753;color:white;border:0;border-radius:8px;padding:10px 16px;font-weight:650;cursor:pointer}button:disabled{opacity:.5;cursor:not-allowed}button:focus-visible{outline:3px solid #60ab94;outline-offset:3px}.hint{font-size:12px;color:#51675c}.error{color:#a22626;padding:10px}#pages{padding:14px;display:grid;gap:16px;justify-content:center}.page{width:4in;height:6in;padding:.15in;background:white;display:flex;flex-direction:column;box-shadow:0 2px 8px #1e3b2020;font:9pt/1.15 Arial,sans-serif;color:#000}.title{font-size:12pt;font-weight:bold;margin:0 0 5px}.meta{font-size:8pt;margin:0 0 8px;overflow-wrap:anywhere}.content{flex:1;min-height:0}table{border-collapse:collapse;width:100%;table-layout:fixed;font:inherit}th,td{border:1px solid #555;padding:5px 4px;overflow-wrap:anywhere;white-space:pre-wrap;text-align:left;vertical-align:top}th{width:25%;font-weight:bold}td{font-weight:500}.footer{font-size:8pt;padding-top:8px;min-height:22px}tr{break-inside:avoid;page-break-inside:avoid}@page{size:4in 6in;margin:0}@media print{html,body{margin:0!important;padding:0!important;background:white}body>.toolbar,body>#error{display:none!important}#pages{display:block;padding:0}.page{margin:0;box-shadow:none;break-after:page;page-break-after:always}.page:last-child{break-after:auto;page-break-after:auto}}
</style></head><body><div class="toolbar"><button id="print" disabled>Print 4 × 6 schedule</button><span class="hint">Portrait · 100% scale · headers/footers off</span><span class="hint" id="count"></span></div><div id="error" class="error" role="alert"></div><div id="pages"></div><script>
const data=__DATA__;
const labels={'M/C No.':'Machine','DESIGN NO':'Design','BEAM':'Beam','PIECE':'Pieces'};
const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
function page(records){const p=el('section');p.className='page';const title=el('h2','Production schedule');title.className='title';const meta=el('p',data.dates);meta.className='meta';const content=el('div');content.className='content';const table=el('table');for(const key of data.columns){const row=el('tr'),heading=el('th',labels[key]||key);heading.scope='row';row.append(heading);for(const record of records)row.append(el('td',record[key]||'—'));table.append(row)}content.append(table);const footer=el('div',' ');footer.className='footer';p.append(title,meta,content,footer);document.getElementById('pages').append(p);return {p,content,table,footer}}
function fits(item){return item.table.getBoundingClientRect().height<=item.content.getBoundingClientRect().height-1}
function build(){const host=document.getElementById('pages');host.replaceChildren();const pages=[];let offset=0;while(offset<data.rows.length){let count=Math.min(data.perPage,data.rows.length-offset),item;while(count>=1){item=page(data.rows.slice(offset,offset+count));if(fits(item))break;item.p.remove();if(count===1){item=page(data.rows.slice(offset,offset+1));let size=9;while(!fits(item)&&size>7){size-=.25;item.table.style.fontSize=size+'pt'}if(!fits(item)){host.replaceChildren();document.getElementById('error').textContent='An entry is too long for a 4 × 6 label. Shorten its design, beam or feeder text before printing.';document.getElementById('print').disabled=true;return false}break}count--}pages.push(item);offset+=count}pages.forEach((item,i)=>item.footer.textContent=`${i+1} / ${pages.length} · ${data.rows.length} entries · Pieces ÷ 3`);document.getElementById('count').textContent=pages.length+(pages.length===1?' label':' labels');document.getElementById('print').disabled=!pages.length;return true}
document.getElementById('print').onclick=()=>{if(build())window.print()};window.addEventListener('beforeprint',build);build();
</script></body></html>'''.replace("__DATA__", payload)


STYLE = """<style>
.block-container{max-width:1440px;padding-top:4.5rem;padding-bottom:3rem}h1{font-size:2.25rem!important;letter-spacing:-.055em}h2,h3{letter-spacing:-.025em}h3{font-size:1.2rem!important}[data-testid="stAppViewContainer"]{background:#f5f7f6}[data-testid="stSidebar"]{background:#edf2ef}[data-testid="stVerticalBlockBorderWrapper"]>div{border-radius:14px}div[data-testid="stVerticalBlockBorderWrapper"]{background:white;border-radius:14px}[data-testid="stButton"] button,[data-testid="stDownloadButton"] button{border-radius:9px;min-height:42px;font-weight:600}button[kind*="primary"]{background:#176753;border-color:#176753;color:white}button[kind*="primary"]:hover{background:#125340;border-color:#125340}button:focus-visible{outline:3px solid #78b59e!important;outline-offset:2px}[data-testid="stTextInput"] input::placeholder,[data-testid="stNumberInput"] input::placeholder{color:#8b9992;opacity:1}[data-testid="stMetric"]{padding:12px 18px;border:1px solid #dae4de;border-radius:12px;background:#fff}[data-testid="stMetricValue"]{font-size:1.65rem}[data-testid="stRadio"] [role="radiogroup"]{gap:.35rem;flex-wrap:wrap}[data-testid="stRadio"] [role="radiogroup"]>label{border:1px solid #cddbd2;border-radius:8px;padding:.35rem .7rem;margin:0}[data-testid="stRadio"] [role="radiogroup"]>label:has(input:checked){background:#e0eee6;border-color:#176753;color:#125340}[data-testid="stButtonGroup"] [role="radiogroup"]{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;width:100%}[data-testid="stButtonGroup"] button[role="radio"]{border:1px solid #cddbd2;border-radius:8px!important;min-height:40px;width:100%;justify-content:center}[data-testid="stButtonGroup"] button[aria-checked="true"],[data-testid="stButtonGroup"] button[aria-pressed="true"]{background:#176753!important;color:white!important;border-color:#176753!important}.eyebrow{font-size:11px;font-weight:700;letter-spacing:2px;color:#176753;text-transform:uppercase;margin-bottom:4px}.section-note{color:#66786e;font-size:13px}.mode-badge{display:inline-block;border:1px solid #cfddd4;border-radius:20px;padding:5px 12px;font-size:12px;color:#35604a;background:#eaf3ed}@media(max-width:700px){.block-container{padding:4.5rem 1rem 1rem}[data-testid="stMetric"]{padding:10px}[data-testid="stMetricValue"]{font-size:1.3rem}h1{font-size:1.8rem!important}}
@media(prefers-color-scheme:dark){[data-testid="stAppViewContainer"],[data-testid="stSidebar"]{background:inherit}div[data-testid="stVerticalBlockBorderWrapper"],[data-testid="stMetric"]{background:inherit}.section-note{color:#9db0a5}}
</style>"""


def show_error(message, exc):
    if isinstance(exc, (ValueError, TimeoutError, csv.Error, pd.errors.ParserError)):
        st.error(f"{message} {exc}")
    else:
        LOG.warning(message, exc_info=exc)
        st.error(f"{message} Check storage access and available disk space, then retry. Your inputs have been kept.")


def main():
    st.set_page_config(page_title="Production Studio", page_icon="🧵", layout="wide", initial_sidebar_state="expanded")
    st.markdown(STYLE, unsafe_allow_html=True)
    for key, value in {"production_data": [], "feeder_count": 6, "feeder_drafts": {},
                       "submission_token": str(uuid.uuid4()), "confirm_clear": False, "confirm_reset": False}.items():
        st.session_state.setdefault(key, value)
    if st.session_state.pop("reset_after_save", False):
        reset_entry()
    st.markdown('<div class="eyebrow">Production workspace</div>', unsafe_allow_html=True)
    title, status = st.columns([4, 1])
    title.title("Production Studio")
    status.markdown('<p class="mode-badge">Daily production</p>', unsafe_allow_html=True)
    st.caption("Enter orders, review your working list, and print clear machine-wise schedules.")
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))

    with st.sidebar:
        st.subheader("Load saved entries")
        st.caption("Choose records to include in your working list.")
        with st.form("fetch_form"):
            enable_date = st.checkbox("Filter by date", value=True)
            fetch_date = st.date_input("Order date", value=local_today(), key="fetch_date")
            enable_mc = st.checkbox("Filter by machine", value=False)
            fetch_mc = st.text_input("Machine number", placeholder="e.g. 12", key="fetch_mc", max_chars=80)
            replace_list = st.checkbox("Replace current list", value=False,
                                       help="The working list is replaced only when matching entries are found. Saved records stay unchanged.")
            fetch = st.form_submit_button("Load entries", type="primary", **STRETCH)
        if fetch:
            if enable_mc and not fetch_mc.strip():
                st.error("Enter a machine number or turn off that filter.")
            else:
                try:
                    with st.spinner("Loading saved entries…"):
                        with storage_lock(DATA_FILE):
                            frame = read_records()
                        if enable_date:
                            frame = frame.loc[frame["Order Date"].str.strip() == fetch_date.isoformat()]
                        if enable_mc:
                            frame = frame.loc[frame["M/C No."].str.strip() == fetch_mc.strip()]
                        loaded = records_to_entries(frame)
                    if not loaded:
                        st.info("No matching entries. Your current list has been kept.")
                    else:
                        if replace_list:
                            st.session_state.production_data = loaded
                            added = len(loaded)
                        else:
                            merged, added = merge_entries(st.session_state.production_data, loaded)
                            st.session_state.production_data = merged
                        invalidate_schedule()
                        st.success(f"{added} entries {'loaded' if replace_list else 'added'} · {len(loaded)} matches.")
                except (OSError, ValueError, csv.Error, pd.errors.ParserError) as exc:
                    show_error("Could not load saved entries.", exc)
        st.divider()
        st.caption("Saved entries remain after you clear the working list. This screen does not delete saved records.")

    entries = st.session_state.production_data
    metrics = st.columns(3)
    metrics[0].metric("Working entries", len(entries))
    metrics[1].metric("Machines", len({entry["M/C No."] for entry in entries}))
    metrics[2].metric("Total pieces", f"{sum(int(entry['Piece']) for entry in entries):,}")
    st.write("")
    entry_column, review_column = st.columns([1, 1.55], gap="large")
    with entry_column:
        with st.container(border=True):
            st.subheader("New production entry")
            st.caption("Fields marked * are required.")
            order_date = st.date_input("Order date *", value=local_today(), key="order_date")
            machine, design = st.columns(2)
            mc_no = machine.text_input("Machine no. *", key="mc_no", placeholder="e.g. 12", max_chars=80)
            design_no = design.text_input("Design no. *", key="design_no", placeholder="e.g. KT-0049", max_chars=120)
            beam = st.text_input("Beam", key="beam", placeholder="Beam reference (optional)", max_chars=160)
            rate_col, piece_col = st.columns(2)
            rate = rate_col.number_input("Rate (₹) *", min_value=0.0, max_value=9007199254740991.0, value=None,
                                          step=1.0, format="%.2f", placeholder="Enter rate", key="rate")
            piece = piece_col.number_input("Pieces *", min_value=1, max_value=9007199254740991, value=None,
                                            step=1, placeholder="Enter pieces", key="piece")
            st.caption("Rate can be zero. Pieces must be a positive whole number.")
            feeder_help = "Only the selected feeders are saved. Hidden values remain in your draft until you save or reset."
            if hasattr(st, "segmented_control"):
                num_feeders = st.segmented_control("How many feeders?", list(range(1, 9)),
                                                  key="feeder_count", selection_mode="single",
                                                  on_change=keep_feeder_selection, help=feeder_help)
                num_feeders = num_feeders or st.session_state.get("last_feeder_count", 6)
            else:
                num_feeders = st.radio("How many feeders?", list(range(1, 9)), horizontal=True,
                                       key="feeder_count", help=feeder_help)
            drafts = st.session_state.feeder_drafts
            for i in range(1, 9):
                key = f"feeder_{i}"
                if key in st.session_state:
                    drafts[key] = st.session_state[key]
                elif i <= num_feeders:
                    st.session_state[key] = drafts.get(key, "")
            feeder_data = {}
            for start in range(1, num_feeders + 1, 2):
                cols = st.columns(2)
                for offset, i in enumerate(range(start, min(start + 2, num_feeders + 1))):
                    feeder_data[f"FEEDER {i}"] = cols[offset].text_input(
                        f"Feeder {i}", key=f"feeder_{i}", placeholder="Colour / yarn", max_chars=100,
                        on_change=remember_feeder, args=(f"feeder_{i}",)).strip()
            hidden = sum(bool(drafts.get(f"feeder_{i}", "").strip()) for i in range(num_feeders + 1, 9))
            if hidden:
                st.warning(f"{hidden} hidden feeder values will not be saved. Increase the feeder count to include them.")
            save_col, reset_col = st.columns([2, 1])
            submitted = save_col.button("Save entry", type="primary", **STRETCH, key="save_entry")
            if reset_col.button("Reset", **STRETCH, key="reset_draft"):
                st.session_state.confirm_reset = True
            if st.session_state.confirm_reset:
                st.warning("Discard the current entry draft? Saved entries will stay unchanged.")
                yes, no = st.columns(2)
                if yes.button("Discard draft", key="confirm_reset_draft", **STRETCH):
                    st.session_state.confirm_reset = False
                    st.session_state.reset_after_save = True
                    st.rerun()
                if no.button("Keep editing", key="keep_draft", **STRETCH):
                    st.session_state.confirm_reset = False
                    st.rerun()
            if submitted:
                try:
                    entry = validate_entry({"Order Date": order_date, "M/C No.": mc_no, "Design No": design_no,
                                            "Beam": beam, "Rate": rate, "Piece": piece, "Feeders": feeder_data})
                    with st.spinner("Saving entry…"):
                        identity = save_entry(entry, entry_id=st.session_state.submission_token)
                    entry[ID_COLUMN] = identity
                    st.session_state.production_data, _ = merge_entries(st.session_state.production_data, [entry])
                except (OSError, ValueError, csv.Error, pd.errors.ParserError) as exc:
                    show_error("Entry was not saved. Please review and try again.", exc)
                else:
                    invalidate_schedule()
                    st.session_state.confirm_reset = False
                    st.session_state.confirm_clear = False
                    st.session_state.reset_after_save = True
                    st.session_state.notice = "Entry saved. Ready for your next order."
                    st.rerun()

    with review_column:
        with st.container(border=True):
            st.subheader("Current production list")
            st.caption("Only these entries are included in the schedule.")
            if not entries:
                st.info("No entries yet. Save your first order or load saved entries from the sidebar.")
            else:
                raw = pd.DataFrame([{**{key: row[key] for key in BASE_COLUMNS}, **row["Feeders"]} for row in entries]).fillna("")
                st.dataframe(raw, **STRETCH, hide_index=True, height=min(440, 38 + 35 * len(raw)))
                generate, clear = st.columns([2, 1])
                generate_clicked = generate.button("Generate schedule", type="primary", **STRETCH)
                if clear.button("Clear list", **STRETCH):
                    st.session_state.confirm_clear = True
                if st.session_state.confirm_clear:
                    st.warning("Clear the working list? Saved records will stay unchanged.")
                    yes, no = st.columns(2)
                    if yes.button("Clear working list", **STRETCH):
                        st.session_state.production_data = []
                        st.session_state.confirm_clear = False
                        invalidate_schedule()
                        st.rerun()
                    if no.button("Keep list", **STRETCH):
                        st.session_state.confirm_clear = False
                        st.rerun()
                if generate_clicked:
                    try:
                        with st.spinner("Preparing schedule…"):
                            clean = [validate_entry(row) for row in entries]
                            result = get_workflow().invoke({"input_data": clean, "processed_data": pd.DataFrame()})
                            processed = result.get("processed_data")
                            required = {"M/C No.", "DESIGN NO", "BEAM", "PIECE"}
                            if not isinstance(processed, pd.DataFrame) or not required.issubset(processed.columns) or len(processed) != len(entries):
                                raise ValueError("The workflow returned an incomplete schedule.")
                            quantities = pd.to_numeric(processed["PIECE"], errors="coerce")
                            if not quantities.map(lambda n: pd.notna(n) and math.isfinite(n) and n >= 0).all():
                                raise ValueError("The schedule contains invalid quantities.")
                            st.session_state.processed_schedule = processed
                    except Exception as exc:
                        invalidate_schedule()
                        LOG.exception("Schedule generation failed")
                        if isinstance(exc, ImportError):
                            st.error("Schedule generation needs workflow.py and its installed dependencies. Your entries are saved.")
                        else:
                            st.error(f"Could not generate the schedule. Your saved entries are safe. {exc}" if isinstance(exc, ValueError) else "Could not generate the schedule. Your saved entries are safe. Check the application log and try again.")

        if "processed_schedule" in st.session_state:
            with st.container(border=True):
                processed = st.session_state.processed_schedule
                st.subheader("Machine-wise schedule")
                download, density = st.columns(2)
                download.download_button("Download schedule CSV", safe_csv(processed),
                                         "production_schedule.csv", "text/csv", **STRETCH)
                per_page = density.selectbox("Maximum entries per label", [1, 2, 3], index=1,
                                               help="Automatically uses fewer columns when text needs more room.")
                dates = sorted({str(row["Order Date"]) for row in entries})
                date_label = "Order date: " + dates[0] if len(dates) == 1 else f"Orders: {dates[0]} to {dates[-1]} · {len(dates)} dates"
                st.caption("Print preview · 4 × 6-inch portrait paper. Use 100% scale and disable browser headers and footers.")
                print_html = make_print_html(processed, date_label, per_page)
                if hasattr(st, "iframe"):
                    st.iframe(print_html, height=600, alt="4 by 6 inch production schedule preview and print control")
                else:
                    import streamlit.components.v1 as components
                    components.html(print_html, height=600, scrolling=True)


if __name__ == "__main__":
    main()
