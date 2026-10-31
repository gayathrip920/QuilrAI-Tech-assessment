"""Input models for the two tools.

All validation lives here so it can be unit tested without a running server.
`extra="forbid"` rejects unknown arguments, and strict mode stops silent
coercion (for example "100" -> 100 or True -> 1).
"""
import math
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

# CUST-XXXXX: "CUST-" followed by exactly 5 ASCII digits, e.g. CUST-00123.
# [0-9] and not \d, because \d also matches digits from other scripts
# (for example Arabic-Indic or full-width digits). Pydantic checks this with
# a Rust regex engine, where $ means the true end of the string, so a
# trailing newline is rejected too (a test covers this).
CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"

CustomerId = Annotated[str, StringConstraints(strict=True, pattern=CUSTOMER_ID_PATTERN)]

class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetCustomerRecordInput(_StrictModel):
    customer_id: CustomerId = Field(description="Customer ID in the form CUST-XXXXX, e.g. CUST-00123")


class TriggerRefundInput(_StrictModel):
    customer_id: CustomerId = Field(description="Customer ID in the form CUST-XXXXX, e.g. CUST-00123")
    amount: float | int = Field(description="Refund amount. Must be a positive number.")
    reason: str = Field(min_length=10, description="Why the refund is issued (at least 10 characters)")

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
        if not math.isfinite(v):
            raise ValueError("amount must be a finite number")
        if v <= 0:
            raise ValueError("amount must be greater than 0")
        return v

    @field_validator("reason")
    @classmethod
    def _check_reason(cls, v: str) -> str:
        stripped = v.strip()
        if len(stripped) < 10:
            raise ValueError("reason must be at least 10 characters (whitespace does not count)")
        return stripped
