from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


ROUTES = ("answer_directly", "web_search", "refuse", "ask_clarification")
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Distribution(StrictModel):
    @model_validator(mode="after")
    def positive_total(self):
        if sum(self.model_dump().values()) <= 0:
            raise ValueError("A distribution must have a positive total probability")
        return self


class RouteProbabilities(Distribution):
    answer_directly: Probability
    web_search: Probability
    refuse: Probability
    ask_clarification: Probability


class FreshnessProbabilities(Distribution):
    level_0: Probability = Field(alias="0", description="timeless")
    level_1: Probability = Field(alias="1", description="very stable")
    level_2: Probability = Field(alias="2", description="recent information could help")
    level_3: Probability = Field(alias="3", description="recent information materially improves correctness")
    level_4: Probability = Field(alias="4", description="current information required")
    level_5: Probability = Field(alias="5", description="live or near-real-time information required")


class DecisionOutput(StrictModel):
    """Probabilities judging an input message, without answering its request."""

    requires_web_probability: Probability
    is_safe_probability: Probability
    route_probabilities: RouteProbabilities
    freshness_probabilities: FreshnessProbabilities
