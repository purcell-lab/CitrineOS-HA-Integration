"""Bounded, session-scoped OCPP meter readings; unavailable is not zero."""
from datetime import UTC, datetime
import math

TRANSACTION_QUERY = """
query LabTransaction($station: Int!, $tenant: Int!) {
  Transactions(where: {stationId: {_eq: $station}, tenantId: {_eq: $tenant}},
    order_by: {createdAt: desc}, limit: 2) {
    id stationId tenantId transactionId isActive startTime endTime
  }
}"""
METER_QUERY = """
query LabMeter($transaction: Int!, $tenant: Int!) {
  MeterValues(where: {transactionDatabaseId: {_eq: $transaction}, tenantId: {_eq: $tenant}},
    order_by: {timestamp: desc}, limit: 12) {
    tenantId transactionDatabaseId timestamp sampledValue
  }
}"""


def parse_meter(rows, transaction_id, tenant_id, now=None):
    now = now or datetime.now(UTC)
    result = {"power_w": None, "import_kwh": None, "export_kwh": None,
              "meter_timestamp": None, "meter_fresh": False}
    if not isinstance(rows, list) or len(rows) > 12:
        raise ValueError("Invalid bounded meter collection")
    for row in rows:
        if row.get("tenantId") != tenant_id or row.get("transactionDatabaseId") != transaction_id:
            raise ValueError("Meter identity mismatch")
        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Meter timestamp missing timezone")
        age = (now - stamp).total_seconds()
        if age < -5:
            continue
        # Use one meter frame, not a composite of stale counters and new power.
        if result["meter_timestamp"] is not None:
            break
        result.update(meter_timestamp=stamp.isoformat(), meter_fresh=age <= 45)
        by_location = {}
        for s in row.get("sampledValue", []):
            if s.get("phase") is not None:
                continue
            location = s.get("location", "Outlet")
            if location not in ("Outlet", "Inlet"):
                continue
            unit = s.get("unitOfMeasure") or {}
            try:
                raw = s.get("value")
                if isinstance(raw, bool):
                    continue
                exponent = unit.get("multiplier", 0)
                if type(exponent) is not int or not -9 <= exponent <= 9:
                    continue
                value = float(raw) * 10 ** exponent
            except (ValueError, TypeError, OverflowError):
                continue
            if not math.isfinite(value):
                continue
            measurand = s.get("measurand", "Energy.Active.Import.Register")
            u = unit.get("unit", s.get("unit"))
            factors = ({"W": 1, "kW": 1000} if measurand.startswith("Power.")
                       else {"Wh": 0.001, "kWh": 1})
            if u not in factors:
                continue
            key = (location, measurand)
            # Do not silently choose between conflicting duplicate samples.
            converted = value * factors[u]
            by_location[key] = converted if key not in by_location else None
        location = "Outlet" if any(k[0] == "Outlet" for k in by_location) else "Inlet"
        imported = by_location.get((location, "Power.Active.Import"))
        exported = by_location.get((location, "Power.Active.Export"))
        if result["meter_fresh"]:
            if imported is not None and exported is None:
                result["power_w"] = imported  # negative Import is preserved
            elif imported is None and exported is not None and exported >= 0:
                result["power_w"] = -exported
            elif imported is not None and exported is not None and imported >= 0 and exported >= 0:
                result["power_w"] = imported - exported
        for key, m in (("import_kwh", "Energy.Active.Import.Register"),
                       ("export_kwh", "Energy.Active.Export.Register")):
            value = by_location.get((location, m))
            result[key] = value if value is not None and value >= 0 else None
        result["meter_location"] = location
    return result


async def fetch_meter(hasura, station, tenant_id):
    result = await hasura.query(TRANSACTION_QUERY, variables={"station": station["databaseId"], "tenant": tenant_id})
    rows = result.get("data", {}).get("Transactions")
    if not isinstance(rows, list) or len(rows) > 2:
        raise ValueError("Invalid lab transaction response")
    if not rows:
        return {"power_w": None, "import_kwh": None, "export_kwh": None, "meter_fresh": False}
    tx = rows[0]
    if tx.get("stationId") != station["databaseId"] or tx.get("tenantId") != tenant_id:
        raise ValueError("Lab transaction identity mismatch")
    result = await hasura.query(METER_QUERY, variables={"transaction": tx["id"], "tenant": tenant_id})
    return {**parse_meter(result.get("data", {}).get("MeterValues"), tx["id"], tenant_id),
            "meter_transaction_id": tx["transactionId"], "transaction_closed": tx.get("isActive") is False}
