"""The parsed form of one slide. Both the text path and the vision path produce it.

Every field is required (nullable where it can be empty) so the model can be used
directly as a strict structured-output schema.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class VisualKind(StrEnum):
    UML_CLASS = "uml_class"
    UML_SEQUENCE = "uml_sequence"
    UML_DOMAIN_MODEL = "uml_domain_model"
    UML_STATE_MACHINE = "uml_state_machine"
    UML_USE_CASE = "uml_use_case"
    UML_ACTIVITY = "uml_activity"
    UML_PACKAGE = "uml_package"
    FLOWCHART = "flowchart"
    CHART = "chart"
    TABLE = "table"
    SCREENSHOT = "screenshot"
    PHOTO = "photo"
    OTHER = "other"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Visual(_Strict):
    kind: VisualKind
    description: str = Field(description="Explicit prose: enough to redraw the visual.")
    key_facts: list[str] = Field(
        description="Short, independently checkable facts shown by the visual, e.g. "
        "'Member 0..1 — 0..5 Copy (association, borrows)'. Empty for decorative images."
    )


class Equation(_Strict):
    latex: str
    context: str | None = Field(description="What the equation is, if the slide says.")


class CodeBlock(_Strict):
    language: str | None
    code: str


class SlideContent(_Strict):
    title: str | None
    content_markdown: str = Field(
        description="All slide text, faithfully, with headings, bullets and tables."
    )
    visuals: list[Visual]
    equations: list[Equation]
    code_blocks: list[CodeBlock]

    @classmethod
    def empty(cls, title: str | None = None) -> "SlideContent":
        return cls(title=title, content_markdown="", visuals=[], equations=[], code_blocks=[])
