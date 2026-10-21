"""Validated canonical model for the bounded shipment vertical slice."""
from datetime import datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.models.report_schema import ReportChart

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Metric(Model):
    id: str
    value: Decimal = Field(ge=0, allow_inf_nan=False)
    unit: Literal['count', 'percent']
    source_references: list[str]
    metric_reference: str

class Chart(ReportChart):
    type: Literal["bar", "line", "pie", "doughnut"] = "bar"
    source_references: list[str]
    metric_references: list[str]
    axis_metadata: dict[str, str] = Field(default_factory=dict)
    presentation_options: dict[str, str] = Field(default_factory=dict)

class Report(Model):
    report_id: str
    version: int = Field(default=1, ge=1)
    account_id: str
    report_type: Literal['shipment-analysis'] = 'shipment-analysis'
    title: str = 'Shipment Analysis'
    requested_period: dict[str, str] = Field(default_factory=dict)
    actual_data_period: dict[str, str] = Field(default_factory=dict)
    filters: dict[str, str] = Field(default_factory=dict)
    source_metadata: dict[str, str | int]
    metric_definitions: dict[str, dict[str, str]]
    kpis: list[Metric]
    charts: list[Chart] = Field(max_length=20)
    tables: list[dict] = Field(default_factory=list)
    narrative_sections: list[dict] = Field(default_factory=list)
    recommendations: list[dict] = Field(default_factory=list)
    validation_results: list[str]
    generated_at: datetime

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [x.id for x in self.charts] + [x.id for x in self.kpis]
        if len(ids) != len(set(ids)):
            raise ValueError('Component IDs must be unique')
        for metric in self.kpis:
            if metric.metric_reference not in self.metric_definitions:
                raise ValueError('Unknown metric definition')
        for chart in self.charts:
            if not set(chart.metric_references).issubset(self.metric_definitions):
                raise ValueError('Unknown chart metric definition')
        return self

OperationName = Literal['CREATE_REPORT', 'EDIT_REPORT', 'ADD_CHART', 'REMOVE_CHART',
                        'UPDATE_CHART', 'UPDATE_CHART_TYPE', 'UPDATE_FILTERS', 'EXPORT_REPORT']

class Operation(Model):
    operation: OperationName
    expected_version: int = Field(ge=1)
    chart_id: str | None = None
    chart_type: Literal['bar', 'line', 'pie', 'doughnut'] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    dimension: Literal['status', 'courier'] | None = None
