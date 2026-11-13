from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Ref = Annotated[str, Field(min_length=1, max_length=128)]
Period = Annotated[str, Field(pattern=r"^\d{4}-Q[1-4]$")]
Currency = Annotated[str, Field(min_length=3, max_length=3)]


class ComplianceCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ursbRegistrationNo: Ref
    ngoBureauPermitNo: Ref
    ampRecordId: Ref | None = None


class ExpenditureLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ampRecordId: Ref
    partnerId: Ref | None = None
    amount: Annotated[float, Field(ge=0)]
    currency: Currency


class ExpenditureReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reportId: Ref
    period: Period
    lines: Annotated[list[ExpenditureLine], Field(min_length=1, max_length=5000)]


class Indicator(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Ref
    value: float
    unit: Annotated[str, Field(max_length=32)] | None = None


class IndicatorSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submissionRef: Ref
    period: Period
    indicators: Annotated[list[Indicator], Field(min_length=1, max_length=2000)]
