"""Content-first slide layout shared by PPTX export and its private SVG preview.

Coordinates are in a 1600 x 900 canvas. Charts keep the stored report types;
text/tables remain editable in PowerPoint. No model call or new facts are added.
"""
from __future__ import annotations
import base64
from functools import lru_cache
from html import escape
from io import BytesIO
from pathlib import Path
import re
import textwrap
import threading

from PIL import ImageFont
from app.charts.render import chart_table, render_png, FALLBACK_NOTE
from app.models.report_schema import ReportDocument

W, H = 1600, 900
INK, MUTED, ACCENT, LIGHT, WHITE = '#152D43', '#607184', '#4F46E5', '#EEF1F7', '#FFFFFF'
_CHART_LOCK=threading.Lock()


def presentation_chart(chart):
    """Use large slide-scale labels for simple charts; retain the report renderer
    and stored type for advanced charts or large category sets."""
    kind=chart.type
    if (len(chart.datasets)!=1 or len(chart.labels)>6 or kind not in {'bar','pie','doughnut','donut'}
            or chart.options.get('horizontal')):
        return render_png(chart)
    values=chart.datasets[0].data
    if not any(values): return render_png(chart)
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import MaxNLocator
    with _CHART_LOCK:
        fig=Figure(figsize=(7.5,5),dpi=160,facecolor='white');FigureCanvasAgg(fig)
        palette=['#5B5FE9','#16A34A','#F59E0B','#EF4444','#3B82F6','#8B5CF6']
        if kind=='bar':
            ax=fig.add_axes([.12,.23,.85,.68]);ax.set_axisbelow(True)
            ax.grid(axis='y',color='#E6EAF0',linewidth=.8)
            bars=ax.bar(range(len(values)),values,color=palette[0],width=.58)
            ax.set_xticks(range(len(values)),['\n'.join(textwrap.wrap(label,13)) for label in chart.labels],fontsize=19,color='#334155')
            ax.tick_params(axis='y',labelsize=17,colors='#64748B',length=0)
            if all(float(v).is_integer() for v in values): ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.set_ylim(0,max(values)*1.23)
            for bar,value in zip(bars,values):
                ax.text(bar.get_x()+bar.get_width()/2,value+max(values)*.035,f'{value:g}',ha='center',fontsize=25,fontweight='bold',color=INK)
            for side in ('top','right','left'): ax.spines[side].set_visible(False)
            ax.spines['bottom'].set_color('#CBD5E1')
        else:
            ax=fig.add_axes([.12,.25,.76,.72]);ax.set_aspect('equal')
            wedge={'edgecolor':'white','linewidth':3}
            if kind in {'doughnut','donut'}: wedge['width']=.43
            wedges,_,labels=ax.pie(values,colors=palette,startangle=90,counterclock=False,
                autopct=lambda p:f'{p:.1f}%' if p else '',pctdistance=.74 if 'width' in wedge else .63,
                textprops={'fontsize':22,'fontweight':'bold','color':'white'},wedgeprops=wedge)
            fig.legend(wedges,chart.labels,loc='lower center',bbox_to_anchor=(.5,.015),frameon=False,
                       fontsize=20,ncol=1,handlelength=1,handletextpad=.6)
        output=BytesIO();fig.savefig(output,format='png',facecolor='white')
        return output.getvalue()


@lru_cache(maxsize=32)
def font(size, bold=False):
    paths = ['C:/Windows/Fonts/arialbd.ttf' if bold else 'C:/Windows/Fonts/arial.ttf',
             '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf' if bold else '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def wrap(text, width, size, bold=False):
    lines = []
    measure = font(size, bold)
    for paragraph in str(text).splitlines() or ['']:
        line = ''
        for word in paragraph.split():
            # Long identifiers must wrap, too; never silently clip a cell.
            fragments = [word]
            if measure.getlength(word) > width:
                fragments, part = [], ''
                for char in word:
                    if part and measure.getlength(part+char) > width:
                        fragments.append(part); part = ''
                    part += char
                if part: fragments.append(part)
            for word in fragments:
                candidate = (line+' '+word).strip()
                if line and measure.getlength(candidate) > width:
                    lines.append(line); line = word
                else:
                    line = candidate
        lines.append(line)
    return lines


def text(slide, value, x, y, width, size=30, color=INK, bold=False):
    lines = wrap(value, width, size, bold)
    item = dict(kind='text', x=x, y=y, w=width, h=len(lines)*size*1.28,
                lines=lines, size=size, color=color, bold=bold, leading=size*1.28)
    slide['items'].append(item)
    return item['h']


def rect(slide, x, y, w, h, fill):
    slide['items'].append(dict(kind='rect', x=x, y=y, w=w, h=h, fill=fill))


def new_slide(title, company, index, kind='content'):
    s = dict(title=title, kind=kind, items=[], notes=[])
    if kind != 'cover':
        title_size=46
        while title_size>28 and len(wrap(title,1440,title_size,True))>2:
            title_size-=2
        text(s, title, 80, 60, 1440, title_size, bold=True)
        rect(s, 80, 155, 60, 5, ACCENT)
        text(s, company, 80, 847, 1320, 16, MUTED)
        text(s, f'{index:02d}', 1470, 847, 55, 16, MUTED)
    return s


def _prose_pages(slides, title, paragraphs, company, size=32, page_lines=13):
    lines = []
    for paragraph in paragraphs:
        if not str(paragraph).strip(): continue
        lines.extend(wrap(paragraph, 1390, size) + [''])
    while lines:
        chunk, lines = lines[:page_lines], lines[page_lines:]
        s = new_slide(title if not slides or not slides[-1]['title'].startswith(title) else title+' (continued)', company, len(slides)+1)
        s['items'].append(dict(kind='text', x=80, y=210, w=1440, h=len(chunk)*size*1.28,
                              lines=chunk, size=size, color=INK, bold=False, leading=size*1.28))
        slides.append(s)


def _table_pages(slides, title, table, company, paragraphs=()):
    columns, rows = table.columns, table.rows
    # Repeat the first category on wide continuations so values retain their identity.
    groups = [list(range(len(columns)))] if len(columns) <= 5 else [
        [0]+list(range(start, min(start+4, len(columns)))) for start in range(1, len(columns), 4)]
    for group_index, indices in enumerate(groups):
        left_width = 530 if paragraphs and len(indices) <= 4 else 0
        x, width = (690, 830) if left_width else (80, 1440)
        col_width = width/len(indices)
        size = 24 if left_width else 27
        header_lines = [wrap(columns[i], col_width-32, size, True) for i in indices]
        header_h = max(len(v) for v in header_lines)*size*1.28+30
        prepared = []
        for row in rows:
            cells = [str(row[i]) if i < len(row) else '' for i in indices]
            lines = [wrap(cell, col_width-32, size) for cell in cells]
            prepared.append(dict(cells=cells, lines=lines, height=max(len(v) for v in lines)*size*1.28+26))
        if not prepared: prepared=[dict(cells=['']*len(indices), lines=[['']]*len(indices), height=60)]
        while prepared:
            chunk, height = [], header_h
            while prepared and height+prepared[0]['height'] <= 580:
                row=prepared.pop(0); chunk.append(row); height += row['height']
            if not chunk:
                # Report cell limits usually prevent this; preserve oversized evidence
                # on text continuation slides rather than clipping it.
                row=prepared.pop(0)
                _prose_pages(slides, title, [f'{columns[i]}: {v}' for i,v in zip(indices,row['cells'])], company)
                continue
            s = new_slide(title+(' (continued)' if group_index or prepared else ''), company, len(slides)+1)
            s['items'].append(dict(kind='table', x=x, y=210, w=width, h=height,
                columns=[columns[i] for i in indices], header_lines=header_lines, header_h=header_h,
                rows=chunk, col_widths=[col_width]*len(indices), size=size))
            overflow=[]
            if left_width:
                narrative='\n\n'.join(str(p) for p in paragraphs)
                lines=wrap(narrative, left_width, 29)
                visible, overflow=lines[:14], lines[14:]
                s['items'].append(dict(kind='text', x=80, y=215, w=left_width, h=len(visible)*38,
                    lines=visible, size=29, color=INK, bold=False, leading=38))
            slides.append(s)
            if overflow:
                _prose_pages(slides, title+' - Details', ['\n'.join(overflow)], company)
            paragraphs=()  # Never repeat the same narrative on every table page.


def _table_covers_chart(table, columns, rows):
    from decimal import Decimal, InvalidOperation
    def cell(value):
        raw=str(value).strip().replace(',','').removesuffix('%')
        try: return Decimal(raw)
        except InvalidOperation: return raw.casefold()
    names=[name.casefold() for name in table.columns]
    if any(name.casefold() not in names for name in columns[1:]): return False
    indices=[0]+[names.index(name.casefold()) for name in columns[1:]]
    return all(any(str(candidate[0]).casefold()==str(row[0]).casefold() and
                   all(cell(candidate[index])==cell(value) for index,value in zip(indices[1:],row[1:]))
                   for candidate in table.rows if len(candidate)>max(indices)) for row in rows)


def build_slides(report):
    report=ReportDocument.model_validate(report.model_dump())
    sr, content = report.structured_report, report.content
    title=sr.title if sr else content.title
    company=report.company_name or (sr.company_name if sr else None) or 'AI Box'
    slides=[]
    cover=new_slide(title, company, 1, 'cover')
    rect(cover, 0, 0, W, H, INK)
    rect(cover, 80, 102, 76, 7, '#A5B4FC')
    text(cover, company.upper(), 80, 142, 1410, 24, '#C7D2FE', True)
    title_size=68
    while title_size>38 and len(wrap(title,1360,title_size,True))>4:
        title_size-=2
    used=text(cover, title, 80, 238, 1360, title_size, WHITE, True)
    period=(sr.scope.get('selected_period') or sr.scope.get('display_period') or {}) if sr else {}
    from_day=period.get('from_date') or period.get('from')
    to_day=period.get('to_date') or period.get('to')
    label=f'{from_day} to {to_day}' if from_day and to_day else report.generated_at.strftime('%d %b %Y')
    text(cover, label, 84, min(238+used+44, 610), 1330, 27, '#CBD5E1')
    text(cover, f'{report.records_analyzed:,}', 80, 675, 550, 80, WHITE, True)
    text(cover, 'SHIPMENTS ANALYSED' if report.report_type=='shipment' else 'RECORDS ANALYSED',
         84, 779, 800, 20, '#CBD5E1')
    metrics=sr.metrics if sr else report.key_metrics
    extra=[]
    for metric in metrics:
        try: duplicate_count=float(metric.value)==report.records_analyzed and 'order' in metric.label.lower()
        except (ValueError, TypeError): duplicate_count=False
        try: total_share=float(metric.value)==100 and 'share' in metric.label.lower()
        except (ValueError,TypeError): total_share=False
        if not duplicate_count and not total_share: extra.append(metric)
    cover_metrics=[m for m in extra if len(wrap(str(m.value),420,62,True))==1][:2]
    for index,metric in enumerate(cover_metrics):
        value=str(metric.value)+('%' if getattr(metric,'unit',None)=='percent' else '')
        label=metric.label.split(':')[-1].strip()
        x=620+index*470
        text(cover,value,x,690,420,62,WHITE,True)
        text(cover,label,x,785,420,20,'#CBD5E1')
    cover['notes'].extend(f'{m.label}: {m.value}. {m.note or ""}' for m in metrics)
    cover['notes']=[content.executive_summary]
    slides.append(cover)
    summary=sr.executive_summary.paragraphs+sr.executive_summary.highlights if sr else [content.executive_summary]
    meaningful=[p for p in summary if not p.strip().startswith('Requested analyses:')]
    if meaningful:
        _prose_pages(slides, 'Executive Summary', meaningful, company)
    # A sole record-count card is already the main figure on the cover.
    if len(extra)>len(cover_metrics):
        from app.models.report_schema import ReportTable
        rows=[[m.label, str(m.value)+('%' if getattr(m,'unit',None)=='percent' else ''), m.note or ''] for m in metrics]
        _table_pages(slides, 'Key Metrics', ReportTable(title='Key Metrics',columns=['Metric','Value','Notes'],rows=rows),company)
    charts=list(sr.charts) if sr else []
    for offset in range(0,len(charts),2):
        pair=charts[offset:offset+2]
        s=new_slide(pair[0].title if len(pair)==1 else 'Shipment Overview' if report.report_type=='shipment' else 'Report Overview',company,len(slides)+1)
        for index,chart in enumerate(pair):
            png=presentation_chart(chart)
            if not png:
                from app.models.report_schema import ReportTable
                cols,rows=chart_table(chart)
                _table_pages(slides,chart.title,ReportTable(title=chart.title,columns=cols,rows=rows),company,[FALLBACK_NOTE])
                continue
            x=80+index*750 if len(pair)>1 else 130
            width=690 if len(pair)>1 else 1340
            text(s,chart.title,x,204,width,26,bold=True)
            from PIL import Image
            image=Image.open(BytesIO(png))
            iw,ih=image.size
            scale=min(width/iw,490/ih)
            s['items'].append(dict(kind='image',x=x+(width-iw*scale)/2,y=280,w=iw*scale,h=ih*scale,
                                   data=base64.b64encode(png).decode('ascii')))
            if chart.description: s['notes'].append(chart.description)
        if any(i['kind']=='image' for i in s['items']): slides.append(s)
    if sr:
        for section in sr.sections:
            paragraphs=[]; tables=[]
            for block in section.blocks:
                if block.type=='paragraph': paragraphs.append(block.text)
                elif block.type=='bullet_list': paragraphs.extend(block.items)
                else: tables.append(block)
            if tables:
                _table_pages(slides, section.title, tables[0], company, paragraphs)
                for table in tables[1:]: _table_pages(slides, table.title, table, company)
            elif paragraphs: _prose_pages(slides,section.title,paragraphs,company)
        for note in sr.chart_notes: _prose_pages(slides,note.title or 'Chart note',[note.reason],company)
    else:
        for section in content.sections: _prose_pages(slides,section.heading,[section.content],company)
        for table in report.tables: _table_pages(slides,table.title,table,company)
    findings=sr.findings if sr else content.findings
    recommendations=sr.recommendations if sr else content.recommendations
    if findings: _prose_pages(slides,'Key Findings',[f'{f.title}\n{f.detail}' for f in findings],company)
    if recommendations: _prose_pages(slides,'Recommendations',[f'{r.action}\n{r.rationale}' for r in recommendations],company)
    limitations=sr.limitations if sr else content.data_limitations
    if limitations: _prose_pages(slides,'Scope & Limitations',list(dict.fromkeys(limitations)),company,size=28,page_lines=16)
    # Disclaimer is available in notes, rather than consuming its own nearly empty slide.
    for s in slides: s['notes'].append(report.disclaimer)
    # Chart data is not duplicated into slides when already present in report tables.
    if sr and charts:
        from app.models.report_schema import ReportTable
        seen=set()
        for chart in charts:
            cols,rows=chart_table(chart)
            key=(tuple(cols),tuple(tuple(row) for row in rows))
            if key not in seen and not any(_table_covers_chart(t,cols,rows) for t in report.tables):
                _table_pages(slides,chart.title+' - Data',ReportTable(title=chart.title,columns=cols,rows=rows),company)
            seen.add(key)
    for index,s in enumerate(slides,1):
        for item in s['items']:
            if item['kind']=='text' and item['x']==1470 and item['y']==847:
                item['lines']=[f'{index:02d}']
    return slides


def slide_svg(slide):
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">',f'<rect width="{W}" height="{H}" fill="white"/>']
    def texts(lines,x,y,size,color,bold=False,leading=None):
        for index,line in enumerate(lines):
            out.append(f'<text x="{x}" y="{y+size+index*(leading or size*1.28)}" font-family="Arial, sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(str(line))}</text>')
    for item in slide['items']:
        k=item['kind']; x,y,w,h=(item[v] for v in ('x','y','w','h'))
        if k=='rect': out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{item["fill"]}"/>')
        elif k=='text': texts(item['lines'],x,y,item['size'],item['color'],item['bold'],item['leading'])
        elif k=='image': out.append(f'<image x="{x}" y="{y}" width="{w}" height="{h}" href="data:image/png;base64,{item["data"]}"/>')
        elif k=='table':
            out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{item["header_h"]}" fill="{INK}"/>')
            xx=x
            for lines,cw in zip(item['header_lines'],item['col_widths']):
                texts(lines,xx+16,y+12,item['size'],WHITE,True); xx+=cw
            yy=y+item['header_h']
            for index,row in enumerate(item['rows']):
                out.append(f'<rect x="{x}" y="{yy}" width="{w}" height="{row["height"]}" fill="{LIGHT if index%2==0 else WHITE}"/>')
                xx=x
                for lines,cw in zip(row['lines'],item['col_widths']):
                    texts(lines,xx+16,yy+10,item['size'],INK); xx+=cw
                yy+=row['height']
    out.append('</svg>')
    return ''.join(out)


def preview_pptx(report):
    slides=build_slides(report)
    return dict(width=W,height=H,slides=[dict(title=s['title'],
        image='data:image/svg+xml;base64,'+base64.b64encode(slide_svg(s).encode('utf-8')).decode('ascii')) for s in slides])
