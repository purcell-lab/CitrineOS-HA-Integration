"""Small, explicit manual control surface for the single enrolled command station."""
from datetime import UTC, timedelta
import secrets
from types import SimpleNamespace

import voluptuous as vol
from homeassistant.components.button import ButtonEntity
from homeassistant.core import SupportsResponse
from homeassistant.components.number import NumberEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import UnitOfPower
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .citrine_api import CitrineApiError
from .lab_control import (BASE_MODES, DER_CONTROL_ID, DER_REQUEST_ID_MAX, DER_TYPES, DER_UNITS,
                          LOCAL_MAX_DURATION_S, LOCAL_MODES, LOCAL_PROFILE_ID, LOCAL_PURPOSE, PROFILE_ID,
                          PROTOCOL, MAX_EXPORT_W, MAX_IMPORT_W, STATION_PATTERN, ScopedLabClient,
                          charging_limit_now, local_modes, power_period, number)
from .power_limits import CHARGE, DISCHARGE, sensor_state
from .pricing import (TOTAL_COST_MAX, amber_intervals, build_cost_updated, build_price_schedule,
                      build_tariff, current_and_next, openadr_forecasts, schedule_summary,
                      tariff_summary)

SERVICE = "set_tx_profile"
SERVICE_RECONCILE = "reconcile_command_state"
SERVICE_COST = "cost_updated"
SERVICE_TARIFF = "set_default_tariff"
SERVICE_DER_SET = "set_der_control"
SERVICE_DER_GET = "get_der_control"
SERVICE_DER_CLEAR = "clear_der_control"
SERVICE_DER_EVENTS = "get_der_events"
SERVICE_LOCAL_SET = "set_local_generation_profile"
SERVICE_LOCAL_CLEAR = "clear_local_generation_profile"
SERVICE_LIMITS = "get_charging_limits"
# Service field -> OCPP key: the fields each controlType's control carries.
DER_COMMON = {"priority": "priority", "start_time": "startTime", "duration": "duration"}
DER_FIELDS = {
    "LimitMaxDischarge": {"pct_max_discharge_power": "pctMaxDischargePower"},
    "FixedPFInject": {"displacement": "displacement", "excitation": "excitation"},
    "FixedPFAbsorb": {"displacement": "displacement", "excitation": "excitation"},
    "FixedVar": {"setpoint": "setpoint", "unit": "unit"},
}
DER_ALL_FIELDS = {f for fields in DER_FIELDS.values() for f in fields}


def station(data, station_id):
    return next((s for s in (data or {}).get("stations", []) if s.get("id") == station_id), {})


def time_zone(hass):
    """HA's configured time zone: tariff times of day and price text are local."""
    return dt_util.get_time_zone(hass.config.time_zone) or UTC


OPENADR_DOMAIN = "openadr3_ven"
# Prices sent to a station cover the next 24 h: the price schedule in a charging
# profile (a 48 h one made the profile over 13 KiB) and the OpenADR forecast read.
PRICE_HORIZON = timedelta(hours=24)
OPENADR_HORIZON = PRICE_HORIZON


async def price_state(hass, entity_id):
    """A price sensor as a State-like object with Amber-shaped forecast attributes.

    Amber sensors carry their forecast in attributes. OpenADR 3 VEN sensors (the
    openadr3_ven integration) return theirs from its get_forecast service instead, so
    the next 24 h are fetched and converted; the builders treat both alike.
    """
    state = hass.states.get(entity_id)
    if state is None:
        raise ServiceValidationError(f"Price entity {entity_id} not found")
    if ("payload_type" not in (state.attributes or {})
            or not hass.services.has_service(OPENADR_DOMAIN, "get_forecast")):
        return state
    now = dt_util.utcnow()
    try:
        response = await hass.services.async_call(
            OPENADR_DOMAIN, "get_forecast",
            {"start": now - timedelta(hours=1), "end": now + OPENADR_HORIZON},
            target={"entity_id": entity_id}, blocking=True, return_response=True)
    except HomeAssistantError as err:
        raise ServiceValidationError(f"OpenADR forecast for {entity_id} unavailable: {err}") from err
    rows = ((response or {}).get(entity_id) or {}).get("forecast")
    return SimpleNamespace(entity_id=entity_id, state=state.state, source="OpenADR",
                           attributes={**state.attributes, "forecasts": openadr_forecasts(rows)})


def source(state):
    """The label shown with the price: "OpenADR" for VEN sensors, else "Amber"."""
    return getattr(state, "source", "Amber")


def forecast(state):
    intervals = amber_intervals(state)
    if not intervals:
        raise ServiceValidationError(
            f"{state.entity_id} has no current or future price interval (an OpenADR 3 VEN "
            "price sensor, or Amber attributes forecasts[] or detailedForecast[])")
    return intervals


def require_prices(client):
    prices = client.price_commands
    if not prices.get("enabled"):
        raise ServiceValidationError(
            "Price commands are disabled: the CitrineOS server policy reports no enabled "
            "price_commands (citrineos_server 0.7.0 price_commands_enabled)"
            + ("; its price_commands policy is malformed" if prices.get("invalid") else ""))
    return prices


async def fresh(ctx, sid=None):
    """Refresh discovery and return the client and a station's fresh record."""
    c = ctx["coordinator"]
    client = ctx["client"]
    if not isinstance(client, ScopedLabClient) or client.read_only:
        raise HomeAssistantError("Scoped lab controls are not enabled")
    await c.async_request_refresh()
    if not client.csms_available:
        raise HomeAssistantError(
            f"CSMS unavailable ({client.csms_unavailable_reason}); lab commands are blocked "
            "until /health/ready passes")
    sid = sid or client.station
    return client, station(c.data, sid) if c.last_update_success else {}


def result(client, sent):
    """Service response: what was sent and the dispatch or station status."""
    lc = client.last_command
    return {"sent": sent, "station_id": lc.get("station_id"), "command_state": lc.get("state"),
            "station_status": lc.get("station_status"), "station_response": lc.get("station_response"),
            "command": dict(lc)}


async def command(ctx, action, *, periods=None, duration=180, immediate=False, price=None):
    c = ctx["coordinator"]
    client, s = await fresh(ctx)
    sid = client.station
    protocol = s.get("protocol")
    if not c.last_update_success or not s.get("isOnline") or protocol not in client.protocols:
        raise HomeAssistantError(
            f"Fresh online evidence on {' or '.join(client.protocols)} is required for {sid}")
    tx = s.get("activeTransactionId")
    sent = None
    try:
        if periods is not None:
            # The fresh policy decides which bounded local modes a period may use.
            check_local_periods(periods, client.local_energy)
        if action == "start":
            if tx:
                raise HomeAssistantError("The simulator already has an active transaction")
            await client.request_start_transaction(protocol=protocol, station_id=sid)
        elif action == "stop":
            if not tx:
                raise HomeAssistantError("No active transaction to stop")
            await client.request_stop_transaction(protocol=protocol, station_id=sid, transaction_id=tx)
        elif action == "clear":
            if tx:
                raise HomeAssistantError("Stop and verify transaction closure before profile cleanup")
            await client.clear_charging_profile(protocol=protocol, station_id=sid, profile_id=PROFILE_ID)
        else:
            if not tx:
                raise HomeAssistantError("A current transaction is required")
            watts = 0 if action == "zero" else ctx.get("lab_requested_w", 0)
            schedule = None
            if price is not None:
                prices = require_prices(client)
                if protocol != PROTOCOL:
                    raise ServiceValidationError(
                        f"A price schedule needs OCPP 2.1; {sid} is on {protocol}")
                limits = prices["price_schedule"]
                schedule = build_price_schedule(
                    forecast(price), currency=prices["currency"],
                    max_rule_stacks=limits["max_rule_stacks"],
                    max_horizon_s=min(limits["max_horizon_s"], int(PRICE_HORIZON.total_seconds())),
                    label=source(price))
                sent = {"price_schedule": schedule_summary(schedule)}
            await client.submit_schedule(
                protocol=protocol, station_id=sid, transaction_id=tx,
                periods=periods if periods is not None else [power_period(0, watts)],
                duration=duration, immediate=immediate, price_schedule=schedule)
            sent = {"transaction_id": tx, "periods": len(periods or [None]), "duration": duration,
                    **(sent or {})}
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()
    return result(client, sent)


def check_local_periods(periods, local):
    """ExternalLimits, ExternalSetpoint and LocalLoadBalancing periods, checked before sending.

    Each needs the server's local_energy policy to list the mode, a limit
    (0 to 25 kW) and a dischargeLimit (-25 kW to 0), and no setpoint. Every
    other period is left to the existing CentralSetpoint and charging-only checks.
    """
    offered = local_modes(local)
    for index, p in enumerate(periods if isinstance(periods, list) else []):
        mode = p.get("operationMode") if isinstance(p, dict) else None
        if mode not in LOCAL_MODES:
            continue
        where = f"Period {index} ({mode})"
        if mode not in offered:
            raise ServiceValidationError(
                f"{where}: {mode} is not offered by the server policy (offered: "
                f"{', '.join((*BASE_MODES, *offered))})"
                + ("; its local_energy policy is malformed" if local.get("invalid") else ""))
        if "setpoint" in p:
            raise ServiceValidationError(
                f"{where}: no setpoint is allowed; the station or a local EMS chooses the power "
                "within limit and dischargeLimit")
        missing = [k for k in ("limit", "dischargeLimit") if p.get(k) is None]
        if missing:
            raise ServiceValidationError(f"{where}: {' and '.join(missing)} required")
        extra = sorted(set(p) - {"startPeriod", "limit", "dischargeLimit", "operationMode"})
        if extra:
            raise ServiceValidationError(
                f"{where}: takes only startPeriod, limit, dischargeLimit and operationMode "
                f"(not {', '.join(extra)})")
        if not number(p["limit"], 0, MAX_IMPORT_W):
            raise ServiceValidationError(f"{where}: limit must be 0 to {MAX_IMPORT_W} W")
        if not number(p["dischargeLimit"], -MAX_EXPORT_W, 0):
            raise ServiceValidationError(f"{where}: dischargeLimit must be -{MAX_EXPORT_W} to 0 W")


async def cost_update(ctx, state, *, total_cost=0, tz=UTC):
    """CostUpdated with the current and next Amber price for the active transaction."""
    c = ctx["coordinator"]
    client, s = await fresh(ctx)
    try:
        prices = require_prices(client)
        sid, protocol = client.station, s.get("protocol")
        if not s.get("isOnline") or protocol not in client.protocols:
            raise HomeAssistantError(f"Fresh online evidence is required for {sid}")
        if protocol not in prices["cost_updated_by_protocol"]:
            raise ServiceValidationError(
                f"CostUpdated is OCPP 2.1 and 2.0.1 only; {sid} is on {protocol}")
        tx = s.get("activeTransactionId")
        if not tx:
            raise ServiceValidationError(f"CostUpdated needs an active transaction on {sid}")
        current, nxt = current_and_next(forecast(state))
        if current is None:
            raise ServiceValidationError(
                f"{state.entity_id} has no price for the current interval (the core Amber forecast "
                "sensor lists future intervals only: use its price sensor or amber_express)")
        body = build_cost_updated(tx, current, nxt, tz, total_cost=total_cost, label=source(state))
        await client.cost_updated(protocol=protocol, station_id=sid, body=body)
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()
    custom = body["customData"]
    return result(client, {
        "transaction_id": tx, "total_cost": body["totalCost"], "protocol": protocol,
        "kwh_price": custom["chargingPrice"]["kWhPrice"],
        "next_period": custom.get("nextPeriod"), "price_text": custom["priceText"]})


async def default_tariff(ctx, state, *, export_state=None, station_id=None, evse_id=0,
                         intervals=48, tz=UTC):
    """SetDefaultTariff with Amber prices to the command station or a listed tariff station."""
    c = ctx["coordinator"]
    client, s = await fresh(ctx, station_id)
    try:
        prices = require_prices(client)
        sid = station_id or client.station
        if sid not in prices["tariff_stations"]:
            raise ServiceValidationError(
                f"{sid} is not a tariff station in the server policy "
                f"({', '.join(prices['tariff_stations']) or 'none listed'})")
        if not prices.get("default_tariff"):
            raise ServiceValidationError("The server policy offers no SetDefaultTariff route")
        protocol = s.get("protocol")
        if not s.get("isOnline"):
            raise HomeAssistantError(f"Fresh online evidence is required for {sid}")
        if protocol != PROTOCOL:
            raise ServiceValidationError(f"SetDefaultTariff needs OCPP 2.1; {sid} is on {protocol}")
        tariff = build_tariff(forecast(state), tz, label=source(state),
                              export_intervals=forecast(export_state) if export_state is not None else (),
                              count=intervals, currency=prices["currency"], limits=prices["tariff_limits"])
        await client.set_default_tariff(protocol=protocol, station_id=sid, evse_id=evse_id, tariff=tariff)
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()
    return result(client, {"station_id": sid, **tariff_summary({"evseId": evse_id, "tariff": tariff})})


def require_der(client):
    der = client.der_commands
    if not der.get("enabled"):
        raise ServiceValidationError(
            "DER controls are disabled: the CitrineOS server policy reports no enabled "
            "der_commands (CitrineOS Isolated Lab App 0.10.0 der_commands_enabled)"
            + ("; its der_commands policy is malformed" if der.get("invalid") else ""))
    return der


def der_set_body(data, der):
    """SetDERControl with exactly the one control field matching control_type.

    Missing or extra fields for the type, or values outside the server's DER
    limits, are refused before anything is sent.
    """
    ctype = data["control_type"]
    if ctype not in der["control_types"]:
        raise ServiceValidationError(
            f"{ctype} is not offered by the server DER policy ({', '.join(der['control_types'])})")
    fields = DER_FIELDS[ctype]
    missing = sorted(f for f in fields if data.get(f) is None)
    extra = sorted(f for f in DER_ALL_FIELDS - set(fields) if data.get(f) is not None)
    if missing or extra:
        problems = ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"not used by {ctype}: {', '.join(extra)}"] if extra else [])
        raise ServiceValidationError(
            f"{'; '.join(problems)} ({ctype} takes {', '.join(sorted(fields))})")
    limits = der["limits"]
    if ctype == "LimitMaxDischarge":
        low, high = limits["pct_max_discharge_power"]
        if not number(data["pct_max_discharge_power"], low, high):
            raise ServiceValidationError(f"pct_max_discharge_power must be {low} to {high} %")
    elif ctype == "FixedVar":
        low, high = limits["var_setpoint_pct"]
        if not number(data["setpoint"], low, high):
            raise ServiceValidationError(f"setpoint must be {low} to {high} %")
    else:
        low, high = limits["displacement"]
        if not number(data["displacement"], low, high) or data["displacement"] <= low:
            raise ServiceValidationError(f"displacement must be above {low} and at most {high}")
    if data.get("duration") is not None and data["duration"] > limits["max_duration_s"]:
        raise ServiceValidationError(f"duration must be 1 to {limits['max_duration_s']} s")
    control = {"priority": data["priority"]}
    for field, key in fields.items():
        control[key] = data[field]
    if data.get("start_time") is not None:
        # A time without a zone is HA local time; sent as UTC.
        control["startTime"] = dt_util.as_utc(data["start_time"]).isoformat()
    if data.get("duration") is not None:
        control["duration"] = data["duration"]
    return {"isDefault": data["is_default"], "controlId": data["control_id"], "controlType": ctype,
            DER_TYPES[ctype]: control}


def der_selection(data, der, body):
    """Optional isDefault, controlType and controlId of a get or clear."""
    if data.get("control_type") is not None:
        if data["control_type"] not in der["control_types"]:
            raise ServiceValidationError(
                f"{data['control_type']} is not offered by the server DER policy "
                f"({', '.join(der['control_types'])})")
        body["controlType"] = data["control_type"]
    if data.get("control_id") is not None:
        body["controlId"] = data["control_id"]
    return body


def der_status(response):
    """The station's status, or "CALLERROR <code>" for CitrineOS's CALLERROR object."""
    if not isinstance(response, dict):
        return None
    if isinstance(response.get("_errorCode"), str):
        return f"CALLERROR {response['_errorCode']}"
    return response.get("status") if isinstance(response.get("status"), str) else None


async def der_command(ctx, action, data=None):
    """One DER service: validate, send on the policy's route, return the gateway reply."""
    c = ctx["coordinator"]
    client, s = await fresh(ctx)
    try:
        der = require_der(client)
        sid = der["station_id"]
        if action != "events":
            # The gateway also answers 409; refusing here keeps an offline station off the wire.
            if not c.last_update_success or not s.get("isOnline"):
                raise HomeAssistantError(f"Fresh online evidence is required for {sid}")
            if s.get("protocol") != PROTOCOL:
                raise ServiceValidationError(f"DER control needs OCPP 2.1; {sid} is on {s.get('protocol')}")
        if action == "set":
            body = der_set_body(data, der)
            response = await client.set_der_control(body)
        elif action == "get":
            request_id = data.get("request_id")
            body = {"requestId": secrets.randbelow(DER_REQUEST_ID_MAX + 1) if request_id is None else request_id}
            if data.get("is_default") is not None:
                body["isDefault"] = data["is_default"]
            body = der_selection(data, der, body)
            response = await client.get_der_control(body)
        elif action == "clear":
            body = der_selection(data, der, {"isDefault": data["is_default"]})
            response = await client.clear_der_control(body)
        else:
            return await client.der_events()
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()
    return {"sent": body, "station_id": sid, "station_status": der_status(response),
            "station_response": response}


def require_local(client, part):
    local = client.local_energy
    if not local.get("enabled") or not local.get(part):
        raise ServiceValidationError(
            f"Local energy {part} is not offered: the CitrineOS server policy has no local_energy."
            f"{part} (CitrineOS Isolated Lab App 0.11.0)"
            + ("; its local_energy policy is malformed" if local.get("invalid") else ""))
    return local


def watts(value):
    """A limit as a whole number of W when it is one."""
    return int(value) if float(value).is_integer() else value


def local_generation_body(data, lg):
    """SetChargingProfile with one LocalGeneration schedule, inside the policy's local_generation.

    start_time given: Absolute from that time (sent as UTC); absent: Relative.
    """
    evse_id, profile_id, duration = data["evse_id"], data["profile_id"], data["duration"]
    low, high = lg["ids"]
    if evse_id not in lg["evse_ids"]:
        raise ServiceValidationError(
            f"evse_id must be one of {', '.join(map(str, lg['evse_ids']))} (server local_generation policy)")
    if not low <= profile_id <= high:
        raise ServiceValidationError(f"profile_id must be {low} to {high} (server local_generation policy)")
    if not 1 <= duration <= lg["max_duration_s"]:
        raise ServiceValidationError(f"duration must be 1 to {lg['max_duration_s']} s")
    periods = data.get("periods") or []
    if not 1 <= len(periods) <= lg["max_periods"]:
        raise ServiceValidationError(f"periods must hold 1 to {lg['max_periods']} periods")
    out, last = [], -1
    for index, p in enumerate(periods):
        start = p["start_period"]
        if index == 0 and start != 0:
            raise ServiceValidationError("The first period must have start_period 0")
        if start <= last:
            raise ServiceValidationError(
                f"Period {index}: start_period {start} must be after the previous ({last})")
        if start >= duration:
            raise ServiceValidationError(f"Period {index}: start_period {start} must be below duration {duration}")
        if not number(p["limit"], 0, MAX_IMPORT_W):
            raise ServiceValidationError(f"Period {index}: limit must be 0 to {MAX_IMPORT_W} W")
        period = {"startPeriod": start, "limit": watts(p["limit"])}
        if p.get("number_phases") is not None:
            period["numberPhases"] = p["number_phases"]
        out.append(period)
        last = start
    schedule = {"id": profile_id}
    absolute = data.get("start_time") is not None
    if absolute:
        # A time without a zone is HA local time; sent as UTC.
        schedule["startSchedule"] = dt_util.as_utc(data["start_time"]).isoformat()
    schedule.update(duration=duration, chargingRateUnit="W", chargingSchedulePeriod=out)
    return {"evseId": evse_id, "chargingProfile": {
        "id": profile_id, "stackLevel": data["stack_level"], "chargingProfilePurpose": LOCAL_PURPOSE,
        "chargingProfileKind": "Absolute" if absolute else "Relative", "chargingSchedule": [schedule]}}


def send_status(response):
    """The station's status or CALLERROR, else "dispatched" for CitrineOS's send confirmation."""
    status = der_status(response)
    if status is None and isinstance(response, list):
        # The native API confirms the message was sent, not the station's CALLRESULT.
        return "dispatched"
    return status


async def local_command(ctx, action, data=None):
    """One local energy service: validate, send to the command station, return the gateway reply."""
    c = ctx["coordinator"]
    client, s = await fresh(ctx)
    sid = client.station
    try:
        if action == "limits":
            require_local(client, "charging_limits")
            return await client.get_charging_limits()
        lg = require_local(client, "local_generation")["local_generation"]
        # The gateway also answers 409; refusing here keeps an offline station off the wire.
        if not c.last_update_success or not s.get("isOnline"):
            raise HomeAssistantError(f"Fresh online evidence is required for {sid}")
        if s.get("protocol") != PROTOCOL:
            raise ServiceValidationError(f"{LOCAL_PURPOSE} profiles need OCPP 2.1; {sid} is on {s.get('protocol')}")
        if action == "set":
            body = local_generation_body(data, lg)
            response = await client.set_local_generation_profile(body)
        else:
            low, high = lg["ids"]
            if not low <= data["profile_id"] <= high:
                raise ServiceValidationError(f"profile_id must be {low} to {high} (server local_generation policy)")
            body = {"chargingProfileId": data["profile_id"]}
            response = await client.clear_local_generation_profile(data["profile_id"])
        # The station may now report a changed limit.
        await client.poll_charging_limits(force=True)
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()
    return {"sent": body, "station_id": sid, "station_status": send_status(response),
            "station_response": response}


async def reconcile(ctx):
    """Re-read the CSMS now and resolve an unresolved or awaiting command outcome."""
    c = ctx["coordinator"]
    client = ctx["client"]
    if not isinstance(client, ScopedLabClient) or client.read_only:
        raise HomeAssistantError("Scoped lab controls are not enabled")
    # Not the debounced request: reconcile needs state read after the call.
    await c.async_refresh()
    try:
        client.require_csms()
        s = station(c.data, client.station)
        if not c.last_update_success or not s:
            raise HomeAssistantError(f"Fresh CSMS evidence for {client.station} is required to reconcile")
        return await client.reconcile(s)
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()


class LabEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, ctx, entry, key, name, sid=None):
        super().__init__(ctx["coordinator"])
        self.ctx = ctx
        command_station = sid is None or sid == ctx["client"].station
        sid = sid or ctx["client"].station
        self._station_id = sid
        self._attr_unique_id = f"{entry.entry_id}_{sid}_lab_{key}"
        self._attr_name = name
        simulator = STATION_PATTERN.fullmatch(sid) is not None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"1:{sid}")},
            "name": f"CitrineOS EVerest V2G lab {sid}" if simulator else f"CitrineOS lab {sid}",
            "model": ("Isolated simulator" if simulator else "Physical charger (lab command station)"
                      if command_station else "Physical charger (read only)"),
            **({"manufacturer": "EVerest"} if simulator else {}),
        }

    @property
    def available(self):
        s = station(self.coordinator.data, self._station_id)
        return (super().available and bool(s.get("isOnline"))
                and s.get("protocol") in self.ctx["client"].protocols)


class LabButton(LabEntity, ButtonEntity):
    def __init__(self, ctx, entry, action, name):
        super().__init__(ctx, entry, action, name)
        self.action = action

    @property
    def available(self):
        if self.action == "reconcile":
            # Reconcile reads the CSMS record; it does not need the station online.
            return (CoordinatorEntity.available.fget(self) and not self.ctx["client"].read_only
                    and bool(station(self.coordinator.data, self._station_id)))
        if not super().available or self.ctx["client"].read_only:
            return False
        tx = station(self.coordinator.data, self._station_id).get("activeTransactionId")
        return not tx if self.action in ("start", "clear") else bool(tx)

    async def async_press(self):
        if self.action == "reconcile":
            await reconcile(self.ctx)
            return
        await command(self.ctx, self.action)


class LabPower(LabEntity, NumberEntity):
    _attr_native_min_value = -MAX_EXPORT_W
    _attr_native_max_value = MAX_IMPORT_W
    _attr_native_step = 100
    _attr_native_unit_of_measurement = "W"
    _attr_mode = "box"

    def __init__(self, ctx, entry):
        super().__init__(ctx, entry, "requested_power", "Requested power (apply separately)")

    @property
    def native_value(self):
        return self.ctx.get("lab_requested_w", 0)

    async def async_set_native_value(self, value):
        if not number(value, -MAX_EXPORT_W, MAX_IMPORT_W):
            raise HomeAssistantError(f"Requested power must be within ±{MAX_IMPORT_W:,} W")
        self.ctx["lab_requested_w"] = value
        self.async_write_ha_state()


class LabSensor(LabEntity, SensorEntity):
    def __init__(self, ctx, entry, key, name, unit=None):
        super().__init__(ctx, entry, key, name)
        self.key = key
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self):
        s = station(self.coordinator.data, self._station_id)
        if self.key == "command":
            return self.ctx["client"].last_command["state"]
        if self.key == "transaction":
            return s.get("activeTransactionId")
        if self.key == "protocol":
            return s.get("protocol")
        if self.key == "status":
            return ", ".join(str(c.get("status", "unknown")) for c in s.get("connectors", [])) or "unknown"
        return s.get("lab_meter", {}).get(self.key)

    @property
    def extra_state_attributes(self):
        if self.key == "command":
            client = self.ctx["client"]
            return {**client.last_command, "csms_available": client.csms_available,
                    "csms_unavailable_reason": client.csms_unavailable_reason,
                    "csms_unavailable_since": client.csms_unavailable_since,
                    "evidence_timeout_s": client.evidence_timeout}
        return {k: v for k, v in station(self.coordinator.data, self._station_id).get("lab_meter", {}).items()
                if k not in ("power_w", "import_kwh", "export_kwh")}


class LabMaxPowerSensor(LabEntity, SensorEntity):
    """Usable charge or discharge power of any discovered station (read only)."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, ctx, entry, sid, direction):
        super().__init__(ctx, entry, f"max_{direction}_power", f"Max {direction} power", sid)
        self.direction = direction
        self._attr_translation_key = f"max_{direction}_power"
        self._attr_icon = "mdi:transmission-tower-export" if direction == DISCHARGE else "mdi:ev-plug-ccs2"

    @property
    def available(self):
        # A cap, not a measurement: keep the fallback while the station is offline.
        return CoordinatorEntity.available.fget(self)

    @property
    def native_value(self):
        return sensor_state(station(self.coordinator.data, self._station_id), self.direction,
                            self.coordinator.default_max_power_w)[0]

    @property
    def extra_state_attributes(self):
        return sensor_state(station(self.coordinator.data, self._station_id), self.direction,
                            self.coordinator.default_max_power_w)[1]


class LabChargingLimitSensor(LabEntity, SensorEntity):
    """The EMS or other external charging limit in effect now on the command station."""

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_translation_key = "ems_charging_limit"
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, ctx, entry):
        super().__init__(ctx, entry, "ems_charging_limit", "EMS charging limit")

    @property
    def available(self):
        # A limit read from the gateway, not a station measurement: offline stations keep it.
        return CoordinatorEntity.available.fget(self) and self.ctx["client"].charging_limits is not None

    @property
    def native_value(self):
        return charging_limit_now(self.ctx["client"].charging_limits)[0]

    @property
    def extra_state_attributes(self):
        return charging_limit_now(self.ctx["client"].charging_limits)[1]


def max_power_sensors(ctx, entry, known):
    """Max discharge and charge power sensors for stations not yet known."""
    entities = []
    for s in (ctx["coordinator"].data or {}).get("stations", []):
        sid = s.get("id")
        if not isinstance(sid, str) or not sid or sid in known:
            continue
        known.add(sid)
        entities += [LabMaxPowerSensor(ctx, entry, sid, DISCHARGE), LabMaxPowerSensor(ctx, entry, sid, CHARGE)]
    return entities


async def setup_lab(hass, entry, add_entities, platform):
    ctx = hass.data[DOMAIN][entry.entry_id]
    if platform == "button":
        add_entities([LabButton(ctx, entry, action, name) for action, name in (
            ("start", "Start charging"), ("stop", "Stop transaction"),
            ("apply", "Apply requested power"), ("zero", "Request zero power"),
            ("clear", "Clear test profile after closure"),
            ("reconcile", "Reconcile command state"))])
        def target(call):
            ctx = hass.data[DOMAIN].get(call.data["entry_id"])
            if not ctx:
                raise HomeAssistantError("Unknown CitrineOS entry")
            return ctx

        if not hass.services.has_service(DOMAIN, SERVICE):
            async def submit(call):
                price = call.data.get("price_entity")
                return await command(
                    target(call), "schedule", periods=call.data["periods"], duration=call.data["duration"],
                    immediate=call.data["immediate"],
                    price=await price_state(hass, price) if price else None)
            hass.services.async_register(DOMAIN, SERVICE, submit, schema=vol.Schema({
                vol.Required("entry_id"): str,
                vol.Required("periods"): [dict],
                vol.Optional("duration", default=240): vol.All(int, vol.Range(min=1, max=600)),
                vol.Optional("immediate", default=False): bool,
                vol.Optional("price_entity"): cv.entity_id,
            }), supports_response=SupportsResponse.OPTIONAL)
        if not hass.services.has_service(DOMAIN, SERVICE_COST):
            async def submit_cost(call):
                ctx = target(call)
                return await cost_update(ctx, await price_state(hass, call.data["price_entity"]),
                                         total_cost=call.data["total_cost"], tz=time_zone(hass))
            hass.services.async_register(DOMAIN, SERVICE_COST, submit_cost, schema=vol.Schema({
                vol.Required("entry_id"): str,
                vol.Required("price_entity"): cv.entity_id,
                vol.Optional("total_cost", default=0): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=TOTAL_COST_MAX)),
            }), supports_response=SupportsResponse.OPTIONAL)
        if not hass.services.has_service(DOMAIN, SERVICE_TARIFF):
            async def submit_tariff(call):
                ctx = target(call)
                export = call.data.get("export_price_entity")
                return await default_tariff(
                    ctx, await price_state(hass, call.data["price_entity"]),
                    export_state=await price_state(hass, export) if export else None,
                    station_id=call.data.get("station_id"), evse_id=call.data["evse_id"],
                    intervals=call.data["intervals"], tz=time_zone(hass))
            hass.services.async_register(DOMAIN, SERVICE_TARIFF, submit_tariff, schema=vol.Schema({
                vol.Required("entry_id"): str,
                vol.Required("price_entity"): cv.entity_id,
                vol.Optional("export_price_entity"): cv.entity_id,
                vol.Optional("station_id"): vol.All(str, vol.Length(min=1, max=48)),
                vol.Optional("evse_id", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
                vol.Optional("intervals", default=48): vol.All(vol.Coerce(int), vol.Range(min=1, max=288)),
            }), supports_response=SupportsResponse.OPTIONAL)
        der_types = vol.In(list(DER_TYPES))
        control_id = vol.All(str, vol.Match(f"^{DER_CONTROL_ID.pattern}$"))
        der_services = {
            SERVICE_DER_SET: ("set", {
                vol.Required("control_type"): der_types,
                vol.Required("control_id"): control_id,
                vol.Optional("is_default", default=False): cv.boolean,
                vol.Optional("priority", default=1): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=2147483647)),
                vol.Optional("pct_max_discharge_power"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=100)),
                vol.Optional("displacement"): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=1, min_included=False)),
                vol.Optional("excitation"): cv.boolean,
                vol.Optional("setpoint"): vol.All(vol.Coerce(float), vol.Range(min=-100, max=100)),
                vol.Optional("unit"): vol.In(list(DER_UNITS)),
                vol.Optional("start_time"): cv.datetime,
                vol.Optional("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=86400)),
            }),
            SERVICE_DER_GET: ("get", {
                vol.Optional("request_id"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=DER_REQUEST_ID_MAX)),
                vol.Optional("is_default"): cv.boolean,
                vol.Optional("control_type"): der_types,
                vol.Optional("control_id"): control_id,
            }),
            SERVICE_DER_CLEAR: ("clear", {
                vol.Required("is_default"): cv.boolean,
                vol.Optional("control_type"): der_types,
                vol.Optional("control_id"): control_id,
            }),
            SERVICE_DER_EVENTS: ("events", {}),
        }
        for name, (action, fields) in der_services.items():
            if hass.services.has_service(DOMAIN, name):
                continue

            async def submit_der(call, action=action):
                return await der_command(target(call), action, call.data)
            hass.services.async_register(
                DOMAIN, name, submit_der, schema=vol.Schema({vol.Required("entry_id"): str, **fields}),
                supports_response=SupportsResponse.OPTIONAL)
        local_services = {
            SERVICE_LOCAL_SET: ("set", {
                vol.Optional("evse_id", default=0): vol.All(vol.Coerce(int), vol.In([0, 1])),
                vol.Optional("profile_id", default=LOCAL_PROFILE_ID): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=2147483647)),
                vol.Optional("stack_level", default=1): vol.All(vol.Coerce(int), vol.Range(min=0, max=10)),
                vol.Optional("start_time"): cv.datetime,
                vol.Optional("duration", default=LOCAL_MAX_DURATION_S): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=LOCAL_MAX_DURATION_S)),
                vol.Required("periods"): vol.All([vol.Schema({
                    vol.Required("start_period"): vol.All(vol.Coerce(int), vol.Range(min=0)),
                    vol.Required("limit"): vol.All(vol.Coerce(float), vol.Range(min=0, max=MAX_IMPORT_W)),
                    vol.Optional("number_phases"): vol.All(vol.Coerce(int), vol.In([1, 3])),
                })], vol.Length(min=1)),
            }),
            SERVICE_LOCAL_CLEAR: ("clear", {
                vol.Optional("profile_id", default=LOCAL_PROFILE_ID): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=2147483647)),
            }),
            SERVICE_LIMITS: ("limits", {}),
        }
        for name, (action, fields) in local_services.items():
            if hass.services.has_service(DOMAIN, name):
                continue

            async def submit_local(call, action=action):
                return await local_command(target(call), action, call.data)
            hass.services.async_register(
                DOMAIN, name, submit_local, schema=vol.Schema({vol.Required("entry_id"): str, **fields}),
                supports_response=SupportsResponse.OPTIONAL)
        if not hass.services.has_service(DOMAIN, SERVICE_RECONCILE):
            async def reconcile_entry(call):
                return await reconcile(target(call))
            hass.services.async_register(
                DOMAIN, SERVICE_RECONCILE, reconcile_entry,
                schema=vol.Schema({vol.Required("entry_id"): str}),
                supports_response=SupportsResponse.OPTIONAL)
    elif platform == "number":
        add_entities([LabPower(ctx, entry)])
    else:
        add_entities([LabSensor(ctx, entry, *spec) for spec in (
            ("power_w", "Measured EV power", "W"),
            ("import_kwh", "Meter energy imported to EV", "kWh"),
            ("export_kwh", "Meter energy exported from EV", "kWh"),
            ("transaction", "Current transaction", None),
            ("protocol", "Observed OCPP protocol", None),
            ("status", "Connector status", None),
            ("command", "Command dispatch status", None),
        )])
        client = ctx["client"]
        if (isinstance(client, ScopedLabClient) and not client.read_only
                and client.local_energy.get("charging_limits")):
            add_entities([LabChargingLimitSensor(ctx, entry)])
        # Every discovered station, not only the command station.
        known = set()
        add_entities(max_power_sensors(ctx, entry, known))

        def add_new():
            new = max_power_sensors(ctx, entry, known)
            if new:
                add_entities(new)
        entry.async_on_unload(ctx["coordinator"].async_add_listener(add_new))
