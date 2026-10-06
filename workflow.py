"""Deterministic production schedule transformation; preserves every order."""
from functools import lru_cache
import math
import re
from typing import TypedDict, List, Dict, Any

from langgraph.graph import StateGraph, END
import pandas as pd
from schemas import normalize_feeders


class GraphState(TypedDict):
    input_data: List[Dict[str, Any]]
    processed_data: pd.DataFrame


def natural_key(value):
    """Sort M-2 before M-10, with comparable tuple components."""
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r"(\d+)", str(value or "").strip()))


def process_node(state: GraphState):
    columns = ["M/C No.", "DESIGN NO", "BEAM", "PIECE"]
    rows = []
    for data in state.get("input_data", []):
        raw_piece = data.get("Piece", 0)
        if isinstance(raw_piece, bool):
            raise ValueError("Piece must be a finite non-negative number")
        try:
            piece = float(raw_piece if raw_piece is not None else 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Piece must be a finite non-negative number") from exc
        if not math.isfinite(piece) or piece < 0:
            raise ValueError("Piece must be a finite non-negative number")
        row = {
            "M/C No.": str(data.get("M/C No.") or "").strip(),
            "DESIGN NO": str(data.get("Design No") or "").strip(),
            "BEAM": data.get("Beam"),
            "PIECE": piece / 3.0,
        }
        # Older databases may expose unused feeder columns as NULL.
        feeders = data.get("Feeders") or {}
        row.update(normalize_feeders({k: v for k, v in feeders.items() if v is not None}))
        rows.append(row)
    rows.sort(key=lambda row: (natural_key(row["M/C No."]), natural_key(row["DESIGN NO"])))
    feeder_columns = sorted({key for row in rows for key in row if key.startswith("FEEDER ")})
    return {"processed_data": pd.DataFrame(rows, columns=columns + feeder_columns)}


@lru_cache(maxsize=1)
def build_workflow():
    """Compile once per worker; each invocation still has independent state."""
    graph = StateGraph(GraphState)
    graph.add_node("process", process_node)
    graph.set_entry_point("process")
    graph.add_edge("process", END)
    return graph.compile()
