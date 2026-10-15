"""Verified shipping-charge calculations; invoice and collectable amounts are excluded."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from app.models.report_schema import ReportTable


def financial_focus(prompt):
    return bool(re.search(r"financial|shipping charg|shipping cost|freight charg|charge per order", prompt, re.I))


# Same accepted keys as the order parser, so charts, tables and financial totals read one field.
CHARGE_KEYS = ("totalcharge", "shippingcharge", "shippingcharges", "freightcharge", "freightcharges")
# The same cleaning the order parser applies, so a charge is valid (or invalid) everywhere at once.
_CURRENCY_MARKS = re.compile(r"(?i)inr|rs\.?|₹|,|\s")


def _charge(record):
    present = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in record.items()}
    return next((present[k] for k in CHARGE_KEYS if present.get(k) not in (None, "")), None)


def shipping_financial_metrics(records, orders):
    groups = {"courier": defaultdict(list), "payment_mode": defaultdict(list)}
    charges = []
    cancelled = []
    invalid = missing = cancelled_orders = 0
    for record, order in zip(records, orders):
        raw = _charge(record)
        charge = None
        if raw is None or raw == "":
            missing += 1
        else:
            try:
                if isinstance(raw, bool):
                    raise InvalidOperation
                charge = Decimal(_CURRENCY_MARKS.sub("", str(raw)))
                if not charge.is_finite() or charge < 0 or charge.adjusted() > 15 or charge.as_tuple().exponent < -12:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                charge = None
                invalid += 1
        name = order.courier or "Not specified"
        mode = order.payment_mode or "Not specified"
        groups["courier"][name.casefold()].append((name, charge))
        groups["payment_mode"][mode.casefold()].append((mode, charge))
        if order.category == "cancelled":
            cancelled_orders += 1
            if charge is not None:
                cancelled.append(charge)
        if charge is not None:
            charges.append(charge)

    def money(values):
        if not values:
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            return format(sum(values, Decimal(0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    def average(values):
        if not values:
            return None
        with localcontext() as ctx:
            ctx.prec = 40
            return format((sum(values, Decimal(0)) / len(values)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")

    result = {"total_shipping_charges": money(charges), "average_charge_per_order": average(charges),
              "charges_associated_with_cancelled_orders": money(cancelled) if cancelled_orders else "0.00",
              "orders_with_valid_charge": len(charges), "orders_with_missing_charge": missing,
              "orders_with_invalid_charge": invalid, "cancelled_orders_with_valid_charge": len(cancelled),
              "currency": "As supplied; currency is not inferred", "average_denominator": "orders_with_valid_charge",
              "courier_charges": [], "payment_mode_charges": []}
    for group, rows in groups.items():
        target = result["courier_charges" if group == "courier" else "payment_mode_charges"]
        for key, items in sorted(rows.items()):
            valid = [value for _,value in items if value is not None]
            target.append({group: items[0][0], "orders": len(items), "orders_with_valid_charge": len(valid),
                           "total_shipping_charges": money(valid), "average_charge_per_order": average(valid)})
    for key in ("cod", "topay", "prepaid"):
        row = next((r for r in result["payment_mode_charges"] if r["payment_mode"].replace(" ", "").casefold() == key), None)
        result[key + "_shipping_charges"] = row["total_shipping_charges"] if row else None
    return result


def financial_tables(financial):
    tables = []
    for key, label, name in (("courier_charges", "Shipping Charges by Courier", "courier"),
                             ("payment_mode_charges", "Shipping Charges by Payment Mode", "payment_mode")):
        rows = financial[key]
        if rows:
            tables.append(ReportTable(title=label, columns=[name.replace("_", " ").title(), "Orders", "Valid Charges", "Shipping Charges", "Average Charge"],
                rows=[[r[name], str(r["orders"]), str(r["orders_with_valid_charge"]),
                       r["total_shipping_charges"] or "Unavailable", r["average_charge_per_order"] or "Unavailable"] for r in rows[:200]]))
    return tables
