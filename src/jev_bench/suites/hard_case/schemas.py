from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HardCaseOutput(StrictModel):
    """Independent probabilities; no normalization or final claim decisions."""

    requires_clarification: Probability
    requires_human_review: Probability
    policy_grounding_required: Probability
    safety_compliance_concern: Probability
    coverage_likely: Probability
    potential_fraud_signal: Probability


PROBABILITY_FIELDS = tuple(HardCaseOutput.model_fields)
