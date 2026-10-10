"""Explicit API profiles and conservative control boundaries."""

CONF_API_PROFILE = "api_profile"
CONF_READ_ONLY = "read_only"
PROFILE_LEGACY = "legacy"
PROFILE_V2 = "v2_hasura_read_only"
API_PROFILES = [PROFILE_LEGACY, PROFILE_V2]


def is_read_only(data, options=None):
    """Only an explicit boolean false enables legacy control."""
    values = {**data, **(options or {})}
    return (values.get(CONF_API_PROFILE, PROFILE_LEGACY) == PROFILE_V2
            or values.get(CONF_READ_ONLY, True) is not False)


# Exact public-table fields from CitrineOS v2.0.1. No automatic field dropping.
# Bound every collection; hitting the bound is reported, not silently truncated.
V2_DISCOVERY_QUERY = """
query ChargingStations($tenantId: Int!) {
  ChargingStations(where: {tenantId: {_eq: $tenantId}}, limit: 501) {
    id ocppConnectionName tenantId protocol isOnline
    chargePointVendor chargePointModel firmwareVersion updatedAt
  }
  Connectors(where: {tenantId: {_eq: $tenantId}}, limit: 501) {
    id stationId tenantId connectorId evseId status errorCode updatedAt
  }
  Transactions(where: {tenantId: {_eq: $tenantId}, isActive: {_eq: true}}, limit: 501) {
    id stationId tenantId transactionId isActive
    startedAt: startTime stoppedAt: endTime updatedAt
  }
}
"""


def normalize_v2_discovery(result, tenant_id):
    """Preserve DB keys separately; use connection names for displayed identity."""
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        raise ValueError("Invalid Hasura discovery response")
    data = result["data"]
    collections = []
    for key in ("ChargingStations", "Connectors", "Transactions"):
        rows = data.get(key)
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError("Missing discovery collection or 500-row limit exceeded")
        if any(not isinstance(row, dict) or row.get("tenantId") != tenant_id for row in rows):
            raise ValueError("Invalid discovery row or tenant mismatch")
        collections.append(rows)
    stations, connectors, transactions = collections
    names = {}
    seen_names = set()
    normalized = []
    for row in stations:
        key, name = row.get("id"), row.get("ocppConnectionName")
        if (type(key) is not int or not isinstance(name, str) or not name.strip()
                or key in names or name in seen_names):
            raise ValueError("Missing or ambiguous station identity")
        names[key] = name
        seen_names.add(name)
        normalized.append({**row, "databaseId": key, "id": name})
    children = []
    for rows in (connectors, transactions):
        values = []
        for row in rows:
            key = row.get("stationId")
            if type(key) is not int or key not in names:
                raise ValueError("Unresolved station relationship")
            values.append({**row, "databaseStationId": key, "stationId": names[key]})
        children.append(values)
    return {"stations": normalized, "connectors": children[0], "transactions": children[1]}
