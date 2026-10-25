"""Explicit API profiles and conservative control boundaries."""
import logging

_LOGGER = logging.getLogger(__name__)

CONF_API_PROFILE = "api_profile"
CONF_READ_ONLY = "read_only"
PROFILE_LEGACY = "legacy"
PROFILE_V2 = "v2_hasura_read_only"
PROFILE_LAB = "v2_scoped_lab_control"
CONF_COMMAND_TOKEN = "command_token"
V2_PROFILES = (PROFILE_V2, PROFILE_LAB)
API_PROFILES = [PROFILE_LEGACY, PROFILE_V2, PROFILE_LAB]


def automatic_control_disabled(data, options=None):
    """Manual lab controls never enable the inherited site-control loop."""
    values = {**data, **(options or {})}
    return values.get(CONF_API_PROFILE) == PROFILE_LAB or is_read_only(data, options)


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
    id stationId tenantId connectorId evseId evseTypeConnectorId status errorCode updatedAt
    Evse { id stationId tenantId evseTypeId }
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
        if any(not isinstance(row, dict) or type(row.get("tenantId")) is not int
               or row.get("tenantId") != tenant_id for row in rows):
            raise ValueError("Invalid discovery row or tenant mismatch")
        collections.append(rows)
    stations, connectors, transactions = collections
    keys = set()
    by_name = {}
    for row in stations:
        key = row.get("id")
        if type(key) is not int or key in keys:
            raise ValueError("Missing or ambiguous station identity")
        keys.add(key)
        name = row.get("ocppConnectionName")
        if isinstance(name, str) and name.strip():
            by_name.setdefault(name, []).append(row)
    # A station row without a usable connection name (a charger that sent an
    # empty identity), or one whose name another row also uses, is left out
    # with a warning: one bad row must not take every other station offline.
    # Its connectors and transactions are left out with it.
    names = {}
    normalized = []
    protocols = {}
    for row in stations:
        key, name = row["id"], row.get("ocppConnectionName")
        if not isinstance(name, str) or not name.strip():
            _LOGGER.warning("Skipping CitrineOS station %s: no OCPP connection name", key)
            continue
        if len(by_name[name]) > 1:
            _LOGGER.warning("Skipping CitrineOS station %s: connection name %r is used by %d stations",
                            key, name, len(by_name[name]))
            continue
        names[key] = name
        protocols[key] = row.get("protocol")
        normalized.append({**row, "databaseId": key, "id": name})
    children = []
    for is_connector, rows in ((True, connectors), (False, transactions)):
        values = []
        for row in rows:
            key = row.get("stationId")
            if type(key) is not int or key not in keys:
                raise ValueError("Unresolved station relationship")
            if key not in names:
                continue
            item = {**row, "databaseStationId": key, "stationId": names[key]}
            if is_connector:
                evse = row.get("Evse")
                if (not isinstance(evse, dict) or evse.get("tenantId") != tenant_id
                        or evse.get("stationId") != key or evse.get("id") != row.get("evseId")):
                    raise ValueError("Unresolved EVSE relationship")
                legacy = protocols[key] in ("1.6", "ocpp1.6")
                connector_id = row.get("connectorId") if legacy else row.get("evseTypeConnectorId")
                evse_id = connector_id if legacy else evse.get("evseTypeId")
                if (type(connector_id) is not int or connector_id < 1
                        or type(evse_id) is not int or evse_id < 1):
                    raise ValueError("Missing OCPP connector or EVSE identity")
                item.update(databaseEvseId=row["evseId"], evseId=evse_id,
                            connectorId=connector_id, legacyConnectorId=row.get("connectorId"))
            values.append(item)
        children.append(values)
    return {"stations": normalized, "connectors": children[0], "transactions": children[1]}
