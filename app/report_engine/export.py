"""Render canonical values without recomputing business metrics."""
from io import BytesIO
from html import escape
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, DoughnutChart, Reference
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, Image, Spacer
from app.charts.render import render_png


def text_cell(cell, value):
    cell.value = str(value)
    cell.data_type = 's'  # untrusted labels must never become spreadsheet formulas


def excel(report):
    book = Workbook()
    sheet = book.active
    sheet.title = 'Report'
    sheet.append(['Metric', 'Value', 'Unit'])
    for metric in report.kpis:
        sheet.append([metric.id, float(metric.value), metric.unit])
    sheet.append(['Version', report.version])
    sheet.append(['Filters', str(report.filters)])
    text_cell(sheet.cell(sheet.max_row, 2), report.filters)
    text_cell(sheet.cell(sheet.max_row + 1, 1), report.title)
    constructors = {'bar': BarChart, 'line': LineChart, 'pie': PieChart, 'doughnut': DoughnutChart}
    for index, chart in enumerate(report.charts):
        ws = book.create_sheet(f'Chart {index + 1}')
        text_cell(ws.cell(1, 1), chart.id)
        text_cell(ws.cell(1, 2), chart.type)
        ws.append(['Category'] + [d.label for d in chart.datasets])
        for col, dataset in enumerate(chart.datasets, 2):
            text_cell(ws.cell(2, col), dataset.label)
        for row, label in enumerate(chart.labels, 3):
            text_cell(ws.cell(row, 1), label)
            for col, dataset in enumerate(chart.datasets, 2):
                ws.cell(row, col, dataset.data[row - 3])
        graphic = constructors[chart.type]()
        graphic.title = chart.title
        graphic.add_data(Reference(ws, min_col=2, max_col=1+len(chart.datasets), min_row=2, max_row=2+len(chart.labels)), titles_from_data=True)
        graphic.set_categories(Reference(ws, min_col=1, min_row=3, max_row=2+len(chart.labels)))
        ws.add_chart(graphic, 'E2')
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def pdf(report):
    output = BytesIO()
    style = getSampleStyleSheet()
    body = [Paragraph(escape(report.title), style['Title']),
            Paragraph(f'Version {report.version}; Filters: {escape(str(report.filters))}', style['Normal']),
            Table([['Metric', 'Value', 'Unit']] + [[m.id, str(m.value), m.unit] for m in report.kpis])]
    for chart in report.charts:
        body.extend([Spacer(1, 16), Paragraph(escape(chart.title), style['Heading2'])])
        image = render_png(chart)
        if image:
            body.append(Image(BytesIO(image), width=450, height=260, kind='proportional'))
        body.append(Table([['Category'] + [d.label for d in chart.datasets]] +
                          [[escape(label)] + [str(d.data[i]) for d in chart.datasets] for i,label in enumerate(chart.labels)]))
    SimpleDocTemplate(output).build(body)
    return output.getvalue()
