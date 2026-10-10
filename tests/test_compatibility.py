"""Focused fixtures, never a live server, station, wallet or MQTT broker."""
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.citrine_ha import _resolve_context
from custom_components.citrine_ha.citrine_api import (
    CitrineApiError, CitrineAuthError, CitrineClient, CitrineCompatibilityError,
    CitrineReadOnlyError,
)
from custom_components.citrine_ha.compatibility import (
    PROFILE_V2, is_read_only, normalize_v2_discovery,
)
from custom_components.citrine_ha.coordinator import CitrineCoordinator
from custom_components.citrine_ha.hasura_client import HasuraClient, HasuraAuthError, HasuraError
from custom_components.citrine_ha.load_controller import CitrineLoadController
from custom_components.citrine_ha.config_flow import CitrineConfigFlow


def client(session=None, **kwargs):
    return CitrineClient(session or MagicMock(), "http://citrine.invalid", 1,
                         "fixture-token", True, 2, **kwargs)


def discovery():
    return {"data": {
        "ChargingStations": [{"id": 42, "ocppConnectionName": "SIM-CITRINE-LAB-01",
                             "tenantId": 1, "protocol": "ocpp2.0.1", "isOnline": True}],
        "Connectors": [{"id": 8, "stationId": 42, "tenantId": 1, "connectorId": 1,
                        "evseId": 7, "evseTypeConnectorId": 2, "status": "Available",
                        "Evse": {"id": 7, "stationId": 42, "tenantId": 1, "evseTypeId": 3}}],
        "Transactions": [{"id": 91, "stationId": 42, "tenantId": 1,
                          "transactionId": "tx-1", "isActive": True}],
    }}


def test_read_only_defaults_and_v2_cannot_unlock():
    assert is_read_only({})
    assert is_read_only({"read_only": "false"})
    assert not is_read_only({"read_only": False})
    assert is_read_only({"api_profile": PROFILE_V2, "read_only": False})
    assert is_read_only({"read_only": False}, {"read_only": True})


def test_v2_station_database_identity_is_not_command_identity():
    rows = normalize_v2_discovery(discovery(), 1)
    assert rows["stations"][0]["id"] == "SIM-CITRINE-LAB-01"
    assert rows["stations"][0]["databaseId"] == 42
    assert rows["connectors"][0]["stationId"] == "SIM-CITRINE-LAB-01"
    assert rows["transactions"][0]["databaseStationId"] == 42
    assert rows["connectors"][0]["databaseEvseId"] == 7
    assert rows["connectors"][0]["evseId"] == 3
    assert rows["connectors"][0]["connectorId"] == 2


@pytest.mark.parametrize("failure", ["tenant", "missing", "duplicate", "orphan", "evse", "bound"])
def test_v2_rejects_unsafe_discovery(failure):
    data = discovery()
    if failure == "tenant":
        data["data"]["Connectors"][0]["tenantId"] = 2
    elif failure == "missing":
        del data["data"]["ChargingStations"][0]["ocppConnectionName"]
    elif failure == "duplicate":
        data["data"]["ChargingStations"] *= 2
    elif failure == "orphan":
        data["data"]["Transactions"][0]["stationId"] = 77
    elif failure == "evse":
        data["data"]["Connectors"][0]["Evse"]["stationId"] = 77
    else:
        data["data"]["Connectors"] *= 501
    with pytest.raises(ValueError):
        normalize_v2_discovery(data, 1)


@pytest.mark.asyncio
async def test_v2_ping_uses_readiness_and_protected_read_not_legacy_ping():
    c = client(api_profile=PROFILE_V2)
    c._request = AsyncMock(side_effect=[
        {"status": "pass"}, CitrineAuthError("denied"), []])
    await c.ping()
    calls = c._request.call_args_list
    assert [call.args[1] for call in calls] == [
        "/health/ready", "/commands/tariff", "/commands/tariff"]
    assert calls[1].kwargs["send_auth"] is False
    assert calls[2].kwargs["params"] == {"tenantId": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [
    [{"status": "fail"}],
    [{"status": "pass"}, []],
    [{"status": "pass"}, CitrineAuthError("denied"), CitrineAuthError("denied")],
])
async def test_ping_rejects_not_ready_public_api_or_bad_credentials(responses):
    c = client(api_profile=PROFILE_V2)
    c._request = AsyncMock(side_effect=responses)
    with pytest.raises(CitrineApiError):
        await c.ping()


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [CitrineAuthError("denied"),
                                 CitrineApiError("failed (500)"),
                                 CitrineApiError("failed (404)")])
async def test_legacy_discovery_errors_are_not_empty_success(error):
    c = client()
    c._request = AsyncMock(side_effect=error)
    for method in (c.get_charging_stations, c.get_connectors, c.get_transactions):
        with pytest.raises(CitrineApiError):
            await method()


@pytest.mark.asyncio
async def test_bad_discovery_payload_does_not_become_empty_success():
    c = client()
    c._request = AsyncMock(return_value="<html>Sign in</html>")
    with pytest.raises(CitrineCompatibilityError):
        await c.get_charging_stations()
    c._request.return_value = []
    assert await c.get_charging_stations() == []


class Response:
    def __init__(self, status=200, data=None, content_type="application/health+json"):
        self.status, self.data = status, data
        self.headers = {"Content-Type": content_type}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def text(self):
        return "fixture response"
    async def json(self, **kwargs):
        return self.data


@pytest.mark.asyncio
async def test_health_json_content_type_redirects_and_write_guard():
    session = MagicMock()
    session.request.return_value = Response(data={"status": "pass"})
    c = client(session, read_only=True)
    assert await c._request("GET", "/health/ready") == {"status": "pass"}
    assert session.request.call_args.kwargs["allow_redirects"] is False
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(CitrineReadOnlyError):
            await c._request(method, "/commands/tariff")
    assert session.request.call_count == 1
    session.request.return_value = Response(status=302)
    with pytest.raises(CitrineApiError, match="redirect"):
        await c._request("GET", "/health/ready")


@pytest.mark.asyncio
async def test_v2_cannot_send_even_with_read_only_false():
    session = MagicMock()
    c = client(session, api_profile=PROFILE_V2, read_only=False)
    with pytest.raises(CitrineReadOnlyError):
        await c._request("POST", "/ocpp/2.0.1/evdriver/requestStartTransaction")
    session.request.assert_not_called()


@pytest.mark.asyncio
async def test_hasura_auth_html_is_classified_before_json_and_errors_are_safe():
    session = MagicMock()
    response = Response(status=401)
    response.json = AsyncMock(side_effect=ValueError("SECRET"))
    session.post.return_value = response
    c = HasuraClient(session, url="http://hasura.invalid", token="fixture",
                     request_timeout=2, verify_ssl=True)
    with pytest.raises(HasuraAuthError):
        await c.ping()
    response.json.assert_not_called()
    session.post.return_value = Response(data={"errors": [
        {"message": "SECRET", "extensions": {"code": "access-denied"}}]})
    with pytest.raises(HasuraAuthError, match="authentication failed"):
        await c.ping()
    session.post.return_value = Response(status=302)
    with pytest.raises(HasuraError, match="redirect"):
        await c.ping()


@pytest.mark.asyncio
async def test_hasura_mutation_rejected_before_network():
    session = MagicMock()
    c = HasuraClient(session, url="http://hasura.invalid", token=None,
                     request_timeout=2, verify_ssl=True)
    with pytest.raises(HasuraError, match="read-only"):
        await c.query("mutation { delete_ChargingStations(where: {}) { affected_rows } }")
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_v2_config_validation_requires_hasura_and_uses_discovery_query():
    flow = CitrineConfigFlow()
    flow.hass = MagicMock()
    values = {"api_profile": PROFILE_V2, "base_url": "http://citrine.invalid",
              "tenant_id": 1, "request_timeout": 2}
    with patch("custom_components.citrine_ha.config_flow.async_get_clientsession"):
        with pytest.raises(CitrineCompatibilityError, match="Hasura"):
            await flow._async_validate(values)
        with patch("custom_components.citrine_ha.config_flow.CitrineClient") as api, \
             patch("custom_components.citrine_ha.config_flow.HasuraClient") as gql:
            api.return_value.ping = AsyncMock()
            gql.return_value.query = AsyncMock(return_value=discovery())
            await flow._async_validate({**values, "hasura_url": "http://hasura.invalid"})
            api.return_value.ping.assert_awaited_once()
            gql.return_value.query.assert_awaited_once()
            gql.return_value.ping.assert_not_called()


def controller_env(read_only=True):
    hass, entry, coord = MagicMock(), MagicMock(), MagicMock()
    entry.data, entry.options = {"read_only": read_only}, {}
    coord.last_update_success = True
    c = client(read_only=read_only)
    c.set_station_limit = AsyncMock()
    ctl = CitrineLoadController(hass, entry=entry, coordinator=coord, client=c)
    ctl.mqtt_publisher.async_publish_intent = AsyncMock()
    return ctl, hass, c


@pytest.mark.asyncio
async def test_read_only_blocks_timer_mqtt_manual_recompute_and_direct_limit():
    ctl, hass, c = controller_env()
    with patch("custom_components.citrine_ha.load_controller.async_track_time_interval") as track:
        await ctl.async_start()
        track.assert_not_called()
    await ctl.async_recompute()
    await ctl._apply_station_limit(MagicMock(), 5000)
    c.set_station_limit.assert_not_called()
    ctl.mqtt_publisher.async_publish_intent.assert_not_called()
    assert ctl.state_status == "Read only"


@pytest.mark.asyncio
async def test_failed_discovery_blocks_control_even_with_cached_station():
    ctl, hass, c = controller_env(False)
    ctl.coordinator.last_update_success = False
    await ctl.async_recompute()
    await ctl._apply_station_limit(MagicMock(), 5000)
    c.set_station_limit.assert_not_called()
    ctl.mqtt_publisher.async_publish_intent.assert_not_called()
    assert ctl.state_status == "Discovery unavailable"


def test_read_only_service_resolution_and_ambiguous_entry():
    hass, entry, coord = MagicMock(), MagicMock(), MagicMock()
    entry.data, entry.options = {}, {}
    coord.last_update_success = True
    hass.data = {"citrine_ha": {"one": {"coordinator": coord}}}
    hass.config_entries.async_get_entry.return_value = entry
    call = MagicMock(data={"entry_id": "one"})
    with pytest.raises(HomeAssistantError, match="Read-only"):
        _resolve_context(hass, call)
    assert _resolve_context(hass, call, control=False)["entry"] is entry
    hass.data["citrine_ha"]["two"] = {"coordinator": coord}
    with pytest.raises(HomeAssistantError, match="entry_id"):
        _resolve_context(hass, MagicMock(data={}), control=False)


@pytest.mark.asyncio
async def test_v2_coordinator_empty_success_and_auth_failure_no_fallback(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    c, hasura = MagicMock(), MagicMock()
    c.ping = AsyncMock()
    hasura.query = AsyncMock(return_value={"data": {
        "ChargingStations": [], "Connectors": [], "Transactions": []}})
    coord = CitrineCoordinator(hass, hasura_client=hasura, citrine_client=c,
                              entry_data={"tenant_id": 1, "api_profile": PROFILE_V2},
                              entry_options={})
    result = await coord._async_update_data()
    assert result["stations"] == []
    assert result["source"] == "hasura_v2"
    hasura.query.side_effect = HasuraAuthError("authentication failed")
    coord.data = {"stations": [{"id": "old"}]}
    with pytest.raises(UpdateFailed, match="authentication"):
        await coord._async_update_data()
    c.discover_chargers_rest.assert_not_called()


@pytest.mark.asyncio
async def test_telemetry_cannot_revalidate_failed_discovery(tmp_path):
    coord = CitrineCoordinator(HomeAssistant(str(tmp_path)),
                              hasura_client=None, citrine_client=MagicMock(),
                              entry_data={"tenant_id": 1}, entry_options={})
    coord.last_update_success = False
    with pytest.raises(UpdateFailed):
        await coord.async_refresh_intake_telemetry()
    with pytest.raises(UpdateFailed):
        coord.clear_intake_telemetry_error()
