"""Async client for CitrineOS APIs."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
import random
import re
from collections.abc import Mapping
from typing import Any

import aiohttp
from .compatibility import PROFILE_LEGACY, PROFILE_V2

_LOGGER = logging.getLogger(__name__)


class CitrineApiError(Exception):
    """Base API error."""


class CitrineAuthError(CitrineApiError):
    """Authentication failed."""

class CitrineCompatibilityError(CitrineApiError):
    """Server API or security contract does not match the selected profile."""


class CitrineReadOnlyError(CitrineApiError):
    """Control is disabled for this entry."""


class CitrineClient:
    """CitrineOS API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        tenant_id: int,
        auth_token: str | None,
        verify_ssl: bool,
        request_timeout: int,
        api_profile: str = PROFILE_LEGACY,
        read_only: bool = False,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._tenant_id = tenant_id
        self._auth_token = auth_token
        self._verify_ssl = verify_ssl
        self._request_timeout = request_timeout
        if api_profile not in (PROFILE_LEGACY, PROFILE_V2):
            raise CitrineCompatibilityError("Unsupported API profile")
        self.api_profile = api_profile
        self.read_only = read_only or api_profile == PROFILE_V2

    @staticmethod
    def normalize_protocol(protocol: str | None) -> str:
        """Normalize protocol labels to supported API families."""
        if not protocol:
            return "ocpp2.0.1"

        value = str(protocol).strip().lower().replace(" ", "")
        if value in {"1.6", "ocpp1.6", "ocpp16", "ocpp-1.6"}:
            return "ocpp1.6"
        if value in {"2.1", "ocpp2.1", "ocpp21", "ocpp-2.1"}:
            return "ocpp2.1"
        if value in {"2.0", "2.0.1", "ocpp2.0", "ocpp2.0.1", "ocpp201", "ocpp-2.0.1"}:
            return "ocpp2.0.1"
        return "ocpp2.0.1"

    async def ping(self) -> None:
        """Validate readiness separately from protected read access."""
        if self.api_profile == PROFILE_LEGACY:
            await self._request("GET", "/data/ocpprouter/systemConfig")
            return
        health = await self._request("GET", "/health/ready")
        if not isinstance(health, dict) or health.get("status") != "pass":
            raise CitrineCompatibilityError("CitrineOS is not ready")
        if not self._auth_token:
            raise CitrineAuthError("The 2.x profile requires an API bearer token")
        # A public health endpoint does not establish authentication. Verify a
        # read-only protected route rejects anonymous access before using a token.
        params = {"tenantId": self._tenant_id}
        try:
            await self._request("GET", "/commands/tariff", params=params, send_auth=False)
        except CitrineAuthError:
            pass
        else:
            raise CitrineCompatibilityError("API protected read permits anonymous access")
        rows = await self._request("GET", "/commands/tariff", params=params)
        if not isinstance(rows, list):
            raise CitrineCompatibilityError("Unexpected protected-read response")

    async def get_charging_stations(self) -> list[dict[str, Any]]:
        """Fetch charging stations directly from CitrineOS REST data endpoints."""
        params = {"tenantId": self._tenant_id}
        paths = [
            "/data/configuration/chargingStation",
            "/data/chargingStation",
            "/data/ocpprouter/chargingStation",
            "/data/monitoring/chargingStation",
            "/data/configuration/chargingStations",
            "/data/chargingStations",
        ]
        try:
            resp = await self._request_with_fallback_paths("GET", paths, params=params)
            return self._extract_items_from_response(resp)
        except CitrineApiError as err:
            raise

    async def get_connectors(self) -> list[dict[str, Any]]:
        """Fetch connectors/EVSEs directly from CitrineOS REST data endpoints."""
        params = {"tenantId": self._tenant_id}
        paths = [
            "/data/configuration/connector",
            "/data/configuration/evse",
            "/data/connector",
            "/data/evse",
            "/data/configuration/connectors",
            "/data/connectors",
        ]
        try:
            resp = await self._request_with_fallback_paths("GET", paths, params=params)
            return self._extract_items_from_response(resp)
        except CitrineApiError as err:
            raise

    async def get_transactions(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Fetch active/recent transactions directly from CitrineOS REST data endpoints."""
        params = {"tenantId": self._tenant_id, "limit": max(1, min(limit, 2000))}
        paths = [
            "/data/transactions/transaction",
            "/data/transactions",
            "/data/transactions/activeTransactions",
            "/data/transaction",
        ]
        try:
            resp = await self._request_with_fallback_paths("GET", paths, params=params)
            return self._extract_items_from_response(resp)
        except CitrineApiError as err:
            raise

    async def discover_chargers_rest(self) -> dict[str, Any]:
        """Perform full discovery via CitrineOS REST endpoints (no Hasura required)."""
        if self.api_profile == PROFILE_V2:
            raise CitrineCompatibilityError("CitrineOS 2.x discovery requires Hasura")
        stations = await self.get_charging_stations()
        connectors = await self.get_connectors()
        transactions = await self.get_transactions()
        return {
            "stations": stations,
            "connectors": connectors,
            "transactions": transactions,
        }

    @staticmethod
    def _extract_items_from_response(response: Any) -> list[dict[str, Any]]:
        """Normalize varied REST responses (list, dict wrapper, etc.) into a list of dicts."""
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            for key in ("data", "items", "rows", "chargingStations", "connectors", "transactions"):
                candidate = response.get(key)
                if isinstance(candidate, list):
                    return [item for item in candidate if isinstance(item, dict)]
            # If the response is a single object representing an entity
            if "id" in response or "stationId" in response or "identifier" in response:
                return [response]
        return []

    async def get_ems_intake_telemetry(
        self,
        *,
        site_id: str | None = None,
        limit: int = 500,
        endpoint_prefix: str = "ems",
    ) -> dict[str, Any]:
        """Fetch EMS intake telemetry summary from CitrineOS EMS Data API."""
        params: dict[str, Any] = {
            "tenantId": self._tenant_id,
            "limit": max(1, min(int(limit), 2000)),
        }
        if site_id:
            params["siteId"] = site_id

        clean_prefix = str(endpoint_prefix or "ems").strip("/")
        return await self._request(
            "GET",
            f"/data/{clean_prefix}/emsIntakeTelemetry",
            params=params,
        )

    async def request_start_transaction(
        self,
        *,
        protocol: str,
        station_id: str,
        id_tag: str,
        evse_id: int | None = None,
        remote_start_id: int | None = None,
    ) -> Any:
        """Start remote charging for 1.6 or 2.x stations."""
        protocol = self.normalize_protocol(protocol)

        if protocol == "ocpp1.6":
            payload: dict[str, Any] = {"idTag": id_tag}
            if evse_id is not None:
                payload["connectorId"] = evse_id
            paths = ["/ocpp/1.6/evdriver/remoteStartTransaction"]
        else:
            payload = {
                "remoteStartId": remote_start_id or random.randint(100000, 999999),
                "idToken": {"idToken": id_tag, "type": "Local"},
            }
            if evse_id is not None:
                payload["evseId"] = evse_id
            paths = self._ocpp2_paths(protocol, "evdriver/requestStartTransaction")

        params = self._identifier_params(station_id)
        response = await self._request_with_fallback_paths(
            "POST",
            paths,
            params=params,
            json=payload,
        )
        return self._validate_command_response(
            action="request_start_transaction",
            station_id=station_id,
            response=response,
        )

    async def request_stop_transaction(
        self,
        *,
        protocol: str,
        station_id: str,
        transaction_id: str,
    ) -> Any:
        """Stop remote charging for 1.6 or 2.x stations."""
        protocol = self.normalize_protocol(protocol)

        if protocol == "ocpp1.6":
            try:
                v16_txn: int | str = int(transaction_id)
            except ValueError:
                v16_txn = transaction_id
            payload = {"transactionId": v16_txn}
            paths = ["/ocpp/1.6/evdriver/remoteStopTransaction"]
        else:
            payload = {"transactionId": str(transaction_id)}
            paths = self._ocpp2_paths(protocol, "evdriver/requestStopTransaction")

        params = self._identifier_params(station_id)
        try:
            response = await self._request_with_fallback_paths(
                "POST",
                paths,
                params=params,
                json=payload,
            )
            return self._validate_command_response(
                action="request_stop_transaction",
                station_id=station_id,
                response=response,
            )
        except CitrineApiError as err:
            # Some OCPP 2.x backends only accept integer-like transaction identifiers.
            if protocol != "ocpp1.6":
                try:
                    int_tx_id = int(transaction_id)
                except (TypeError, ValueError):
                    raise
                response = await self._request_with_fallback_paths(
                    "POST",
                    paths,
                    params=params,
                    json={"transactionId": int_tx_id},
                )
                return self._validate_command_response(
                    action="request_stop_transaction",
                    station_id=station_id,
                    response=response,
                )
            raise err

    async def set_station_limit(
        self,
        *,
        protocol: str,
        station_id: str,
        limit: float,
        unit: str = "W",
        evse_id: int = 0,
        duration: int = 300,
    ) -> Any:
        """Apply a station-level smart charging limit."""
        protocol = self.normalize_protocol(protocol)
        normalized_unit = unit.upper()

        if protocol == "ocpp1.6":
            schedule: dict[str, Any] = {
                "chargingRateUnit": normalized_unit,
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": round(limit, 1)}
                ],
            }
            if duration > 0:
                schedule["duration"] = duration

            payload = {
                "connectorId": evse_id,
                "csChargingProfiles": {
                    "chargingProfileId": random.randint(1000, 9999),
                    "stackLevel": 1,
                    "chargingProfilePurpose": "ChargePointMaxProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": schedule,
                },
            }
            paths = ["/ocpp/1.6/smartcharging/setChargingProfile"]
        else:
            start_schedule = self._iso_utc_now()
            schedule = {
                "id": random.randint(1000, 9999),
                "chargingRateUnit": normalized_unit,
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": round(limit, 1)}
                ],
            }
            if duration > 0:
                schedule["duration"] = duration

            payload = {
                "evseId": evse_id,
                "chargingProfile": {
                    "id": random.randint(1000, 9999),
                    "stackLevel": 1,
                    "chargingProfilePurpose": "ChargingStationMaxProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": [
                        {
                            **schedule,
                            "startSchedule": start_schedule,
                        }
                    ],
                },
            }
            paths = self._ocpp2_paths(protocol, "smartcharging/setChargingProfile")

        params = self._identifier_params(station_id)
        try:
            response = await self._request_with_fallback_paths(
                "POST",
                paths,
                params=params,
                json=payload,
            )
            return self._validate_command_response(
                action="set_station_limit",
                station_id=station_id,
                response=response,
            )
        except CitrineApiError as err:
            message = str(err).lower()
            # Common OCPP 1.6 quirk: connectorId 0 rejected, retry with connector 1.
            if protocol == "ocpp1.6" and evse_id == 0:
                retry_payload = dict(payload)
                retry_payload["connectorId"] = 1
                response = await self._request_with_fallback_paths(
                    "POST",
                    paths,
                    params=params,
                    json=retry_payload,
                )
                return self._validate_command_response(
                    action="set_station_limit",
                    station_id=station_id,
                    response=response,
                )

            # Some stations reject W and only accept A in profile schedules.
            if protocol != "ocpp1.6" and normalized_unit == "W" and "chargingrateunit" in message:
                retry_payload = {
                    **payload,
                    "chargingProfile": {
                        **payload["chargingProfile"],
                        "chargingSchedule": [
                            {
                                **payload["chargingProfile"]["chargingSchedule"][0],
                                "chargingRateUnit": "A",
                            }
                        ],
                    },
                }
                response = await self._request_with_fallback_paths(
                    "POST",
                    paths,
                    params=params,
                    json=retry_payload,
                )
                return self._validate_command_response(
                    action="set_station_limit",
                    station_id=station_id,
                    response=response,
                )
            raise err

    async def set_charging_profile(
        self,
        *,
        protocol: str,
        station_id: str,
        limit: float,
        setpoint: float | None = None,
        discharge_limit: float | None = None,
        operation_mode: str | None = None,
        unit: str = "W",
        evse_id: int = 0,
        duration: int = 300,
        stack_level: int = 1,
        profile_id: int | None = None,
        profile_purpose: str | None = None,
        profile_kind: str | None = None,
        profile_periods: list[dict[str, Any]] | None = None,
        transaction_id: str | None = None,
        txprofile_compatibility_fallback: bool = True,
    ) -> Any:
        """Apply an explicit charging profile with OCPP-specific structure."""
        protocol = self.normalize_protocol(protocol)
        normalized_unit = unit.upper()
        normalized_purpose = str(profile_purpose or "TxProfile").strip()
        purpose_key = normalized_purpose.lower()
        requested_kind = str(profile_kind or "").strip().capitalize()
        if requested_kind not in {"Absolute", "Relative", "Dynamic"}:
            requested_kind = ""
        if protocol != "ocpp2.1":
            operation_mode = None
            discharge_limit = None
            if requested_kind == "Dynamic":
                requested_kind = ""

        # Some OCPP 2.x firmware crashes/drops websocket when TxProfile carries UUID transaction ids.
        # Downgrade to TxDefaultProfile in that case to keep EVSE online while still applying a limit.
        if (
            txprofile_compatibility_fallback
            and
            protocol != "ocpp1.6"
            and purpose_key == "txprofile"
            and transaction_id is not None
            and re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
                str(transaction_id),
            )
        ):
            _LOGGER.warning(
                "TxProfile compatibility fallback: station=%s protocol=%s tx_id=%s downgraded_to=TxDefaultProfile",
                station_id,
                protocol,
                transaction_id,
            )
            normalized_purpose = "TxDefaultProfile"
            purpose_key = "txdefaultprofile"
            transaction_id = None

        # Keep caller-provided evseId on first attempt for compatibility.
        # Some backends validate evseId >= 1 even for station-scope profile purposes.

        payload_variants: list[dict[str, Any]] = []

        if protocol == "ocpp1.6":
            schedule: dict[str, Any] = {
                "chargingRateUnit": normalized_unit,
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": round(limit, 1)}
                ],
            }
            if duration > 0:
                schedule["duration"] = duration

            payload_base: dict[str, Any] = {
                "connectorId": evse_id,
                "csChargingProfiles": {
                    "chargingProfileId": profile_id or random.randint(1000, 9999),
                    "stackLevel": stack_level,
                    "chargingProfilePurpose": normalized_purpose,
                    # OCPP 1.6 profile kind support is effectively Absolute for this payload shape.
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": schedule,
                },
            }
            payload_variants.append(payload_base)

            if transaction_id is not None and purpose_key == "txprofile":
                try:
                    tx_value: int | str = int(transaction_id)
                except (TypeError, ValueError):
                    tx_value = transaction_id

                # Variant 1: transactionId inside profile object.
                payload_profile_tx = deepcopy(payload_base)
                payload_profile_tx["csChargingProfiles"]["transactionId"] = tx_value
                payload_variants.insert(0, payload_profile_tx)

                # Variant 2: transactionId at request root.
                payload_root_tx = deepcopy(payload_base)
                payload_root_tx["transactionId"] = tx_value
                payload_variants.append(payload_root_tx)

                # Variant 3: include both locations for strict/legacy backends.
                payload_both_tx = deepcopy(payload_profile_tx)
                payload_both_tx["transactionId"] = tx_value
                payload_variants.append(payload_both_tx)

            paths = ["/ocpp/1.6/smartcharging/setChargingProfile"]
        else:
            start_schedule = self._iso_utc_now()
            txprofile_relative = purpose_key == "txprofile"
            default_kind = "Relative" if txprofile_relative else "Absolute"
            if protocol == "ocpp2.1" and not txprofile_relative:
                default_kind = "Dynamic"
            effective_kind = requested_kind or default_kind
            if effective_kind == "Dynamic" and protocol != "ocpp2.1":
                # Dynamic profile kind is OCPP 2.1-specific. Fallback for mixed deployments.
                effective_kind = "Absolute"

            schedule_periods_payload = self._build_profile_periods(
                limit=limit,
                setpoint=setpoint,
                discharge_limit=discharge_limit,
                schedule_periods=profile_periods,
            )
            schedule: dict[str, Any] = {
                "id": random.randint(1000, 9999),
                "chargingRateUnit": normalized_unit,
                "chargingSchedulePeriod": schedule_periods_payload,
            }
            if duration > 0:
                schedule["duration"] = duration
            if operation_mode:
                schedule["operationMode"] = str(operation_mode)

            payload_base = {
                "evseId": evse_id,
                "chargingProfile": {
                    "id": profile_id or random.randint(1000, 9999),
                    "stackLevel": stack_level,
                    "chargingProfilePurpose": normalized_purpose,
                    "chargingProfileKind": effective_kind,
                    "chargingSchedule": [schedule],
                },
            }
            # startSchedule is mandatory for Absolute/Recurring, but not for Relative.
            if effective_kind in {"Absolute", "Recurring"}:
                payload_base["chargingProfile"]["chargingSchedule"][0]["startSchedule"] = start_schedule
            payload_variants.append(payload_base)

            if transaction_id is not None and purpose_key == "txprofile":
                tx_value = str(transaction_id)

                # Variant 1: transactionId inside chargingProfile object (OCPP 2.x common).
                payload_profile_tx = deepcopy(payload_base)
                payload_profile_tx["chargingProfile"]["transactionId"] = tx_value
                payload_variants.insert(0, payload_profile_tx)

                # Variant 2: transactionId at request root (backend-specific).
                payload_root_tx = deepcopy(payload_base)
                payload_root_tx["transactionId"] = tx_value
                payload_variants.append(payload_root_tx)

                # Variant 3: include both locations for strict/legacy backends.
                payload_both_tx = deepcopy(payload_profile_tx)
                payload_both_tx["transactionId"] = tx_value
                payload_variants.append(payload_both_tx)

                # Compatibility fallback: some chargers reject Absolute TxProfile but accept Relative.
                payload_absolute_profile_tx = deepcopy(payload_profile_tx)
                payload_absolute_profile_tx["chargingProfile"]["chargingProfileKind"] = "Absolute"
                payload_absolute_profile_tx["chargingProfile"]["chargingSchedule"][0]["startSchedule"] = start_schedule
                payload_variants.append(payload_absolute_profile_tx)

                payload_absolute_root_tx = deepcopy(payload_root_tx)
                payload_absolute_root_tx["chargingProfile"]["chargingProfileKind"] = "Absolute"
                payload_absolute_root_tx["chargingProfile"]["chargingSchedule"][0]["startSchedule"] = start_schedule
                payload_variants.append(payload_absolute_root_tx)

                payload_absolute_both_tx = deepcopy(payload_both_tx)
                payload_absolute_both_tx["chargingProfile"]["chargingProfileKind"] = "Absolute"
                payload_absolute_both_tx["chargingProfile"]["chargingSchedule"][0]["startSchedule"] = start_schedule
                payload_variants.append(payload_absolute_both_tx)

            # Additional interoperability fallbacks for strict validator implementations.
            extra_variants: list[dict[str, Any]] = []
            for variant in payload_variants:
                cp = variant.get("chargingProfile", {}) if isinstance(variant, dict) else {}
                schedules = cp.get("chargingSchedule", []) if isinstance(cp, dict) else []
                if not schedules or not isinstance(schedules[0], dict):
                    continue

                # Some chargers require startSchedule even for Relative profiles.
                if cp.get("chargingProfileKind") in {"Relative", "Dynamic"} and "startSchedule" not in schedules[0]:
                    with_start_schedule = deepcopy(variant)
                    with_start_schedule["chargingProfile"]["chargingSchedule"][0]["startSchedule"] = start_schedule
                    extra_variants.append(with_start_schedule)

                # Some chargers only accept A as chargingRateUnit for smart charging payloads.
                if str(schedules[0].get("chargingRateUnit", "")).upper() == "W":
                    with_amp_unit = deepcopy(variant)
                    with_amp_unit["chargingProfile"]["chargingSchedule"][0]["chargingRateUnit"] = "A"
                    extra_variants.append(with_amp_unit)

                # For station- or default-scope profiles, retry at station scope (evseId=0).
                if (
                    variant.get("evseId") not in (None, 0)
                    and str(cp.get("chargingProfilePurpose", "")).lower()
                    in {
                        "txdefaultprofile",
                        "chargingstationmaxprofile",
                        "chargingstationexternalconstraints",
                        "prioritycharging",
                        "localgeneration",
                    }
                ):
                    with_station_scope = deepcopy(variant)
                    with_station_scope["evseId"] = 0
                    extra_variants.append(with_station_scope)

            payload_variants.extend(extra_variants)

            paths = self._ocpp2_paths(protocol, "smartcharging/setChargingProfile")

        params = self._identifier_params(station_id)
        last_error: CitrineApiError | None = None
        for index, payload in enumerate(payload_variants, start=1):
            _LOGGER.warning(
                "set_charging_profile request variant=%s station=%s protocol=%s purpose=%s evse=%s payload=%s",
                index,
                station_id,
                protocol,
                normalized_purpose,
                evse_id,
                payload,
            )
            try:
                response = await self._request_with_fallback_paths(
                    "POST",
                    paths,
                    params=params,
                    json=payload,
                )
                _LOGGER.warning(
                    "set_charging_profile response variant=%s station=%s protocol=%s purpose=%s evse=%s response=%s",
                    index,
                    station_id,
                    protocol,
                    normalized_purpose,
                    evse_id,
                    response,
                )
                return self._validate_command_response(
                    action="set_charging_profile",
                    station_id=station_id,
                    response=response,
                )
            except CitrineApiError as err:
                last_error = err
                _LOGGER.warning(
                    "set_charging_profile variant failed: variant=%s station=%s error=%s",
                    index,
                    station_id,
                    err,
                )
                continue

        if last_error is not None:
            raise last_error
        raise CitrineApiError("set_charging_profile failed with no payload variants")

    async def clear_charging_profile(
        self,
        *,
        protocol: str,
        station_id: str,
        evse_id: int = 0,
        profile_id: int | None = None,
        stack_level: int | None = None,
        profile_purpose: str | None = None,
    ) -> Any:
        """Clear charging profile(s) for a station using protocol-specific payloads."""
        protocol = self.normalize_protocol(protocol)

        if protocol == "ocpp1.6":
            payload: dict[str, Any] = {}
            if profile_id is not None:
                payload["id"] = profile_id
            if stack_level is not None:
                payload["stackLevel"] = stack_level
            if profile_purpose:
                payload["chargingProfilePurpose"] = profile_purpose
            if evse_id:
                payload["connectorId"] = evse_id

            paths = ["/ocpp/1.6/smartcharging/clearChargingProfile"]
        else:
            criteria: dict[str, Any] = {}
            if profile_id is not None:
                criteria["id"] = profile_id
            if stack_level is not None:
                criteria["stackLevel"] = stack_level
            if profile_purpose:
                criteria["chargingProfilePurpose"] = profile_purpose

            payload = {"evseId": evse_id, "chargingProfileCriteria": criteria}
            paths = self._ocpp2_paths(protocol, "smartcharging/clearChargingProfile")

        params = self._identifier_params(station_id)
        response = await self._request_with_fallback_paths(
            "POST",
            paths,
            params=params,
            json=payload,
        )
        return self._validate_command_response(
            action="clear_charging_profile",
            station_id=station_id,
            response=response,
        )

    async def set_group_limit(
        self,
        *,
        group_id: str,
        station_protocols: Mapping[str, str],
        total_limit: float,
        unit: str,
        duration: int,
    ) -> dict[str, Any]:
        """Split a group cap equally and dispatch station limit commands."""
        if not station_protocols:
            return {"group_id": group_id, "results": {}}

        per_station = total_limit / len(station_protocols)
        results: dict[str, Any] = {}
        for station_id, protocol in station_protocols.items():
            try:
                results[station_id] = await self.set_station_limit(
                    protocol=protocol,
                    station_id=station_id,
                    limit=per_station,
                    unit=unit,
                    duration=duration,
                )
            except CitrineApiError as err:
                results[station_id] = {"error": str(err)}

        return {"group_id": group_id, "results": results}

    def _ocpp2_paths(self, protocol: str, route: str) -> list[str]:
        """Return endpoint fallback order for OCPP 2.x API families."""
        cleaned_route = route.lstrip("/")
        if protocol == "ocpp2.1":
            return [
                f"/ocpp/2.1/{cleaned_route}",
                f"/ocpp/2.0.1/{cleaned_route}",
                f"/ocpp/2.0/{cleaned_route}",
            ]

        return [
            f"/ocpp/2.0.1/{cleaned_route}",
            f"/ocpp/2.0/{cleaned_route}",
            f"/ocpp/2.1/{cleaned_route}",
        ]

    def _build_profile_periods(
        self,
        *,
        limit: float,
        setpoint: float | None,
        discharge_limit: float | None,
        schedule_periods: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Normalize schedule periods for OCPP charging profiles."""
        if not schedule_periods:
            period: dict[str, Any] = {
                "startPeriod": 0,
                "limit": round(limit, 1),
            }
            if setpoint is not None:
                period["setpoint"] = round(setpoint, 1)
            if discharge_limit is not None:
                period["dischargeLimit"] = round(discharge_limit, 1)
            return [period]

        normalized: list[dict[str, Any]] = []
        for index, raw_period in enumerate(schedule_periods):
            if not isinstance(raw_period, dict):
                continue

            period: dict[str, Any] = dict(raw_period)
            if "discharge_limit" in period and "dischargeLimit" not in period:
                period["dischargeLimit"] = period.pop("discharge_limit")
            if "startPeriod" in period:
                try:
                    period["startPeriod"] = int(period["startPeriod"])
                except (TypeError, ValueError):
                    period["startPeriod"] = 0
            elif index == 0:
                period["startPeriod"] = 0

            if "limit" in period:
                try:
                    period["limit"] = round(float(period["limit"]), 1)
                except (TypeError, ValueError):
                    period.pop("limit", None)

            if "setpoint" in period:
                try:
                    period["setpoint"] = round(float(period["setpoint"]), 1)
                except (TypeError, ValueError):
                    period.pop("setpoint", None)

            if "dischargeLimit" in period:
                try:
                    period["dischargeLimit"] = round(float(period["dischargeLimit"]), 1)
                except (TypeError, ValueError):
                    period.pop("dischargeLimit", None)

            if "limit" not in period:
                period["limit"] = round(limit, 1)
            if "setpoint" not in period and setpoint is not None:
                period["setpoint"] = round(setpoint, 1)
            if "dischargeLimit" not in period and discharge_limit is not None:
                period["dischargeLimit"] = round(discharge_limit, 1)

            normalized.append(period)

        if not normalized:
            return [{"startPeriod": 0, "limit": round(limit, 1)}]

        return normalized

    def _identifier_params(self, station_id: str) -> dict[str, Any]:
        return {
            "identifier": station_id,
            "tenantId": self._tenant_id,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        send_auth: bool = True,
    ) -> Any:
        if self.read_only and method.upper() not in ("GET", "HEAD"):
            raise CitrineReadOnlyError("Read-only entry: station commands are disabled")
        headers: dict[str, str] = {}
        if self._auth_token and send_auth:
            headers["Authorization"] = f"Bearer {self._auth_token}"

        url = f"{self._base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=self._request_timeout)

        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                ssl=self._verify_ssl,
                timeout=timeout,
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    raise CitrineAuthError("Authentication or authorization failed")

                text = await resp.text()
                if resp.status >= 400:
                    raise CitrineApiError(
                        f"Citrine API {method} {path} failed ({resp.status})"
                    )
                if 300 <= resp.status < 400:
                    raise CitrineApiError("Citrine API redirect refused")

                if not text:
                    return None

                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type or "+json" in content_type:
                    return await resp.json()

                return text
        except asyncio.TimeoutError as err:
            raise CitrineApiError(f"Citrine API timeout calling {path}") from err
        except aiohttp.ClientError as err:
            raise CitrineApiError("Citrine API transport error") from err
        except ValueError as err:
            raise CitrineApiError("Invalid Citrine API JSON response") from err

    async def _request_with_fallback_paths(
        self,
        method: str,
        paths: list[str],
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Try equivalent endpoints in sequence for backend compatibility."""
        last_error: CitrineApiError | None = None
        for path in paths:
            try:
                return await self._request(
                    method,
                    path,
                    params=params,
                    json=json,
                )
            except CitrineApiError as err:
                last_error = err
                message = str(err).lower()
                if "failed (404)" in message or "failed (405)" in message:
                    continue
                raise

        if last_error is not None:
            raise last_error
        raise CitrineApiError("No endpoint paths provided")

    def _validate_command_response(
        self,
        *,
        action: str,
        station_id: str,
        response: Any,
    ) -> Any:
        """Raise explicit errors when command payload indicates rejection despite HTTP success."""
        if isinstance(response, list):
            if not response:
                return response

            list_success_values: list[bool] = []
            for item in response:
                if isinstance(item, dict) and "success" in item and isinstance(item["success"], bool):
                    list_success_values.append(item["success"])

            if list_success_values and not all(list_success_values):
                compact = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
                _LOGGER.warning(
                    "Citrine command rejected by success=false list payload: action=%s station=%s response=%s",
                    action,
                    station_id,
                    compact,
                )
                raise CitrineApiError(
                    f"Command {action} was not accepted for station {station_id}: {compact}"
                )
            return response

        if not isinstance(response, dict):
            return response

        tokens = self._extract_status_tokens(response)
        reject_markers = {
            "rejected",
            "reject",
            "notsupported",
            "not_supported",
            "unsupported",
            "denied",
            "failed",
            "error",
            "invalid",
            "faulted",
        }
        accept_markers = {
            "accepted",
            "accept",
            "ok",
            "success",
            "succeeded",
            "scheduled",
            "inprogress",
            "pending",
        }

        if "accepted" in response and isinstance(response["accepted"], bool):
            if not response["accepted"]:
                compact = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
                _LOGGER.warning(
                    "Citrine command rejected by payload: action=%s station=%s response=%s",
                    action,
                    station_id,
                    compact,
                )
                raise CitrineApiError(
                    f"Command {action} was not accepted for station {station_id}: {compact}"
                )
            return response

        rejected = any(token in reject_markers for token in tokens)
        accepted = any(token in accept_markers for token in tokens)
        if rejected and not accepted:
            compact = json.dumps(response, ensure_ascii=True, separators=(",", ":"))
            _LOGGER.warning(
                "Citrine command rejected by status token: action=%s station=%s response=%s",
                action,
                station_id,
                compact,
            )
            raise CitrineApiError(
                f"Command {action} was rejected for station {station_id}: {compact}"
            )

        _LOGGER.debug(
            "Citrine command response: action=%s station=%s tokens=%s",
            action,
            station_id,
            sorted(tokens),
        )
        return response

    def _extract_status_tokens(self, payload: Any) -> set[str]:
        """Recursively collect lower-cased status-ish values from a response payload."""
        tokens: set[str] = set()

        def _walk(node: Any, depth: int) -> None:
            if depth > 6:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    key_l = str(key).lower()
                    if isinstance(value, str) and (
                        "status" in key_l
                        or "result" in key_l
                        or key_l in {"message", "reason", "state", "outcome"}
                    ):
                        tokens.add(value.strip().lower().replace(" ", ""))
                    _walk(value, depth + 1)
                return
            if isinstance(node, list):
                for item in node:
                    _walk(item, depth + 1)

        _walk(payload, 0)
        return tokens

    @staticmethod
    def _iso_utc_now() -> str:
        """Return current UTC timestamp in RFC3339 format expected by OCPP 2.x."""
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
