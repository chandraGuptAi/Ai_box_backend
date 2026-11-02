from io import BytesIO

from openpyxl import load_workbook

from app.generators.xlsx_generator import generate_xlsx
from tests.test_ordered_reports import generate, section


def test_dashboard_is_first_with_native_charts_and_unchanged_source_values():
    report, _ = generate(sections=[section('status', ('shipment_status',))])
    book = load_workbook(BytesIO(generate_xlsx(report)))
    dashboard, data = book.worksheets[:2]
    assert dashboard.title == 'Dashboard'
    assert not dashboard.sheet_view.showGridLines
    assert dashboard['B2'].value == report.report_name
    assert dashboard['B7'].value == report.records_analyzed
    assert len(dashboard._charts) == 2
    assert list(data.values)[1:1 + len(report.tables[0].values)] == [tuple(row) for row in report.tables[0].values]
    assert dashboard._charts[0].series[0].val.numRef.f.endswith(f'$B$2:$B${len(report.tables[0].values) + 1}')
    assert dashboard.page_setup.orientation == 'landscape'


def test_average_metrics_do_not_get_a_misleading_share_chart():
    report, _ = generate(sections=[section('courier', ('courier',), metrics=('avg_charge',))])
    book = load_workbook(BytesIO(generate_xlsx(report)))
    assert len(book['Dashboard']._charts) == 1
