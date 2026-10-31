"""Input models for the two tools.

All validation lives here so it can be unit tested without a running server.
`extra="forbid"` rejects unknown arguments, and strict mode stops silent
coercion (for example "100" -> 100 or True -> 1).
"""
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# CUST- followed by exactly 6 digits, e.g. CUST-000123.
CUSTOMER_ID_PATTERN = r"^CUST-\d{6}$"

CustomerId = Annotated[str, StringConstraints(strict=True, pattern=CUSTOMER_ID_PATTERN)]

MAX_REFUND = Decimal("10000.00")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LookupCustomerInput(_StrictModel):
    customer_id: CustomerId = Field(description="Customer ID in the form CUST-123456")


class IssueRefundInput(_StrictModel):
    customer_id: CustomerId = Field(description="Customer ID in the form CUST-123456")
    amount: float | int = Field(
        description="Refund amount in USD. Must be > 0, at most 2 decimal places.",
    )
    reason: str = Field(min_length=10, max_length=500, description="Why the refund is issued (10-500 chars)")

    @field_validator("amount", mode="before")
    @classmethod
    def _no_bool(cls, v):
        # bool is a subclass of int in Python, so reject it explicitly.
        if isinstance(v, bool):
            raise ValueError("amount must be a number, not a boolean")
        return v

    @field_validator("amount")
    @classmethod
    def _check_amount(cls, v):
        d = Decimal(str(v))
        if not d.is_finite():
            raise ValueError("amount must be a finite number")
        if d <= 0:
            raise ValueError("amount must be greater than 0")
        if d > MAX_REFUND:
            raise ValueError(f"amount must not exceed {MAX_REFUND}")
        if d.as_tuple().exponent < -2:
            raise ValueError("amount must have at most 2 decimal places")
        return v

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < 10:
            raise ValueError("reason must be at least 10 characters (whitespace does not count)")
        return stripped
