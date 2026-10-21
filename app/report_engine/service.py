"""Controlled shipment aggregation and atomic, capability-protected report persistence."""
from contextlib import contextmanager
import hashlib
import secrets
import sqlite3
import psycopg
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from fastapi import HTTPException
from .models import Chart, Metric, Operation, Report
from .registry import SkillRegistry
from app.prompts.shipment_prompt import parse_order

DEFINITIONS = {
    'total': {'aggregation': 'count included records', 'unit': 'count', 'dimensions': 'status,courier', 'filters': 'none', 'denominator': 'none'},
    'delivered': {'aggregation': 'count category=delivered', 'unit': 'count', 'dimensions': 'status,courier', 'filters': 'none', 'denominator': 'none'},
    'cancelled': {'aggregation': 'count category=cancelled', 'unit': 'count', 'dimensions': 'status,courier', 'filters': 'none', 'denominator': 'none'},
    'unknown': {'aggregation': 'count category=unknown', 'unit': 'count', 'dimensions': 'status,courier', 'filters': 'none', 'denominator': 'none'},
    'cancellation_rate': {'aggregation': '100 * cancelled / total; round half up 2 places', 'unit': 'percent', 'dimensions': 'status,courier', 'filters': 'none', 'denominator': 'all included records including unknown'},
}

class Engine:
    def __init__(self, path):
        self.path = str(path)
        self.registry = SkillRegistry()
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS canonical_reports (id TEXT PRIMARY KEY, account TEXT NOT NULL, token_hash TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        if self.path.startswith(('postgresql://', 'postgres://')):
            with psycopg.connect(self.path, connect_timeout=5, options='-c statement_timeout=5000') as db:
                class Parameters:
                    def execute(self, sql, params=()):
                        return db.execute(sql.replace('?', '%s'), params)
                yield Parameters()
            return
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, account, records, source_label='uploaded application data'):
        self.registry.load('shipment-analysis')
        self.registry.load('chart-management')
        if not records or len(records) > 5000:
            raise HTTPException(422, 'Provide between 1 and 5000 shipment records; larger reports require background processing.')
        orders = [parse_order(r) for r in records]
        counts = Counter(o.category for o in orders)
        total = len(orders)
        values = {'total': Decimal(total), 'delivered': Decimal(counts['delivered']),
                  'cancelled': Decimal(counts['cancelled']), 'unknown': Decimal(counts['unknown']),
                  'cancellation_rate': (Decimal(counts['cancelled']) * 100 / total).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)}
        charts = []
        aggregations = {'status': counts, 'courier': Counter(o.courier or 'Unknown' for o in orders)}
        for dimension, groups in aggregations.items():
            if len(groups) > 200:
                raise HTTPException(422, 'Too many chart categories; narrow the source dataset.')
            charts.append(Chart(id=f'chart_{dimension}', title=f'Shipments by {dimension}',
                type='pie' if dimension == 'status' else 'bar', labels=sorted(groups),
                datasets=[{'label': 'Shipments', 'data': [groups[k] for k in sorted(groups)]}],
                source_references=['source_shipments'], metric_references=['total'],
                axis_metadata={'dimension': dimension, 'unit': 'count'}))
        dates = sorted(o.order_date.isoformat() for o in orders if o.order_date)
        report = Report(report_id=uuid4().hex, account_id=account,
            source_metadata={'id': 'source_shipments', 'kind': 'application_payload', 'label': source_label,
                'records': total, 'excluded_records': 0, 'missing_dates': sum(o.order_date is None for o in orders),
                'unrecognized_statuses': counts['other']},
            actual_data_period={'start': dates[0], 'end': dates[-1]} if dates else {},
            metric_definitions=DEFINITIONS,
            kpis=[Metric(id=k, value=v, unit=DEFINITIONS[k]['unit'], source_references=['source_shipments'], metric_reference=k) for k,v in values.items()],
            charts=charts, validation_results=['Deterministic counts verified; missing tracking is not a failure.'],
            generated_at=datetime.now(timezone.utc))
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO canonical_reports VALUES (?,?,?,?,?)',
                       (report.report_id, account, self.digest(token), 1, report.model_dump_json()))
        return report, token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, report_id, account, token):
        with self.connect() as db:
            row = db.execute('SELECT body FROM canonical_reports WHERE id=? AND account=? AND token_hash=?',
                             (report_id, account, self.digest(token))).fetchone()
        if not row:
            raise HTTPException(404, 'Report not found')
        return Report.model_validate_json(row[0])

    def execute(self, report_id, account, token, op: Operation):
        report = self.get(report_id, account, token)
        if report.version != op.expected_version:
            raise HTTPException(409, 'Report changed. Reload before editing.')
        if op.operation == 'EXPORT_REPORT':
            return report
        if op.operation not in {'EDIT_REPORT', 'REMOVE_CHART', 'UPDATE_CHART', 'UPDATE_CHART_TYPE', 'ADD_CHART'}:
            raise HTTPException(422, 'This operation requires a new authorized source extraction and is not available in this slice.')
        self.registry.load('chart-management')
        if op.operation == 'EDIT_REPORT':
            if op.title is None:
                raise HTTPException(422, 'A title is required.')
            report.title = op.title
        elif op.operation == 'ADD_CHART':
            if len(report.charts) >= 20:
                raise HTTPException(422, 'A report supports at most 20 charts.')
            source = next((c for c in report.charts if c.axis_metadata.get('dimension') == op.dimension), None)
            if source is None:
                raise HTTPException(422, 'Requested dimension is unavailable. Supply an authorized dataset containing that dimension.')
            chart = source.model_dump()
            chart.update(id='chart_' + uuid4().hex, type=op.chart_type or source.type, title=op.title or source.title)
            report.charts.append(Chart.model_validate(chart))
        else:
            chart = next((c for c in report.charts if c.id == op.chart_id), None)
            if chart is None:
                raise HTTPException(404, 'Chart not found')
            if op.operation == 'REMOVE_CHART':
                report.charts = [c for c in report.charts if c.id != op.chart_id]
            else:
                if op.operation == 'UPDATE_CHART' and op.title is None:
                    raise HTTPException(422, 'A chart title is required in this slice.')
                updated = chart.model_dump()
                if op.title is not None:
                    updated['title'] = op.title
                if op.operation == 'UPDATE_CHART_TYPE':
                    if op.chart_type is None:
                        raise HTTPException(422, 'A supported chart type is required.')
                    updated['type'] = op.chart_type
                report.charts = [Chart.model_validate(updated) if c.id == chart.id else c for c in report.charts]
        report.version += 1
        report = Report.model_validate(report.model_dump())
        with self.connect() as db:
            result = db.execute('UPDATE canonical_reports SET version=?,body=? WHERE id=? AND account=? AND token_hash=? AND version=?',
                (report.version, report.model_dump_json(), report_id, account, self.digest(token), op.expected_version))
            if result.rowcount != 1:
                raise HTTPException(409, 'Report changed. Reload before editing.')
        return report
