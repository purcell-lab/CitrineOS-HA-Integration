"""Config flow for CitrineOS integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .citrine_api import CitrineApiError, CitrineAuthError, CitrineClient, CitrineCompatibilityError
from .compatibility import (
    API_PROFILES, CONF_API_PROFILE, CONF_READ_ONLY, CONF_COMMAND_TOKEN,
    PROFILE_LEGACY, PROFILE_V2, PROFILE_LAB, V2_PROFILES,
    V2_DISCOVERY_QUERY, is_read_only, normalize_v2_discovery,
)
from .const import (
    CONF_AUTH_TOKEN,
    CONF_BASE_URL,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CONTROLLER_INTERVAL_SECS,
    CONF_CONTROLLER_MODE,
    CONF_EMS_INTENT_MODE,
    CONF_DEADBAND_W,
    CONF_DEFAULT_EVSE_ID,
    CONF_DEFAULT_ID_TAG,
    CONF_DOE_EXPORT_LIMIT_SENSOR,
    CONF_DOE_IMPORT_LIMIT_SENSOR,
    CONF_EMS_ENDPOINT_PREFIX,
    CONF_EMS_TELEMETRY_LIMIT,
    CONF_EMS_TELEMETRY_SITE_ID,
    CONF_EMS_TELEMETRY_STALE_SECS,
    CONF_GRID_PHASE_A_CURRENT_SENSOR,
    CONF_GRID_PHASE_B_CURRENT_SENSOR,
    CONF_GRID_PHASE_C_CURRENT_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_HASURA_QUERY,
    CONF_HASURA_TOKEN,
    CONF_HASURA_URL,
    CONF_MAIN_FUSE_CURRENT_A,
    CONF_MAIN_FUSE_LIMIT_W,
    CONF_MAX_PHASE_UNBALANCE_A,
    CONF_MIN_CHARGE_CURRENT_A,
    CONF_MIN_DWELL_SECS,
    CONF_MQTT_TOPIC_PREFIX,
    CONF_NAME,
    CONF_NOMINAL_VOLTAGE,
    CONF_RAMP_RATE_W_S,
    CONF_REQUEST_TIMEOUT,
    CONF_SCAN_INTERVAL,
    CONF_SITE_EXPORT_LIMIT_W,
    CONF_SITE_ID,
    CONF_SITE_PHASES,
    CONF_SOLAR_POWER_SENSOR,
    CONF_SOLAR_START_BUFFER_W,
    CONF_TENANT_ID,
    CONF_VERIFY_SSL,
    CONTROLLER_MODES,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_CONTROLLER_INTERVAL_SECS,
    DEFAULT_CONTROLLER_MODE,
    DEFAULT_EMS_INTENT_MODE,
    DEFAULT_DEADBAND_W,
    DEFAULT_DEFAULT_EVSE_ID,
    DEFAULT_DEFAULT_ID_TAG,
    DEFAULT_EMS_ENDPOINT_PREFIX,
    DEFAULT_EMS_TELEMETRY_LIMIT,
    DEFAULT_EMS_TELEMETRY_STALE_SECS,
    DEFAULT_HASURA_QUERY,
    DEFAULT_MAIN_FUSE_CURRENT_A,
    DEFAULT_MAIN_FUSE_LIMIT_W,
    DEFAULT_MAX_PHASE_UNBALANCE_A,
    DEFAULT_MIN_CHARGE_CURRENT_A,
    DEFAULT_MIN_DWELL_SECS,
    DEFAULT_MQTT_TOPIC_PREFIX,
    DEFAULT_NAME,
    DEFAULT_NOMINAL_VOLTAGE,
    DEFAULT_RAMP_RATE_W_S,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SITE_EXPORT_LIMIT_W,
    DEFAULT_SITE_ID,
    DEFAULT_SITE_PHASES,
    DEFAULT_SOLAR_START_BUFFER_W,
    DEFAULT_TENANT_ID,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    EMS_INTENT_MODES,
)
from .hasura_client import HasuraAuthError, HasuraClient, HasuraError
from .lab_control import ScopedLabClient


class CitrineConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for CitrineOS."""

    VERSION = 1

    def __init__(self) -> None:
        self._user_data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Step 1: Connect to CitrineOS."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await self._async_validate(user_input)
            except CitrineAuthError:
                errors["base"] = "invalid_auth"
            except CitrineCompatibilityError:
                errors["base"] = "incompatible_api"
            except (CitrineApiError, HasuraError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_BASE_URL]}::{user_input[CONF_TENANT_ID]}"
                )
                self._abort_if_unique_id_configured()
                self._user_data = dict(user_input)
                if is_read_only(user_input) or user_input.get(CONF_API_PROFILE) == PROFILE_LAB:
                    self._user_data[CONF_READ_ONLY] = is_read_only(user_input)
                    return self.async_create_entry(
                        title=user_input.get(CONF_NAME, DEFAULT_NAME),
                        data=self._user_data)
                return await self.async_step_load_control()

        values = user_input or {}
        return self.async_show_form(
            step_id="user",
            data_schema=self._user_schema(values),
            errors=errors,
        )

    async def async_step_load_control(self, user_input: dict[str, Any] | None = None):
        """Step 2: Configure local load controller & site sensors."""
        if user_input is not None:
            combined_data = {**self._user_data, **user_input}
            return self.async_create_entry(
                title=self._user_data.get(CONF_NAME, DEFAULT_NAME),
                data=combined_data,
            )

        values = user_input or {}
        return self.async_show_form(
            step_id="load_control",
            data_schema=self._load_control_schema(values),
        )

    @staticmethod
    def _user_schema(values: dict[str, Any]) -> vol.Schema:
        schema: dict[Any, Any] = {}
        schema[vol.Required(CONF_API_PROFILE, default=values.get(
            CONF_API_PROFILE, PROFILE_LEGACY))] = selector.SelectSelector(
                selector.SelectSelectorConfig(options=API_PROFILES))
        schema[vol.Required(CONF_READ_ONLY, default=is_read_only(values))] = selector.BooleanSelector()
        schema[vol.Optional(CONF_COMMAND_TOKEN)] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))

        def _val(k: str, default: Any = None) -> Any:
            v = values.get(k)
            return default if (v is None or v == "") else v

        def _int(k: str, default: int) -> int:
            try:
                return int(_val(k, default))
            except (ValueError, TypeError):
                return default

        def _bool(k: str, default: bool) -> bool:
            v = _val(k, default)
            return bool(v) if v is not None else default

        schema[vol.Required(
            CONF_NAME,
            default=str(_val(CONF_NAME, DEFAULT_NAME)),
        )] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT))

        schema[vol.Required(
            CONF_BASE_URL,
            default=str(_val(CONF_BASE_URL, "http://localhost:8080")),
        )] = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL))

        schema[vol.Required(
            CONF_TENANT_ID,
            default=_int(CONF_TENANT_ID, DEFAULT_TENANT_ID),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=999999, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        if _val(CONF_AUTH_TOKEN):
            schema[vol.Optional(CONF_AUTH_TOKEN, description={"suggested_value": str(_val(CONF_AUTH_TOKEN))})] = (
                selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
            )
        else:
            schema[vol.Optional(CONF_AUTH_TOKEN)] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )

        schema[vol.Required(
            CONF_VERIFY_SSL,
            default=_bool(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        )] = selector.BooleanSelector()

        schema[vol.Required(
            CONF_REQUEST_TIMEOUT,
            default=_int(CONF_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=120, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        if _val(CONF_HASURA_URL):
            schema[vol.Optional(CONF_HASURA_URL, description={"suggested_value": str(_val(CONF_HASURA_URL))})] = (
                selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.URL))
            )
        else:
            schema[vol.Optional(CONF_HASURA_URL)] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
            )

        if _val(CONF_HASURA_TOKEN):
            schema[vol.Optional(CONF_HASURA_TOKEN, description={"suggested_value": str(_val(CONF_HASURA_TOKEN))})] = (
                selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
            )
        else:
            schema[vol.Optional(CONF_HASURA_TOKEN)] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            )

        schema[vol.Optional(
            CONF_SCAN_INTERVAL,
            default=_int(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=3600, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_DEFAULT_ID_TAG,
            default=str(_val(CONF_DEFAULT_ID_TAG, DEFAULT_DEFAULT_ID_TAG)),
        )] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

        schema[vol.Optional(
            CONF_DEFAULT_EVSE_ID,
            default=_int(CONF_DEFAULT_EVSE_ID, DEFAULT_DEFAULT_EVSE_ID),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=999, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        return vol.Schema(schema)

    @staticmethod
    def _load_control_schema(values: dict[str, Any]) -> vol.Schema:
        schema: dict[Any, Any] = {}

        def _val(k: str, default: Any = None) -> Any:
            v = values.get(k)
            return default if (v is None or v == "") else v

        def _float(k: str, default: float) -> float:
            try:
                return float(_val(k, default))
            except (ValueError, TypeError):
                return default

        for ent_key in (
            CONF_GRID_POWER_SENSOR,
            CONF_GRID_PHASE_A_CURRENT_SENSOR,
            CONF_GRID_PHASE_B_CURRENT_SENSOR,
            CONF_GRID_PHASE_C_CURRENT_SENSOR,
            CONF_SOLAR_POWER_SENSOR,
            CONF_BATTERY_POWER_SENSOR,
            CONF_BATTERY_SOC_SENSOR,
            CONF_DOE_IMPORT_LIMIT_SENSOR,
            CONF_DOE_EXPORT_LIMIT_SENSOR,
        ):
            if _val(ent_key):
                schema[vol.Optional(ent_key, description={"suggested_value": str(_val(ent_key))})] = (
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
                )
            else:
                schema[vol.Optional(ent_key)] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                )

        schema[vol.Optional(
            CONF_CONTROLLER_MODE,
            default=str(_val(CONF_CONTROLLER_MODE, DEFAULT_CONTROLLER_MODE)),
        )] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONTROLLER_MODES,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        schema[vol.Optional(
            CONF_EMS_INTENT_MODE,
            default=str(_val(CONF_EMS_INTENT_MODE, DEFAULT_EMS_INTENT_MODE)),
        )] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=EMS_INTENT_MODES,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        schema[vol.Optional(
            CONF_MAIN_FUSE_LIMIT_W,
            default=_float(CONF_MAIN_FUSE_LIMIT_W, DEFAULT_MAIN_FUSE_LIMIT_W),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1000, max=250000, step=100, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_SOLAR_START_BUFFER_W,
            default=_float(CONF_SOLAR_START_BUFFER_W, DEFAULT_SOLAR_START_BUFFER_W),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=5000, step=50, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_SITE_PHASES,
            default=str(_val(CONF_SITE_PHASES, DEFAULT_SITE_PHASES)),
        )] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["1", "3"],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        return vol.Schema(schema)

    async def _async_validate(self, data: dict[str, Any]) -> None:
        verify_ssl = bool(data.get(CONF_VERIFY_SSL, True))
        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)

        client_type = ScopedLabClient if data.get(CONF_API_PROFILE) == PROFILE_LAB else CitrineClient
        lab_args = {"command_token": data.get(CONF_COMMAND_TOKEN)} if client_type is ScopedLabClient else {}
        client = client_type(
            session,
            base_url=data[CONF_BASE_URL],
            tenant_id=int(data[CONF_TENANT_ID]),
            auth_token=data.get(CONF_AUTH_TOKEN) or None,
            verify_ssl=verify_ssl,
            request_timeout=int(data[CONF_REQUEST_TIMEOUT]),
            api_profile=data.get(CONF_API_PROFILE, PROFILE_LEGACY),
            read_only=is_read_only(data),
            **lab_args,
        )
        if data.get(CONF_API_PROFILE) in V2_PROFILES and not str(data.get(CONF_HASURA_URL) or "").strip():
            raise CitrineCompatibilityError("CitrineOS 2.x requires a Hasura URL")
        await client.ping()

        hasura_url = data.get(CONF_HASURA_URL) or ""
        if hasura_url.strip():
            hasura_client = HasuraClient(
                session,
                url=hasura_url,
                token=data.get(CONF_HASURA_TOKEN) or None,
                bearer_auth=data.get(CONF_API_PROFILE) in V2_PROFILES,
                request_timeout=int(data[CONF_REQUEST_TIMEOUT]),
                verify_ssl=verify_ssl,
            )
            try:
                if data.get(CONF_API_PROFILE) in V2_PROFILES:
                    result = await hasura_client.query(
                        V2_DISCOVERY_QUERY, variables={"tenantId": int(data[CONF_TENANT_ID])})
                    try:
                        normalize_v2_discovery(result, int(data[CONF_TENANT_ID]))
                    except ValueError as err:
                        raise CitrineCompatibilityError(str(err)) from err
                else:
                    await hasura_client.ping()
            except HasuraAuthError as err:
                raise CitrineAuthError(str(err)) from err

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return CitrineOptionsFlow(config_entry)


class CitrineOptionsFlow(config_entries.OptionsFlow):
    """Handle Citrine options flow."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    def _options_schema(self, values: dict[str, Any]) -> vol.Schema:
        if values.get(CONF_API_PROFILE) in V2_PROFILES:
            return vol.Schema({
                vol.Required(CONF_API_PROFILE, default=values[CONF_API_PROFILE]):
                    selector.SelectSelector(selector.SelectSelectorConfig(options=list(V2_PROFILES))),
                vol.Required(CONF_READ_ONLY, default=is_read_only(values)): selector.BooleanSelector(),
                vol.Optional(CONF_COMMAND_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
                vol.Optional(CONF_SCAN_INTERVAL, default=values.get(CONF_SCAN_INTERVAL, 10)):
                    selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=60, step=1)),
            })
        schema: dict[Any, Any] = {}
        schema[vol.Required(CONF_READ_ONLY, default=is_read_only(values))] = selector.BooleanSelector()

        def _val(k: str, default: Any = None) -> Any:
            v = values.get(k)
            return default if (v is None or v == "") else v

        def _int(k: str, default: int) -> int:
            try:
                return int(_val(k, default))
            except (ValueError, TypeError):
                return default

        def _float(k: str, default: float) -> float:
            try:
                return float(_val(k, default))
            except (ValueError, TypeError):
                return default

        for ent_key in (
            CONF_GRID_POWER_SENSOR,
            CONF_GRID_PHASE_A_CURRENT_SENSOR,
            CONF_GRID_PHASE_B_CURRENT_SENSOR,
            CONF_GRID_PHASE_C_CURRENT_SENSOR,
            CONF_SOLAR_POWER_SENSOR,
            CONF_BATTERY_POWER_SENSOR,
            CONF_BATTERY_SOC_SENSOR,
            CONF_DOE_IMPORT_LIMIT_SENSOR,
            CONF_DOE_EXPORT_LIMIT_SENSOR,
        ):
            if _val(ent_key):
                schema[vol.Optional(ent_key, description={"suggested_value": str(_val(ent_key))})] = (
                    selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
                )
            else:
                schema[vol.Optional(ent_key)] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                )

        schema[vol.Optional(
            CONF_CONTROLLER_MODE,
            default=str(_val(CONF_CONTROLLER_MODE, DEFAULT_CONTROLLER_MODE)),
        )] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=CONTROLLER_MODES,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        schema[vol.Optional(
            CONF_EMS_INTENT_MODE,
            default=str(_val(CONF_EMS_INTENT_MODE, DEFAULT_EMS_INTENT_MODE)),
        )] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=EMS_INTENT_MODES,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

        schema[vol.Optional(
            CONF_MAIN_FUSE_LIMIT_W,
            default=_float(CONF_MAIN_FUSE_LIMIT_W, DEFAULT_MAIN_FUSE_LIMIT_W),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1000, max=250000, step=100, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_MAIN_FUSE_CURRENT_A,
            default=_float(CONF_MAIN_FUSE_CURRENT_A, DEFAULT_MAIN_FUSE_CURRENT_A),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=10, max=500, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_MAX_PHASE_UNBALANCE_A,
            default=_float(CONF_MAX_PHASE_UNBALANCE_A, DEFAULT_MAX_PHASE_UNBALANCE_A),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=100, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_SOLAR_START_BUFFER_W,
            default=_float(CONF_SOLAR_START_BUFFER_W, DEFAULT_SOLAR_START_BUFFER_W),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=5000, step=50, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_MIN_CHARGE_CURRENT_A,
            default=_float(CONF_MIN_CHARGE_CURRENT_A, DEFAULT_MIN_CHARGE_CURRENT_A),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=6, max=16, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_RAMP_RATE_W_S,
            default=_float(CONF_RAMP_RATE_W_S, DEFAULT_RAMP_RATE_W_S),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=50, max=10000, step=50, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_CONTROLLER_INTERVAL_SECS,
            default=_int(CONF_CONTROLLER_INTERVAL_SECS, DEFAULT_CONTROLLER_INTERVAL_SECS),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=60, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_DEADBAND_W,
            default=_float(CONF_DEADBAND_W, DEFAULT_DEADBAND_W),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=2000, step=50, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_MIN_DWELL_SECS,
            default=_int(CONF_MIN_DWELL_SECS, DEFAULT_MIN_DWELL_SECS),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=600, step=5, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_SITE_ID,
            default=str(_val(CONF_SITE_ID, DEFAULT_SITE_ID)),
        )] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

        schema[vol.Optional(
            CONF_MQTT_TOPIC_PREFIX,
            default=str(_val(CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_TOPIC_PREFIX)),
        )] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

        schema[vol.Optional(
            CONF_SCAN_INTERVAL,
            default=_int(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=5, max=3600, step=5, mode=selector.NumberSelectorMode.BOX)
        )

        schema[vol.Optional(
            CONF_DEFAULT_ID_TAG,
            default=str(_val(CONF_DEFAULT_ID_TAG, DEFAULT_DEFAULT_ID_TAG)),
        )] = selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
        )

        schema[vol.Optional(
            CONF_DEFAULT_EVSE_ID,
            default=_int(CONF_DEFAULT_EVSE_ID, DEFAULT_DEFAULT_EVSE_ID),
        )] = selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=999, step=1, mode=selector.NumberSelectorMode.BOX)
        )

        return vol.Schema(schema)

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            merged = {**self._config_entry.data, **self._config_entry.options, **user_input}
            if merged.get(CONF_API_PROFILE) in V2_PROFILES:
                if merged.get(CONF_API_PROFILE) == PROFILE_V2:
                    user_input = {**user_input, CONF_READ_ONLY: True}
                    merged[CONF_READ_ONLY] = True
                try:
                    await CitrineConfigFlow._async_validate(self, merged)
                except (CitrineApiError, HasuraError):
                    return self.async_show_form(
                        step_id="init", data_schema=self._options_schema(merged),
                        errors={"base": "cannot_connect"})
                user_input = {**self._config_entry.options, **user_input}
            return self.async_create_entry(title="", data=user_input)

        entry_vals = {**self._config_entry.data, **self._config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=self._options_schema(entry_vals),
        )
