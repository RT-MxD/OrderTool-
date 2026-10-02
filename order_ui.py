"""Production entry UI. Run with: streamlit run ui.py

Keep workflow.py and backend_raw_data.csv in the existing app working directory.
"""
import copy
import datetime
import os
from pathlib import Path
import tempfile
import threading

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Production Studio", page_icon="🧵", layout="wide")
DATA_FILE = Path("backend_raw_data.csv")
BASE_COLUMNS = ["Order Date", "M/C No.", "Design No", "Beam", "Rate", "Piece"]
FEEDER_COLUMNS = [f"FEEDER {i}" for i in range(1, 9)]
COLUMNS = BASE_COLUMNS + FEEDER_COLUMNS


@st.cache_resource
def data_lock():
    """Serialize CSV operations across sessions in this Streamlit process."""
    return threading.RLock()


def read_records(path=DATA_FILE):
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS)
    # Keep identifiers such as 001 and literal text such as NA intact.
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = set(BASE_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError("Saved data is missing columns: " + ", ".join(sorted(missing)))
    return frame.reindex(columns=list(dict.fromkeys(COLUMNS + list(frame.columns))), fill_value="")


def save_entry(entry, path=DATA_FILE):
    """Use a consistent schema and atomic replacement, preserving older columns."""
    row = {key: entry[key] for key in BASE_COLUMNS}
    row.update(entry["Feeders"])
    temporary = None
    with data_lock():
        try:
            existing = read_records(path)
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


def records_to_entries(frame):
    entries = []
    for rec in frame.to_dict("records"):
        date = pd.to_datetime(rec["Order Date"], errors="coerce")
        if pd.isna(date):
            raise ValueError("A saved entry has an invalid order date.")
        rate = pd.to_numeric(rec["Rate"], errors="coerce")
        piece = pd.to_numeric(rec["Piece"], errors="coerce")
        if pd.isna(rate) or pd.isna(piece) or rate < 0 or piece <= 0 or piece % 1:
            raise ValueError("A saved entry has an invalid rate or piece quantity.")
        if not rec["M/C No."].strip() or not rec["Design No"].strip():
            raise ValueError("A saved entry is missing its machine or design number.")
        entries.append({
            **{key: rec[key] for key in BASE_COLUMNS},
            "Order Date": date.date(), "Rate": float(rate), "Piece": int(piece),
            "Feeders": {key: rec[key] for key in FEEDER_COLUMNS if rec.get(key, "").strip()},
        })
    return entries


def comparable_entry(entry):
    return {**entry, "Feeders": {k: v for k, v in entry["Feeders"].items() if str(v).strip()}}


def reset_entry():
    for key in ("mc_no", "design_no", "beam"):
        st.session_state[key] = ""
    st.session_state.rate = None
    st.session_state.piece = None
    for i in range(1, 9):
        st.session_state[f"feeder_{i}"] = ""


def invalidate_schedule():
    st.session_state.pop("processed_schedule", None)


st.markdown("""
<style>
.block-container {max-width: 1180px; padding-top: 2.2rem; padding-bottom: 3rem;}
h1 {letter-spacing: -0.04em; font-size: 2.2rem !important;}
h2, h3 {letter-spacing: -0.025em;}
[data-testid="stButton"] button, [data-testid="stDownloadButton"] button {border-radius: 10px; min-height: 42px;}
button[kind="primary"] {background: #4f46e5; border-color: #4f46e5; color: white;}
button[kind="primary"]:hover {background: #4338ca; border-color: #4338ca; color: white;}
[data-testid="stTextInput"] input::placeholder,
[data-testid="stNumberInput"] input::placeholder {color: #9ca3af; opacity: 1;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 14px;}
[data-testid="stRadio"] [role="radiogroup"] {gap: .5rem; flex-wrap: wrap;}
[data-testid="stRadio"] [role="radiogroup"] > label {
    border: 1px solid #cbd5e1; border-radius: 9px; padding: .4rem .9rem; margin: 0;
}
[data-testid="stRadio"] [role="radiogroup"] > label:has(input:checked) {
    background: #eef2ff; border-color: #4f46e5; color: #3730a3;
}
</style>
""", unsafe_allow_html=True)

st.session_state.setdefault("production_data", [])
st.session_state.setdefault("feeder_count", 6)
if st.session_state.pop("reset_after_save", False):
    reset_entry()
if "notice" in st.session_state:
    st.success(st.session_state.pop("notice"))

st.title("Production Studio")
st.caption("Add production entries, review your list, and generate a machine-wise schedule.")

with st.sidebar:
    st.subheader("Saved entries")
    st.caption("Load entries into your current list. Saved records stay in your CSV.")
    with st.form("fetch_form"):
        enable_date = st.checkbox("Filter by order date", value=True)
        fetch_date = st.date_input("Order date", key="fetch_date")
        enable_mc = st.checkbox("Filter by machine", value=False)
        fetch_mc = st.text_input("Machine number", placeholder="e.g. 12", key="fetch_mc")
        replace_list = st.checkbox("Replace current list", value=False,
                                   help="Leave off to keep current entries and add new matches.")
        fetch = st.form_submit_button("Load entries", use_container_width=True)
    if fetch:
        if not enable_date and not enable_mc:
            st.error("Choose at least one filter.")
        elif enable_mc and not fetch_mc.strip():
            st.error("Enter a machine number to filter by.")
        else:
            try:
                with data_lock():
                    frame = read_records()
                if enable_date:
                    dates = pd.to_datetime(frame["Order Date"], errors="coerce")
                    frame = frame.loc[dates.dt.date == fetch_date]
                if enable_mc:
                    frame = frame.loc[frame["M/C No."].str.strip() == fetch_mc.strip()]
                loaded = records_to_entries(frame)
                if not loaded:
                    st.info("No entries match these filters.")
                else:
                    if replace_list:
                        st.session_state.production_data = loaded
                    else:
                        # Respect repeated identical rows in the source without multiplying
                        # them every time the user loads the same filters.
                        current = st.session_state.production_data
                        seen = []
                        normalized = [comparable_entry(entry) for entry in current]
                        for entry in loaded:
                            comparable = comparable_entry(entry)
                            seen.append(comparable)
                            if normalized.count(comparable) < seen.count(comparable):
                                current.append(entry)
                                normalized.append(comparable)
                    invalidate_schedule()
                    st.success(f"Loaded {len(loaded)} matching entries.")
            except (OSError, ValueError, pd.errors.ParserError) as exc:
                st.error(f"Could not load saved entries: {exc}")

with st.container(border=True):
    st.subheader("New production entry")
    st.caption("Machine, design, rate and pieces are required. Beam and feeder values are optional.")
    left, right = st.columns(2)
    with left:
        order_date = st.date_input("Order date", datetime.date.today(), key="order_date")
        mc_no = st.text_input("Machine number *", key="mc_no", placeholder="e.g. 12")
        design_no = st.text_input("Design number *", key="design_no", placeholder="e.g. KT-0049")
    with right:
        beam = st.text_input("Beam", key="beam", placeholder="Enter beam reference")
        rate = st.number_input("Rate (₹) *", min_value=0.0, value=None, step=1.0,
                               format="%.2f", placeholder="Enter rate", key="rate")
        piece = st.number_input("Pieces *", min_value=1, value=None, step=1,
                                placeholder="Enter number of pieces", key="piece")
    st.divider()
    # Outside a form: selection immediately changes the visible feeder inputs.
    num_feeders = st.radio("How many feeders?", list(range(1, 9)), horizontal=True,
                           key="feeder_count", help="Select 1–8. Only selected feeders are saved.")
    # Preserve feeder drafts even when Streamlit removes hidden widget state.
    drafts = st.session_state.setdefault("feeder_drafts", {})
    for i in range(1, 9):
        key = f"feeder_{i}"
        if key in st.session_state:
            drafts[key] = st.session_state[key]
        elif i <= num_feeders:
            st.session_state[key] = drafts.get(key, "")
    feeder_data = {}
    for start in range(1, num_feeders + 1, 4):
        columns = st.columns(4)
        for offset, i in enumerate(range(start, min(start + 4, num_feeders + 1))):
            with columns[offset]:
                feeder_data[f"FEEDER {i}"] = st.text_input(
                    f"Feeder {i}", key=f"feeder_{i}", placeholder="Enter value").strip()
    st.caption(f"{num_feeders} feeders selected · Empty feeder values are allowed.")
    add_col, reset_col = st.columns([3, 1])
    submitted = add_col.button("Save & add entry", type="primary", use_container_width=True)
    if reset_col.button("Reset fields", use_container_width=True):
        st.session_state.reset_after_save = True
        st.session_state.feeder_drafts = {}
        st.rerun()
    if submitted:
        errors = []
        if not mc_no.strip():
            errors.append("Enter a machine number.")
        if not design_no.strip():
            errors.append("Enter a design number.")
        if rate is None:
            errors.append("Enter the rate (zero is allowed).")
        if piece is None:
            errors.append("Enter the number of pieces (at least 1).")
        if errors:
            st.error(" ".join(errors))
        else:
            entry = {"Order Date": order_date, "M/C No.": mc_no.strip(),
                     "Design No": design_no.strip(), "Beam": beam.strip(),
                     "Rate": rate, "Piece": piece, "Feeders": feeder_data}
            try:
                save_entry(entry)
            except (OSError, ValueError, pd.errors.ParserError) as exc:
                st.error(f"Entry was not saved. Your inputs are still here. {exc}")
            else:
                st.session_state.production_data.append(entry)
                invalidate_schedule()
                st.session_state.reset_after_save = True
                st.session_state.feeder_drafts = {}
                st.session_state.notice = "Entry saved and added to your list. Ready for the next entry."
                st.rerun()

entries = st.session_state.production_data
st.subheader("Current production list")
if not entries:
    st.info("Your list is empty. Add an entry above or load saved entries from the sidebar.")
else:
    raw_df = pd.DataFrame([{**{k: v for k, v in row.items() if k != "Feeders"},
                            **row["Feeders"]} for row in entries])
    a, b, c = st.columns(3)
    a.metric("Entries", len(entries))
    b.metric("Machines", raw_df["M/C No."].nunique())
    c.metric("Total pieces", int(pd.to_numeric(raw_df["Piece"], errors="coerce").fillna(0).sum()))
    st.dataframe(raw_df, use_container_width=True, hide_index=True)
    generate, clear = st.columns([3, 1])
    if clear.button("Clear current list", use_container_width=True,
                    help="Removes entries from this screen only. Saved CSV records are kept."):
        st.session_state.production_data = []
        invalidate_schedule()
        st.rerun()
    if generate.button("Generate schedule", type="primary", use_container_width=True):
        try:
            with st.spinner("Generating your schedule…"):
                # Import only when needed so entry and loading still work if absent.
                from workflow import build_workflow
                app = build_workflow()
                result = app.invoke({"input_data": copy.deepcopy(entries), "processed_data": pd.DataFrame()})
                processed = result.get("processed_data")
                if not isinstance(processed, pd.DataFrame):
                    raise ValueError("The workflow did not return a valid schedule table.")
                st.session_state.processed_schedule = processed
        except Exception as exc:
            invalidate_schedule()
            st.error(f"Could not generate the schedule. Your saved entries are safe. {exc}")

if "processed_schedule" in st.session_state:
    processed = st.session_state.processed_schedule
    st.subheader("Machine-wise schedule")
    st.dataframe(processed, use_container_width=True, hide_index=True)
    st.download_button("Download schedule CSV", processed.to_csv(index=False).encode("utf-8-sig"),
                       "production_schedule.csv", "text/csv")
    # Keep the print view across reruns; pandas escapes all table content.
    html_table = processed.to_html(index=False, classes="schedule", escape=True)
    components.html(f"""<!doctype html><html><head><style>
        body {{font-family:system-ui,sans-serif; color:#1e293b; padding:16px;}}
        button {{background:#4f46e5;color:white;border:0;border-radius:8px;padding:12px 20px;cursor:pointer;}}
        .schedule {{border-collapse:collapse;width:100%;font-size:12px;}}
        th,td {{border:1px solid #cbd5e1;padding:8px;text-align:left;}}
        th {{background:#f1f5f9;}} h2 {{font-size:20px;}}
        @media print {{button {{display:none;}} thead {{display:table-header-group;}} tr {{break-inside:avoid;}}}}
        </style></head><body><button onclick="window.print()">Print schedule</button>
        <h2>Machine-wise production schedule</h2><p>{datetime.date.today():%d %b %Y}</p>
        {html_table}</body></html>""", height=440, scrolling=True)
