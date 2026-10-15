"""Unit tests for the server-side Chart Skill and its API endpoints."""
import json
from pathlib import Path
from starlette.testclient import TestClient

from app.main import app
from app.models.report_schema import ReportChart, ChartDataset
from app.skills.chart_skill import chart_skill
from app.prompts.shipment_prompt import parse_order, compute_metrics


def test_chart_skill_blueprint():
    bp = chart_skill.get_skill_blueprint()
    assert bp["name"] == "chart-crafting-skill"
    assert "area" in bp["supported_types"]
    assert "line" in bp["supported_types"]
    assert "bar" in bp["supported_types"]
    assert "donut" in bp["supported_types"]
    assert "pie" in bp["supported_types"]
    assert len(bp["palette"]) >= 6


def test_chart_skill_detection():
    prompt = (
        "1. A area Chart comparing courier-wise volume.\n"
        "2. A line Chart showing overall delivery status breakdown."
    )
    specs = chart_skill.detect_preferences(prompt)
    assert len(specs) == 2
    assert specs[0]["type"] == "area"
    assert specs[0]["topic"] == "courier"
    assert specs[1]["type"] == "line"
    assert specs[1]["topic"] == "status"


def test_craft_charts_from_metrics():
    sample_file = Path(__file__).parent.parent / "samples" / "shipment_request.json"
    sample = json.loads(sample_file.read_text(encoding="utf-8"))
    raw_orders = sample["data"]["getOrderList"]
    orders = [parse_order(o) for o in raw_orders]
    metrics = compute_metrics(orders)

    prompt = (
        "Please include exactly two charts:\n"
        "1. A area Chart comparing courier-wise total shipment volume.\n"
        "2. A line Chart showing the overall delivery status breakdown."
    )
    charts = chart_skill.craft_charts_from_metrics(metrics, prompt)
    assert len(charts) == 2
    assert charts[0].type == "area"
    assert charts[0].id == "courier_orders"
    assert charts[1].type == "line"
    assert charts[1].id == "shipment_status"


def test_render_png_and_base64():
    chart = ReportChart(
        id="test_area",
        title="Courier Volume Area",
        type="area",
        labels=["Delhivery", "Bluedart", "Ekart"],
        datasets=[ChartDataset(label="Orders", data=[30.0, 15.0, 8.0])]
    )
    png_bytes = chart_skill.render_png(chart)
    assert isinstance(png_bytes, bytes)
    assert len(png_bytes) > 1000

    b64 = chart_skill.render_base64(chart)
    assert b64.startswith("data:image/png;base64,")


def test_render_svg():
    chart = ReportChart(
        id="test_line",
        title="Delivery Status Trend",
        type="line",
        labels=["Delivered", "In Transit", "Cancelled"],
        datasets=[ChartDataset(label="Orders", data=[20.0, 5.0, 10.0])]
    )
    svg_str = chart_skill.render_svg(chart)
    assert "<svg" in svg_str
    assert "</svg>" in svg_str
    assert "polyline" in svg_str


def test_skill_api_endpoints():
    client = TestClient(app)

    # 1. Blueprint endpoint
    res_bp = client.get("/api/skills/charts/blueprint")
    assert res_bp.status_code == 200
    data_bp = res_bp.json()
    assert data_bp["name"] == "chart-crafting-skill"
    assert "area" in data_bp["supported_types"]

    # 2. Render endpoint
    chart_payload = {
        "id": "sample_bar",
        "title": "Top Couriers",
        "type": "bar",
        "labels": ["Courier A", "Courier B"],
        "datasets": [{"label": "Volume", "data": [10.0, 20.0]}]
    }
    res_render = client.post("/api/skills/charts/render", json=chart_payload)
    assert res_render.status_code == 200
    data_render = res_render.json()
    assert data_render["base64"].startswith("data:image/png;base64,")
    assert "<svg" in data_render["svg"]

    # 3. Direct PNG image stream endpoint
    res_png = client.post("/api/skills/charts/render/png", json=chart_payload)
    assert res_png.status_code == 200
    assert res_png.headers["content-type"] == "image/png"
    assert len(res_png.content) > 1000
