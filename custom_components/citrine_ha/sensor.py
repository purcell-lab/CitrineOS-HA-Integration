"""Sensor platform for discovered charging stations."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_TENANT_ID, DOMAIN
from .coordinator import CitrineCoordinator
from .load_controller import CitrineLoadController


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from coordinator data."""
    from .compatibility import PROFILE_LAB
    if hass.data[DOMAIN][entry.entry_id]["client"].api_profile == PROFILE_LAB:
        from .lab_entities import setup_lab
        await setup_lab(hass, entry, async_add_entities, "sensor")
        return
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: CitrineCoordinator = data["coordinator"]
    controller: CitrineLoadController = data["controller"]

    known_ids: set[str] = set()

    def _build_entities() -> list[SensorEntity]:
        entities: list[SensorEntity] = []
        for station in coordinator.data.get("stations", []):
            station_id = station.get("id")
            if not station_id or station_id in known_ids:
                continue
            known_ids.add(station_id)
            entities.append(CitrineStationOnlineSensor(coordinator, entry, station))
            entities.append(CitrineStationTransactionSensor(coordinator, entry, station))
            entities.append(CitrineStationProtocolSensor(coordinator, entry, station))
            entities.append(CitrineStationEmsProfileEligibilitySensor(coordinator, entry, station))
            entities.append(CitrineStationConnectorCountSensor(coordinator, entry, station))
            entities.append(CitrineStationActiveSessionSensor(coordinator, entry, station))
            entities.append(CitrineStationProfileStatusSensor(coordinator, entry, station))
            entities.append(CitrineStationHeartbeatAgeSensor(coordinator, entry, station))
        return entities

    async_add_entities(_build_entities())
    async_add_entities(
        [
            # Site Controller Sensors
            CitrineLoadControllerStateSensor(controller, entry),
            CitrineAllocatedEvPowerSensor(controller, entry),
            CitrineSiteHeadroomPowerSensor(controller, entry),
            CitrineSolarSurplusPowerSensor(controller, entry),
            CitrineActiveEvCountSensor(controller, entry),
            CitrinePhaseUnbalanceSensor(controller, entry),
            CitrinePhaseCurrentSensor(controller, entry, phase_name="A"),
            CitrinePhaseCurrentSensor(controller, entry, phase_name="B"),
            CitrinePhaseCurrentSensor(controller, entry, phase_name="C"),
            CitrineMqttIntentStatusSensor(controller, entry),
            # Telemetry Intake Sensors
            CitrineEmsIntakeAcceptedSensor(coordinator, entry),
            CitrineEmsIntakeRejectedSensor(coordinator, entry),
            CitrineEmsIntakeTotalSensor(coordinator, entry),
            CitrineEmsTelemetryFreshnessSensor(coordinator, entry),
            CitrineEmsTelemetryHealthStatusSensor(coordinator, entry),
            CitrineLastCommandStatusSensor(controller, entry),
        ]
    )

    def _async_handle_update() -> None:
        new_entities = _build_entities()
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_async_handle_update))


class CitrineStationOnlineSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Online state sensor for one station."""

    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_online"
        self._attr_name = f"{self._station_id} Online"

    @property
    def native_value(self) -> str:
        station = self._station()
        return "online" if bool(station.get("isOnline")) else "offline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._station()
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        return {
            "protocol": station.get("protocol"),
            "normalized_protocol": self.coordinator.get_station_protocol(
                self._station_id,
                str(station.get("protocol") or ""),
            ),
            "vendor": station.get("chargePointVendor"),
            "model": station.get("chargePointModel"),
            "serial": station.get("chargePointSerialNumber"),
            "latest_ocpp_message": station.get("latestOcppMessageTimestamp"),
            "updated_at": station.get("updatedAt"),
            "default_evse_id": station.get("defaultEvseId"),
            "active_transaction_id": station.get("activeTransactionId"),
            "previous_transaction_id": station.get("previousTransactionId"),
            "next_remote_start_id": station.get("nextRemoteStartId"),
            "connectors": station.get("connectors", []),
            "capabilities": capabilities,
        }

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


class CitrineStationTransactionSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Diagnostics sensor for current station transaction mapping."""

    _attr_icon = "mdi:identifier"

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_transaction"
        self._attr_name = f"{self._station_id} Transaction ID"

    @property
    def native_value(self) -> str | None:
        station = self._station()
        for key in (
            "activeTransactionId",
            "currentTransactionId",
            "transactionId",
            "previousTransactionId",
        ):
            value = station.get(key)
            if value is not None:
                return str(value)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._station()
        return {
            "active_transaction_id": station.get("activeTransactionId"),
            "current_transaction_id": station.get("currentTransactionId"),
            "previous_transaction_id": station.get("previousTransactionId"),
            "next_remote_start_id": station.get("nextRemoteStartId"),
            "default_evse_id": station.get("defaultEvseId"),
            "protocol": station.get("protocol"),
        }

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


class CitrineStationProtocolSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Diagnostic sensor for station protocol."""

    _attr_icon = "mdi:api"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_protocol"
        self._attr_name = f"{self._station_id} Protocol"

    @property
    def native_value(self) -> str:
        station = self._station()
        protocol = station.get("protocol")
        return str(protocol) if protocol else "unknown"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry, self._station())

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineStationEmsProfileEligibilitySensor(
    CoordinatorEntity[CitrineCoordinator], SensorEntity
):
    """Diagnostic sensor for CSMS EMS charging-profile eligibility."""

    _attr_icon = "mdi:clipboard-check-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_ems_profile_eligibility"
        self._attr_name = f"{self._station_id} EMS Profile Eligibility"

    @property
    def native_value(self) -> str:
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        return (
            "eligible"
            if bool(capabilities.get("supports_ems_profile_control", False))
            else "ineligible"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._station()
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        return {
            "protocol": station.get("protocol"),
            "normalized_protocol": self.coordinator.get_station_protocol(
                self._station_id,
                str(station.get("protocol") or ""),
            ),
            "ems_profile_support_reason": capabilities.get("ems_profile_support_reason"),
            "supports_dynamic_profiles": bool(
                capabilities.get("supports_dynamic_profiles", False)
            ),
            "supports_set_charging_profile": bool(
                capabilities.get("supports_set_charging_profile", False)
            ),
            "advertised_capabilities": capabilities.get("advertised_capabilities", []),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry, self._station())

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineStationConnectorCountSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Diagnostic sensor for number of connectors discovered."""

    _attr_icon = "mdi:ev-plug-type2"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_connector_count"
        self._attr_name = f"{self._station_id} Connector Count"

    @property
    def native_value(self) -> int:
        station = self._station()
        return len(station.get("connectors", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._station()
        connectors = station.get("connectors", [])
        available = [c for c in connectors if str(c.get("status", "")).lower() == "available"]
        occupied = [
            c
            for c in connectors
            if str(c.get("status", "")).lower() in {"occupied", "charging"}
        ]
        faults = [
            c
            for c in connectors
            if str(c.get("status", "")).lower() in {"faulted", "unavailable"}
        ]
        return {
            "available_connectors": len(available),
            "occupied_connectors": len(occupied),
            "faulted_or_unavailable_connectors": len(faults),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry, self._station())

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineStationActiveSessionSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Diagnostic sensor for active session state."""

    _attr_icon = "mdi:ev-station"

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_session"
        self._attr_name = f"{self._station_id} Session State"

    @property
    def native_value(self) -> str:
        station = self._station()
        if station.get("activeTransactionId") is not None:
            return "active"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        station = self._station()
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        return {
            "active_transaction_id": station.get("activeTransactionId"),
            "current_transaction_id": station.get("currentTransactionId"),
            "previous_transaction_id": station.get("previousTransactionId"),
            "next_remote_start_id": station.get("nextRemoteStartId"),
            "dynamic_session_active": bool(prefs.get("dynamic_session_active", False)),
            "profile_kind": prefs.get("profile_kind"),
            "profile_purpose": prefs.get("profile_purpose"),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry, self._station())

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineStationHeartbeatAgeSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Diagnostic sensor for age of latest OCPP message in seconds."""

    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "s"

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_heartbeat_age"
        self._attr_name = f"{self._station_id} OCPP Heartbeat Age"

    @property
    def native_value(self) -> int | None:
        station = self._station()
        value = station.get("latestOcppMessageTimestamp") or station.get("updatedAt")
        if not value:
            return None

        parsed = dt_util.parse_datetime(str(value))
        if parsed is None:
            return None

        if parsed.tzinfo is None:
            parsed = dt_util.as_utc(parsed)
        now = dt_util.utcnow()
        age = int((now - parsed).total_seconds())
        return max(age, 0)

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry, self._station())

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


class CitrineStationProfileStatusSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Status sensor for profile push and dynamic session state."""

    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(
        self,
        coordinator: CitrineCoordinator,
        entry: ConfigEntry,
        station: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._station_id = str(station["id"])
        self._attr_unique_id = f"{entry.entry_id}_{self._station_id}_profile_status"
        self._attr_name = f"{self._station_id} Profile Status"

    @property
    def native_value(self) -> str:
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        status = str(prefs.get("last_profile_push_status", "idle"))
        dynamic = bool(prefs.get("dynamic_session_active", False))
        if dynamic and status == "idle":
            return "dynamic_ready"
        return status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        prefs = self.coordinator.get_station_profile_preferences(self._station_id)
        capabilities = self.coordinator.get_station_capabilities(self._station_id)
        return {
            "dynamic_session_active": bool(prefs.get("dynamic_session_active", False)),
            "profile_kind": prefs.get("profile_kind"),
            "profile_purpose": prefs.get("profile_purpose"),
            "limit": prefs.get("limit"),
            "setpoint": prefs.get("setpoint"),
            "discharge_limit": prefs.get("discharge_limit"),
            "unit": prefs.get("unit"),
            "operation_mode": prefs.get("operation_mode"),
            "last_profile_push_at": prefs.get("last_profile_push_at"),
            "last_profile_push_error": prefs.get("last_profile_push_error"),
            "ems_profile_supported": bool(
                capabilities.get("supports_ems_profile_control", False)
            ),
            "ems_profile_support_reason": capabilities.get("ems_profile_support_reason"),
            "advertised_capabilities": capabilities.get("advertised_capabilities", []),
            "supports_dynamic_profiles": bool(
                capabilities.get("supports_dynamic_profiles", False)
            ),
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry, self._station())

    def _station(self) -> dict[str, Any]:
        for station in self.coordinator.data.get("stations", []):
            if str(station.get("id")) == self._station_id:
                return station
        return {"id": self._station_id}


def _device_info(entry: ConfigEntry, station: dict[str, Any]) -> DeviceInfo:
    tenant = station.get("tenantId", entry.data.get(CONF_TENANT_ID, 1))
    station_id = str(station.get("id", "unknown"))
    return DeviceInfo(
        identifiers={(DOMAIN, f"{tenant}:{station_id}")},
        name=f"Citrine Charger {station_id}",
        manufacturer=station.get("chargePointVendor") or "Unknown",
        model=station.get("chargePointModel") or station.get("protocol"),
        sw_version=station.get("firmwareVersion"),
    )


class CitrineEmsIntakeTotalSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Integration-level EMS intake telemetry total count."""

    _attr_icon = "mdi:chart-bar"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ems_intake_total"
        self._attr_name = "Citrine EMS Intake Total"

    @property
    def native_value(self) -> int:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        return int(telemetry.get("total", 0) or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        by_reason = telemetry.get("byReasonCode", {})
        fetched_at = telemetry.get("fetchedAt")
        last_success_at = telemetry.get("lastSuccessAt")
        return {
            "accepted": int(telemetry.get("accepted", 0) or 0),
            "rejected": int(telemetry.get("rejected", 0) or 0),
            "site_id": telemetry.get("siteId"),
            "limit": int(telemetry.get("limit", 0) or 0),
            "stale_after_seconds": int(telemetry.get("staleAfterSeconds", 0) or 0),
            "latest_created_at": telemetry.get("latestCreatedAt"),
            "fetched_at": fetched_at,
            "last_success_at": last_success_at,
            "by_reason_code": by_reason if isinstance(by_reason, dict) else {},
            "telemetry_error": telemetry.get("error"),
        }


class CitrineEmsTelemetryFreshnessSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Integration-level EMS telemetry freshness in seconds since last success."""

    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "s"

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ems_telemetry_freshness_seconds"
        self._attr_name = "Citrine EMS Telemetry Freshness"

    @property
    def native_value(self) -> int | None:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        last_success_at = telemetry.get("lastSuccessAt")
        if not last_success_at:
            return None

        parsed = dt_util.parse_datetime(str(last_success_at))
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)

        now = dt_util.utcnow()
        delta = (now - parsed).total_seconds()
        return max(0, int(delta))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        return {
            "fetched_at": telemetry.get("fetchedAt"),
            "last_success_at": telemetry.get("lastSuccessAt"),
            "stale_after_seconds": int(telemetry.get("staleAfterSeconds", 0) or 0),
            "telemetry_error": telemetry.get("error"),
        }


class CitrineEmsTelemetryHealthStatusSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Integration-level EMS telemetry health status."""

    _attr_icon = "mdi:heart-pulse"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ems_telemetry_health_status"
        self._attr_name = "Citrine EMS Telemetry Health"

    @property
    def native_value(self) -> str:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        if telemetry.get("error"):
            return "error"

        freshness = self._freshness_seconds(telemetry)
        if freshness is None:
            return "unknown"

        stale_after_seconds = int(telemetry.get("staleAfterSeconds", 0) or 0)
        if stale_after_seconds > 0 and freshness > stale_after_seconds:
            return "stale"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        return {
            "freshness_seconds": self._freshness_seconds(telemetry),
            "stale_after_seconds": int(telemetry.get("staleAfterSeconds", 0) or 0),
            "fetched_at": telemetry.get("fetchedAt"),
            "last_success_at": telemetry.get("lastSuccessAt"),
            "telemetry_error": telemetry.get("error"),
        }

    @staticmethod
    def _freshness_seconds(telemetry: dict[str, Any]) -> int | None:
        last_success_at = telemetry.get("lastSuccessAt")
        if not last_success_at:
            return None

        parsed = dt_util.parse_datetime(str(last_success_at))
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.UTC)

        now = dt_util.utcnow()
        delta = (now - parsed).total_seconds()
        return max(0, int(delta))


class CitrineEmsIntakeAcceptedSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Integration-level EMS accepted intake count."""

    _attr_icon = "mdi:check-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ems_intake_accepted"
        self._attr_name = "Citrine EMS Intake Accepted"

    @property
    def native_value(self) -> int:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        return int(telemetry.get("accepted", 0) or 0)


class CitrineEmsIntakeRejectedSensor(CoordinatorEntity[CitrineCoordinator], SensorEntity):
    """Integration-level EMS rejected intake count."""

    _attr_icon = "mdi:alert-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: CitrineCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ems_intake_rejected"
        self._attr_name = "Citrine EMS Intake Rejected"

    @property
    def native_value(self) -> int:
        telemetry = self.coordinator.data.get("intake_telemetry", {})
        return int(telemetry.get("rejected", 0) or 0)


def _site_device_info(entry: ConfigEntry) -> DeviceInfo:
    tenant = entry.data.get(CONF_TENANT_ID, 1)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{tenant}:site_controller")},
        name="Citrine Energy Controller Hub",
        manufacturer="CitrineOS",
        model="Local Load Controller",
        sw_version="0.2.0",
    )


class CitrineLoadControllerStateSensor(SensorEntity):
    """Real-time operation state of the energy controller."""

    _attr_icon = "mdi:shield-sync"

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_controller_state"
        self._attr_name = "Citrine Controller State"

    @property
    def native_value(self) -> str:
        return self._controller.state_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "mode": self._controller.mode,
            "last_run": self._controller.last_run_timestamp,
            "last_error": self._controller.last_error,
            "telemetry_health": self._controller.telemetry_health,
            "telemetry_error": self._controller.telemetry_error,
            "last_command_status": self._controller.last_command_status,
            "last_command_station_id": self._controller.last_command_station_id,
            "last_command_limit_w": self._controller.last_command_limit_w,
            "last_command_timestamp": self._controller.last_command_timestamp,
            "last_command_error": self._controller.last_command_error,
            "active_ev_count": self._controller.active_ev_count,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineLastCommandStatusSensor(SensorEntity):
    """Status and details of the most recent CitrineOS limit command."""

    _attr_icon = "mdi:transmission-tower-export"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_command_status"
        self._attr_name = "Citrine Last Command Status"

    @property
    def native_value(self) -> str:
        return self._controller.last_command_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "station_id": self._controller.last_command_station_id,
            "limit_w": self._controller.last_command_limit_w,
            "timestamp": self._controller.last_command_timestamp,
            "error": self._controller.last_command_error,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineAllocatedEvPowerSensor(SensorEntity):
    """Total allocated power for active EV charging."""

    _attr_icon = "mdi:ev-station"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_allocated_ev_power"
        self._attr_name = "Citrine Allocated EV Power"

    @property
    def native_value(self) -> float:
        return round(self._controller.allocated_ev_power_w, 1)

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineSiteHeadroomPowerSensor(SensorEntity):
    """Remaining site power headroom before reaching main fuse limit."""

    _attr_icon = "mdi:speedometer"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_site_headroom_power"
        self._attr_name = "Citrine Site Headroom Power"

    @property
    def native_value(self) -> float:
        return round(self._controller.site_headroom_w, 1)

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineSolarSurplusPowerSensor(SensorEntity):
    """Available excess solar generation for EV charging."""

    _attr_icon = "mdi:solar-power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_surplus_power"
        self._attr_name = "Citrine Solar Surplus Power"

    @property
    def native_value(self) -> float:
        return round(self._controller.solar_surplus_w, 1)

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineActiveEvCountSensor(SensorEntity):
    """Count of currently active EVs connected and managed."""

    _attr_icon = "mdi:car-electric"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_active_ev_count"
        self._attr_name = "Citrine Active EV Count"

    @property
    def native_value(self) -> int:
        return self._controller.active_ev_count

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrineMqttIntentStatusSensor(SensorEntity):
    """Diagnostic sensor for real-time MQTT intent bus publishing status."""

    _attr_icon = "mdi:transit-connection-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_mqtt_intent_status"
        self._attr_name = "Citrine MQTT Intent Status"

    @property
    def native_value(self) -> str:
        pub = self._controller.mqtt_publisher
        if not pub.is_mqtt_available():
            return "unavailable"
        return "streaming" if pub.last_publish_success else "error"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pub = self._controller.mqtt_publisher
        return {
            "intent_topic": pub.intent_topic,
            "site_id": pub.site_id,
            "ttl_seconds": pub.ttl_seconds,
            "publish_count": pub.publish_count,
            "publish_failure_count": pub.publish_failure_count,
            "last_attempt_count": pub.last_attempt_count,
            "last_published_at": pub.last_published_at,
            "last_publish_success": pub.last_publish_success,
            "last_error": pub.last_error,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrinePhaseUnbalanceSensor(SensorEntity):
    """Real-time phase-to-phase current unbalance."""

    _attr_icon = "mdi:scale-unbalanced"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(self, controller: CitrineLoadController, entry: ConfigEntry) -> None:
        self._controller = controller
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_phase_unbalance_a"
        self._attr_name = "Citrine Phase Current Unbalance"

    @property
    def native_value(self) -> float:
        return self._controller.phase_unbalance_a

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "phase_a_current_a": self._controller.phase_a_current_a,
            "phase_b_current_a": self._controller.phase_b_current_a,
            "phase_c_current_a": self._controller.phase_c_current_a,
        }

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


class CitrinePhaseCurrentSensor(SensorEntity):
    """Grid phase current sensor for an individual phase."""

    _attr_icon = "mdi:current-ac"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    def __init__(
        self,
        controller: CitrineLoadController,
        entry: ConfigEntry,
        phase_name: str,
    ) -> None:
        self._controller = controller
        self._entry = entry
        self._phase_name = phase_name.upper()
        self._attr_unique_id = f"{entry.entry_id}_phase_{self._phase_name.lower()}_current"
        self._attr_name = f"Citrine Phase {self._phase_name} Current"

    @property
    def native_value(self) -> float:
        if self._phase_name == "A":
            return round(self._controller.phase_a_current_a, 1)
        elif self._phase_name == "B":
            return round(self._controller.phase_b_current_a, 1)
        elif self._phase_name == "C":
            return round(self._controller.phase_c_current_a, 1)
        return 0.0

    @property
    def device_info(self) -> DeviceInfo:
        return _site_device_info(self._entry)


