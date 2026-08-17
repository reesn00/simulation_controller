from pydantic import BaseModel, ConfigDict, Field


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str
    source_id: str
    path: str


class TaskProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fields: dict[str, SourceRef] = Field(default_factory=dict)
