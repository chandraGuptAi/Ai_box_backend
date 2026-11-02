"""Export typed verified table values; strings are never interpreted as formulas."""
from io import BytesIO
import re

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.chart import BarChart, DoughnutChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.utils import get_column_letter


def dashboard(book, report):
    sheet = book.worksheets[0]
    sheet.title = 'Dashboard'
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = 'B10'
    for column in range(1, 23):
        sheet.column_dimensions[get_column_letter(column)].width = 8
    for row in range(1, 49):
        sheet.row_dimensions[row].height = 22
        for column in range(1, 23):
            sheet.cell(row, column).fill = PatternFill('solid', fgColor='F4F6FB')
    def block(area, value, fill, color='182B49', size=12):
        for row in sheet[area]:
            for cell in row:
                cell.fill = PatternFill('solid', fgColor=fill)
        sheet.merge_cells(area)
        cell = sheet[area.split(':')[0]]
        cell.value = value
        cell.font = Font(name='Calibri', size=size, color=color, bold=True)
        cell.alignment = Alignment(vertical='center', wrap_text=True)
    block('B2:U3', report.report_name, '182B49', 'FFFFFF', 22)
    scope = report.structured_report.scope if report.structured_report else {}
    from app.report_period import period_label
    period = period_label(scope.get('selected_period')) if scope.get('selected_period') else ''
    block('B4:U4', '  '.join(filter(None, [report.company_name, period])), 'F4F6FB', '61718B', 11)
    selected = None
    for index, table in enumerate(report.tables):
        meta = table.column_meta or []
        dimensions = [i for i, c in enumerate(meta) if c.type in ('text', 'date')]
        measures = [i for i, c in enumerate(meta) if c.type in ('number', 'currency')]
        if len(dimensions) == 1 and measures and table.values and all(isinstance(row[measures[0]], (int, float)) for row in table.values):
            selected = (index, table, dimensions[0], measures[0])
            break
    cards = [('Records analyzed', report.records_analyzed), ('Analysis tables', len(report.tables))]
    if selected:
        _, table, dimension, measure = selected
        peak = max(table.values, key=lambda row: row[measure])
        cards += [('Groups shown', len(table.values)), ('Largest group', str(peak[dimension]))]
    for index, (label, value) in enumerate(cards):
        start = 2 + index * 5
        left, right = get_column_letter(start), get_column_letter(start + 3)
        block(f'{left}6:{right}6', label, 'FFFFFF', '61718B', 11)
        block(f'{left}7:{right}8', value, 'FFFFFF', '4638C8', 23)
    if selected:
        index, table, dimension, measure = selected
        source = book.worksheets[index + 1]
        categories = Reference(source, min_col=dimension + 1, min_row=2, max_row=len(table.values) + 1)
        data = Reference(source, min_col=measure + 1, min_row=1, max_row=len(table.values) + 1)
        bar = BarChart()
        bar.type = 'bar'
        bar.title = table.title
        bar.add_data(data, titles_from_data=True)
        bar.set_categories(categories)
        bar.style = 13
        bar.width, bar.height = 14.5, 10
        bar.legend = None
        bar.dataLabels = DataLabelList()
        bar.dataLabels.showVal = True
        sheet.add_chart(bar, 'B11')
        # A share chart is meaningful for additive positive values, never for averages/rates.
        measure_key = ((table.column_meta[measure].key or '') + ' ' + table.columns[measure]).lower()
        if all(row[measure] >= 0 for row in table.values) and sum(row[measure] for row in table.values) > 0 and not any(word in measure_key for word in ('avg', 'average', 'median', 'rate', 'max', 'min')):
            donut = DoughnutChart()
            donut.title = 'Share of displayed groups' if table.truncated else 'Share of ' + table.columns[measure]
            donut.holeSize = 65
            donut.add_data(data, titles_from_data=True)
            donut.set_categories(categories)
            donut.style = 10
            donut.dataLabels = DataLabelList()
            donut.dataLabels.showPercent = True
            donut.dataLabels.numFmt = '0.0%'
            donut.width, donut.height = 14.5, 10
            sheet.add_chart(donut, 'L11')
        block('B32:U32', 'Chart source: ' + source.title + (' (displayed groups only)' if table.truncated else ''), 'F4F6FB', '61718B', 10)
        block('B34:U36', table.note or 'See the detailed analysis sheets for source values.', 'FFFFFF', '61718B', 11)
    else:
        block('B12:U15', report.content.executive_summary, 'FFFFFF', '182B49', 13)
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 1
    sheet.print_options.horizontalCentered = True
    sheet.print_area = 'A1:V38'


def generate_xlsx(report):
    book = Workbook()
    summary = book.active
    for index, table in enumerate(report.tables):
        name = re.sub(r'[\\/*?:\[\]]', ' ', table.title).strip()[:25] or 'Analysis'
        sheet = book.create_sheet(f'{index + 1} {name}'[:31])
        sheet.append(table.columns)
        for row in table.values if table.values is not None else table.rows:
            sheet.append(row)
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        sheet.sheet_view.showGridLines = False
        for cell in sheet[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='4638C8')
        for column in sheet.columns:
            sheet.column_dimensions[column[0].column_letter].width = 24
        if table.column_meta:
            for row in sheet.iter_rows(min_row=2):
                for cell, meta in zip(row, table.column_meta):
                    if meta.type == 'currency' and meta.currency:
                        symbol = '\u20b9' if meta.currency == 'INR' else meta.currency
                        cell.number_format = f'"{symbol}"#,##0.00'
                    elif meta.type == 'percent':
                        cell.number_format = '0.00"%"'
        if table.note:
            sheet.append([])
            sheet.append([table.note])
    dashboard(book, report)
    for sheet in book:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
    output = BytesIO()
    book.save(output)
    return output.getvalue()
