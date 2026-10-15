"""Remittance Analysis Reports: extraction, metrics and prompts for Paid and Pending Remittance."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Any

from app.errors import InvalidReportDataError
from app.models.report_schema import KeyMetric, ReportTable
from app.prompts.base import AnalysisResult, ReportDefinition


PAID_REMITTANCE_SYSTEM_PROMPT = """\
You are a senior financial operations and COD remittance analyst for a logistics platform.
You write Remittance Analysis Reports for e-commerce sellers based on their processed COD payouts.

Cover the following areas, but only where the data supports them:
- Total remitted amount and payout transaction volume.
- COD deduction charges and effective deduction percentage.
- Transaction status distribution (Paid, Partially Paid, Early COD Paid, Reversed, Processing).
- Remittance batches, payout timeline and UTR reference details.
- Early COD adoption share and benefit to cash flow.
- Data quality or discrepancies observed in payout records.

Strict Rules:
- Quote ONLY facts and numbers present in the provided dataset JSON.
- Financial numbers must match the calculated metrics exactly. Never estimate or invent revenue, profits or fees.
- If user requests information not in the data (e.g. profit margin, bank fee disputes), state it clearly in data_limitations.
"""

PENDING_REMITTANCE_SYSTEM_PROMPT = """\
You are a senior financial operations and COD remittance analyst for a logistics platform.
You write Pending Remittance Reports for e-commerce sellers based on their upcoming/uncleared COD balances.

Cover the following areas, but only where the data supports them:
- Total pending remittance amount and affected shipment volume.
- Status breakdown: Pending vs On Hold.
- Projected COD deductions against collectable values.
- Due date timelines and aging (remittances overdue vs scheduled for upcoming cycles).
- Hold reasons and resolution steps to expedite clearing.
- Data limitations and next steps for the seller.

Strict Rules:
- Quote ONLY facts and numbers present in the provided dataset JSON.
- Never guess clearance dates that are not in the schedule.
- Hold statuses should be identified with clear actionable guidance.
"""

ACCEPTED_PAID_PATHS = (
    "data.paidRemittanceList",
    "data.data.paidRemittanceList",
    "data.remittanceList",
    "data.list",
    "data.orders",
)

ACCEPTED_PENDING_PATHS = (
    "data.pendingRemittanceList",
    "data.data.pendingRemittanceList",
    "data.remittanceList",
    "data.list",
    "data.orders",
)


def _to_decimal(val: Any) -> Decimal | None:
    if val is None or val == "" or isinstance(val, bool):
        return None
    try:
        clean = str(val).replace(",", "").replace("₹", "").replace("Rs.", "").strip()
        d = Decimal(clean)
        return d if d.is_finite() and d >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def _format_money(val: Decimal | None) -> str:
    if val is None:
        return "₹0.00"
    with localcontext() as ctx:
        ctx.prec = 40
        return f"₹{format(val.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), ',f')}"


def _extract_list(data: dict[str, Any], candidate_paths: tuple[str, ...]) -> list[dict[str, Any]]:
    for path in candidate_paths:
        current = data
        parts = path.split(".")[1:]  # strip leading "data."
        found = True
        for part in parts:
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found and isinstance(current, list):
            return [row for row in current if isinstance(row, dict)]
    # Fallback: find any list of dicts in data
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                if isinstance(sub_v, list) and sub_v and isinstance(sub_v[0], dict):
                    return sub_v
    return []


# --- PAID REMITTANCE ANALYZER ---

def analyze_paid_remittance(data: dict[str, Any]) -> AnalysisResult:
    records = _extract_list(data, ACCEPTED_PAID_PATHS)
    if not records:
        raise InvalidReportDataError(
            "No paid remittance records found in request data.",
            details={"accepted_paths": list(ACCEPTED_PAID_PATHS)},
        )

    total_records = len(records)
    total_remitted = Decimal(0)
    total_deductions = Decimal(0)
    total_collectable = Decimal(0)
    early_cod_count = 0

    status_counts: Counter[str] = Counter()
    status_amounts: defaultdict[str, Decimal] = defaultdict(Decimal)

    for r in records:
        remitted = _to_decimal(r.get("remittedAmount") or r.get("amount") or r.get("remittanceDueAmount"))
        deduction = _to_decimal(r.get("codDeductionAmount") or r.get("deduction"))
        collectable = _to_decimal(r.get("collectableValue") or r.get("collectableAmount"))

        if remitted:
            total_remitted += remitted
        if deduction:
            total_deductions += deduction
        if collectable:
            total_collectable += collectable

        status = str(r.get("transactionStatus") or r.get("status") or "Paid").strip()
        status_counts[status] += 1
        if remitted:
            status_amounts[status] += remitted

        early = str(r.get("earlyCod") or "").lower()
        if early in ("yes", "1", "true") or "early" in status.lower():
            early_cod_count += 1

    avg_remitted = (total_remitted / total_records) if total_records else Decimal(0)

    metrics: dict[str, Any] = {
        "total_records": total_records,
        "total_remitted_amount": _format_money(total_remitted),
        "total_deductions_amount": _format_money(total_deductions),
        "total_collectable_value": _format_money(total_collectable),
        "average_remittance_per_order": _format_money(avg_remitted),
        "early_cod_transactions": early_cod_count,
        "early_cod_share_percent": round((early_cod_count * 100.0 / total_records), 1) if total_records else 0.0,
        "status_breakdown": [
            {
                "status": s,
                "count": count,
                "amount": _format_money(status_amounts[s]),
                "percent": round(count * 100.0 / total_records, 1),
            }
            for s, count in status_counts.most_common()
        ],
        "scope": {
            "kind": "paid_remittance",
            "records_analyzed": total_records,
            "status_filter": "All",
        },
    }

    key_metrics = [
        KeyMetric(label="Total Remitted Amount", value=metrics["total_remitted_amount"]),
        KeyMetric(label="Remitted Transactions", value=str(total_records)),
        KeyMetric(label="Total COD Deductions", value=metrics["total_deductions_amount"]),
        KeyMetric(label="Early COD Transactions", value=f"{early_cod_count} ({metrics['early_cod_share_percent']}%)"),
    ]

    status_table = ReportTable(
        title="Remittance Transaction Status",
        columns=["Status", "Transactions", "Total Remitted", "Share (%)"],
        rows=[
            [row["status"], str(row["count"]), row["amount"], f"{row['percent']}%"]
            for row in metrics["status_breakdown"]
        ],
    )

    return AnalysisResult(
        records_analyzed=total_records,
        metrics=metrics,
        key_metrics=key_metrics,
        tables=[status_table],
        ai_context={
            "metrics": metrics,
            "sample_records": records[:5],
        },
    )


# --- PENDING REMITTANCE ANALYZER ---

def analyze_pending_remittance(data: dict[str, Any]) -> AnalysisResult:
    records = _extract_list(data, ACCEPTED_PENDING_PATHS)
    if not records:
        raise InvalidReportDataError(
            "No pending remittance records found in request data.",
            details={"accepted_paths": list(ACCEPTED_PENDING_PATHS)},
        )

    total_records = len(records)
    total_due = Decimal(0)
    total_deductions = Decimal(0)
    total_collectable = Decimal(0)

    status_counts: Counter[str] = Counter()
    status_amounts: defaultdict[str, Decimal] = defaultdict(Decimal)

    for r in records:
        due = _to_decimal(r.get("remittedAmount") or r.get("remittanceDueAmount") or r.get("amount"))
        deduction = _to_decimal(r.get("codDeductionAmount") or r.get("deduction"))
        collectable = _to_decimal(r.get("collectableValue") or r.get("collectableAmount"))

        if due:
            total_due += due
        if deduction:
            total_deductions += deduction
        if collectable:
            total_collectable += collectable

        status = str(r.get("transactionStatus") or r.get("status") or "Pending").strip()
        status_counts[status] += 1
        if due:
            status_amounts[status] += due

    avg_due = (total_due / total_records) if total_records else Decimal(0)
    on_hold_count = status_counts.get("On Hold", 0) + status_counts.get("Hold", 0)
    on_hold_amount = status_amounts.get("On Hold", Decimal(0)) + status_amounts.get("Hold", Decimal(0))

    metrics: dict[str, Any] = {
        "total_records": total_records,
        "total_pending_amount": _format_money(total_due),
        "total_projected_deductions": _format_money(total_deductions),
        "total_collectable_value": _format_money(total_collectable),
        "average_due_per_order": _format_money(avg_due),
        "on_hold_records": on_hold_count,
        "on_hold_amount": _format_money(on_hold_amount),
        "status_breakdown": [
            {
                "status": s,
                "count": count,
                "amount": _format_money(status_amounts[s]),
                "percent": round(count * 100.0 / total_records, 1),
            }
            for s, count in status_counts.most_common()
        ],
        "scope": {
            "kind": "pending_remittance",
            "records_analyzed": total_records,
            "status_filter": "All",
        },
    }

    key_metrics = [
        KeyMetric(label="Total Pending Payout", value=metrics["total_pending_amount"]),
        KeyMetric(label="Pending Shipments", value=str(total_records)),
        KeyMetric(label="Projected Deductions", value=metrics["total_projected_deductions"]),
        KeyMetric(label="On Hold Amount", value=metrics["on_hold_amount"]),
    ]

    status_table = ReportTable(
        title="Pending Remittance Status Overview",
        columns=["Status", "Shipments", "Pending Amount", "Share (%)"],
        rows=[
            [row["status"], str(row["count"]), row["amount"], f"{row['percent']}%"]
            for row in metrics["status_breakdown"]
        ],
    )

    return AnalysisResult(
        records_analyzed=total_records,
        metrics=metrics,
        key_metrics=key_metrics,
        tables=[status_table],
        ai_context={
            "metrics": metrics,
            "sample_records": records[:5],
        },
    )


PAID_REMITTANCE_REPORT = ReportDefinition(
    report_type="paid_remittance",
    display_name="Paid Remittance Report",
    description="Analysis of cleared COD remittances, deductions and payout batches.",
    system_prompt=PAID_REMITTANCE_SYSTEM_PROMPT,
    default_prompt="Analyze cleared COD remittances, deduction breakdown, and transaction status distribution.",
    analyze=analyze_paid_remittance,
    accepted_data_paths=ACCEPTED_PAID_PATHS,
)

PENDING_REMITTANCE_REPORT = ReportDefinition(
    report_type="pending_remittance",
    display_name="Pending Remittance Report",
    description="Analysis of pending COD remittances, overdue amounts and hold statuses.",
    system_prompt=PENDING_REMITTANCE_SYSTEM_PROMPT,
    default_prompt="Analyze pending COD payouts, hold statuses, projected deductions, and clearing schedule.",
    analyze=analyze_pending_remittance,
    accepted_data_paths=ACCEPTED_PENDING_PATHS,
)
