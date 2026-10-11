"""Coordinator for Citrine charger discovery and state."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .citrine_api import CitrineApiError, CitrineClient
from .const import (
    CONF_EMS_ENDPOINT_PREFIX,
    CONF_EMS_TELEMETRY_LIMIT,
    CONF_EMS_TELEMETRY_SITE_ID,
    CONF_EMS_TELEMETRY_STALE_SECS,
    CONF_HASURA_QUERY,
    CONF_HASURA_URL,
    CONF_SCAN_INTERVAL,
    CONF_TENANT_ID,
    DEFAULT_HASURA_QUERY,
    DEFAULT_PROFILE_DURATION,
    DEFAULT_PROFILE_DISCHARGE_LIMIT,
    DEFAULT_PROFILE_KIND,
    DEFAULT_PROFILE_LIMIT,
    DEFAULT_PROFILE_OPERATION_MODE,
    DEFAULT_PROFILE_SETPOINT,
    DEFAULT_PROFILE_PURPOSE,
    DEFAULT_PROFILE_STACK_LEVEL,
    DEFAULT_PROFILE_UNIT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_EMS_ENDPOINT_PREFIX,
    DEFAULT_EMS_TELEMETRY_LIMIT,
    DEFAULT_EMS_TELEMETRY_STALE_SECS,
)
from .hasura_client import HasuraAuthError, HasuraClient, HasuraError
from .compatibility import (
    CONF_API_PROFILE, PROFILE_V2, PROFILE_LAB, V2_PROFILES, V2_DISCOVERY_QUERY, normalize_v2_discovery,
)

_LOGGER = logging.getLogger(__name__)


class CitrineCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data coordinator for discovered charging stations."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        hasura_client: HasuraClient | None,
        citrine_client: CitrineClient,
        entry_data: dict[str, Any],
        entry_options: dict[str, Any],
        config_entry: ConfigEntry | None = None,
    ) -> None:
        scan_interval = entry_options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name="citrine_ha",
            update_interval=timedelta(seconds=scan_interval),
            config_entry=config_entry,
        )
        self._hasura_client = hasura_client
        self._citrine_client = citrine_client
        self._entry_data = entry_data
        self._entry_options = entry_options
        self._protocol_cache: dict[str, str] = {}
        self._capability_cache: dict[str, dict[str, Any]] = {}
        self._profile_prefs: dict[str, dict[str, Any]] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        tenant_id = int(self._entry_data[CONF_TENANT_ID])
        try:
            return await self._fetch_discovery(tenant_id)
        except (CitrineApiError, HasuraError, ValueError) as err:
            # HA retains last data but marks entities unavailable. Never report
            # cached or empty discovery as a successful refresh after failure.
            raise UpdateFailed(str(err)) from err

    async def _fetch_discovery(self, tenant_id: int) -> dict[str, Any]:
        if self._entry_data.get(CONF_API_PROFILE) in V2_PROFILES:
            if not self._hasura_client:
                raise ValueError("CitrineOS 2.x requires Hasura discovery")
            await self._citrine_client.ping()
            result = await self._hasura_client.query(
                V2_DISCOVERY_QUERY, variables={"tenantId": tenant_id})
            rows = normalize_v2_discovery(result, tenant_id)
            merged = self._merge_station_state(**rows)
            if self._entry_data.get(CONF_API_PROFILE) == PROFILE_LAB:
                from .lab_control import STATION
                from .lab_telemetry import fetch_meter
                for station in merged:
                    if station["id"] == STATION:
                        station["lab_meter"] = await fetch_meter(self._hasura_client, station, tenant_id)
                        self._citrine_client.observe_transaction(station)
            self._refresh_station_caches(merged)
            # Protocol is not a capability declaration. Keep all unverified
            # stations read-only, even in an entry with scoped lab controls.
            from .lab_control import STATION, PROTOCOL
            for station in merged:
                allowed = (self._entry_data.get(CONF_API_PROFILE) == PROFILE_LAB
                           and not self._citrine_client.read_only
                           and station["id"] == STATION and station.get("protocol") == PROTOCOL)
                self._capability_cache[station["id"]] = {
                    "supports_remote_start": allowed, "supports_remote_stop": allowed,
                    "supports_set_charging_profile": allowed,
                    "supports_clear_charging_profile": allowed,
                    "supports_bidirectional_power_transfer": allowed,
                    "supports_ems_profile_control": False,
                    "ems_profile_support_reason": "Automatic site controller disabled",
                    "capability_evidence": "scoped_lab_policy" if allowed else "not_verified",
                }
            return {**rows, "stations": merged, "source": "hasura_v2",
                    "read_only": self._citrine_client.read_only,
                    "api_access": "protected_read_verified",
                    "station_control": "not_verified",
                    "intake_telemetry": self._telemetry_defaults(tenant_id, None)}

        intake_telemetry = await self._fetch_intake_telemetry_with_fallback(tenant_id)
        # A configured discovery backend is authoritative, including empty data.
        if self._hasura_client:
            hasura_payload = await self._try_hasura_discovery(tenant_id, intake_telemetry)
            if hasura_payload is not None:
                return hasura_payload
            raise HasuraError("Hasura discovery did not return a usable collection")
        rows = await self._citrine_client.discover_chargers_rest()
        merged = self._merge_station_state(**rows)
        self._refresh_station_caches(merged)
        return {**rows, "stations": merged, "intake_telemetry": intake_telemetry,
                "source": "citrine_rest"}

    async def _try_hasura_discovery(
        self,
        tenant_id: int,
        intake_telemetry: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Execute Hasura GraphQL discovery query with automatic schema adaptation."""
        if not self._hasura_client:
            return None

        query = self._entry_options.get(CONF_HASURA_QUERY) or self._entry_data.get(
            CONF_HASURA_QUERY,
            DEFAULT_HASURA_QUERY,
        )

        active_query = query
        result: dict[str, Any] | None = None
        last_hasura_error: HasuraError | None = None
        for _attempt in range(4):
            try:
                result = await self._hasura_client.query(
                    active_query,
                    variables={"tenantId": tenant_id},
                )
                break
            except HasuraAuthError:
                raise
            except HasuraError as err:
                last_hasura_error = err
                if "not a valid graphql query" in str(err).lower() and active_query != DEFAULT_HASURA_QUERY:
                    active_query = DEFAULT_HASURA_QUERY
                    continue
                fallback_query = self._query_without_missing_fields(active_query, err)
                if fallback_query == active_query:
                    break
                active_query = fallback_query

        if result is None:
            message = "Hasura discovery failed after schema fallback retries"
            if last_hasura_error is not None:
                message = f"Hasura discovery failed: {last_hasura_error}"
            minimal_result = await self._try_minimal_discovery_query(tenant_id)
            if minimal_result is not None:
                data = minimal_result.get("data", {})
                stations = self._extract_stations(data)
                connectors = self._extract_connectors(data)
                merged_stations = self._merge_station_state(
                    stations=stations,
                    connectors=connectors,
                    transactions=[],
                )
                if merged_stations:
                    self._refresh_station_caches(merged_stations)
                    return {
                        "stations": merged_stations,
                        "connectors": connectors,
                        "transactions": [],
                        "intake_telemetry": intake_telemetry,
                        "source": "hasura_minimal",
                        "hasura_url": self._entry_data.get(CONF_HASURA_URL),
                        "hasura_error": message,
                    }
            return None

        data = result.get("data", {})
        stations = self._extract_stations(data)
        connectors = self._extract_connectors(data)
        transactions = self._extract_transactions(data)
        merged_stations = self._merge_station_state(
            stations=stations,
            connectors=connectors,
            transactions=transactions,
        )
        if isinstance(data.get("ChargingStations"), list) or merged_stations:
            self._refresh_station_caches(merged_stations)
            return {
                "stations": merged_stations,
                "connectors": connectors,
                "transactions": transactions,
                "intake_telemetry": intake_telemetry,
                "source": "hasura",
                "hasura_url": self._entry_data.get(CONF_HASURA_URL),
            }
        return None

    def _fallback_discovery_payload(
        self,
        intake_telemetry: dict[str, Any],
        message: str,
    ) -> dict[str, Any]:
        previous = self.data or {}
        stations = [item for item in previous.get("stations", []) if isinstance(item, dict)]
        connectors = [item for item in previous.get("connectors", []) if isinstance(item, dict)]
        transactions = [
            item for item in previous.get("transactions", []) if isinstance(item, dict)
        ]

        return {
            "stations": stations,
            "connectors": connectors,
            "transactions": transactions,
            "intake_telemetry": intake_telemetry,
            "source": "hasura_error",
            "hasura_url": self._entry_data.get(CONF_HASURA_URL),
            "hasura_error": message,
        }

    async def _try_minimal_discovery_query(self, tenant_id: int) -> dict[str, Any] | None:
        if not self._hasura_client:
            return None

        minimal_query = (
            "query ChargingStations($tenantId: Int!) {"
            " ChargingStations(where: {tenantId: {_eq: $tenantId}}) {"
            " id protocol isOnline chargePointVendor chargePointModel chargePointSerialNumber"
            " firmwareVersion tenantId locationId updatedAt latestOcppMessageTimestamp capabilities"
            " }"
            " Connectors(where: {tenantId: {_eq: $tenantId}}) {"
            " id stationId chargingStationId connectorId evseId status isOnline updatedAt"
            " }"
            "}"
        )

        try:
            return await self._hasura_client.query(
                minimal_query,
                variables={"tenantId": tenant_id},
            )
        except HasuraAuthError:
            raise
        except HasuraError as err:
            _LOGGER.debug("Minimal Hasura discovery query also failed: %s", err)
            return None

    async def async_refresh_intake_telemetry(
        self,
        site_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Refresh only EMS intake telemetry and push coordinator update."""
        if not self.last_update_success:
            raise UpdateFailed("Refresh discovery before updating telemetry")
        tenant_id = int(self._entry_data[CONF_TENANT_ID])
        intake_telemetry = await self._fetch_intake_telemetry_with_fallback(
            tenant_id,
            site_id=site_id,
            limit=limit,
        )

        current = dict(self.data or {})
        current["intake_telemetry"] = intake_telemetry
        self.async_set_updated_data(current)
        return intake_telemetry

    def clear_intake_telemetry_error(self) -> None:
        """Clear cached telemetry error without forcing a refetch."""
        if not self.last_update_success:
            raise UpdateFailed("Refresh discovery before clearing telemetry errors")
        current = dict(self.data or {})
        telemetry = dict(current.get("intake_telemetry") or {})
        if not telemetry:
            return
        telemetry["error"] = None
        current["intake_telemetry"] = telemetry
        self.async_set_updated_data(current)

    def _telemetry_defaults(self, tenant_id: int, site_id: str | None) -> dict[str, Any]:
        return {
            "tenantId": tenant_id,
            "siteId": site_id,
            "total": 0,
            "accepted": 0,
            "rejected": 0,
            "byReasonCode": {},
            "latestCreatedAt": None,
            "fetchedAt": None,
            "lastSuccessAt": None,
            "error": None,
        }

    def _telemetry_scope_site(self, site_id: str | None = None) -> str | None:
        if site_id and str(site_id).strip():
            return str(site_id).strip()
        configured = self._entry_options.get(CONF_EMS_TELEMETRY_SITE_ID)
        if configured and str(configured).strip():
            return str(configured).strip()
        return None

    def _telemetry_scope_limit(self, limit: int | None = None) -> int:
        if limit is not None:
            return max(1, min(int(limit), 2000))

        configured = self._entry_options.get(
            CONF_EMS_TELEMETRY_LIMIT,
            self._entry_data.get(CONF_EMS_TELEMETRY_LIMIT, DEFAULT_EMS_TELEMETRY_LIMIT),
        )
        try:
            return max(1, min(int(configured), 2000))
        except (TypeError, ValueError):
            return DEFAULT_EMS_TELEMETRY_LIMIT

    def telemetry_stale_after_seconds(self) -> int:
        configured = self._entry_options.get(
            CONF_EMS_TELEMETRY_STALE_SECS,
            self._entry_data.get(
                CONF_EMS_TELEMETRY_STALE_SECS,
                DEFAULT_EMS_TELEMETRY_STALE_SECS,
            ),
        )
        try:
            return max(5, min(int(configured), 86400))
        except (TypeError, ValueError):
            return DEFAULT_EMS_TELEMETRY_STALE_SECS

    async def _fetch_intake_telemetry_with_fallback(
        self,
        tenant_id: int,
        *,
        site_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        fetched_at = datetime.now(UTC).isoformat()
        if self._entry_data.get(CONF_API_PROFILE) in V2_PROFILES:
            result = self._telemetry_defaults(tenant_id, site_id)
            result["error"] = "EMS intake telemetry is not supported by the 2.x read-only profile"
            return result
        endpoint_prefix = str(
            self._entry_options.get(CONF_EMS_ENDPOINT_PREFIX, DEFAULT_EMS_ENDPOINT_PREFIX)
            or DEFAULT_EMS_ENDPOINT_PREFIX
        )
        scoped_site_id = self._telemetry_scope_site(site_id)
        scoped_limit = self._telemetry_scope_limit(limit)
        intake_telemetry = self._telemetry_defaults(tenant_id, scoped_site_id)
        previous_last_success = None
        if self.data and isinstance(self.data.get("intake_telemetry"), dict):
            previous_last_success = self.data["intake_telemetry"].get("lastSuccessAt")

        intake_telemetry["limit"] = scoped_limit
        intake_telemetry["staleAfterSeconds"] = self.telemetry_stale_after_seconds()
        intake_telemetry["fetchedAt"] = fetched_at
        intake_telemetry["lastSuccessAt"] = previous_last_success

        try:
            intake_telemetry = {
                **intake_telemetry,
                **(
                    await self._citrine_client.get_ems_intake_telemetry(
                        endpoint_prefix=endpoint_prefix,
                        site_id=scoped_site_id,
                        limit=scoped_limit,
                    )
                ),
                "fetchedAt": fetched_at,
                "lastSuccessAt": fetched_at,
                "error": None,
            }
        except CitrineApiError as err:
            error_message = str(err)
            intake_telemetry["error"] = error_message
            if "failed (404)" in error_message and "emsIntakeTelemetry" in error_message:
                _LOGGER.info(
                    "EMS intake telemetry endpoint unavailable (404). "
                    "Check Citrine EMS module enablement and ems_endpoint_prefix. %s",
                    err,
                )
            else:
                _LOGGER.warning("EMS intake telemetry unavailable: %s", err)

        return intake_telemetry

    def get_station_protocol(self, station_id: str, fallback: str | None = None) -> str | None:
        station_key = str(station_id)
        if station_key in self._protocol_cache:
            return self._protocol_cache[station_key]
        return fallback

    def get_station_capabilities(self, station_id: str) -> dict[str, Any]:
        station_key = str(station_id)
        return dict(self._capability_cache.get(station_key, {}))

    def get_station_profile_preferences(self, station_id: str) -> dict[str, Any]:
        station_key = str(station_id)
        defaults = {
            "limit": DEFAULT_PROFILE_LIMIT,
            "setpoint": DEFAULT_PROFILE_SETPOINT,
            "discharge_limit": DEFAULT_PROFILE_DISCHARGE_LIMIT,
            "unit": DEFAULT_PROFILE_UNIT,
            "duration": DEFAULT_PROFILE_DURATION,
            "evse_id": 0,
            "stack_level": DEFAULT_PROFILE_STACK_LEVEL,
            "profile_id": None,
            "profile_purpose": DEFAULT_PROFILE_PURPOSE,
            "profile_kind": DEFAULT_PROFILE_KIND,
            "operation_mode": DEFAULT_PROFILE_OPERATION_MODE,
            "profile_periods": None,
            "profile_sign_mode": "normal",
            "profile_tx_mode": "safe_fallback",
            "dynamic_session_active": False,
            "last_profile_push_status": "idle",
            "last_profile_push_at": None,
            "last_profile_push_error": None,
        }
        if station_key not in self._profile_prefs:
            self._profile_prefs[station_key] = dict(defaults)
        return {**defaults, **self._profile_prefs[station_key]}

    def update_station_profile_preferences(self, station_id: str, **kwargs: Any) -> None:
        station_key = str(station_id)
        prefs = self.get_station_profile_preferences(station_key)
        for key, value in kwargs.items():
            if value is not None:
                prefs[key] = value
        self._profile_prefs[station_key] = prefs

    def mark_profile_push_started(self, station_id: str) -> None:
        self.update_station_profile_preferences(
            station_id,
            last_profile_push_status="pending",
            last_profile_push_error=None,
        )

    def mark_profile_push_succeeded(self, station_id: str) -> None:
        self.update_station_profile_preferences(
            station_id,
            last_profile_push_status="applied",
            last_profile_push_error=None,
            last_profile_push_at=datetime.now(UTC).isoformat(),
        )

    def mark_profile_push_failed(self, station_id: str, error: str) -> None:
        self.update_station_profile_preferences(
            station_id,
            last_profile_push_status="failed",
            last_profile_push_error=str(error)[:300],
            last_profile_push_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    def _extract_stations(data: dict[str, Any]) -> list[dict[str, Any]]:
        # Default Hasura table naming can vary by configuration.
        for key in ("ChargingStations", "chargingStations", "charging_stations"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _query_without_missing_fields(query: str, error: HasuraError) -> str:
        updated = query
        for item in error.errors:
            message = item.get("message", "")

            missing_selection = re.search(r"missing selection set for '([^']+)'", message)
            if missing_selection:
                block_name = missing_selection.group(1)
                updated = CitrineCoordinator._remove_root_selection_block(updated, block_name)
                continue

            match = re.search(r"field '([^']+)' not found in type: '([^']+)'", message)
            if not match:
                continue
            field_name, type_name = match.groups()

            # If Transactions shape differs, prefer dropping the block so station discovery still works.
            if type_name == "Transactions":
                updated = CitrineCoordinator._remove_root_selection_block(updated, "Transactions")
                continue

            candidate = CitrineCoordinator._remove_field_from_block(updated, type_name, field_name)
            if candidate == updated:
                candidate = CitrineCoordinator._remove_field_token(candidate, field_name)
            updated = candidate
        return " ".join(updated.split())

    @staticmethod
    def _remove_field_from_block(query: str, type_name: str, field_name: str) -> str:
        pattern = re.compile(
            rf"({re.escape(type_name)}(?:\s*\([^)]*\))?\s*\{{)([^}}]*)(\}})"
        )

        def _replace(match: re.Match[str]) -> str:
            prefix, body, suffix = match.groups()
            fields = [field for field in body.split() if field != field_name]
            return f"{prefix}{' '.join(fields)}{suffix}"

        return pattern.sub(_replace, query, count=1)

    @staticmethod
    def _remove_field_token(query: str, field_name: str) -> str:
        token_pattern = re.compile(rf"\b{re.escape(field_name)}\b")
        return token_pattern.sub("", query, count=1)

    @staticmethod
    def _remove_root_selection_block(query: str, block_name: str) -> str:
        block_pattern = re.compile(
            rf"\b{re.escape(block_name)}(?:\s*\([^)]*\))?\s*\{{[^{{}}]*\}}"
        )
        return block_pattern.sub("", query, count=1)

    @staticmethod
    def _extract_connectors(data: dict[str, Any]) -> list[dict[str, Any]]:
        for key in (
            "Connectors",
            "connectors",
            "ChargingStationConnectors",
            "chargingStationConnectors",
            "evses",
            "EVSEs",
        ):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _extract_transactions(data: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("Transactions", "transactions", "TransactionEvents", "transactionEvents"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _merge_station_state(
        *,
        stations: list[dict[str, Any]],
        connectors: list[dict[str, Any]],
        transactions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        connectors_by_station: dict[str, list[dict[str, Any]]] = {}
        for connector in connectors:
            station_id = CitrineCoordinator._station_ref(connector)
            if not station_id:
                continue
            connectors_by_station.setdefault(station_id, []).append(connector)

        tx_by_station: dict[str, list[dict[str, Any]]] = {}
        for tx in transactions:
            station_id = CitrineCoordinator._station_ref(tx)
            if not station_id:
                continue
            tx_by_station.setdefault(station_id, []).append(tx)

        merged: list[dict[str, Any]] = []
        for station in stations:
            station_id = str(station.get("id", ""))
            station_connectors = connectors_by_station.get(station_id, [])
            station_transactions = tx_by_station.get(station_id, [])

            ordered_transactions = sorted(
                station_transactions,
                key=lambda tx: str(
                    tx.get("updatedAt")
                    or tx.get("stoppedAt")
                    or tx.get("endedAt")
                    or tx.get("startedAt")
                    or ""
                ),
                reverse=True,
            )

            active_tx = next(
                (
                    tx
                    for tx in ordered_transactions
                    if tx.get("isActive") is True
                    or tx.get("active") is True
                    or (
                        tx.get("stoppedAt") is None
                        and tx.get("endedAt") is None
                        and tx.get("startedAt") is not None
                    )
                ),
                None,
            )

            current_tx_id = CitrineCoordinator._transaction_ref(active_tx)
            previous_tx_id = None
            if ordered_transactions:
                previous_tx_id = CitrineCoordinator._transaction_ref(ordered_transactions[0])

            numeric_tx_ids: list[int] = []
            for tx in ordered_transactions:
                tx_id = CitrineCoordinator._transaction_ref(tx)
                if tx_id is None:
                    continue
                try:
                    numeric_tx_ids.append(int(tx_id))
                except (TypeError, ValueError):
                    continue
            next_remote_start_id = (max(numeric_tx_ids) + 1) if numeric_tx_ids else 1

            normalized_connectors = sorted(
                [
                    {
                        "id": conn.get("id"),
                        "connectorId": conn.get("connectorId") or conn.get("id"),
                        "evseId": conn.get("evseId") or conn.get("connectorId") or conn.get("id"),
                        "status": conn.get("status"),
                        "isOnline": conn.get("isOnline"),
                        "updatedAt": conn.get("updatedAt"),
                    }
                    for conn in station_connectors
                ],
                key=lambda conn: str(conn.get("evseId") or conn.get("connectorId") or ""),
            )

            default_evse_id = 1
            for conn in normalized_connectors:
                candidate = conn.get("evseId")
                try:
                    if candidate is not None:
                        default_evse_id = int(candidate)
                        break
                except (TypeError, ValueError):
                    continue

            merged.append(
                {
                    **station,
                    "connectors": normalized_connectors,
                    "activeTransactionId": current_tx_id,
                    "currentTransactionId": current_tx_id,
                    "previousTransactionId": previous_tx_id,
                    "defaultEvseId": default_evse_id,
                    "nextRemoteStartId": next_remote_start_id,
                }
            )

        return merged

    @staticmethod
    def _station_ref(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        station_id = payload.get("stationId") or payload.get("chargingStationId") or payload.get("identifier")
        if station_id is None:
            return None
        return str(station_id)

    @staticmethod
    def _transaction_ref(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        tx_id = payload.get("transactionId") or payload.get("id")
        if tx_id is None:
            return None
        return str(tx_id)

    def _refresh_station_caches(self, stations: list[dict[str, Any]]) -> None:
        self._protocol_cache.clear()
        self._capability_cache.clear()
        for station in stations:
            station_id = str(station.get("id", ""))
            if not station_id:
                continue

            protocol = self._normalize_protocol(station.get("protocol"))
            self._protocol_cache[station_id] = protocol

            capabilities = self._derive_capabilities(station, protocol)
            self._capability_cache[station_id] = capabilities

            if station_id not in self._profile_prefs:
                self._profile_prefs[station_id] = {
                    "limit": DEFAULT_PROFILE_LIMIT,
                    "setpoint": DEFAULT_PROFILE_SETPOINT,
                    "discharge_limit": DEFAULT_PROFILE_DISCHARGE_LIMIT,
                    "unit": capabilities.get("preferred_unit", DEFAULT_PROFILE_UNIT),
                    "duration": DEFAULT_PROFILE_DURATION,
                    "evse_id": int(station.get("defaultEvseId") or 0),
                    "stack_level": DEFAULT_PROFILE_STACK_LEVEL,
                    "profile_id": None,
                    "profile_purpose": capabilities.get(
                        "default_profile_purpose",
                        DEFAULT_PROFILE_PURPOSE,
                    ),
                    "profile_kind": capabilities.get("default_profile_kind", DEFAULT_PROFILE_KIND),
                    "operation_mode": capabilities.get(
                        "default_operation_mode",
                        DEFAULT_PROFILE_OPERATION_MODE,
                    ),
                    "profile_periods": None,
                    "profile_sign_mode": "normal",
                    "profile_tx_mode": "safe_fallback",
                    "dynamic_session_active": False,
                    "last_profile_push_status": "idle",
                    "last_profile_push_at": None,
                    "last_profile_push_error": None,
                }
            else:
                # Migrate old defaults that used TxProfile by default, which can fail without a transaction.
                existing = self._profile_prefs[station_id]
                if existing.get("profile_purpose") == "TxProfile":
                    existing["profile_purpose"] = capabilities.get(
                        "default_profile_purpose",
                        existing.get("profile_purpose"),
                    )
                if "dynamic_session_active" not in existing:
                    existing["dynamic_session_active"] = False
                if "last_profile_push_status" not in existing:
                    existing["last_profile_push_status"] = "idle"
                if "last_profile_push_at" not in existing:
                    existing["last_profile_push_at"] = None
                if "last_profile_push_error" not in existing:
                    existing["last_profile_push_error"] = None

    @staticmethod
    def _normalize_protocol(protocol: Any) -> str:
        value = str(protocol or "").strip().lower().replace(" ", "")
        if value in {"1.6", "ocpp1.6", "ocpp16", "ocpp-1.6"}:
            return "ocpp1.6"
        if value in {"2.1", "ocpp2.1", "ocpp21", "ocpp-2.1"}:
            return "ocpp2.1"
        if value in {"2.0", "2.0.1", "ocpp2.0", "ocpp2.0.1", "ocpp201", "ocpp-2.0.1"}:
            return "ocpp2.0.1"
        return "ocpp2.0.1"

    @staticmethod
    def _derive_capabilities(station: dict[str, Any], protocol: str) -> dict[str, Any]:
        connectors = station.get("connectors", [])
        connector_count = len(connectors)
        advertised_capabilities = [
            str(item)
            for item in (station.get("capabilities") or [])
            if isinstance(item, str) and str(item).strip()
        ]

        if protocol == "ocpp1.6":
            capabilities = {
                "supports_remote_start": True,
                "supports_remote_stop": True,
                "supports_set_charging_profile": True,
                "supports_clear_charging_profile": True,
                "supports_bidirectional_power_transfer": False,
                "allowed_units": ["A", "W"],
                "preferred_unit": "A",
                "min_profile_limit": 0.0,
                "max_profile_limit": 500000.0,
                "supported_profile_purposes": [
                    "ChargePointMaxProfile",
                    "TxDefaultProfile",
                    "TxProfile",
                ],
                "default_profile_purpose": "ChargePointMaxProfile",
                "supported_profile_kinds": ["Absolute"],
                "default_profile_kind": "Absolute",
                "supports_transaction_profile": True,
                "connector_count": connector_count,
            }
            capabilities["advertised_capabilities"] = advertised_capabilities
            capabilities["supports_ems_profile_control"] = False
            capabilities["ems_profile_support_reason"] = (
                "EMS charging profiles require OCPP 2.0.1 or 2.1"
            )
            return capabilities

        if protocol == "ocpp2.1":
            capabilities = {
                "supports_remote_start": True,
                "supports_remote_stop": True,
                "supports_set_charging_profile": True,
                "supports_clear_charging_profile": True,
                "supports_bidirectional_power_transfer": True,
                "allowed_units": ["W", "A"],
                "preferred_unit": "W",
                "min_profile_limit": -500000.0,
                "max_profile_limit": 500000.0,
                "supported_profile_purposes": [
                    "ChargingStationMaxProfile",
                    "TxDefaultProfile",
                    "TxProfile",
                    "ChargingStationExternalConstraints",
                    "PriorityCharging",
                    "LocalGeneration",
                ],
                "default_profile_purpose": "ChargingStationMaxProfile",
                "supported_profile_kinds": ["Absolute", "Relative", "Dynamic"],
                "default_profile_kind": "Dynamic",
                "supports_transaction_profile": True,
                "supports_dynamic_profiles": True,
                "supports_operation_mode_control": True,
                "supports_profile_setpoint": True,
                "supports_discharge_limit": True,
                "supported_operation_modes": [
                    "ChargingOnly",
                    "ExternalLimits",
                    "CentralSetpoint",
                    "ExternalSetpoint",
                    "LocalFrequency",
                    "LocalLoadBalancing",
                    "Idle",
                ],
                "default_operation_mode": "ChargingOnly",
                "connector_count": connector_count,
            }
            capabilities["advertised_capabilities"] = advertised_capabilities
            capabilities["supports_ems_profile_control"] = (
                not advertised_capabilities
                or "ChargingProfileCapable" in advertised_capabilities
            )
            capabilities["ems_profile_support_reason"] = (
                None
                if capabilities["supports_ems_profile_control"]
                else "Station does not advertise ChargingProfileCapable capability"
            )
            return capabilities

        capabilities = {
            "supports_remote_start": True,
            "supports_remote_stop": True,
            "supports_set_charging_profile": True,
            "supports_clear_charging_profile": True,
            "supports_bidirectional_power_transfer": True,
            "allowed_units": ["W", "A"],
            "preferred_unit": "W",
            "min_profile_limit": -500000.0,
            "max_profile_limit": 500000.0,
            "supported_profile_purposes": [
                "ChargingStationMaxProfile",
                "TxDefaultProfile",
                "TxProfile",
            ],
            "default_profile_purpose": "ChargingStationMaxProfile",
            "supported_profile_kinds": ["Absolute", "Relative"],
            "default_profile_kind": "Absolute",
            "supports_transaction_profile": True,
            "supports_dynamic_profiles": False,
            "supports_operation_mode_control": False,
            "supports_profile_setpoint": False,
            "supports_discharge_limit": False,
            "supported_operation_modes": ["ChargingOnly"],
            "default_operation_mode": "ChargingOnly",
            "connector_count": connector_count,
        }
        capabilities["advertised_capabilities"] = advertised_capabilities
        capabilities["supports_ems_profile_control"] = (
            not advertised_capabilities or "ChargingProfileCapable" in advertised_capabilities
        )
        capabilities["ems_profile_support_reason"] = (
            None
            if capabilities["supports_ems_profile_control"]
            else "Station does not advertise ChargingProfileCapable capability"
        )
        return capabilities
