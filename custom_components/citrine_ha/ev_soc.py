"""The plugged-in EV's state of charge per station, read from CitrineOS.

A station that reports the EV's SoC sends it as an OCPP sampled value with
measurand "SoC" (unit Percent, usually location EV) in a TransactionEvent or
MeterValues. CitrineOS keeps it in MeterValues. Each station gets the latest
SoC of its active transaction, else None: a departed EV has no SoC.

One flat Hasura query by transaction database id, so no relationship names
are needed. A refused query only drops the value; it never fails discovery.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from .power_limits import PowerLimitReader

_LOGGER = logging.getLogger(__name__)
RETRY_S = 600
ROWS = 50

SOC_QUERY = """
query EvSoc($transactions: [Int!]!, $tenant: Int!) {
  MeterValues(where: {transactionDatabaseId: {_in: $transactions}, tenantId: {_eq: $tenant},
    sampledValue: {_contains: [{measurand: "SoC"}]}}, order_by: {timestamp: desc}, limit: 50) {
    transactionDatabaseId timestamp sampledValue
  }
}"""


def soc_value(sampled: Any) -> float | None:
    """The SoC in % from one sampledValue list, or None. Prefers location EV."""
    found = {}
    for s in sampled if isinstance(sampled, list) else []:
        if not isinstance(s, dict) or s.get("measurand") != "SoC" or s.get("phase") is not None:
            continue
        unit = s.get("unitOfMeasure") or {}
        if unit.get("unit", "Percent") != "Percent":
            continue
        exponent = unit.get("multiplier", 0)
        raw = s.get("value")
        if type(exponent) is not int or isinstance(raw, bool):
            continue
        try:
            value = float(raw) * 10 ** exponent
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and 0 <= value <= 100:
            found.setdefault(s.get("location", "EV"), value)
    return found.get("EV", next(iter(found.values()), None))


class EvSocReader:
    """Adds "ev_soc" to every station of a discovery payload."""

    def __init__(self, hasura: Any | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._hasura = hasura
        self._clock = clock
        self._blocked_until: float | None = None
        self._warned = False

    async def async_attach(self, data: dict[str, Any], tenant_id: int) -> None:
        stations = [s for s in data.get("stations", []) if isinstance(s, dict) and s.get("id")]
        active = PowerLimitReader._active_transactions(stations, data.get("transactions") or [])
        rows: list[dict[str, Any]] = []
        if self._hasura is not None and active and (
                self._blocked_until is None or self._clock() >= self._blocked_until):
            try:
                result = await self._hasura.query(SOC_QUERY, variables={
                    "transactions": sorted({t["db"] for t in active.values()}), "tenant": tenant_id})
                rows = (result.get("data") or {}).get("MeterValues")
                if not isinstance(rows, list) or len(rows) > ROWS:
                    raise ValueError("Invalid SoC meter collection")
            except Exception as err:  # noqa: BLE001 - a refused query must not fail discovery
                self._blocked_until = self._clock() + RETRY_S
                (_LOGGER.debug if self._warned else _LOGGER.warning)(
                    "CitrineOS SoC meter values are not readable (%s); EV SoC is unknown for %s s",
                    err, RETRY_S)
                self._warned, rows = True, []
        latest: dict[int, dict[str, Any]] = {}
        for row in rows:  # newest first
            db = row.get("transactionDatabaseId") if isinstance(row, dict) else None
            value = soc_value(row.get("sampledValue")) if type(db) is int else None
            if value is not None and db not in latest:
                latest[db] = {"value": value, "timestamp": row.get("timestamp")}
        for station in stations:
            tx = active.get(str(station["id"]))
            reading = latest.get(tx["db"]) if tx else None
            station["ev_soc"] = {"value": reading["value"] if reading else None,
                                 "timestamp": reading["timestamp"] if reading else None,
                                 "transaction_id": station.get("activeTransactionId") if tx else None}
