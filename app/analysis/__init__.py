"""Prompt-driven analysis: user prompt + current data -> validated plan -> Python results -> verification.

- ``vocabulary``: per report type, the only fields, filters and columns a plan may use (``register_vocabulary``)
- ``interpreter``: rule-based prompt -> ``AnalysisPlan`` (optional, validated AI plan for ambiguous prompts)
- ``plan``: the plan dataclasses and ``validate_plan``
- ``executor``: filters, date ranges, plan tables and the dataset-scope statement
- ``narrative``: code-written summary for requests Python answers alone
- ``verify``: independent recount of every figure, and removal of false "data is missing" statements
"""
from app.analysis.executor import annotate_table, build_plan_tables, dataset_scope, matching_rows
from app.analysis.interpreter import ai_plan_messages, interpret, merge_ai_plan
from app.analysis.narrative import plan_narrative
from app.analysis.plan import AnalysisPlan, DateRange, Filter, TableSpec, Unsupported, validate_plan
from app.analysis.verify import remove_false_absence_claims, verify_report
from app.analysis.vocabulary import DataContext, PlanVocabulary, get_vocabulary, register_vocabulary

__all__ = ["AnalysisPlan", "DataContext", "DateRange", "Filter", "PlanVocabulary", "TableSpec", "Unsupported",
           "ai_plan_messages", "annotate_table", "build_plan_tables", "dataset_scope", "get_vocabulary", "interpret",
           "matching_rows", "merge_ai_plan", "plan_narrative", "register_vocabulary", "remove_false_absence_claims",
           "validate_plan", "verify_report"]
