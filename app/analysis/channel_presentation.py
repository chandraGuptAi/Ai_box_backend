"""Request-local presentation vocabulary for store orders sharing the shipment engine."""
import re
from dataclasses import replace

ORDER_STATUSES = ('Unshipped', 'Processing', 'Shipped', 'Cancelled', 'Fulfilled')


def order_text(text):
    return re.sub(r'\bshipment(s?)\b', lambda m: ('Order' if m[0][0].isupper() else 'order') + m[1].lower(), text, flags=re.I)


def order_adapter(adapter):
    fields = {key: replace(spec, label=order_text(spec.label)) for key, spec in adapter.fields.items()}
    fields['shipment_status'] = replace(fields['shipment_status'], label='Order Status')
    return replace(adapter, fields=fields)


def order_plan(plan):
    notes = [order_text(note) for note in plan.notes
             if not re.search(r'shipment_status|status counts|status values', note, re.I)]
    notes.append('Channel order statuses: ' + ', '.join(ORDER_STATUSES) + '. Other source statuses are preserved as supplied.')
    return replace(plan, sections=tuple(section.model_copy(update={'title': order_text(section.title)})
                                       for section in plan.sections), notes=tuple(notes),
                   report_title=order_text(plan.report_title) if plan.report_title else plan.report_title)
