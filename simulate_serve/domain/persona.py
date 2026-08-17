from pydantic import BaseModel, ConfigDict


class PersonaSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role_description: str = "普通用户"
    background: str = ""
    tone: str = "口语化、自然"
    verbosity: str = "medium"
