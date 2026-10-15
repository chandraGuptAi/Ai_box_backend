"""FastAPI routes for server-side AI Skills.

Allows HTTP clients to inspect skill blueprints, craft tailored charts,
and render chart images on demand.
"""
from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.models.report_schema import ReportChart
from app.skills.chart_skill import chart_skill
from app.skills.prompt_skill import prompt_skill
from app.prompts.registry import get_report_definition
from app.services.report_service import ReportService
from app.api.report_routes import get_report_service

router = APIRouter(prefix="/api/skills/charts", tags=["Skills - Charts"])
prompt_router = APIRouter(prefix="/api/skills/prompt", tags=["Skills - Prompts"])


class CraftChartsRequest(BaseModel):
    prompt: str = Field(default="", max_length=4000, description="Natural language prompt describing desired charts.")
    data: list[dict[str, Any]] = Field(default_factory=list, description="Raw shipment records list (optional).")
    metrics: dict[str, Any] = Field(default_factory=dict, description="Pre-computed metrics (optional).")


class ChartCraftResponse(BaseModel):
    charts: list[ReportChart]
    previews: list[str] = Field(description="Base64 PNG data URLs for immediate rendering.")


class RenderChartResponse(BaseModel):
    id: str
    title: str
    type: str
    base64: str
    svg: str


@router.get("/blueprint", summary="Get Chart Skill Blueprint & System Instructions")
async def get_chart_skill_blueprint() -> dict[str, Any]:
    """Return the chart skill specification, guidelines, and prompt rules."""
    return chart_skill.get_skill_blueprint()


@router.get("/registry", summary="List registered chart skills and approved chart fields")
async def get_chart_registry() -> dict[str, Any]:
    """Chart skills (types, aliases, requirements, limits) and, per report type, the fields charts may use."""
    from app.charts import ADAPTERS, SKILLS
    return {
        "skills": [skill.describe() for skill in SKILLS.values()],
        "report_types": {
            report_type: {
                "dimensions": [n for n, f in adapter.fields.items() if f.kind != "number"],
                "numeric_fields": [n for n, f in adapter.fields.items() if f.kind == "number"],
                "measures": {m.name: {"label": m.label, "aggregation": m.agg, "format": m.value_format}
                             for m in adapter.measures.values()},
            }
            for report_type, adapter in ADAPTERS.items()
        },
    }


@router.post("/detect", summary="Detect Chart Preferences from Prompt")
async def detect_chart_preferences(payload: dict[str, str]) -> dict[str, Any]:
    """Analyze a user prompt and extract requested chart types and topics."""
    prompt = payload.get("prompt", "")
    specs = chart_skill.detect_preferences(prompt)
    return {"prompt": prompt, "detected_specs": specs}


@router.post("/craft", response_model=ChartCraftResponse, summary="Craft Charts from Prompt and Data")
async def craft_charts(req: CraftChartsRequest) -> ChartCraftResponse:
    """Dynamically craft charts matching prompt specifications from metrics or records."""
    if req.data:
        charts = chart_skill.craft_charts_from_data(req.data, req.prompt)
    elif req.metrics:
        charts = chart_skill.craft_charts_from_metrics(req.metrics, req.prompt)
    else:
        # Default empty craft if no data passed
        charts = []

    previews = [chart_skill.render_base64(c) for c in charts]
    return ChartCraftResponse(charts=charts, previews=previews)


@router.post("/render", response_model=RenderChartResponse, summary="Render a ReportChart object")
async def render_chart(chart: ReportChart) -> RenderChartResponse:
    """Render a given ReportChart contract into both Base64 PNG and SVG formats."""
    b64 = chart_skill.render_base64(chart)
    svg = chart_skill.render_svg(chart)
    return RenderChartResponse(
        id=chart.id,
        title=chart.title,
        type=chart.type,
        base64=b64,
        svg=svg
    )


@router.post("/render/png", summary="Render a ReportChart directly to PNG image stream")
async def render_chart_raw_png(chart: ReportChart) -> Response:
    """Render a given ReportChart and stream the raw PNG image bytes."""
    png_bytes = chart_skill.render_png(chart)
    return Response(content=png_bytes, media_type="image/png")


# --- Prompt Skill Endpoints ---

class ValidatePromptRequest(BaseModel):
    prompt: str = Field(description="The user-provided prompt to validate.")
    report_type: str = Field(default="shipment", description="Report type context (e.g. shipment, paid_remittance, pending_remittance).")
    data: dict[str, Any] | None = Field(default=None, description="Optional dataset to produce tailored suggestions.")


class ValidatePromptResponse(BaseModel):
    is_valid: bool
    category: str
    message: str
    reason: str
    confidence: float
    suggested_prompts: list[str]


class PromptSuggestionsRequest(BaseModel):
    report_type: str = Field(default="shipment", description="Report type context.")
    prompt: str | None = Field(default=None, description="Optional initial user prompt.")
    data: dict[str, Any] | None = Field(default=None, description="Optional dataset to ground suggestions.")


@prompt_router.get("/blueprint", summary="Get Prompt Skill Blueprint")
async def get_prompt_skill_blueprint() -> dict[str, Any]:
    """Return the prompt skill specification and capabilities."""
    return prompt_skill.get_skill_blueprint()


@prompt_router.post("/validate", response_model=ValidatePromptResponse, summary="Validate a user prompt")
async def validate_user_prompt(
    req: ValidatePromptRequest,
    report_service: ReportService = Depends(get_report_service),
) -> ValidatePromptResponse:
    """Check if the user input contains valid analytical intent for reports or is gibberish/off-topic."""
    res = prompt_skill.validate(req.prompt, report_type=req.report_type)
    if not res.is_valid and req.data:
        try:
            definition = get_report_definition(req.report_type)
            ai_suggs = await report_service._generate_data_tailored_suggestions(
                prompt=req.prompt,
                report_type=req.report_type,
                data=req.data,
                definition=definition,
                validation_reason=res.reason,
            )
            if ai_suggs:
                res.suggested_prompts = ai_suggs
        except Exception:
            pass
    return ValidatePromptResponse(**res.to_dict())


@prompt_router.get("/suggestions", summary="Get Prompt Suggestions")
async def get_prompt_suggestions(report_type: str = Query(default="shipment")) -> dict[str, Any]:
    """Get curated prompt suggestions tailored to a specific report type."""
    suggestions = prompt_skill.get_suggestions(report_type)
    return {"report_type": report_type, "suggestions": suggestions}


@prompt_router.post("/suggestions", summary="Get AI-Tailored Prompt Suggestions with Data")
async def get_data_tailored_prompt_suggestions(
    req: PromptSuggestionsRequest,
    report_service: ReportService = Depends(get_report_service),
) -> dict[str, Any]:
    """Get prompt suggestions, tailored with DeepSeek if dataset is provided."""
    suggestions = prompt_skill.get_suggestions(req.report_type)
    if req.data:
        try:
            definition = get_report_definition(req.report_type)
            ai_suggs = await report_service._generate_data_tailored_suggestions(
                prompt=req.prompt or "",
                report_type=req.report_type,
                data=req.data,
                definition=definition,
                validation_reason="user_requested_suggestions",
            )
            if ai_suggs:
                suggestions = ai_suggs
        except Exception:
            pass
    return {"report_type": req.report_type, "suggestions": suggestions}

