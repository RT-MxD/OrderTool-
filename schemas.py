"""
Pydantic schemas for request/response validation.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict
from datetime import date


# ── Request Schemas ─────────────────────────────────────────────────────────
class ProductionOrderCreate(BaseModel):
    """Schema for creating a new production order."""
    order_date: date
    mc_no: str = Field(..., min_length=1, description="Machine number")
    design_no: str = Field(..., min_length=1, description="Design number")
    beam: Optional[str] = None
    rate: float = 0
    piece: float = 0
    feeders: Dict[str, str] = Field(
        default_factory=dict,
        description="Feeder values, e.g. {'FEEDER 1': 'value', 'FEEDER 2': 'value'}"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "order_date": "2026-10-02",
                    "mc_no": "M-01",
                    "design_no": "D-1234",
                    "beam": "B-100",
                    "rate": 150,
                    "piece": 300,
                    "feeders": {
                        "FEEDER 1": "Red",
                        "FEEDER 2": "Blue",
                        "FEEDER 3": "Green"
                    }
                }
            ]
        }
    }


class ProductionOrderUpdate(BaseModel):
    """Schema for updating an existing production order (partial update)."""
    order_date: Optional[date] = None
    mc_no: Optional[str] = None
    design_no: Optional[str] = None
    beam: Optional[str] = None
    rate: Optional[float] = None
    piece: Optional[float] = None
    feeders: Optional[Dict[str, str]] = None


# ── Response Schemas ────────────────────────────────────────────────────────
class ProductionOrderResponse(BaseModel):
    """Schema for a single production order response."""
    id: int
    order_date: Optional[str] = None
    mc_no: str
    design_no: str
    beam: Optional[str] = None
    rate: float
    piece: float
    feeders: Dict[str, str] = {}
    created_at: Optional[str] = None


class ProcessedOrderResponse(BaseModel):
    """Schema for a processed order (piece divided by 3)."""
    order_date: Optional[str] = None
    mc_no: str
    design_no: str
    beam: Optional[str] = None
    rate: float
    piece_divided_by_3: float
    feeders: Dict[str, str] = {}


class MessageResponse(BaseModel):
    """Generic message response."""
    message: str
    count: Optional[int] = None
