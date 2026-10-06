"""Validated request and response contracts for the production planner."""
from datetime import date, datetime
import re
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_feeders(value):
    """Accept documented aliases, reject unknown/duplicate keys, keep blank clears."""
    if not isinstance(value, dict):
        raise ValueError("Feeders must be an object")
    normalized = {}
    for key, item in value.items():
        match = re.fullmatch(r"feeder[ _-]*([1-8])", str(key).strip(), re.I)
        if not match:
            raise ValueError("Feeder names must be FEEDER 1 through FEEDER 8")
        canonical = f"FEEDER {int(match.group(1))}"
        if canonical in normalized:
            raise ValueError(f"Duplicate feeder: {canonical}")
        if not isinstance(item, str):
            raise ValueError(f"{canonical} must contain text")
        item = item.strip()
        if len(item) > 100:
            raise ValueError(f"{canonical} must be at most 100 characters")
        normalized[canonical] = item
    return dict(sorted(normalized.items()))


class OrderFields(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    @field_validator("feeders", mode="before", check_fields=False)
    @classmethod
    def validate_feeders(cls, value):
        return normalize_feeders(value)

    @field_validator("rate", "piece", mode="before", check_fields=False)
    @classmethod
    def reject_boolean_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("Enter a number, not a boolean")
        return value


class ProductionOrderCreate(OrderFields):
    order_date: date
    mc_no: str = Field(min_length=1, max_length=80)
    design_no: str = Field(min_length=1, max_length=120)
    beam: Optional[str] = Field(default=None, max_length=160)
    rate: float = Field(default=0, ge=0, allow_inf_nan=False)
    piece: float = Field(default=0, ge=0, allow_inf_nan=False)
    feeders: Dict[str, str] = Field(default_factory=dict)


class ProductionOrderUpdate(OrderFields):
    """Omit fields to retain them. Supplied feeders replace the entire feeder set."""
    order_date: Optional[date] = None
    mc_no: Optional[str] = Field(default=None, min_length=1, max_length=80)
    design_no: Optional[str] = Field(default=None, min_length=1, max_length=120)
    beam: Optional[str] = Field(default=None, max_length=160)
    rate: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    piece: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)
    feeders: Optional[Dict[str, str]] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_required_fields(cls, values):
        if isinstance(values, dict):
            for name in ("order_date", "mc_no", "design_no", "rate", "piece", "feeders"):
                if name in values and values[name] is None:
                    raise ValueError(f"{name} cannot be null; omit it to keep its value")
        return values


class ProductionOrderResponse(BaseModel):
    id: int
    order_date: Optional[str] = None
    mc_no: str
    design_no: str
    beam: Optional[str] = None
    rate: float
    piece: float
    feeders: Dict[str, str] = Field(default_factory=dict)
    created_at: Optional[str] = None

    @field_validator("order_date", "created_at", mode="before")
    @classmethod
    def serialize_dates(cls, value):
        return value.isoformat() if isinstance(value, (date, datetime)) else value


class ProcessedOrderResponse(BaseModel):
    mc_no: str
    design_no: str
    beam: Optional[str] = None
    piece: float
    feeders: Dict[str, str] = Field(default_factory=dict)


class MessageResponse(BaseModel):
    message: str
    count: Optional[int] = None

