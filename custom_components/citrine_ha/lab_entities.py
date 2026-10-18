"""Small, explicit manual control surface for the single enrolled command station."""
from datetime import UTC

import voluptuous as vol
from homeassistant.components.button import ButtonEntity
from homeassistant.core import SupportsResponse
from homeassistant.components.number import NumberEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .citrine_api import CitrineApiError
from .lab_control import (PROFILE_ID, PROTOCOL, MAX_EXPORT_W, MAX_IMPORT_W, STATION_PATTERN, ScopedLabClient,
                          power_period, number)
from .pricing import (TOTAL_COST_MAX, amber_intervals, build_cost_updated, build_price_schedule,
                      build_tariff, current_and_next, schedule_summary, tariff_summary)

SERVICE = "lab_schedule"
SERVICE_RECONCILE = "lab_reconcile"
SERVICE_COST = "lab_cost_update"
SERVICE_TARIFF = "lab_default_tariff"


def station(data, station_id):
    return next((s for s in (data or {}).get("stations", []) if s.get("id") == station_id), {})


def time_zone(hass):
    """HA's configured time zone: tariff times of day and price text are local."""
    return dt_util.get_time_zone(hass.config.time_zone) or UTC


def price_state(hass, entity_id):
    """The HA state of a price sensor; Amber forecast attributes are parsed later."""
    state = hass.states.get(entity_id)
    if state is None:
        raise ServiceValidationError(f"Price entity {entity_id} not found")
    return state


def forecast(state):
    intervals = amber_intervals(state)
    if not intervals:
        raise ServiceValidationError(
            f"{state.entity_id} has no current or future Amber price interval "
            "(attributes forecasts[] or detailedForecast[] with per_kwh, start_time, end_time)")
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
                    max_rule_stacks=limits["max_rule_stacks"], max_horizon_s=limits["max_horizon_s"])
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
        body = build_cost_updated(tx, current, nxt, tz, total_cost=total_cost)
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
        tariff = build_tariff(forecast(state), tz,
                              export_intervals=forecast(export_state) if export_state is not None else (),
                              count=intervals, currency=prices["currency"], limits=prices["tariff_limits"])
        await client.set_default_tariff(protocol=protocol, station_id=sid, evse_id=evse_id, tariff=tariff)
    except CitrineApiError as err:
        raise HomeAssistantError(str(err)) from err
    finally:
        c.async_update_listeners()
    return result(client, {"station_id": sid, **tariff_summary({"evseId": evse_id, "tariff": tariff})})


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

    def __init__(self, ctx, entry, key, name):
        super().__init__(ctx["coordinator"])
        self.ctx = ctx
        sid = ctx["client"].station
        self._station_id = sid
        self._attr_unique_id = f"{entry.entry_id}_{sid}_lab_{key}"
        self._attr_name = name
        simulator = STATION_PATTERN.fullmatch(sid) is not None
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"1:{sid}")},
            "name": f"CitrineOS EVerest V2G lab {sid}" if simulator else f"CitrineOS lab {sid}",
            "model": "Isolated simulator" if simulator else "Physical charger (lab command station)",
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
                    price=price_state(hass, price) if price else None)
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
                return await cost_update(ctx, price_state(hass, call.data["price_entity"]),
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
                    ctx, price_state(hass, call.data["price_entity"]),
                    export_state=price_state(hass, export) if export else None,
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
