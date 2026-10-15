# Report v3 reference: 11 In-Transit orders (as of 2026-10-13)

Source: the target dashboard https://claude.ai/artifact/K5KdQXzE6vMg4XpQYteF8D.
Every number there is computed by the five rules below from the two data files in this folder.
The backend must compute the same numbers in Python and send them to DeepSeek and Angular.
DeepSeek only writes the wording.

| File | What it is |
|---|---|
| `shipments_in_transit_11.json` | The 11 orders, already **flattened** (names, emails and phones removed). |
| `account_status.json` | The account summary counters from the same API response (64 orders). |
| `expected_in_transit_11.json` | The exact expected output of the rules below (use it in pytest). |
| `target_dashboard.html` | The dashboard page code (layout reference only). |

> The flattened field names are not the raw Bigship names. The backend needs a `normalize_order()` that maps the raw keys to these. Confirm the raw names from one real API row:
> `shipping_charge` ← `totalCharge` · `invoice_value` ← `invoiceTotalAmount` · `cancel_remark`/`cancel_reason` ← `orderCancelRemark`/`orderCancelReason` · `courier` ← `courierName` · `order_date` ← `orderDate` · plus pickup date, delivery date, courier remark, weight (charged kg), e-way bill and product.

## Rules (`as_of` = 2026-10-13, injectable)

**Per order (`order_checks`)**
- `age_days` = days from `order_date` to `as_of`
- `delivered_date` = `delivery_date` present
- `cancel_note` = `cancel_remark` or `cancel_reason` present
- `pickup_before_order` = `pickup_date` < `order_date`
- `charge_outlier` = `shipping_charge` > 3 × median charge (median = sorted[n // 2])
- `over_30_days` = `age_days` > 30
- `flags` = number of the five checks above that are true

**Headline KPIs**: in_transit 11 · all_orders 64 · share 17.2% · flagged 11 · delivered_but_open 6 · cancel_noted 3 · median_age 55 · spend ₹12,304.40 · avg ₹1,118.58 · median ₹508.94 · goods value ₹6,17,802

**Issues (anomalies)**

| Issue | Count | Severity |
|---|---|---|
| Delivery date on file but status still in transit | 6 | High |
| Courier remark says delivered | 11 | High |
| Cancellation note on an active shipment | 3 | High |
| Open more than 30 days | 10 | Medium |
| Pickup date earlier than order date | 11 | Medium |
| Shipping charge over 3× median | 1 (`252742834`) | Medium |
| Test orders (product starts with "test") | 10 | Low |

**By courier** (sorted by orders): orders, weight_kg, charges, avg_charge, charge_per_kg = charges / weight
- LTL Ekart: 7 orders · 90 kg · ₹3,562.58 · ₹39.58/kg
- LTL Delhivery: 2 orders · 34 kg · ₹7,339.94 · **₹215.88/kg**
- LTL TCI Express: 2 orders · 30 kg · ₹1,401.88 · ₹46.73/kg

**Account status mix** (from the summary counters, not the filtered rows): Delivered 35 (54.7%) · Cancelled 15 · In transit 11 · Pickup scheduled 1 · Undelivered 1 · RTO in transit 1.
Do **not** show "Delivery rate" for the filtered 11 rows (it is always 0% on an In-Transit filter). Show account-level rates instead.

## Report layout (top to bottom)
1. Title with filter and period, plus a one-paragraph takeaway (delivered-but-open, cancel notes, median age)
2. Five KPI cards (above), the "issues" card in red
3. "What needs attention": the issues table with severity and action
4. "Where these shipments sit": account status bar (in transit highlighted) and days-open per order (red past 30 days)
5. "Shipping cost": charge per shipment with median line (outlier red) and the courier table
6. "Shipment detail": all orders with flags
