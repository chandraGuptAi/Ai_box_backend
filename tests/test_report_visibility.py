from app.report_visibility import customer_report


def test_issue_diagnostics_hidden_without_changing_orders_or_totals():
    source = {
        "insights": {"issues": [{"issue": "Internal issue"}], "order_checks": [{"flags": 3}],
                     "kpis": {"orders": 124, "flagged": 108, "spend": 285327.73},
                     "orders": [{"order_id": "1", "status": "Undelivered", "flags": 2}]},
        "metrics": [{"key": "flagged", "label": "Flagged Issues", "value": "108"},
                    {"key": "spend", "label": "Shipping Spend", "value": "285327.73"}],
        "findings": [{"title": "Data quality", "detail": "108 tracking issues"},
                     {"title": "Shipping spend", "detail": "Total charges"}],
        "paragraphs": ["Tracking issues detected.", "124 shipments analysed."],
    }
    result = customer_report(source)
    assert result["insights"]["kpis"] == {"orders": 124, "spend": 285327.73}
    assert result["insights"]["orders"] == [{"order_id": "1", "status": "Undelivered"}]
    assert len(result["metrics"]) == len(result["findings"]) == 1
    assert result["paragraphs"] == ["124 shipments analysed."]
    assert "issues" in source["insights"]  # internal data is untouched


def test_diagnostic_table_columns_stay_aligned():
    result = customer_report({"columns": ["Order ID", "Flags", "Charge"],
                              "rows": [["1", "3", "100"]], "values": [["1", 3, 100]],
                              "column_meta": [{"key": "id"}, {"key": "flags"}, {"key": "charge"}]})
    assert result["columns"] == ["Order ID", "Charge"]
    assert result["rows"] == [["1", "100"]]
    assert result["values"] == [["1", 100]]
    assert result["column_meta"] == [{"key": "id"}, {"key": "charge"}]


def test_response_serialization_and_export_input_hide_late_attached_issues():
    from app.models.report_schema import ReportDocument

    report = ReportDocument.model_validate({
        "report_type": "shipment", "report_name": "Shipments", "generated_at": "2026-10-21T10:00:00Z",
        "content": {"title": "Shipments", "executive_summary": "124 shipments analysed. Tracking issues detected."},
    })
    report.insights = {"issues": [{"issue": "Internal issue"}], "kpis": {"flagged": 108, "orders": 124}}
    result = report.model_dump()
    assert result["content"]["executive_summary"] == "124 shipments analysed."
    assert result["insights"] == {"kpis": {"orders": 124}}
    export = ReportDocument.model_validate(result)
    assert export.insights == result["insights"]
