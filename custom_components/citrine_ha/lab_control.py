"""Single-attempt control path for one command station. No legacy mutation fallbacks.

The station is a SIM-EV- simulator, or a physical charger when the server reports
real_devices_enabled (citrineos_server 0.6.0 command_real_stations_enabled). Commands
use the station's observed OCPP version: 2.1, or charging-only 2.0.1 or 1.6.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import math
import re
from typing import Any

from .citrine_api import (
    CitrineClient, CitrineApiError, CitrineAuthError,
    CitrineCompatibilityError, CitrineReadOnlyError,
)
from .compatibility import PROFILE_LAB, PROFILE_V2

STATION = "SIM-EV-EVEREST-V2G-01"
# The server names its single command station: a simulator identity, or any OCPP
# identity when the server allows physical stations.
STATION_PATTERN = re.compile(r"SIM-EV-[A-Za-z0-9_-]{1,36}")
REAL_STATION_PATTERN = re.compile(r"[A-Za-z0-9._~*+-]{1,48}")
PROTOCOL = "ocpp2.1"
# Servers before citrineos_server 0.6.0 report only OCPP 2.1.
PROTOCOLS = ("ocpp2.1", "ocpp2.0.1", "ocpp1.6")
# OCPP 1.6 names the remote start and stop messages differently.
PATHS_16 = {
    "start": "/ocpp/1.6/evdriver/remoteStartTransaction",
    "stop": "/ocpp/1.6/evdriver/remoteStopTransaction",
    "profile": "/ocpp/1.6/smartcharging/setChargingProfile",
    "clear": "/ocpp/1.6/smartcharging/clearChargingProfile",
}
PROFILE_ID = 9001
# Lab bounds per period, charging (import) and V2G discharge (export).
# citrineos_server 0.5.3 raised both from 11 kW to 25 kW for a 3-phase 32 A
# AC simulator; older servers still report 11 kW.
MAX_IMPORT_W = 25000
MAX_EXPORT_W = 25000
KNOWN_BOUNDS = ((11000, 11000), (MAX_IMPORT_W, MAX_EXPORT_W))
PATHS = {
    "start": "/ocpp/2.1/evdriver/requestStartTransaction",
    "stop": "/ocpp/2.1/evdriver/requestStopTransaction",
    "profile": "/ocpp/2.1/smartcharging/setChargingProfile",
    "clear": "/ocpp/2.1/smartcharging/clearChargingProfile",
}


def paths_for(protocol):
    """The scoped command routes for an OCPP version, e.g. /ocpp/2.0.1/... for ocpp2.0.1."""
    if protocol == "ocpp1.6":
        return dict(PATHS_16)
    return {k: v.replace("/ocpp/2.1/", f"/ocpp/{protocol.removeprefix('ocpp')}/") for k, v in PATHS.items()}


def number(value, low, high):
    return (isinstance(value, (float, int)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def transaction(value):
    # OCPP identifierString characters: physical chargers use more than the simulators do.
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9*_=:+|@.-]{1,64}", value) is not None


def valid_period(p):
    """A V2G CentralSetpoint period, or an AC charging-only limit period.

    Mirrors the lab server policy: charging-only periods carry a limit and
    optionally numberPhases 1 or 3, with phaseToUse (1-3) only for one phase.
    """
    if not isinstance(p, dict) or not number(p.get("limit"), 0, MAX_IMPORT_W):
        return False
    if p.get("operationMode") == "CentralSetpoint":
        return (not set(p) - {"startPeriod", "limit", "dischargeLimit", "setpoint", "operationMode"}
                and number(p.get("dischargeLimit"), -MAX_EXPORT_W, 0)
                and number(p.get("setpoint"), p["dischargeLimit"], p["limit"]))
    if set(p) - {"startPeriod", "limit", "numberPhases", "phaseToUse"}:
        return False
    phases = p.get("numberPhases")
    if phases is not None and phases not in (1, 3):
        return False
    return "phaseToUse" not in p or (phases == 1 and integer(p["phaseToUse"], 1, 3))


def build_profile(transaction_id, periods, *, duration=240, profile_id=PROFILE_ID,
                  stack_level=1, start_at=None, immediate=False):
    """Produce one bounded absolute TxProfile, never a default/station profile.

    immediate starts the schedule one second ago, so a replacement profile
    leaves no unlimited gap before its first period.
    """
    if (not transaction(transaction_id) or not integer(duration, 1, 600)
            or not integer(profile_id, 1, 2147483647) or not integer(stack_level, 0, 10)
            or not isinstance(periods, list) or not 1 <= len(periods) <= 10
            or not isinstance(immediate, bool) or (immediate and start_at is not None)):
        raise CitrineCompatibilityError("Invalid bounded lab schedule")
    now = datetime.now(UTC)
    if immediate:
        start_at = now - timedelta(seconds=1)
    elif start_at is None:
        start_at = now + timedelta(seconds=5)
    if not isinstance(start_at, datetime) or start_at.tzinfo is None:
        raise CitrineCompatibilityError("An explicit timezone is required")
    if not immediate and not 1 <= (start_at - now).total_seconds() <= 120:
        raise CitrineCompatibilityError("Schedule must start 1 to 120 seconds in the future")
    last = -1
    normalized = []
    for index, p in enumerate(periods):
        if (not valid_period(p)
                or not integer(p.get("startPeriod"), 0, duration - 1)
                or (index == 0 and p["startPeriod"] != 0) or p["startPeriod"] <= last):
            raise CitrineCompatibilityError(
                "Invalid lab period: W bounds, and CentralSetpoint or a charging-only limit")
        last = p["startPeriod"]
        normalized.append(dict(p))
    return {"evseId": 1, "chargingProfile": {
        "id": profile_id, "stackLevel": stack_level,
        "chargingProfilePurpose": "TxProfile", "chargingProfileKind": "Absolute",
        "transactionId": transaction_id,
        "chargingSchedule": [{"id": profile_id, "startSchedule": start_at.isoformat(timespec="milliseconds"),
                              "duration": duration, "chargingRateUnit": "W",
                              "chargingSchedulePeriod": normalized}],
    }}


def to_ocpp16(action, payload):
    """Rewrite a 2.x-shaped lab command as its OCPP 1.6 message.

    1.6 has integer transaction IDs, connectorId for evseId, one schedule per
    profile, no setpoints, and limits in multiples of 0.1 (sent as whole watts).
    """
    def tx_int(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{1,10}", value) or int(value) > 2147483647:
            raise CitrineCompatibilityError("OCPP 1.6 needs an integer transaction ID")
        return int(value)

    if action == "start":
        return {"connectorId": payload["evseId"], "idTag": payload["idToken"]["idToken"]}
    if action == "stop":
        return {"transactionId": tx_int(payload["transactionId"])}
    if action == "clear":
        return {"id": payload["chargingProfileId"]}
    c = payload["chargingProfile"]
    schedule = c["chargingSchedule"][0]
    periods = []
    for p in schedule["chargingSchedulePeriod"]:
        if set(p) - {"startPeriod", "limit", "numberPhases"}:
            raise CitrineCompatibilityError(
                "OCPP 1.6 accepts charging-only periods (limit and numberPhases)")
        periods.append({**p, "limit": round(p["limit"])})
    return {"connectorId": payload["evseId"], "csChargingProfiles": {
        "chargingProfileId": c["id"], "stackLevel": c["stackLevel"],
        "chargingProfilePurpose": c["chargingProfilePurpose"],
        "chargingProfileKind": c["chargingProfileKind"],
        "transactionId": tx_int(c["transactionId"]),
        "chargingSchedule": {k: schedule[k] for k in (
            "startSchedule", "duration", "chargingRateUnit") if k in schedule}
        | {"chargingSchedulePeriod": periods},
    }}


def charge_period(offset, watts, number_phases=None):
    """An AC charging-only limit period."""
    p = {"startPeriod": offset, "limit": watts}
    if number_phases is not None:
        p["numberPhases"] = number_phases
    return p


def power_period(offset, watts):
    # Bounds stay at 11 kW (what older servers accept) and widen only for a
    # setpoint beyond them, up to the 25 kW lab bound.
    valid = number(watts, -MAX_EXPORT_W, MAX_IMPORT_W)
    return {"startPeriod": offset, "limit": max(11000, watts) if valid else 11000,
            "dischargeLimit": min(-11000, watts) if valid else -11000,
            "setpoint": watts, "operationMode": "CentralSetpoint"}


class ScopedLabClient(CitrineClient):
    """Reader credential for discovery; separate command transport and policy."""

    def __init__(self, *args, command_token=None, **kwargs):
        requested_read_only = kwargs.pop("read_only", True)
        kwargs.pop("api_profile", None)
        super().__init__(*args, **kwargs, api_profile=PROFILE_V2, read_only=True)
        self.api_profile = PROFILE_LAB
        self.read_only = requested_read_only is not False
        if self._tenant_id != 1:
            raise CitrineCompatibilityError("The isolated lab is tenant 1 only")
        if not self.read_only and (not command_token or command_token == self._auth_token):
            raise CitrineAuthError("A separate command credential is required")
        self._command_client = CitrineClient(
            self._session, self._base_url, self._tenant_id, command_token,
            self._verify_ssl, self._request_timeout, read_only=self.read_only)
        self._command_lock = asyncio.Lock()
        # Default until the server policy is read; pinned once learned.
        self.station = STATION
        self._station_learned = False
        self.protocols = (PROTOCOL,)
        self.real_devices = False
        self.last_command: dict[str, Any] = {"state": "not_sent"}

    def observe_transaction(self, station):
        """Resolve only observed lifecycle states, never invent profile acceptance."""
        if station.get("id") != self.station or station.get("protocol") not in self.protocols:
            return
        active = station.get("activeTransactionId")
        meter = station.get("lab_meter", {})
        action = self.last_command.get("action")
        if action == "start" and active:
            self.last_command.update(state="transaction_observed", transaction_id=active,
                                     observed_at=datetime.now(UTC).isoformat())
        elif (action == "stop" and not active
              and meter.get("transaction_closed") is True
              and meter.get("meter_transaction_id") == self.last_command.get("transaction_id")):
            self.last_command.update(state="transaction_closure_observed",
                                     observed_at=datetime.now(UTC).isoformat())

    async def _request(self, method, path, **kwargs):
        # Any inherited legacy operation fails before network I/O.
        if method.upper() not in ("GET", "HEAD"):
            raise CitrineReadOnlyError("Use only the explicit scoped lab commands")
        return await super()._request(method, path, **kwargs)

    async def control_policy(self):
        if self.read_only:
            raise CitrineReadOnlyError("Lab controls are disabled")
        p = await self._command_client._request("GET", "/lab/control-policy")
        real = p.get("real_devices_enabled") if isinstance(p, dict) else None
        protocols = p.get("protocols", [PROTOCOL]) if isinstance(p, dict) else None
        by_protocol = p.get("commands_by_protocol") if isinstance(p, dict) else None
        if (not isinstance(p, dict) or p.get("can_control") is not True
                or not isinstance(p.get("station_id"), str)
                or not isinstance(real, bool)
                or not (STATION_PATTERN.fullmatch(p["station_id"])
                        or (real and REAL_STATION_PATTERN.fullmatch(p["station_id"])))
                or not isinstance(protocols, list) or PROTOCOL not in protocols
                or not set(protocols) <= set(PROTOCOLS) or len(set(protocols)) != len(protocols)
                or (by_protocol is not None and (
                    not isinstance(by_protocol, dict) or set(by_protocol) != set(protocols)
                    or any(set(by_protocol[x]) != set(paths_for(x).values()) for x in protocols)))
                or (self._station_learned and p["station_id"] != self.station)
                or p.get("protocol") != PROTOCOL
                or (p.get("max_import_w"), p.get("max_export_w")) not in KNOWN_BOUNDS
                or p.get("max_profile_seconds") != 600 or p.get("max_periods") != 10
                or p.get("automatic_control") is not False
                or p.get("clear_requires_closed_transaction") is not True
                or set(p.get("commands", [])) != set(PATHS.values())):
            raise CitrineCompatibilityError("Server does not match the scoped lab policy")
        # A changed command station needs an integration reload, never a silent retarget.
        self.station = p["station_id"]
        self._station_learned = True
        self.protocols = tuple(protocols)
        self.real_devices = real
        return p

    async def ping(self):
        await super().ping()
        if not self.read_only:
            await self.control_policy()

    async def _dispatch(self, action, payload, *, protocol, station_id):
        if protocol not in self.protocols or station_id != self.station:
            raise CitrineCompatibilityError(
                "The enrolled command station on an observed protocol the server accepts is required")
        if self._command_lock.locked():
            raise CitrineApiError("A lab command is already in progress")
        if action != "stop" and self.last_command.get("state") in ("outcome_unknown", "dispatch_not_confirmed"):
            raise CitrineApiError("Previous outcome unresolved; reconcile before further commands")
        if action == "start" and self.last_command.get("action") == "start" and self.last_command.get("state") == "dispatched_awaiting_station_evidence":
            raise CitrineApiError("Start already dispatched; wait for the transaction instead of retrying")
        # Translate before any state change, so an untranslatable command is refused cleanly.
        body = to_ocpp16(action, payload) if protocol == "ocpp1.6" else payload
        async with self._command_lock:
            await self.control_policy()
            self.last_command = {"action": action, "state": "dispatching",
                                 "dispatched_at": datetime.now(UTC).isoformat(),
                                 "station_id": self.station}
            tx = payload.get("transactionId") or payload.get("chargingProfile", {}).get("transactionId")
            if tx:
                self.last_command["transaction_id"] = tx
            try:
                response = await self._command_client._request(
                    "POST", paths_for(protocol)[action],
                    params={"identifier": self.station, "tenantId": 1}, json=body)
            except CitrineApiError as err:
                status = getattr(err, "status", None)
                # A 4xx is refused before dispatch. A 500 on a profile is safe to follow with the
                # next profile: it has the same id and stack level, so it replaces whatever was
                # applied. Timeouts, transport errors and 502-504 may follow dispatch.
                if status is not None and (400 <= status < 500 or (status == 500 and action == "profile")):
                    self.last_command.update(state="rejected", http_status=status,
                                             detail="Server refused the command; no automatic retry.")
                    raise
                self.last_command.update(state="outcome_unknown",
                                         detail="Reconcile the station before another command; no automatic retry.")
                raise
            # Native API confirms message dispatch, not the EVSE's CALLRESULT.
            if (not isinstance(response, list) or len(response) != 1
                    or not isinstance(response[0], dict) or response[0].get("success") is not True):
                self.last_command.update(state="dispatch_not_confirmed")
                raise CitrineApiError("Command dispatch not confirmed; inspect station before retry")
            self.last_command.update(state="dispatched_awaiting_station_evidence",
                                     dispatch_response_at=datetime.now(UTC).isoformat())
            return deepcopy(self.last_command)

    async def request_start_transaction(self, *, protocol, station_id, id_tag="C17E0001",
                                        evse_id=1, remote_start_id=None):
        if evse_id != 1 or id_tag != "C17E0001":
            raise CitrineCompatibilityError("Only lab EVSE 1 and synthetic token C17E0001 are permitted")
        if remote_start_id is None:
            remote_start_id = int(datetime.now(UTC).timestamp()) % 2147483647
        if not integer(remote_start_id, 0, 2147483647):
            raise CitrineCompatibilityError("Invalid remote start ID")
        return await self._dispatch("start", {
            "remoteStartId": remote_start_id, "evseId": 1,
            "idToken": {"idToken": id_tag, "type": "ISO14443"}},
            protocol=protocol, station_id=station_id)

    async def request_stop_transaction(self, *, protocol, station_id, transaction_id):
        if not transaction(transaction_id):
            raise CitrineCompatibilityError("An exact transaction ID is required")
        return await self._dispatch("stop", {"transactionId": transaction_id},
                                    protocol=protocol, station_id=station_id)

    async def submit_schedule(self, *, protocol, station_id, transaction_id, periods,
                              duration=240, profile_id=PROFILE_ID, stack_level=1, start_at=None,
                              immediate=False):
        if protocol in ("ocpp2.0.1", "ocpp1.6") and any(
                isinstance(x, dict) and {"operationMode", "setpoint", "dischargeLimit"} & set(x) for x in periods or []):
            raise CitrineCompatibilityError(
                f"OCPP {protocol.removeprefix('ocpp')} accepts charging-only periods (no setpoint or discharge)")
        payload = build_profile(transaction_id, periods, duration=duration, profile_id=profile_id,
                                stack_level=stack_level, start_at=start_at, immediate=immediate)
        return await self._dispatch("profile", payload, protocol=protocol, station_id=station_id)

    async def set_charging_profile(self, *, protocol, station_id, limit, setpoint=None,
                                    discharge_limit=None, operation_mode=None, unit="W",
                                    evse_id=1, duration=180, stack_level=1, profile_id=PROFILE_ID,
                                    profile_purpose="TxProfile", profile_kind="Absolute",
                                    profile_periods=None, transaction_id=None,
                                    txprofile_compatibility_fallback=False):
        if (unit != "W" or evse_id != 1 or profile_purpose != "TxProfile"
                or profile_kind != "Absolute" or txprofile_compatibility_fallback
                or operation_mode != "CentralSetpoint"):
            raise CitrineCompatibilityError("Only exact absolute W TxProfiles without fallback are permitted")
        periods = profile_periods if profile_periods is not None else [{
            "startPeriod": 0, "limit": limit, "dischargeLimit": discharge_limit,
            "setpoint": setpoint, "operationMode": operation_mode}]
        return await self.submit_schedule(
            protocol=protocol, station_id=station_id, transaction_id=transaction_id,
            periods=periods, duration=duration, profile_id=profile_id, stack_level=stack_level)

    async def clear_charging_profile(self, *, protocol, station_id, profile_id=None, **kwargs):
        if kwargs or not integer(profile_id, 1, 2147483647):
            raise CitrineCompatibilityError("Clear requires only an exact profile ID after transaction closure")
        return await self._dispatch("clear", {"chargingProfileId": profile_id},
                                    protocol=protocol, station_id=station_id)

    async def set_station_limit(self, **kwargs):
        raise CitrineCompatibilityError("Station-wide limits are not enabled in the lab profile")

    async def set_group_limit(self, **kwargs):
        raise CitrineCompatibilityError("Group control is not enabled in the lab profile")
