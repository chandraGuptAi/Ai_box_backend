"""Report schema 3.0: deterministic shipment insights, matched to the reference dashboard."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.report_routes import get_report_service
from app.main import app
from app.services.report_service import ReportService
from app.services.shipment_insights import build_insights, normalize_order

FIXTURES = Path(__file__).parent / "fixtures" / "report_v3"
SHIPMENTS = json.loads((FIXTURES / "shipments_in_transit_11.json").read_text(encoding="utf-8"))
ACCOUNT = json.loads((FIXTURES / "account_status.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "expected_in_transit_11.json").read_text(encoding="utf-8"))
AS_OF = date(2026, 10, 13)


@pytest.fixture(scope="module")
def insights():
    return build_insights({"getOrderList": SHIPMENTS, "account_summary": ACCOUNT}, as_of=AS_OF)


def test_kpis_match_reference_dashboard(insights):
    k, e = insights["kpis"], EXPECTED["kpis"]
    assert (k["orders"], k["all_orders"], k["share_of_all"], k["flagged"]) == (e["in_transit"], e["all_orders"], e["share_of_all"], e["flagged"])
    assert (k["delivered_but_open"], k["cancel_noted"], k["median_age_days"]) == (6, 3, 55)
    assert (k["spend"], k["avg_charge"], k["median_charge"], k["goods_value"]) == (12304.40, 1118.58, 508.94, 617802)


def test_issues_match_reference_dashboard(insights):
    issues = {i["id"]: i for i in insights["issues"]}
    for key, value in EXPECTED["issues"].items():
        if key == "charge_outlier":
            assert issues[key]["order_ids"] == ["252742834"]
        else:
            assert issues[key]["orders"] == value, key
    assert [i["severity"] for i in insights["issues"]][:3] == ["High", "High", "High"]


def test_couriers_and_account_mix(insights):
    delhivery = next(c for c in insights["couriers"] if c["courier"] == "LTL Delhivery")
    assert (delhivery["orders"], delhivery["charges"], delhivery["charge_per_kg"]) == (2, 7339.94, 215.88)
    assert insights["account_status_mix"] == EXPECTED["account_status_mix"]
    assert insights["account_status_mix"][0] == {"status": "Delivered", "orders": 35, "share": 0.5469}
    assert insights["theme"]["brand"]["primary"] == "#6C4CF1"


def test_single_status_scope_and_takeaway(insights):
    assert insights["scope"]["single_status"] is True
    assert insights["takeaway"].startswith("6 of the 11 shipments already have a delivery date")
    assert "Delivery rate" not in json.dumps(insights["kpis"])


def test_raw_bigship_field_names_are_mapped():
    row = normalize_order({
        "MasterCustomOrderId": "563583354", "InvoiceNo": "261910172", "createdAt": "2026-08-17 10:12:00",
        "courierName": "LTL Ekart", "totalCharge": "508.94", "invoiceTotalAmount": "12,500.00",
        "totalWeight": "10000", "WeightUnit": "gm", "payment_mode": "Prepaid", "status": "In Transit",
        "orderCancelReason": "Customer refused", "consignee": {"city": "Pune", "state": "Maharashtra",
                                                             "companyName": "Secret Person", "companyMobile": "9876543210"},
    })
    assert row["order_id"] == "563583354" and row["order_date"] == "2026-08-17"
    assert str(row["shipping_charge"]) == "508.94" and str(row["invoice_value"]) == "12500.00"
    assert row["weight_kg"] == 10 and row["dest_city"] == "Pune" and row["cancel_reason"] == "Customer refused"
    assert "Secret Person" not in json.dumps(row, default=str) and "9876543210" not in json.dumps(row, default=str)


def test_raw_account_counters_and_missing_fields_are_not_zero():
    result = build_insights({"getOrderList": [{"MasterCustomOrderId": "1", "status": "Delivered", "courierName": "A",
                                               "totalCharge": 100, "createdAt": "2026-10-01"}],
                             "account_summary": {"all_orders": 64, "delivered": 35, "cancel": 15, "in_transit": 11,
                                                 "pickup_scheduled": 1, "undelivered": 1, "rto_in_transit": 1, "lost": 0}},
                            as_of=AS_OF)
    assert result["kpis"]["all_orders"] == 64 and result["account_status_mix"][1] == {"status": "Cancelled", "orders": 15, "share": 0.2344}
    # No delivery-date / remark / cancel fields in the data: those checks are reported as not run.
    assert result["kpis"]["delivered_but_open"] is None and result["kpis"]["cancel_noted"] is None
    assert any("Delivery date on file" in g for g in result["data_gaps"])


@pytest.fixture
def client_with(request):
    def make(ai):
        app.dependency_overrides[get_report_service] = lambda: ReportService(ai)
        return TestClient(app)
    yield make
    app.dependency_overrides.clear()


class InventingAI:
    model = "fake-model"
    def __init__(self): self.calls = 0
    async def generate_json(self, messages, **kwargs):
        self.calls += 1
        return {"title": "Report", "executive_summary": "999999 invented shipments were delivered.", "sections": [],
                "findings": [], "recommendations": [], "data_limitations": []}


def test_api_returns_insights_and_falls_back_instead_of_502(client_with):
    ai = InventingAI()
    body = {"report_type": "shipment", "prompt": "create full analytic document",
            "data": {"getOrderList": SHIPMENTS, "account_summary": ACCOUNT, "as_of": "2026-10-13",
                     "report_filter": {"status_label": "In transit", "from_date": "2026-08-14", "to_date": "2026-10-13"}}}
    response = client_with(ai).post("/api/reports/generate", json=body)
    assert response.status_code == 200, response.text
    report = response.json()
    assert ai.calls == 1  # one paid call, then the code-written narrative; never a correction call
    assert "999999" not in response.text
    ins = report["insights"]
    assert ins["schema_version"] == "3.0" and ins["meta"]["fallback_used"] is True
    assert report["generation"]["narrative_source"] == "deterministic_fallback" and report["generation"]["notice"]
    assert ins["title"] == "In transit shipments, 14 Aug 2026 – 13 Oct 2026"
    assert ins["kpis"]["spend"] == 12304.40
    assert not any("rate" in m["label"].lower() for m in report["key_metrics"])
    assert [m["value"] for m in report["key_metrics"]] == ["11", "11", "₹12,304.40", "₹1,118.58", "₹6,17,802"]
    assert report["key_metrics"][0]["note"] == "17.2% of 64 orders on the account"
    assert report["structured_report"]["title"] == ins["title"]
    assert response.headers.get("X-Report-Access-Token")
