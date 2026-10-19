"""Usable charge and discharge power per station, read from CitrineOS.

Each station gets two values in W: the smallest of the available sources, else
a configured fallback.

1. ev_charging_needs: the EV's limits from the latest ChargingNeeds row of the
   station's active transaction (NotifyEVChargingNeeds, OCPP 2.0.1 / 2.1).
2. evse_rating: the device model rating, variable "Power" of component "EVSE"
   (else "ChargingStation"): the station's own MaxSet VariableAttribute, else
   VariableCharacteristics.maxLimit (evse_max_limit_shared: CitrineOS keeps one
   per variable and tenant, shared by every station). Discharge-specific
   variables are used for discharge when the station reports one.
3. fallback: the default_max_power_w option. OCPP 1.6 stations always use it.

All reads are flat Hasura queries by id, so no relationship names are needed.
A table or column the reader may not select only drops that source: the
values never become unavailable for it, and the problem is logged once.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
import math
import time
from typing import Any, Callable

from .hasura_client import HasuraAuthError, HasuraError

_LOGGER = logging.getLogger(__name__)

SOURCE_EV = "ev_charging_needs"
SOURCE_EVSE = "evse_rating"
SOURCE_EVSE_SHARED = "evse_max_limit_shared"
SOURCE_FALLBACK = "fallback"

CHARGE = "charge"
DISCHARGE = "discharge"

# Device model ratings. "Power" is the OCPP 2.0.1/2.1 rated power of an EVSE or
# the station. The discharge names are not standard: a station may report one,
# and the App's ha_reader may or may not allow it. Unknown names are ignored.
RATING_COMPONENTS = ("EVSE", "ChargingStation")
CHARGE_RATING_VARIABLES = ("Power",)
DISCHARGE_RATING_VARIABLES = ("DischargePower", "MaxDischargePower", "PowerDischarge",
                              "V2XMaxDischargePower")
RATING_NAMES = CHARGE_RATING_VARIABLES + DISCHARGE_RATING_VARIABLES

# ChargingNeeds parameter keys, OCPP 2.0.1 and 2.1 (DCChargingParametersType,
# ACChargingParametersType, V2XChargingParametersType). Power keys win over
# current x voltage; *_L2/_L3 per-phase powers are added when present.
POWER_KEYS = {CHARGE: ("evMaxPower", "evMaxChargePower", "maxChargePower"),
              DISCHARGE: ("evMaxDischargePower", "maxDischargePower")}
CURRENT_KEYS = {CHARGE: ("evMaxCurrent", "evMaxChargeCurrent", "maxChargeCurrent"),
                DISCHARGE: ("evMaxDischargeCurrent", "maxDischargeCurrent")}
VOLTAGE_KEYS = ("evMaxVoltage", "maxVoltage")
PHASE_KEYS = ("numberOfPhases", "numberPhases", "evMaxPhases", "phases")

RATING_REFRESH_S = 60       # device model changes at boot or on SetVariables
NEEDS_REFRESH_S = 15        # unless the set of active transactions changes
UNAVAILABLE_RETRY_S = 600   # a refused query is retried after this
UNITS = {None: 1, "": 1, "W": 1, "w": 1, "kW": 1000, "KW": 1000, "kw": 1000}

# Station keys are ChargingStations.id: Int on CitrineOS 2.x, String on 1.x.
VARIABLES_QUERY = """
query PowerRatingVariables($names: [String!]!) {
  Variables(where: {_or: [{name: {_in: $names}}, {name: {_ilike: "%discharge%power%"}}]},
    limit: 100) {
    id name instance
  }
}"""
ATTRIBUTES_QUERY = """
query PowerRatingAttributes($stations: [STATION_KEY!]!, $variables: [Int!]!) {
  VariableAttributes(where: {stationId: {_in: $stations}, variableId: {_in: $variables}},
    limit: 500) {
    id stationId evseDatabaseId componentId variableId type value updatedAt
  }
}"""
COMPONENTS_QUERY = """
query PowerRatingComponents($ids: [Int!]!) {
  Components(where: {id: {_in: $ids}}, limit: 500) {
    id name instance evseDatabaseId
  }
}"""
CHARACTERISTICS_QUERY = """
query PowerRatingCharacteristics($variables: [Int!]!) {
  VariableCharacteristics(where: {variableId: {_in: $variables}}, limit: 100) {
    id variableId maxLimit minLimit unit
  }
}"""
NEEDS_QUERY = """
query ChargingNeeds($transactions: [Int!]!, $evses: [Int!]!) {
  ChargingNeeds(where: {_or: [{transactionDatabaseId: {_in: $transactions}},
      {_and: [{transactionDatabaseId: {_is_null: true}}, {evseId: {_in: $evses}}]}]},
    order_by: {id: desc}, limit: 50) {
    id evseId transactionDatabaseId acChargingParameters dcChargingParameters createdAt updatedAt
  }
}"""


def number(value: Any) -> float | None:
    """A finite number from JSON (numbers or numeric strings), else None."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _params(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _power(params: dict[str, Any], key: str) -> float | None:
    total = number(params.get(key))
    if total is None:
        return None
    for phase in ("_L2", "_L3"):
        extra = number(params.get(key + phase))
        if extra is not None:
            total += abs(extra)
    return abs(total)


def _from_params(params: dict[str, Any], direction: str, phases: float = 1) -> float | None:
    """One container's limit: a power key, else current x voltage (x phases for AC)."""
    for key in POWER_KEYS[direction]:
        power = _power(params, key)
        if power is not None:
            return power
    voltage = next((v for v in (number(params.get(k)) for k in VOLTAGE_KEYS) if v is not None), None)
    for key in CURRENT_KEYS[direction]:
        current = number(params.get(key))
        if current is not None and voltage is not None:
            return abs(current) * abs(voltage) * phases
    return None


def ev_limits(row: dict[str, Any]) -> dict[str, float | None]:
    """EV charge and discharge limits (W) from one ChargingNeeds row.

    AC current is per phase: the phase count is used when the parameters carry
    one, else one phase (the smaller, safer value). energyAmount, SoC and
    departure time are ignored. A missing key is "not available", not zero.
    """
    dc = _params(row.get("dcChargingParameters"))
    ac = _params(row.get("acChargingParameters"))
    containers: list[tuple[dict[str, Any], float]] = [(dc, 1)]
    phases = next((p for p in (number(ac.get(k)) for k in PHASE_KEYS) if p and 1 <= p <= 3), 1)
    containers.append((ac, phases))
    for parent in (row, dc, ac):
        v2x = _params(parent.get("v2xChargingParameters"))
        if v2x:
            containers.append((v2x, 1))
    result: dict[str, float | None] = {}
    for direction in (CHARGE, DISCHARGE):
        values = [v for v in (_from_params(p, direction, n) for p, n in containers) if v is not None]
        result[direction] = min(values) if values else None
    return result


def _kind(variable: dict[str, Any]) -> str | None:
    name = str(variable.get("name") or "")
    instance = str(variable.get("instance") or "").lower()
    lower = name.lower()
    if name in DISCHARGE_RATING_VARIABLES or ("discharge" in lower and "power" in lower):
        return DISCHARGE
    if name in CHARGE_RATING_VARIABLES:
        return DISCHARGE if "discharge" in instance else CHARGE
    return None


def _max_set(characteristic: dict[str, Any] | None, attribute: dict[str, Any]) -> float | None:
    """The station's own MaxSet attribute value (W), in the characteristic's unit if known."""
    unit = (characteristic or {}).get("unit")
    if attribute.get("type") != "MaxSet" or unit not in UNITS:
        return None  # a rating in A or % is not a power
    value = number(attribute.get("value"))
    return value * UNITS[unit] if value is not None and value > 0 else None


def _max_limit(characteristic: dict[str, Any] | None) -> float | None:
    """VariableCharacteristics.maxLimit (W): one row per variable and tenant, shared by stations."""
    unit = (characteristic or {}).get("unit")
    limit = number((characteristic or {}).get("maxLimit"))
    return limit * UNITS[unit] if unit in UNITS and limit is not None and limit > 0 else None


def _negative_min_limit(characteristic: dict[str, Any] | None) -> float | None:
    unit = (characteristic or {}).get("unit")
    low = number((characteristic or {}).get("minLimit"))
    return -low * UNITS[unit] if unit in UNITS and low is not None and low < 0 else None


def device_model_ratings(
    attributes: list[dict[str, Any]],
    components: dict[Any, dict[str, Any]],
    variables: dict[Any, dict[str, Any]],
    characteristics: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, tuple[float, str]]]:
    """Ratings per station key: {"charge": (W, source), "discharge": (W, source)}.

    CitrineOS keeps one VariableCharacteristics row per variable and tenant, so
    its maxLimit (and minLimit) is whatever any station reported last. The
    station's own MaxSet attribute therefore wins; a characteristic is used only
    when the station has no MaxSet, with source evse_max_limit_shared.

    Charge: Power MaxSet, then Power maxLimit. Discharge: discharge-specific
    MaxSet, then its maxLimit, then a negative Power minLimit, then the charge
    rating (a bidirectional EVSE is rated the same both ways). Within a step
    the largest EVSE rating is used (ha_reader cannot map an EVSE component to
    the transaction's EVSE), else the ChargingStation rating.
    """
    found: dict[str, dict[tuple[str, str], list[float]]] = {}
    for attribute in attributes:
        component = components.get(attribute.get("componentId"))
        variable = variables.get(attribute.get("variableId"))
        if not component or not variable or component.get("name") not in RATING_COMPONENTS:
            continue
        kind = _kind(variable)
        if kind is None:
            continue
        characteristic = characteristics.get(attribute.get("variableId"))
        slots = found.setdefault(str(attribute.get("stationId")), {})
        steps = [(f"{kind}_max_set", _max_set(characteristic, attribute)),
                 (f"{kind}_max_limit", _max_limit(characteristic))]
        if kind == CHARGE:
            steps.append(("min_limit", _negative_min_limit(characteristic)))
        for step, value in steps:
            if value is not None:
                slots.setdefault((component["name"], step), []).append(value)
    ratings: dict[str, dict[str, tuple[float, str]]] = {}
    for station, slots in found.items():
        def pick(*steps: str) -> tuple[float, str] | None:
            for step in steps:
                for name in RATING_COMPONENTS:
                    values = slots.get((name, step))
                    if values:
                        return max(values), SOURCE_EVSE if step.endswith("max_set") else SOURCE_EVSE_SHARED
            return None
        charge = pick("charge_max_set", "charge_max_limit")
        discharge = pick("discharge_max_set", "discharge_max_limit", "min_limit") or charge
        ratings[station] = {k: v for k, v in ((CHARGE, charge), (DISCHARGE, discharge)) if v is not None}
    return ratings


def select_limit(ev_w: float | None, rating: tuple[float, str] | None, fallback_w: float,
                 transaction_id: Any = None) -> dict[str, Any]:
    """The smallest available source, else the fallback, with its attributes."""
    rating_w, rating_source = rating if rating else (None, SOURCE_EVSE)
    candidates = [(v, s) for v, s in ((ev_w, SOURCE_EV), (rating_w, rating_source)) if v is not None]
    value, source = min(candidates, key=lambda c: c[0]) if candidates else (fallback_w, SOURCE_FALLBACK)
    return {"value": round(value), "source": source,
            "ev_limit_w": None if ev_w is None else round(ev_w),
            "evse_rating_w": None if rating_w is None else round(rating_w),
            "fallback_w": round(fallback_w), "transaction_id": transaction_id}


def is_ocpp16(protocol: Any) -> bool:
    return str(protocol or "").strip().lower().replace(" ", "") in {
        "1.6", "ocpp1.6", "ocpp16", "ocpp-1.6"}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else None


def _rows(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = (result.get("data") or {}).get(key)
    if not isinstance(rows, list):
        raise ValueError(f"Missing {key} collection")
    return [row for row in rows if isinstance(row, dict)]


def _error_text(err: Exception) -> str:
    details = [e.get("message") for e in getattr(err, "errors", []) if isinstance(e, dict)]
    return "; ".join(str(d) for d in details if d)[:300] or str(err)


class PowerLimitReader:
    """Reads ChargingNeeds and device model ratings; never fails discovery."""

    def __init__(self, hasura: Any | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._hasura = hasura
        self._clock = clock
        self._ratings: dict[str, dict[str, tuple[float, str]]] = {}
        self._ratings_key: tuple | None = None
        self._ratings_at: float | None = None
        self._needs: dict[int, dict[str, float | None]] = {}
        self._needs_key: tuple | None = None
        self._needs_at: float | None = None
        self._blocked: dict[str, float] = {}
        self._warned: set[str] = set()
        self._transient = False

    async def async_attach(self, data: dict[str, Any], fallback_w: float) -> None:
        """Add "power_limits" to every station in a discovery payload."""
        stations = [s for s in data.get("stations", []) if isinstance(s, dict) and s.get("id")]
        eligible = [s for s in stations if not is_ocpp16(s.get("protocol"))]
        active = self._active_transactions(eligible, data.get("transactions") or [])
        if self._hasura is not None and eligible:
            try:
                await self._refresh_ratings(eligible)
                await self._refresh_needs(eligible, active)
            except Exception as err:  # noqa: BLE001 - a malformed row must not fail discovery
                self._degrade("max power data", err)
                self._ratings, self._needs = {}, {}
        for station in stations:
            sid = str(station["id"])
            tx = active.get(sid)
            needs = self._needs.get(tx["db"], {}) if tx and not is_ocpp16(station.get("protocol")) else {}
            rating = {} if is_ocpp16(station.get("protocol")) else self._ratings.get(self._key(station), {})
            tx_id = station.get("activeTransactionId")
            station["power_limits"] = {
                direction: select_limit(needs.get(direction), rating.get(direction), fallback_w, tx_id)
                for direction in (CHARGE, DISCHARGE)}

    @staticmethod
    def _key(station: dict[str, Any]) -> str:
        db = station.get("databaseId")
        return str(db if type(db) is int else station.get("id"))

    @staticmethod
    def _active_transactions(stations: list[dict[str, Any]],
                             transactions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Station id -> the active transaction's database id, start and EVSE ids."""
        result = {}
        for station in stations:
            sid, tx_id = str(station["id"]), station.get("activeTransactionId")
            if tx_id is None:
                continue
            row = next((t for t in transactions if str(t.get("stationId") or t.get("chargingStationId")) == sid
                        and str(t.get("transactionId") or t.get("id")) == str(tx_id)
                        and t.get("isActive") is not False), None)
            if row is None or type(row.get("id")) is not int:
                continue
            evses = sorted({c["databaseEvseId"] for c in station.get("connectors", [])
                            if type(c.get("databaseEvseId")) is int})
            result[sid] = {"db": row["id"], "start": _time(row.get("startedAt") or row.get("startTime")),
                           "evses": evses}
        return result

    def _usable(self, name: str) -> bool:
        until = self._blocked.get(name)
        return until is None or self._clock() >= until

    def _degrade(self, name: str, err: Exception) -> None:
        """Drop one source for a while; warn once per query, then debug."""
        self._blocked[name] = self._clock() + UNAVAILABLE_RETRY_S
        log = _LOGGER.debug if name in self._warned else _LOGGER.warning
        self._warned.add(name)
        log("CitrineOS %s is not readable for the max power sensors (%s); using the next "
            "source. The App's ha_reader role may need select on it (older CitrineOS App)",
            name, _error_text(err))

    async def _query(self, name: str, query: str, variables: dict[str, Any], key: str) -> list[dict[str, Any]] | None:
        """Rows, or None when the source is refused (schema) or briefly unreachable."""
        if not self._usable(name):
            return None
        try:
            rows = _rows(await self._hasura.query(query, variables=variables), key)
        except HasuraError as err:
            if err.errors or isinstance(err, HasuraAuthError):
                self._degrade(name, err)  # missing table, column or permission
            else:
                self._transient = True  # timeout or transport: retry next poll
                _LOGGER.debug("CitrineOS %s read for the max power sensors failed: %s", name, err)
            return None
        except (ValueError, TypeError) as err:
            self._degrade(name, err)
            return None
        self._blocked.pop(name, None)
        return rows

    async def _refresh_ratings(self, stations: list[dict[str, Any]]) -> None:
        keys = tuple(sorted(self._key(s) for s in stations))
        now = self._clock()
        if (self._ratings_key == keys and self._ratings_at is not None
                and now - self._ratings_at < RATING_REFRESH_S):
            return
        self._ratings_at, self._ratings_key, self._transient = now, keys, False
        integer = all(type(s.get("databaseId")) is int for s in stations)
        station_ids = [s["databaseId"] if integer else str(s["id"]) for s in stations]
        ratings: dict[str, dict[str, tuple[float, str]]] = {}
        variables = await self._query("Variables", VARIABLES_QUERY, {"names": list(RATING_NAMES)}, "Variables")
        variables = {v.get("id"): v for v in variables or [] if _kind(v)}
        if variables:
            attributes = await self._query(
                "VariableAttributes", ATTRIBUTES_QUERY.replace("STATION_KEY", "Int" if integer else "String"),
                {"stations": station_ids, "variables": list(variables)}, "VariableAttributes") or []
            component_ids = sorted({a.get("componentId") for a in attributes if type(a.get("componentId")) is int})
            components = {}
            if component_ids:
                rows = await self._query("Components", COMPONENTS_QUERY, {"ids": component_ids}, "Components")
                components = {c.get("id"): c for c in rows or []}
            characteristics = {}
            if components:
                rows = await self._query("VariableCharacteristics", CHARACTERISTICS_QUERY,
                                         {"variables": list(variables)}, "VariableCharacteristics")
                characteristics = {c.get("variableId"): c for c in rows or []}
            ratings = device_model_ratings(attributes, components, variables, characteristics)
        if self._transient:
            self._ratings_at = None  # keep the previous ratings and retry next poll
            return
        self._ratings = ratings

    async def _refresh_needs(self, stations: list[dict[str, Any]], active: dict[str, dict[str, Any]]) -> None:
        keys = tuple(sorted((sid, tx["db"]) for sid, tx in active.items()))
        now = self._clock()
        if not keys:
            self._needs, self._needs_key = {}, keys
            return
        if self._needs_key == keys and self._needs_at is not None and now - self._needs_at < NEEDS_REFRESH_S:
            return
        self._needs_at, self._transient = now, False
        evses = sorted({e for tx in active.values() for e in tx["evses"]})
        rows = await self._query("ChargingNeeds", NEEDS_QUERY,
                                 {"transactions": [tx["db"] for tx in active.values()], "evses": evses},
                                 "ChargingNeeds")
        if self._transient and self._needs_key == keys:
            self._needs_at = None  # keep the previous needs and retry next poll
            return
        self._needs_key = keys
        needs: dict[int, dict[str, float | None]] = {}
        for tx in active.values():
            row = self._latest_needs(rows or [], tx)
            if row is not None:
                needs[tx["db"]] = ev_limits(row)
        self._needs = needs

    @staticmethod
    def _latest_needs(rows: list[dict[str, Any]], tx: dict[str, Any]) -> dict[str, Any] | None:
        """The newest row of the transaction, else the newest unlinked row on its EVSE since it started.

        CitrineOS links ChargingNeeds to the EVSE's active transaction; a row sent
        before the transaction existed has no transactionDatabaseId.
        """
        def newest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
            return max(items, key=lambda r: number(r.get("id")) or 0, default=None)

        linked = newest([r for r in rows if r.get("transactionDatabaseId") == tx["db"]])
        if linked is not None or tx["start"] is None:
            return linked
        unlinked = []
        for row in rows:
            created = _time(row.get("createdAt"))
            if (row.get("transactionDatabaseId") is None and row.get("evseId") in tx["evses"]
                    and created is not None and created >= tx["start"]):
                unlinked.append(row)
        return newest(unlinked)


def sensor_state(station: dict[str, Any], direction: str, fallback_w: float) -> tuple[int, dict[str, Any]]:
    """State and attributes of a max power sensor; the fallback before the first read."""
    limit = (station.get("power_limits") or {}).get(direction) or select_limit(
        None, None, fallback_w, station.get("activeTransactionId"))
    return limit["value"], {k: v for k, v in limit.items() if k != "value"}
