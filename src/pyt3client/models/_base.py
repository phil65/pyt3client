"""Base class for the generated tool models."""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class T3Model(BaseModel):
    """Snake-case Python fields over T3 Code's camelCase wire names.

    Wire names come from `alias_generator` rather than per-field `alias=...`,
    so type checkers (which treat a per-field alias as the constructor keyword)
    accept `ThreadWaitInput(thread_id=...)`. The few wire names `to_camel` cannot
    produce (`_tag`, `async`) keep an explicit alias in the generated code.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )
