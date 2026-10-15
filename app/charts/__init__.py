"""Reusable chart skills engine: prompt -> chart requests -> Python-computed chart JSON.

- ``fields``: approved fields/measures per report type (``register_adapter`` for new report types)
- ``skills``: one registered builder per chart type (``register`` for new chart types)
- ``planner``: rule-based prompt parsing, plus validated AI plans for ambiguous requests
- ``engine``: ``build_charts`` (numbers are always computed in Python)
- ``render``: static PNG rendering and table fallback for PDF/Word exports
"""
from app.charts.engine import ChartResult, build_charts
from app.charts.fields import ADAPTERS, DatasetAdapter, get_adapter, register_adapter
from app.charts.planner import ChartPlan, chart_planning_instructions, plan_charts
from app.charts.skills import SKILLS, ChartRequest, ChartSkill, ChartUnavailable, register

__all__ = ["ADAPTERS", "SKILLS", "ChartPlan", "ChartRequest", "ChartResult", "ChartSkill", "ChartUnavailable",
           "DatasetAdapter", "build_charts", "chart_planning_instructions", "get_adapter", "plan_charts", "register",
           "register_adapter"]
