"""Chargetronic webhooks: the EV's energy needs from ISO 15118-2 and DIN 70121 cars.

Chargetronic reads what the car tells the charger over PLC and posts it to a
public HTTPS endpoint as Standard Webhooks (signed JSON POSTs). This module
verifies and de-duplicates the events and keeps the latest session for each
Chargetronic device. A device mapped to a CitrineOS station gives that station
an EV charge limit while the car is plugged in, so an OCPP 1.6 or 2.0.1 charger
that sends no NotifyEVChargingNeeds still has the EV's limits and needs.

Events: session.plugged, ev.energy_request, ev.soc_final (DIN only) and
session.ended. Delivery is at least once, so every event id is applied once.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

EVENT_PLUGGED = "session.plugged"
EVENT_ENERGY_REQUEST = "ev.energy_request"
EVENT_SOC_FINAL = "ev.soc_final"
EVENT_ENDED = "session.ended"
EVENT_TYPES = (EVENT_PLUGGED, EVENT_ENERGY_REQUEST, EVENT_SOC_FINAL, EVENT_ENDED)

HA_EVENT = "citrine_ha_chargetronic"
SOURCE_CHARGETRONIC = "chargetronic"

# Standard Webhooks: refuse a timestamp more than five minutes from now (replay).
TOLERANCE_S = 300
# Event ids remembered for de-duplication; retries stop after about 7 minutes.
SEEN_MAX = 2048
# Nominal AC phase voltage, used only when the car reports currents, not power.
AC_PHASE_VOLTAGE_V = 230

APPLIED = "applied"
DUPLICATE = "duplicate"
STALE = "stale"


def signal(entry_id: str) -> str:
    """Dispatcher signal sent with the device id after an event is applied."""
    return f"{HA_EVENT}_{entry_id}"


def _key(secret: str) -> bytes:
    """A Standard Webhooks "whsec_" secret is base64; anything else is used as given."""
    if secret.startswith("whsec_"):
        return base64.b64decode(secret[len("whsec_"):])
    return secret.encode()


def sign(secret: str, msg_id: str, timestamp: int | str, body: bytes) -> str:
    """The v1 signature of a message, as Chargetronic computes it."""
    content = f"{msg_id}.{timestamp}.".encode() + body
    return base64.b64encode(hmac.new(_key(secret), content, hashlib.sha256).digest()).decode()


def verify_signature(secret: str, msg_id: str | None, timestamp: str | None,
                     signature: str | None, body: bytes, now: float) -> bool:
    """True when one v1 signature matches and the timestamp is within the tolerance."""
    if not (secret and msg_id and timestamp and signature):
        return False
    try:
        sent = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent) > TOLERANCE_S:
        return False
    try:
        expected = sign(secret, msg_id, sent, body)
    except ValueError:  # a malformed whsec_ secret
        return False
    for part in signature.split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def parse_devices(text: Any) -> dict[str, str]:
    """"sp-dev-01=STATION-1, sp-dev-02=STATION-2" -> {device id: station id}."""
    devices: dict[str, str] = {}
    for item in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        device, sep, station = item.partition("=")
        if sep and device.strip() and station.strip():
            devices[device.strip()] = station.strip()
    return devices


def number(value: Any) -> float | None:
    """A finite number, else None (missing is not zero)."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def ev_max_power_w(data: dict[str, Any]) -> float | None:
    """The car's maximum charge power (W) from an ev.energy_request.

    DIN 70121 reports power. ISO 15118-2 AC reports currents per phase and the
    on-board charger rating: the smaller of the two, with one phase assumed
    when the phase count is missing (the safer value).
    """
    if data.get("protocol") == "din70121":
        return number(data.get("ev_max_power_w"))
    candidates = [number(data.get("onboard_charger_w"))]
    current = number(data.get("ev_max_current_a"))
    if current is not None:
        phases = number(data.get("phases"))
        phases = phases if phases is not None and 1 <= phases <= 3 else 1
        candidates.append(current * phases * AC_PHASE_VOLTAGE_V)
    values = [v for v in candidates if v is not None and v >= 0]
    return min(values) if values else None


def departure(event_time: Any, seconds: Any) -> str | None:
    """ISO 15118-2 DepartureTime is seconds from the request: an absolute UTC time."""
    start, offset = _time(event_time), number(seconds)
    if start is None or offset is None or offset < 0:
        return None
    return (start + timedelta(seconds=offset)).isoformat()


class ChargetronicSessions:
    """The latest session per Chargetronic device, built from verified events."""

    def __init__(self, devices: dict[str, str] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.devices = dict(devices or {})
        self.sessions: dict[str, dict[str, Any]] = {}
        self.received = 0
        self.last_event: dict[str, Any] | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._clock = clock

    def apply(self, event: Any) -> str:
        """Merge one event; raises ValueError for a malformed envelope."""
        if not isinstance(event, dict):
            raise ValueError("event is not a JSON object")
        event_id, kind, device = event.get("id"), event.get("type"), event.get("device_id")
        if not (isinstance(event_id, str) and isinstance(kind, str) and isinstance(device, str)
                and event_id and device):
            raise ValueError("event needs id, type and device_id")
        if event_id in self._seen:
            return DUPLICATE
        self._seen[event_id] = None
        while len(self._seen) > SEEN_MAX:
            self._seen.popitem(last=False)
        self.received += 1
        self.last_event = {"id": event_id, "type": kind, "device_id": device, "time": event.get("time")}
        if kind not in EVENT_TYPES:
            _LOGGER.debug("Chargetronic event type %s ignored", kind)
            return STALE
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        session_id = event.get("session_id")
        current = self.sessions.get(device)
        if current is None or kind == EVENT_PLUGGED or current.get("session_id") != session_id:
            if current is not None and current.get("session_id") == session_id and current["ended"]:
                return STALE  # a retried plug-in after the session ended
            current = self._new(device, session_id)
            self.sessions[device] = current
        elif current["ended"]:
            return STALE  # a late retry for a session that has ended
        current["last_event"], current["updated"] = kind, event.get("time")
        if kind == EVENT_PLUGGED:
            current["plugged_at"] = data.get("plugged_at") or event.get("time")
        elif kind == EVENT_ENERGY_REQUEST:
            self._energy_request(current, event, data)
        elif kind == EVENT_SOC_FINAL:
            for key in ("soc_start", "soc_final", "energy_wh", "capacity_measured_wh"):
                current[key] = number(data.get(key))
        else:
            current["ended"] = True
            current["energy_wh"] = number(data.get("energy_wh"))
            for key in ("energy_quality", "mode", "reason"):
                current[key] = data.get(key)
            current["duration_s"] = number(data.get("duration_s"))
        return APPLIED

    def _new(self, device: str, session_id: Any) -> dict[str, Any]:
        return {"device_id": device, "station_id": self.devices.get(device), "session_id": session_id,
                "ended": False, "plugged_at": None, "protocol": None, "energy_need_wh": None,
                "energy_need_source": None, "departure": None, "ev_max_power_w": None,
                "soc": None, "capacity_wh": None, "soc_start": None, "soc_final": None,
                "capacity_measured_wh": None, "energy_wh": None, "energy_quality": None,
                "mode": None, "reason": None, "duration_s": None, "last_event": None, "updated": None}

    @staticmethod
    def _energy_request(current: dict[str, Any], event: dict[str, Any], data: dict[str, Any]) -> None:
        current["protocol"] = data.get("protocol")
        current["energy_need_wh"] = number(data.get("energy_need_wh"))
        if current["energy_need_wh"] is None:
            current["energy_need_wh"] = number(data.get("e_amount_wh", data.get("energy_request_wh")))
        current["energy_need_source"] = data.get("energy_need_source")
        current["ev_max_power_w"] = ev_max_power_w(data)
        current["departure"] = departure(event.get("time"), data.get("departure_time_s"))
        current["soc"] = number(data.get("soc"))  # DIN only: ISO 15118-2 reports no SoC
        current["capacity_wh"] = number(data.get("capacity_wh"))

    def active(self, device: str) -> dict[str, Any] | None:
        """The device's session while the car is plugged in, else None."""
        session = self.sessions.get(device)
        return session if session is not None and not session["ended"] else None

    def charge_limits(self) -> dict[str, float]:
        """Station id -> the plugged-in car's maximum charge power (W)."""
        limits: dict[str, float] = {}
        for device, station in self.devices.items():
            session = self.active(device)
            if session is not None and session["ev_max_power_w"] is not None:
                limits[station] = session["ev_max_power_w"]
        return limits


async def async_setup(hass: Any, entry: Any, *, secret: str, webhook_id: str,
                      devices: dict[str, str], on_update: Callable[[], Any] | None = None
                      ) -> ChargetronicSessions:
    """Register the webhook; the caller unregisters it with async_unload."""
    from aiohttp import web
    from homeassistant.components import webhook
    from homeassistant.helpers.dispatcher import async_dispatcher_send

    sessions = ChargetronicSessions(devices)

    async def handle(hass: Any, hook_id: str, request: Any) -> Any:
        body = await request.read()
        headers = request.headers
        if not verify_signature(secret, headers.get("webhook-id"), headers.get("webhook-timestamp"),
                                headers.get("webhook-signature"), body, time.time()):
            _LOGGER.warning("Chargetronic webhook refused: missing or invalid signature")
            return web.Response(status=401)
        try:
            event = json.loads(body)
            result = sessions.apply(event)
        except ValueError as err:
            _LOGGER.warning("Chargetronic webhook refused: %s", err)
            return web.Response(status=400)
        if result == APPLIED:
            device = event["device_id"]
            hass.bus.async_fire(HA_EVENT, {**event, "station_id": sessions.devices.get(device)})
            async_dispatcher_send(hass, signal(entry.entry_id), device)
            if on_update is not None and device in sessions.devices:
                on_update()
        elif result == STALE:
            # Counted but no session change: refresh the connector sensor only.
            async_dispatcher_send(hass, signal(entry.entry_id), None)
        return web.Response(status=200)

    webhook.async_register(hass, "citrine_ha", "Chargetronic", webhook_id, handle,
                           local_only=False, allowed_methods=["POST"])
    return sessions


def async_unload(hass: Any, webhook_id: str) -> None:
    from homeassistant.components import webhook

    webhook.async_unregister(hass, webhook_id)
