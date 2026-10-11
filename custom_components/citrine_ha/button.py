"""Button entities for start/stop charging actions."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .citrine_api import CitrineApiError, CitrineClient
from .const import (
    ATTR_DURATION,
    ATTR_ENTRY_ID,
    ATTR_EVSE_ID,
    ATTR_ID_TAG,
    ATTR_LIMIT,
    ATTR_PROFILE_ID,
    ATTR_PROFILE_PURPOSE,
    ATTR_PROTOCOL,
    ATTR_SETPOINT,
    ATTR_STACK_LEVEL,
    ATTR_STATION_ID,
    ATTR_TRANSACTION_ID,
    ATTR_UNIT,
    CONF_DEFAULT_EVSE_ID,
    CONF_DEFAULT_ID_TAG,
    CONF_TENANT_ID,
    DEFAULT_DEFAULT_EVSE_ID,
    DEFAULT_DEFAULT_ID_TAG,
    DOMAIN,
    MODE_EMERGENCY_SAFE,
    SERVICE_START_CHARGING,
    SERVICE_STOP_CHARGING,
)
from .coordinator import CitrineCoordinator
from .load_controller import CitrineLoadController
from .profile_controls import async_push_profile_update

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities."""
    from .compatibility import PROFILE_LAB
    if hass.data[DOMAIN][entry.entry_id]["client"].api_profile == PROFILE_LAB:
        from .lab_entities import setup_lab
        await setup_lab(hass, entry, async_add_entities, "button")
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CitrineCoordinator = data["coordinator"]
    client: CitrineClient = data["client"]
    controller: CitrineLoadController = data["controller"]

    known_ids: set[str] = set()

    def _build_entities() -> list[ButtonEntity]:
        entities: list[ButtonEntity] = []
        for station in coordinator.data.get("stations", []):
            station_id = station.get("id")
            if not station_id or station_id in known_ids:
                continue
            known_ids.add(station_id)
            capabilities = coordinator.get_station_capabilities(str(station_id))
            entities.append(CitrineStartChargingButton(hass, coordinator, client, entry, station))
            entities.append(CitrineStopChargingButton(hass, coordinator, client, entry, station))
            entities.append(CitrineApplyChargingProfileButton(hass, coordinator, client, entry, station))
            entities.append(CitrineClearChargingProfileButton(hass, coordinator, client, entry, station))
            if bool(capabilities.get("supports_dynamic_profiles", False)):
                entities.append(CitrineStartDynamicSessionButton(hass, coordinator, client, entry, station))
                entities.append(CitrineStopDynamicSessionButton(hass, coordinator, client, entry, station))
        return entities

    async_add_entities(_build_entities())
    async_add_entities(
        [
            CitrineRecomputeButton(controller, entry),
            CitrineEmergencySafeButton(controller, entry),
        ]
    )

    def _async_handle_update() -> None:
        new_entities = _build_entities()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_handle_update))


class CitrineBaseButton(CoordinatorEntity[CitrineCoordinator], ButtonEntity):
    """Shared station device mapping for button entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._hass_instance = hass
        self._client = client
        self._entry = entry
        self._station_id = str(station["id"])

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


class CitrineProfileControlButtonBase(CitrineBaseButton):
    """Base class for EMS profile-control buttons with capability gating."""

    @property
    def available(self) -> bool:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        return bool(capabilities.get("supports_ems_profile_control", False))

    def _assert_ems_profile_control_supported(self) -> None:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        if bool(capabilities.get("supports_ems_profile_control", False)):
            return
        reason = capabilities.get("ems_profile_support_reason") or "EMS charging-profile control is not supported for this station"
        raise HomeAssistantError(str(reason))


class CitrineStartChargingButton(CitrineBaseButton):
    """Start charging using default id tag and EVSE."""

    _attr_icon = "mdi:play-circle"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(hass, coordinator, client, entry, station)
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_start"
        self._attr_name = f"{self._station_id} Start Charging"

    async def async_press(self) -> None:
        station = self._station()
        protocol = self._client.normalize_protocol(station.get("protocol"))

        id_tag = self._entry.options.get(
            CONF_DEFAULT_ID_TAG,
            self._entry.data.get(CONF_DEFAULT_ID_TAG, DEFAULT_DEFAULT_ID_TAG),
        )
        evse_id = self._resolve_start_target(station)

        try:
            await self._hass_instance.services.async_call(
                DOMAIN,
                SERVICE_START_CHARGING,
                {
                    ATTR_ENTRY_ID: self._entry.entry_id,
                    ATTR_STATION_ID: self._station_id,
                    ATTR_PROTOCOL: protocol,
                    ATTR_ID_TAG: id_tag,
                    ATTR_EVSE_ID: evse_id,
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Start command failed: {err}") from err

    def _resolve_start_target(self, station: dict[str, Any]) -> int:
        configured_evse_id = self._entry.options.get(
            CONF_DEFAULT_EVSE_ID,
            self._entry.data.get(CONF_DEFAULT_EVSE_ID, DEFAULT_DEFAULT_EVSE_ID),
        )
        if configured_evse_id is not None:
            try:
                return int(configured_evse_id)
            except (TypeError, ValueError):
                pass

        default_evse = station.get("defaultEvseId")
        if default_evse is not None:
            try:
                return int(default_evse)
            except (TypeError, ValueError):
                pass

        for connector in station.get("connectors", []):
            evse_id = connector.get("evseId") or connector.get("connectorId")
            if evse_id is None:
                continue
            try:
                return int(evse_id)
            except (TypeError, ValueError):
                continue

        return int(
            self._entry.options.get(
                CONF_DEFAULT_EVSE_ID,
                self._entry.data.get(CONF_DEFAULT_EVSE_ID, DEFAULT_DEFAULT_EVSE_ID),
            )
        )

class CitrineStopChargingButton(CitrineBaseButton):
    """Stop charging for discovered active transaction id."""

    _attr_icon = "mdi:stop-circle"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(hass, coordinator, client, entry, station)
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_stop"
        self._attr_name = f"{self._station_id} Stop Charging"

    async def async_press(self) -> None:
        station = self._station()
        protocol = self._client.normalize_protocol(station.get("protocol"))

        transaction_id = (
            station.get("activeTransactionId")
            or station.get("currentTransactionId")
            or station.get("transactionId")
            or station.get("previousTransactionId")
        )
        if transaction_id is None:
            raise HomeAssistantError(
                "No active transaction id found for station. Use the stop_charging service with transaction_id."
            )

        try:
            await self._hass_instance.services.async_call(
                DOMAIN,
                SERVICE_STOP_CHARGING,
                {
                    ATTR_ENTRY_ID: self._entry.entry_id,
                    ATTR_STATION_ID: self._station_id,
                    ATTR_PROTOCOL: protocol,
                    ATTR_TRANSACTION_ID: str(transaction_id),
                },
                blocking=True,
            )
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Stop command failed: {err}") from err


class CitrineApplyChargingProfileButton(CitrineProfileControlButtonBase):
    """Apply station charging profile from profile control entities."""

    _attr_icon = "mdi:chart-timeline-variant-shimmer"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(hass, coordinator, client, entry, station)
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_apply_profile"
        self._attr_name = f"{self._station_id} Apply Charging Profile"

    async def async_press(self) -> None:
        self._assert_ems_profile_control_supported()
        station = self._station()
        protocol = self._client.normalize_protocol(
            self.coordinator.get_station_protocol(self._station_id, str(station.get("protocol", "")))
        )
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        capabilities = self.coordinator.get_station_capabilities(self._station_id)

        requested_unit = str(prefs.get("unit", "W")).upper()
        allowed_units = [str(unit).upper() for unit in capabilities.get("allowed_units", [])]
        if allowed_units and requested_unit not in allowed_units:
            requested_unit = str(capabilities.get("preferred_unit", allowed_units[0])).upper()

        requested_purpose = str(
            prefs.get(
                "profile_purpose",
                capabilities.get("default_profile_purpose", "TxDefaultProfile"),
            )
        )
        requested_kind = str(
            prefs.get(
                "profile_kind",
                capabilities.get("default_profile_kind", "Absolute"),
            )
        )
        supported_purposes = [str(item) for item in capabilities.get("supported_profile_purposes", [])]
        supported_kinds = [str(item) for item in capabilities.get("supported_profile_kinds", [])]
        if supported_purposes and requested_purpose not in supported_purposes:
            requested_purpose = str(capabilities.get("default_profile_purpose", supported_purposes[0]))
        if supported_kinds and requested_kind not in supported_kinds:
            requested_kind = str(capabilities.get("default_profile_kind", supported_kinds[0]))

        transaction_id = (
            station.get("activeTransactionId")
            or station.get("currentTransactionId")
            or station.get("transactionId")
            or station.get("previousTransactionId")
        )
        tx_mode = str(prefs.get("profile_tx_mode", "safe_fallback"))
        if requested_purpose == "TxProfile" and transaction_id is None:
            if tx_mode == "strict_txprofile":
                raise HomeAssistantError(
                    "TxProfile strict mode requires an active transaction id, but no transaction is available"
                )
            requested_purpose = str(capabilities.get("default_profile_purpose", "TxDefaultProfile"))
            if requested_purpose == "TxProfile":
                requested_purpose = next(
                    (item for item in supported_purposes if item != "TxProfile"),
                    "TxDefaultProfile",
                )

        purpose_key = requested_purpose.lower()
        tx_for_command = str(transaction_id) if (transaction_id is not None and purpose_key == "txprofile") else None

        limit_value = float(prefs.get("limit", 7000.0))
        setpoint_value = prefs.get(ATTR_SETPOINT)
        discharge_limit_value = prefs.get("discharge_limit")
        operation_mode_value = prefs.get("operation_mode")
        if setpoint_value is not None:
            try:
                setpoint_value = float(setpoint_value)
            except (TypeError, ValueError):
                setpoint_value = None
        if discharge_limit_value is not None:
            try:
                discharge_limit_value = max(0.0, float(discharge_limit_value))
            except (TypeError, ValueError):
                discharge_limit_value = None
        if operation_mode_value is not None:
            operation_mode_value = str(operation_mode_value)
        profile_periods = prefs.get("profile_periods")
        if profile_periods is not None and not isinstance(profile_periods, list):
            profile_periods = None

        sign_mode = str(prefs.get("profile_sign_mode", "normal"))
        if sign_mode == "invert_negative" and limit_value < 0:
            limit_value = abs(limit_value)
        if sign_mode == "invert_negative" and isinstance(setpoint_value, float) and setpoint_value < 0:
            setpoint_value = abs(setpoint_value)

        supports_bidirectional = bool(capabilities.get("supports_bidirectional_power_transfer", False))
        if limit_value < 0 and not supports_bidirectional:
            raise HomeAssistantError(
                f"Station {self._station_id} does not advertise bidirectional profile support"
            )
        min_profile_limit = capabilities.get("min_profile_limit")
        max_profile_limit = capabilities.get("max_profile_limit")
        if min_profile_limit is not None:
            limit_value = max(float(min_profile_limit), limit_value)
        if max_profile_limit is not None:
            limit_value = min(float(max_profile_limit), limit_value)

        profile_id = prefs.get("profile_id")
        evse_id = int(prefs.get("evse_id", 0))
        duration = int(prefs.get("duration", 300))
        stack_level = int(prefs.get("stack_level", 1))

        try:
            _LOGGER.warning(
                "Apply profile requested: station=%s protocol=%s evse=%s limit=%s unit=%s purpose=%s kind=%s tx=%s sign_mode=%s tx_mode=%s",
                self._station_id,
                protocol,
                evse_id,
                limit_value,
                requested_unit,
                requested_purpose,
                requested_kind,
                tx_for_command,
                sign_mode,
                tx_mode,
            )

            await self._client.set_charging_profile(
                protocol=protocol,
                station_id=self._station_id,
                limit=limit_value,
                setpoint=setpoint_value,
                discharge_limit=discharge_limit_value,
                operation_mode=operation_mode_value,
                unit=requested_unit,
                evse_id=evse_id,
                duration=duration,
                stack_level=stack_level,
                profile_id=int(profile_id) if profile_id is not None else None,
                profile_purpose=requested_purpose,
                profile_kind=requested_kind,
                profile_periods=profile_periods,
                transaction_id=tx_for_command,
                txprofile_compatibility_fallback=(tx_mode != "strict_txprofile"),
            )
            self.coordinator.update_station_profile_preferences(
                self._station_id,
                dynamic_session_active=(str(requested_kind).strip().capitalize() == "Dynamic"),
            )
            self.coordinator.mark_profile_push_succeeded(self._station_id)
            await self.coordinator.async_request_refresh()
        except CitrineApiError as err:
            self.coordinator.mark_profile_push_failed(self._station_id, str(err))
            raise HomeAssistantError(f"Apply profile command failed: {err}") from err
        except Exception as err:  # noqa: BLE001
            self.coordinator.mark_profile_push_failed(self._station_id, str(err))
            raise HomeAssistantError(f"Apply profile command failed: {err}") from err


class CitrineClearChargingProfileButton(CitrineProfileControlButtonBase):
    """Clear station charging profile from profile control entities."""

    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(hass, coordinator, client, entry, station)
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_clear_profile"
        self._attr_name = f"{self._station_id} Clear Charging Profile"

    async def async_press(self) -> None:
        self._assert_ems_profile_control_supported()
        station = self._station()
        protocol = self._client.normalize_protocol(
            self.coordinator.get_station_protocol(self._station_id, str(station.get("protocol", "")))
        )
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        supported_purposes = [str(item) for item in capabilities.get("supported_profile_purposes", [])]
        requested_purpose = str(
            prefs.get(
                "profile_purpose",
                capabilities.get("default_profile_purpose", "TxDefaultProfile"),
            )
        )
        if supported_purposes and requested_purpose not in supported_purposes:
            requested_purpose = str(capabilities.get("default_profile_purpose", supported_purposes[0]))

        profile_id = prefs.get("profile_id")
        evse_id = int(prefs.get("evse_id", 0))
        stack_level = int(prefs.get("stack_level", 1))

        try:
            _LOGGER.warning(
                "Clear profile requested: station=%s protocol=%s evse=%s purpose=%s profile_id=%s",
                self._station_id,
                protocol,
                evse_id,
                requested_purpose,
                profile_id,
            )

            await self._client.clear_charging_profile(
                protocol=protocol,
                station_id=self._station_id,
                evse_id=evse_id,
                profile_id=int(profile_id) if profile_id is not None else None,
                stack_level=stack_level,
                profile_purpose=requested_purpose,
            )
            self.coordinator.update_station_profile_preferences(
                self._station_id,
                dynamic_session_active=False,
            )
            self.coordinator.mark_profile_push_succeeded(self._station_id)
            await self.coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            self.coordinator.mark_profile_push_failed(self._station_id, str(err))
            raise HomeAssistantError(f"Clear profile command failed: {err}") from err


class CitrineStartDynamicSessionButton(CitrineProfileControlButtonBase):
    """Start a dynamic profile session and enable live preference pushes."""

    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(hass, coordinator, client, entry, station)
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_start_dynamic"
        self._attr_name = f"{self._station_id} Start Dynamic Session"

    async def async_press(self) -> None:
        self._assert_ems_profile_control_supported()
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        if not bool(capabilities.get("supports_dynamic_profiles", False)):
            raise HomeAssistantError("Station does not support OCPP dynamic charging profiles")

        self.coordinator.update_station_profile_preferences(
            self._station_id,
            profile_kind="Dynamic",
            dynamic_session_active=True,
        )

        try:
            await async_push_profile_update(
                self._hass_instance,
                self.coordinator,
                self._entry,
                self._station_id,
            )
            await self.coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"Start dynamic session failed: {err}") from err


class CitrineStopDynamicSessionButton(CitrineProfileControlButtonBase):
    """Stop dynamic profile session and clear active dynamic profile."""

    _attr_icon = "mdi:chart-bell-curve-cumulative-off"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: CitrineCoordinator,
        client: CitrineClient,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(hass, coordinator, client, entry, station)
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_stop_dynamic"
        self._attr_name = f"{self._station_id} Stop Dynamic Session"

    async def async_press(self) -> None:
        self._assert_ems_profile_control_supported()
        station = self._station()
        protocol = self._client.normalize_protocol(
            self.coordinator.get_station_protocol(self._station_id, str(station.get("protocol", "")))
        )
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)

        profile_id = prefs.get("profile_id")
        evse_id = int(prefs.get("evse_id", 0))
        stack_level = int(prefs.get("stack_level", 1))
        profile_purpose = str(prefs.get("profile_purpose", "TxDefaultProfile"))

        try:
            await self._client.clear_charging_profile(
                protocol=protocol,
                station_id=self._station_id,
                evse_id=evse_id,
                profile_id=int(profile_id) if profile_id is not None else None,
                stack_level=stack_level,
                profile_purpose=profile_purpose,
            )
            self.coordinator.update_station_profile_preferences(
                self._station_id,
                dynamic_session_active=False,
                profile_kind=capabilities_default_kind(self.coordinator, self._station_id),
            )
            self.coordinator.mark_profile_push_succeeded(self._station_id)
            await self.coordinator.async_request_refresh()
        except CitrineApiError as err:
            self.coordinator.mark_profile_push_failed(self._station_id, str(err))
            raise HomeAssistantError(f"Stop dynamic session failed: {err}") from err
        except Exception as err:  # noqa: BLE001
            self.coordinator.mark_profile_push_failed(self._station_id, str(err))
            raise HomeAssistantError(f"Stop dynamic session failed: {err}") from err


def capabilities_default_kind(coordinator: CitrineCoordinator, station_id: str) -> str:
    capabilities = coordinator.get_station_capabilities(station_id)
    return str(capabilities.get("default_profile_kind", "Absolute"))


def _site_device_info(entry: ConfigEntry) -> DeviceInfo:
    tenant = entry.data.get(CONF_TENANT_ID, 1)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{tenant}:site_controller")},
        name="Citrine Energy Controller Hub",
        manufacturer="CitrineOS",
        model="Local Load Controller",
        sw_version="0.2.0",
    )


class CitrineRecomputeButton(ButtonEntity):
    """Trigger an immediate load controller cycle."""

    _attr_icon = "mdi:refresh-auto"

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_recompute_load_control"
        self._attr_name = "Citrine Recompute Load Control"

    async def async_press(self) -> None:
        await self._controller.async_recompute()

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineEmergencySafeButton(ButtonEntity):
    """Force controller into emergency safe mode (zero / minimal charging)."""

    _attr_icon = "mdi:alert-octagon"

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_emergency_safe_mode"
        self._attr_name = "Citrine Emergency Safe Mode"

    async def async_press(self) -> None:
        self._controller.set_mode(MODE_EMERGENCY_SAFE)

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)
