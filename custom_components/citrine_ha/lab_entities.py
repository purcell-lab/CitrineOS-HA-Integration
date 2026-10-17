"""Small, explicit manual control surface for the single enrolled command station."""
import voluptuous as vol
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .citrine_api import CitrineApiError
from .lab_control import (PROFILE_ID, MAX_EXPORT_W, MAX_IMPORT_W, STATION_PATTERN, ScopedLabClient,
                          power_period, number)

SERVICE = "lab_schedule"


def station(data, station_id):
    return next((s for s in (data or {}).get("stations", []) if s.get("id") == station_id), {})


async def command(ctx, action, *, periods=None, duration=180, immediate=False):
    c = ctx["coordinator"]
    client = ctx["client"]
    if not isinstance(client, ScopedLabClient) or client.read_only:
        raise HomeAssistantError("Scoped lab controls are not enabled")
    await c.async_request_refresh()
    sid = client.station
    s = station(c.data, sid)
    protocol = s.get("protocol")
    if not c.last_update_success or not s.get("isOnline") or protocol not in client.protocols:
        raise HomeAssistantError(
            f"Fresh online evidence on {' or '.join(client.protocols)} is required for {sid}")
    tx = s.get("activeTransactionId")
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
            await client.submit_schedule(
                protocol=protocol, station_id=sid, transaction_id=tx,
                periods=periods if periods is not None else [power_period(0, watts)],
                duration=duration, immediate=immediate)
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
        if not super().available or self.ctx["client"].read_only:
            return False
        tx = station(self.coordinator.data, self._station_id).get("activeTransactionId")
        return not tx if self.action in ("start", "clear") else bool(tx)

    async def async_press(self):
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
            return dict(self.ctx["client"].last_command)
        return {k: v for k, v in station(self.coordinator.data, self._station_id).get("lab_meter", {}).items()
                if k not in ("power_w", "import_kwh", "export_kwh")}


async def setup_lab(hass, entry, add_entities, platform):
    ctx = hass.data[DOMAIN][entry.entry_id]
    if platform == "button":
        add_entities([LabButton(ctx, entry, action, name) for action, name in (
            ("start", "Start charging"), ("stop", "Stop transaction"),
            ("apply", "Apply requested power"), ("zero", "Request zero power"),
            ("clear", "Clear test profile after closure"))])
        if not hass.services.has_service(DOMAIN, SERVICE):
            async def submit(call):
                target = hass.data[DOMAIN].get(call.data["entry_id"])
                if not target:
                    raise HomeAssistantError("Unknown CitrineOS entry")
                await command(target, "schedule", periods=call.data["periods"], duration=call.data["duration"],
                              immediate=call.data["immediate"])
            hass.services.async_register(DOMAIN, SERVICE, submit, schema=vol.Schema({
                vol.Required("entry_id"): str,
                vol.Required("periods"): [dict],
                vol.Optional("duration", default=240): vol.All(int, vol.Range(min=1, max=600)),
                vol.Optional("immediate", default=False): bool,
            }))
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
