"""Number entities for per-station load limits."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .citrine_api import CitrineApiError, CitrineClient
from .const import (
    ATTR_DURATION,
    ATTR_ENTRY_ID,
    ATTR_EVSE_ID,
    ATTR_LIMIT,
    ATTR_PROTOCOL,
    ATTR_STATION_ID,
    ATTR_UNIT,
    CONF_MAIN_FUSE_LIMIT_W,
    CONF_MIN_CHARGE_CURRENT_A,
    CONF_RAMP_RATE_W_S,
    CONF_SOLAR_START_BUFFER_W,
    CONF_TENANT_ID,
    DEFAULT_MAIN_FUSE_LIMIT_W,
    DEFAULT_MIN_CHARGE_CURRENT_A,
    DEFAULT_PROFILE_DISCHARGE_LIMIT,
    DEFAULT_PROFILE_DURATION,
    DEFAULT_PROFILE_LIMIT,
    DEFAULT_PROFILE_SETPOINT,
    DEFAULT_RAMP_RATE_W_S,
    DEFAULT_SOLAR_START_BUFFER_W,
    DOMAIN,
    SERVICE_SET_STATION_LIMIT,
)
from .coordinator import CitrineCoordinator
from .load_controller import CitrineLoadController
from .profile_controls import async_push_profile_update


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities."""
    from .compatibility import PROFILE_LAB
    if hass.data[DOMAIN][entry.entry_id]["client"].api_profile == PROFILE_LAB:
        from .lab_entities import setup_lab
        await setup_lab(hass, entry, async_add_entities, "number")
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CitrineCoordinator = data["coordinator"]
    client: CitrineClient = data["client"]
    controller: CitrineLoadController = data["controller"]

    known_ids: set[str] = set()

    def _build_entities() -> list[NumberEntity]:
        entities: list[NumberEntity] = []
        for station in coordinator.data.get("stations", []):
            station_id = station.get("id")
            if not station_id or station_id in known_ids:
                continue
            known_ids.add(station_id)
            capabilities = coordinator.get_station_capabilities(str(station_id))
            entities.append(CitrineStationLimitNumber(coordinator, client, entry, station))
            entities.append(CitrineStationProfileLimitNumber(coordinator, entry, station))
            entities.append(CitrineStationProfileDurationNumber(coordinator, entry, station))
            entities.append(CitrineStationProfileEvseNumber(coordinator, entry, station))
            entities.append(CitrineStationProfileStackLevelNumber(coordinator, entry, station))
            entities.append(CitrineStationProfileIdNumber(coordinator, entry, station))

            if bool(capabilities.get("supports_profile_setpoint", False)):
                entities.append(CitrineStationProfileSetpointNumber(coordinator, entry, station))
            if bool(capabilities.get("supports_discharge_limit", False)):
                entities.append(CitrineStationProfileDischargeLimitNumber(coordinator, entry, station))
        return entities

    async_add_entities(_build_entities())
    async_add_entities(
        [
            CitrineMainFuseLimitNumber(controller, entry),
            CitrineSolarBufferNumber(controller, entry),
            CitrineMinChargeCurrentNumber(controller, entry),
            CitrineRampRateNumber(controller, entry),
        ]
    )

    def _async_handle_update() -> None:
        new_entities = _build_entities()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_handle_update))


class CitrineStationLimitNumber(CoordinatorEntity[CitrineCoordinator], NumberEntity):
    """Writable number for station max charging limit."""

    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_min_value = 0
    _attr_native_max_value = 500000
    _attr_native_step = 100
    _attr_mode = "box"
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._client = client
        self._entry = entry
        self._station_id = str(station["id"])
        # Keep entry-scoped unique_id for stable HA entity registry mapping.
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_max_limit"
        self._attr_name = f"{self._station_id} Max Limit"
        self._value = 0.0

    @property
    def native_value(self) -> float:
        return self._value

    @property
    def available(self) -> bool:
        # Keep writable control available even if discovery refresh is transiently unavailable.
        return True

    async def async_set_native_value(self, value: float) -> None:
        station = self._station()
        protocol = self._client.normalize_protocol(
            self.coordinator.get_station_protocol(self._station_id, station.get("protocol"))
        )
        try:
            await self.hass.services.async_call(
                DOMAIN,
                SERVICE_SET_STATION_LIMIT,
                {
                    ATTR_ENTRY_ID: self._entry.entry_id,
                    ATTR_STATION_ID: self._station_id,
                    ATTR_PROTOCOL: protocol,
                    ATTR_LIMIT: float(value),
                    ATTR_UNIT: "W",
                    ATTR_EVSE_ID: 0,
                    ATTR_DURATION: 300,
                },
                blocking=True,
            )
        except CitrineApiError as err:
            raise ValueError(f"Failed to apply station limit: {err}") from err
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"Failed to apply station limit: {err}") from err

        self._value = value
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        station = self._station()
        tenant = station.get("tenantId", self._entry.data.get(CONF_TENANT_ID, 1))
        station_id = str(station.get("id", self._station_id))
        return DeviceInfo(
            identifiers={(DOMAIN, f"{tenant}:{station_id}")},
            name=f"Citrine Charger {station_id}",
            manufacturer=station.get("chargePointVendor") or "Unknown",
            model=station.get("chargePointModel") or station.get("protocol"),
            sw_version=station.get("firmwareVersion"),
        )

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineProfilePreferenceNumber(CoordinatorEntity[CitrineCoordinator], NumberEntity):
    """Base writable number for cached per-station profile preferences."""

    _attr_mode = "box"

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
        *,
        key: str,
        name_suffix: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_{unique_suffix}"
        self._attr_name = f"{self._station_id} {name_suffix}"

    @property
    def native_value(self) -> float:
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        value = prefs.get(self._key)
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @property
    def available(self) -> bool:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        return bool(capabilities.get("supports_ems_profile_control", False))

    async def async_set_native_value(self, value: float) -> None:
        self._assert_ems_profile_control_supported()
        self.coordinator.update_station_profile_preferences(self._station_id, **{self._key: value})
        self.async_write_ha_state()

    def _assert_ems_profile_control_supported(self) -> None:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        if bool(capabilities.get("supports_ems_profile_control", False)):
            return
        reason = capabilities.get("ems_profile_support_reason") or "EMS charging-profile control is not supported for this station"
        raise ValueError(str(reason))

    def _is_dynamic_live(self) -> bool:
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        profile_kind = str(prefs.get("profile_kind", "")).strip().capitalize()
        return bool(prefs.get("dynamic_session_active", False)) or profile_kind == "Dynamic"

    async def _async_push_profile_update(self) -> None:
        try:
            await async_push_profile_update(
                self.hass,
                self.coordinator,
                self._entry,
                self._station_id,
            )
        except CitrineApiError as err:
            raise ValueError(f"Failed to push profile update: {err}") from err
        except Exception as err:  # noqa: BLE001
            raise ValueError(f"Failed to push profile update: {err}") from err

    @property
    def device_info(self) -> DeviceInfo:
        station = self._station()
        tenant = station.get("tenantId", self._entry.data.get(CONF_TENANT_ID, 1))
        station_id = str(station.get("id", self._station_id))
        return DeviceInfo(
            identifiers={(DOMAIN, f"{tenant}:{station_id}")},
            name=f"Citrine Charger {station_id}",
            manufacturer=station.get("chargePointVendor") or "Unknown",
            model=station.get("chargePointModel") or station.get("protocol"),
            sw_version=station.get("firmwareVersion"),
        )

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineStationProfileLimitNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:gauge"
    _attr_native_step = 100
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    @property
    def native_min_value(self) -> float:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        try:
            return float(capabilities.get("min_profile_limit", -500000.0))
        except (TypeError, ValueError):
            return -500000.0

    @property
    def native_max_value(self) -> float:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        try:
            return float(capabilities.get("max_profile_limit", 500000.0))
        except (TypeError, ValueError):
            return 500000.0

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="limit",
            name_suffix="Profile Limit",
            unique_suffix="profile_limit",
        )
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            limit=self.coordinator.get_station_profile_preferences(self._station_id).get("limit", DEFAULT_PROFILE_LIMIT),
        )

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.update_station_profile_preferences(self._station_id, limit=float(value))
        if self._is_dynamic_live():
            await self._async_push_profile_update()
        self.async_write_ha_state()


class CitrineStationProfileSetpointNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:target"
    _attr_native_step = 100
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    @property
    def native_min_value(self) -> float:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        try:
            return float(capabilities.get("min_profile_limit", -500000.0))
        except (TypeError, ValueError):
            return -500000.0

    @property
    def native_max_value(self) -> float:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        try:
            return float(capabilities.get("max_profile_limit", 500000.0))
        except (TypeError, ValueError):
            return 500000.0

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="setpoint",
            name_suffix="Profile Setpoint",
            unique_suffix="profile_setpoint",
        )
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            setpoint=self.coordinator.get_station_profile_preferences(self._station_id).get(
                "setpoint",
                DEFAULT_PROFILE_SETPOINT,
            ),
        )

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.update_station_profile_preferences(self._station_id, setpoint=float(value))
        if self._is_dynamic_live():
            await self._async_push_profile_update()
        self.async_write_ha_state()


class CitrineStationProfileDischargeLimitNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:battery-arrow-down"
    _attr_native_step = 100
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    @property
    def native_min_value(self) -> float:
        return 0.0

    @property
    def native_max_value(self) -> float:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        try:
            return float(capabilities.get("max_profile_limit", 500000.0))
        except (TypeError, ValueError):
            return 500000.0

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="discharge_limit",
            name_suffix="Profile Discharge Limit",
            unique_suffix="profile_discharge_limit",
        )
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            discharge_limit=self.coordinator.get_station_profile_preferences(self._station_id).get(
                "discharge_limit",
                DEFAULT_PROFILE_DISCHARGE_LIMIT,
            ),
        )

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            discharge_limit=max(0.0, float(value)),
        )
        if self._is_dynamic_live():
            await self._async_push_profile_update()
        self.async_write_ha_state()


class CitrineStationProfileDurationNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:timer-cog"
    _attr_native_min_value = 0
    _attr_native_max_value = 86400
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="duration",
            name_suffix="Profile Duration",
            unique_suffix="profile_duration",
        )
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            duration=self.coordinator.get_station_profile_preferences(self._station_id).get("duration", DEFAULT_PROFILE_DURATION),
        )


class CitrineStationProfileEvseNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:ev-plug-type2"
    _attr_native_min_value = 0
    _attr_native_max_value = 999
    _attr_native_step = 1

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="evse_id",
            name_suffix="Profile EVSE ID",
            unique_suffix="profile_evse",
        )


class CitrineStationProfileStackLevelNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:layers-triple"
    _attr_native_min_value = 0
    _attr_native_max_value = 999
    _attr_native_step = 1

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="stack_level",
            name_suffix="Profile Stack Level",
            unique_suffix="profile_stack",
        )
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            stack_level=self.coordinator.get_station_profile_preferences(self._station_id).get("stack_level", DEFAULT_PROFILE_STACK_LEVEL),
        )


class CitrineStationProfileIdNumber(CitrineProfilePreferenceNumber):
    _attr_icon = "mdi:numeric"
    _attr_native_min_value = 0
    _attr_native_max_value = 999999
    _attr_native_step = 1

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry, station: dict[str, Any]) -> None:
        super().__init__(
            coordinator,
            entry,
            station,
            key="profile_id",
            name_suffix="Profile ID",
            unique_suffix="profile_id",
        )

    @property
    def native_value(self) -> float:
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        value = prefs.get(self._key)
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    async def async_set_native_value(self, value: float) -> None:
        # Value 0 means auto-generated profile id on apply.
        self.coordinator.update_station_profile_preferences(
            self._station_id,
            profile_id=None if int(value) == 0 else int(value),
        )
        self.async_write_ha_state()


def _site_device_info(entry: ConfigEntry) -> DeviceInfo:
    tenant = entry.data.get(CONF_TENANT_ID, 1)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{tenant}:site_controller")},
        name="Citrine Energy Controller Hub",
        manufacturer="CitrineOS",
        model="Local Load Controller",
        sw_version="0.2.0",
    )


class CitrineMainFuseLimitNumber(NumberEntity):
    """Main grid fuse import safety limit."""

    _attr_icon = "mdi:fuse-blade"
    _attr_native_min_value = 1000.0
    _attr_native_max_value = 250000.0
    _attr_native_step = 500.0
    _attr_mode = "box"
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_main_fuse_limit"
        self._attr_name = "Citrine Main Fuse Limit"

    @property
    def native_value(self) -> float:
        return float(
            self._entry.options.get(CONF_MAIN_FUSE_LIMIT_W)
            or self._entry.data.get(CONF_MAIN_FUSE_LIMIT_W, DEFAULT_MAIN_FUSE_LIMIT_W)
        )

    async def async_set_native_value(self, value: float) -> None:
        new_opts = dict(self._entry.options)
        new_opts[CONF_MAIN_FUSE_LIMIT_W] = float(value)
        self.hass.config_entries.async_update_entry(self._entry, options=new_opts)
        await self._controller.async_recompute()
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineSolarBufferNumber(NumberEntity):
    """Surplus margin required before starting or increasing EV charge."""

    _attr_icon = "mdi:solar-power"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 5000.0
    _attr_native_step = 50.0
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_start_buffer"
        self._attr_name = "Citrine Solar Start Buffer"

    @property
    def native_value(self) -> float:
        return float(
            self._entry.options.get(CONF_SOLAR_START_BUFFER_W)
            or self._entry.data.get(CONF_SOLAR_START_BUFFER_W, DEFAULT_SOLAR_START_BUFFER_W)
        )

    async def async_set_native_value(self, value: float) -> None:
        new_opts = dict(self._entry.options)
        new_opts[CONF_SOLAR_START_BUFFER_W] = float(value)
        self.hass.config_entries.async_update_entry(self._entry, options=new_opts)
        await self._controller.async_recompute()
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineMinChargeCurrentNumber(NumberEntity):
    """Minimum pilot current floor (typically 6A per phase)."""

    _attr_icon = "mdi:current-ac"
    _attr_native_min_value = 6.0
    _attr_native_max_value = 16.0
    _attr_native_step = 1.0
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_min_charge_current"
        self._attr_name = "Citrine Min Charge Current"

    @property
    def native_value(self) -> float:
        return float(
            self._entry.options.get(CONF_MIN_CHARGE_CURRENT_A)
            or self._entry.data.get(CONF_MIN_CHARGE_CURRENT_A, DEFAULT_MIN_CHARGE_CURRENT_A)
        )

    async def async_set_native_value(self, value: float) -> None:
        new_opts = dict(self._entry.options)
        new_opts[CONF_MIN_CHARGE_CURRENT_A] = float(value)
        self.hass.config_entries.async_update_entry(self._entry, options=new_opts)
        await self._controller.async_recompute()
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineRampRateNumber(NumberEntity):
    """Maximum power slew rate in Watts per second."""

    _attr_icon = "mdi:arrow-top-right-bottom-left"
    _attr_native_min_value = 50.0
    _attr_native_max_value = 10000.0
    _attr_native_step = 50.0
    _attr_native_unit_of_measurement = "W/s"

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ramp_rate"
        self._attr_name = "Citrine Max Ramp Rate"

    @property
    def native_value(self) -> float:
        return float(
            self._entry.options.get(CONF_RAMP_RATE_W_S)
            or self._entry.data.get(CONF_RAMP_RATE_W_S, DEFAULT_RAMP_RATE_W_S)
        )

    async def async_set_native_value(self, value: float) -> None:
        new_opts = dict(self._entry.options)
        new_opts[CONF_RAMP_RATE_W_S] = float(value)
        self.hass.config_entries.async_update_entry(self._entry, options=new_opts)
        await self._controller.async_recompute()
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)
