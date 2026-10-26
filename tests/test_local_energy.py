"""OCPP 2.1 local energy: local_energy policy, TxProfile modes, LocalGeneration profiles and charging limits."""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import voluptuous as vol  # after homeassistant: the module the service schemas use

from custom_components.citrine_ha.citrine_api import (
    CitrineCompatibilityError, CitrineHttpError, CitrineTransportError)
from custom_components.citrine_ha.lab_control import (
    CHARGING_LIMITS, PATHS, PROTOCOL, STATION, LocalOutcomeUnknownError, ScopedLabClient,
    build_profile, charging_limit_now, parse_local_energy)
from custom_components.citrine_ha.lab_entities import (
    SERVICE, SERVICE_LIMITS, SERVICE_LOCAL_CLEAR, SERVICE_LOCAL_SET, LabChargingLimitSensor,
    setup_lab)

PARAMS = {"identifier": STATION, "tenantId": 1}
MODES = ["ChargingOnly", "CentralSetpoint", "ExternalLimits", "ExternalSetpoint", "LocalLoadBalancing"]
CONFIRMED = [{"success": True, "payload": "sent"}]
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)


def local_policy(**kw):
    return {"local_generation": {"purpose": "LocalGeneration", "ids": [9100, 9199], "evse_ids": [0, 1],
                                 "max_duration_s": 86400, "max_periods": 48, "protocol": "ocpp2.1"},
            "operation_modes": list(MODES), "charging_limits": CHARGING_LIMITS, **kw}


def lg(**kw):
    return {**local_policy()["local_generation"], **kw}


def policy(local=None, **kw):
    p = {"station_id": STATION, "protocol": PROTOCOL, "can_control": True,
         "max_import_w": 25000, "max_export_w": 25000, "max_profile_seconds": 600,
         "max_periods": 10, "automatic_control": False, "real_devices_enabled": False,
         "clear_requires_closed_transaction": True, "commands": list(PATHS.values()), **kw}
    if local is not None:
        p["local_energy"] = local
    return p


def limit(**kw):
    return {"evse_id": 0, "source": "EMS", "grid_critical": False, "local_generation": True, "active": True,
            "schedule": [{"id": 1, "chargingRateUnit": "W", "duration": 3600, "chargingSchedulePeriod": [
                {"startPeriod": 0, "limit": 4000}, {"startPeriod": 600, "limit": 2500},
                {"startPeriod": 1200, "limit": 1000}]}],
            "reported_at": "2026-10-26T01:55:00+00:00", "cleared_at": None, **kw}


LIMITS = {"station_id": STATION, "active": [limit()], "recent": [limit()]}


def client(pol=None, reply=None, limits=None):
    """A lab client whose transport answers GETs and records POSTs."""
    c = ScopedLabClient(MagicMock(), "http://lab.invalid", 1, "reader", True, 3,
                        command_token="commander", read_only=False)
    c.ready = AsyncMock()
    c.posts = []
    state = {"policy": policy(local_policy()) if pol is None else pol,
             "reply": CONFIRMED if reply is None else reply, "limits": LIMITS if limits is None else limits}

    async def request(method, path, **kw):
        if method == "GET":
            if path == "/lab/control-policy":
                return state["policy"]
            if isinstance(state["limits"], Exception):
                raise state["limits"]
            return state["limits"]
        c.posts.append((path, kw["params"], kw["json"]))
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]
    c._command_client._request = AsyncMock(side_effect=request)
    c.fake = state
    return c


# Policy: local_energy is optional and never fails compatibility

@pytest.mark.parametrize("local,enabled,invalid", [
    (None, False, False), (local_policy(), True, False),
    (local_policy(operation_modes=["CentralSetpoint", "ExternalLimits", "FutureMode"]), True, False),
    ({"operation_modes": ["ExternalLimits"]}, True, False),
    ("yes", False, True), ([], False, True),
    (local_policy(charging_limits="/evil"), False, True),
    (local_policy(operation_modes="ExternalLimits"), False, True),
    (local_policy(operation_modes=["ExternalLimits", "ExternalLimits"]), False, True),
    (local_policy(operation_modes=[3]), False, True),
    (local_policy(local_generation="on"), False, True),
    (local_policy(local_generation=lg(purpose="TxProfile")), False, True),
    (local_policy(local_generation=lg(protocol="ocpp2.0.1")), False, True),
    (local_policy(local_generation=lg(ids=[9199, 9100])), False, True),
    (local_policy(local_generation=lg(ids=[9100])), False, True),
    (local_policy(local_generation=lg(evse_ids=[])), False, True),
    (local_policy(local_generation=lg(max_duration_s=86401)), False, True),
    (local_policy(local_generation=lg(max_periods=49)), False, True),
])
@pytest.mark.asyncio
async def test_policy_local_energy_optional(local, enabled, invalid):
    c = client(policy(local))
    await c.control_policy()
    assert c.local_energy["enabled"] is enabled
    assert c.local_energy.get("invalid", False) is invalid
    assert parse_local_energy(policy(local)) == c.local_energy


def test_parsed_policy_parts():
    p = parse_local_energy(policy(local_policy(operation_modes=["CentralSetpoint", "ExternalLimits", "Other"])))
    assert p["operation_modes"] == ["CentralSetpoint", "ExternalLimits"]
    assert p["local_generation"]["ids"] == [9100, 9199] and p["charging_limits"] == CHARGING_LIMITS
    # A part left out is not offered; modes default to the base two.
    p = parse_local_energy(policy({"charging_limits": CHARGING_LIMITS}))
    assert p["local_generation"] is None and p["operation_modes"] == ["ChargingOnly", "CentralSetpoint"]


@pytest.mark.asyncio
async def test_malformed_local_energy_warns_once(caplog):
    c = client(policy(local_policy(charging_limits="/x")))
    await c.control_policy()
    await c.control_policy()
    assert caplog.text.count("local_energy policy is malformed") == 1


# set_tx_profile with the bounded local modes

def tx_station(sid=STATION, protocol=PROTOCOL, online=True, tx="tx-1"):
    return {"id": sid, "protocol": protocol, "isOnline": online, "activeTransactionId": tx, "lab_meter": {}}


async def services(c, stations):
    coordinator = MagicMock();coordinator.async_request_refresh = AsyncMock()
    coordinator.data = {"stations": stations};coordinator.last_update_success = True
    hass = MagicMock();hass.data = {"citrine_ha": {"entry": {"client": c, "coordinator": coordinator}}}
    hass.services.has_service.return_value = False
    entry = MagicMock();entry.entry_id = "entry"
    await setup_lab(hass, entry, [].extend, "button")
    calls = {call.args[1]: call for call in hass.services.async_register.call_args_list}
    for name in (SERVICE_LOCAL_SET, SERVICE_LOCAL_CLEAR, SERVICE_LIMITS):
        assert calls[name].kwargs["supports_response"] == SupportsResponse.OPTIONAL

    def call(name, **data):
        schema = calls[name].kwargs["schema"]
        return calls[name].args[2](MagicMock(data=schema({"entry_id": "entry", **data})))
    return call


def local_period(mode="ExternalLimits", **kw):
    return {"startPeriod": 0, "limit": 11000, "dischargeLimit": -7000, "operationMode": mode, **kw}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ExternalLimits", "ExternalSetpoint", "LocalLoadBalancing"])
async def test_tx_profile_accepts_offered_local_modes(mode):
    c = client();await c.control_policy()
    call = await services(c, [tx_station()])
    periods = [local_period(mode), {**local_period(mode), "startPeriod": 60, "limit": 0, "dischargeLimit": 0},
               {"startPeriod": 120, "limit": 5000, "dischargeLimit": -5000, "setpoint": 1000,
                "operationMode": "CentralSetpoint"}]
    out = await call(SERVICE, periods=periods, duration=240)
    path, params, body = c.posts[-1]
    assert path == PATHS["profile"] and params == PARAMS
    profile = body["chargingProfile"]
    assert profile["chargingProfilePurpose"] == "TxProfile" and profile["transactionId"] == "tx-1"
    assert profile["chargingSchedule"][0]["chargingSchedulePeriod"] == periods
    assert out["command_state"] == "dispatched_awaiting_station_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("pol,period,match", [
    (None, local_period(setpoint=2000), "no setpoint is allowed"),
    (None, {k: v for k, v in local_period().items() if k != "dischargeLimit"}, "dischargeLimit required"),
    (None, {k: v for k, v in local_period().items() if k != "limit"}, "limit required"),
    (None, local_period(numberPhases=3), "not numberPhases"),
    (None, local_period(dischargeLimit=100), "dischargeLimit must be -25000 to 0 W"),
    (None, local_period(limit=25001), "limit must be 0 to 25000 W"),
    (policy(local_policy(operation_modes=["CentralSetpoint", "ExternalLimits"])),
     local_period("LocalLoadBalancing"), "LocalLoadBalancing is not offered.*CentralSetpoint, ExternalLimits"),
    (policy(), local_period(), "ExternalLimits is not offered by the server policy "
                               r"\(offered: ChargingOnly, CentralSetpoint\)"),
    (policy("bad"), local_period(), "local_energy policy is malformed"),
])
async def test_tx_profile_rejects_local_mode_misuse_before_send(pol, period, match):
    c = client(pol);await c.control_policy()
    call = await services(c, [tx_station()])
    with pytest.raises(ServiceValidationError, match=match):
        await call(SERVICE, periods=[period])
    assert c.posts == []


def test_build_profile_needs_the_mode_offered():
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t", [local_period()])
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t", [local_period(setpoint=0)], modes=("ExternalLimits",))
    p = build_profile("t", [local_period()], modes=("ExternalLimits",))
    assert p["chargingProfile"]["chargingSchedule"][0]["chargingSchedulePeriod"] == [local_period()]


@pytest.mark.asyncio
async def test_tx_profile_mode_withdrawn_since_last_poll_is_refused():
    c = client();await c.control_policy()
    c.fake["policy"] = policy(local_policy(operation_modes=["CentralSetpoint"]))
    with pytest.raises(CitrineCompatibilityError, match="not offered.*ExternalLimits"):
        await c.submit_schedule(protocol=PROTOCOL, station_id=STATION, transaction_id="tx-1",
                                periods=[local_period()], immediate=True)
    assert c.posts == [] and c.last_command == {"state": "not_sent"}


# set_local_generation_profile

def gen_station(**kw):
    return tx_station(tx=None, **kw)


@pytest.mark.asyncio
async def test_local_generation_absolute_body_and_reply():
    c = client();await c.control_policy()
    call = await services(c, [gen_station()])
    out = await call(SERVICE_LOCAL_SET, start_time="2026-10-27T10:00:00+10:00", duration=18000, evse_id=1,
                     profile_id=9150, stack_level=2,
                     periods=[{"start_period": 0, "limit": 3000}, {"start_period": 7200, "limit": "6000.5",
                                                                  "number_phases": 3}])
    body = {"evseId": 1, "chargingProfile": {
        "id": 9150, "stackLevel": 2, "chargingProfilePurpose": "LocalGeneration", "chargingProfileKind": "Absolute",
        "chargingSchedule": [{"id": 9150, "startSchedule": "2026-10-27T00:00:00+00:00", "duration": 18000,
                              "chargingRateUnit": "W", "chargingSchedulePeriod": [
                                  {"startPeriod": 0, "limit": 3000},
                                  {"startPeriod": 7200, "limit": 6000.5, "numberPhases": 3}]}]}}
    assert c.posts == [(PATHS["profile"], PARAMS, body)]
    assert out == {"sent": body, "station_id": STATION, "station_status": "dispatched",
                   "station_response": CONFIRMED}
    # Never touches the Start/Stop/schedule outcome; the limits are re-read after it.
    assert c.last_command == {"state": "not_sent"} and c.charging_limits == LIMITS


@pytest.mark.asyncio
async def test_local_generation_relative_defaults_with_an_active_transaction():
    c = client();await c.control_policy()
    call = await services(c, [tx_station(tx="tx-9")])
    out = await call(SERVICE_LOCAL_SET, periods=[{"start_period": 0, "limit": 0}])
    assert out["sent"] == {"evseId": 0, "chargingProfile": {
        "id": 9100, "stackLevel": 1, "chargingProfilePurpose": "LocalGeneration", "chargingProfileKind": "Relative",
        "chargingSchedule": [{"id": 9100, "duration": 86400, "chargingRateUnit": "W",
                              "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 0}]}]}}
    assert "transactionId" not in out["sent"]["chargingProfile"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pol,fields,match", [
    (None, {"periods": [{"start_period": 5, "limit": 1}]}, "first period must have start_period 0"),
    (None, {"periods": [{"start_period": 0, "limit": 1}, {"start_period": 0, "limit": 2}]},
     "must be after the previous"),
    (None, {"duration": 600, "periods": [{"start_period": 0, "limit": 1}, {"start_period": 600, "limit": 2}]},
     "below duration 600"),
    (None, {"profile_id": 9001, "periods": [{"start_period": 0, "limit": 1}]}, "profile_id must be 9100 to 9199"),
    (policy(local_policy(local_generation=lg(evse_ids=[0]))),
     {"evse_id": 1, "periods": [{"start_period": 0, "limit": 1}]}, "evse_id must be one of 0"),
    (policy(local_policy(local_generation=lg(max_duration_s=3600))),
     {"periods": [{"start_period": 0, "limit": 1}]}, "duration must be 1 to 3600 s"),
    (policy(local_policy(local_generation=lg(max_periods=2))),
     {"periods": [{"start_period": i, "limit": 1} for i in range(3)]}, "1 to 2 periods"),
    (policy({"charging_limits": CHARGING_LIMITS}), {"periods": [{"start_period": 0, "limit": 1}]},
     "local_generation is not offered"),
    (policy(), {"periods": [{"start_period": 0, "limit": 1}]}, "local_generation is not offered"),
])
async def test_local_generation_validation_before_send(pol, fields, match):
    c = client(pol);await c.control_policy()
    call = await services(c, [gen_station()])
    with pytest.raises(ServiceValidationError, match=match):
        await call(SERVICE_LOCAL_SET, **fields)
    assert c.posts == []


@pytest.mark.asyncio
async def test_local_generation_schema_and_station_checks():
    c = client();await c.control_policy()
    call = await services(c, [gen_station()])
    for bad in ({"evse_id": 2}, {"stack_level": 11}, {"duration": 86401}, {"duration": 0}, {"periods": []},
                {"periods": [{"start_period": 0, "limit": 25001}]}, {"periods": [{"start_period": 0}]},
                {"periods": [{"start_period": 0, "limit": 1, "number_phases": 2}]},
                {"periods": [{"start_period": 0, "limit": 1, "setpoint": 1}]}):
        with pytest.raises(vol.Invalid):
            await call(SERVICE_LOCAL_SET, **{"periods": [{"start_period": 0, "limit": 1}], **bad})
    call = await services(c, [gen_station(online=False)])
    with pytest.raises(HomeAssistantError, match="online"):
        await call(SERVICE_LOCAL_SET, periods=[{"start_period": 0, "limit": 1}])
    call = await services(c, [gen_station(protocol="ocpp2.0.1")])
    with pytest.raises(ServiceValidationError, match="need OCPP 2.1"):
        await call(SERVICE_LOCAL_CLEAR)
    assert c.posts == []


@pytest.mark.asyncio
async def test_client_refuses_bodies_outside_the_policy():
    c = client();await c.control_policy()
    good = {"evseId": 0, "chargingProfile": {
        "id": 9100, "stackLevel": 1, "chargingProfilePurpose": "LocalGeneration", "chargingProfileKind": "Relative",
        "chargingSchedule": [{"id": 1, "duration": 60, "chargingRateUnit": "W",
                              "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1}]}]}}
    assert await c.set_local_generation_profile(good) == CONFIRMED
    sched = good["chargingProfile"]["chargingSchedule"][0]
    for bad in ({**good, "evseId": 2},
                {**good, "chargingProfile": {**good["chargingProfile"], "transactionId": "t"}},
                {**good, "chargingProfile": {**good["chargingProfile"], "chargingProfileKind": "Absolute"}},
                {**good, "chargingProfile": {**good["chargingProfile"], "chargingSchedule": [
                    {**sched, "startSchedule": "2026-10-26T00:00:00Z"}]}},
                {**good, "chargingProfile": {**good["chargingProfile"], "chargingSchedule": [
                    {**sched, "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1, "setpoint": 1}]}]}},
                {**good, "chargingProfile": {**good["chargingProfile"], "chargingSchedule": [
                    {**sched, "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1,
                                                          "operationMode": "ExternalLimits"}]}]}}):
        with pytest.raises(CitrineCompatibilityError):
            await c.set_local_generation_profile(bad)
    with pytest.raises(CitrineCompatibilityError):
        await c.clear_local_generation_profile(9001)
    assert len(c.posts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error,match", [
    (CitrineHttpError("x", 502, "not sent"), r"bad gateway \(502\) \(not sent\); the outcome may be unknown, "
                                             "do not retry automatically"),
    (CitrineHttpError("x", 504), r"gateway timeout \(504\); the outcome may be unknown"),
    (CitrineTransportError("timeout"), "no gateway reply; the outcome may be unknown"),
    (CitrineHttpError("Citrine API POST failed (409): station not online on OCPP 2.1", 409,
                      "station not online on OCPP 2.1"), "station not online on OCPP 2.1"),
])
async def test_gateway_errors_map_to_home_assistant_error(error, match):
    c = client(reply=error);await c.control_policy()
    call = await services(c, [gen_station()])
    with pytest.raises(HomeAssistantError, match=match) as err:
        await call(SERVICE_LOCAL_SET, periods=[{"start_period": 0, "limit": 1}])
    assert not isinstance(err.value, ServiceValidationError)
    assert len(c.posts) == 1 and c.csms_available


@pytest.mark.asyncio
async def test_unconfirmed_dispatch_is_outcome_unknown():
    c = client(reply=[{"success": False}]);await c.control_policy()
    with pytest.raises(LocalOutcomeUnknownError, match="do not retry automatically"):
        await c.clear_local_generation_profile(9100)


# clear_local_generation_profile and get_charging_limits

@pytest.mark.asyncio
async def test_clear_service_body_any_time():
    c = client();await c.control_policy()
    call = await services(c, [tx_station(tx="tx-1")])
    out = await call(SERVICE_LOCAL_CLEAR)
    assert c.posts[-1] == (PATHS["clear"], PARAMS, {"chargingProfileId": 9100})
    out = await call(SERVICE_LOCAL_CLEAR, profile_id=9199)
    assert out["sent"] == {"chargingProfileId": 9199} and out["station_status"] == "dispatched"
    with pytest.raises(ServiceValidationError, match="9100 to 9199"):
        await call(SERVICE_LOCAL_CLEAR, profile_id=9001)


@pytest.mark.asyncio
async def test_get_charging_limits_service_returns_reply_even_offline():
    c = client();await c.control_policy()
    call = await services(c, [gen_station(online=False)])
    assert await call(SERVICE_LIMITS) == LIMITS
    assert c._command_client._request.call_args_list[-1].args == ("GET", CHARGING_LIMITS)
    assert c.charging_limits == LIMITS
    c.fake["limits"] = ["not", "a", "reply"]
    with pytest.raises(HomeAssistantError, match="Unexpected"):
        await call(SERVICE_LIMITS)
    c.fake["limits"] = CitrineHttpError("Citrine API GET failed (409): station unknown", 409, "station unknown")
    with pytest.raises(HomeAssistantError, match="station unknown"):
        await call(SERVICE_LIMITS)
    off = client(policy(local_policy(charging_limits=None)));await off.control_policy()
    call = await services(off, [gen_station()])
    with pytest.raises(ServiceValidationError, match="charging_limits is not offered"):
        await call(SERVICE_LIMITS)


@pytest.mark.asyncio
async def test_poll_throttled_never_raises_and_warns_once(caplog):
    c = client();await c.control_policy()
    await c.poll_charging_limits(now=NOW)
    await c.poll_charging_limits(now=NOW + timedelta(seconds=10))
    assert c.charging_limits == LIMITS
    reads = [x for x in c._command_client._request.call_args_list if x.args == ("GET", CHARGING_LIMITS)]
    assert len(reads) == 1
    c.fake["limits"] = CitrineHttpError("x", 502, "read refused")
    await c.poll_charging_limits(now=NOW + timedelta(seconds=30))
    await c.poll_charging_limits(now=NOW + timedelta(seconds=31))
    assert c.charging_limits is None and caplog.text.count("Charging limits read failed") == 1
    c.fake["limits"] = LIMITS
    await c.poll_charging_limits(now=NOW + timedelta(seconds=32))
    assert c.charging_limits == LIMITS and c.charging_limits_error is None
    # Without local_energy.charging_limits nothing is read.
    off = client(policy());await off.control_policy()
    await off.poll_charging_limits()
    assert off.charging_limits is None


# EMS charging limit sensor

def reply(*limits):
    return {"station_id": STATION, "active": list(limits), "recent": list(limits)}


def test_sensor_no_active_limit_is_unknown():
    value, attrs = charging_limit_now(reply(), NOW)
    assert value is None and attrs["active_count"] == 0 and attrs["source"] is None
    assert charging_limit_now(None, NOW)[0] is None
    assert charging_limit_now(reply(limit(active=False)), NOW)[0] is None
    assert charging_limit_now(reply(limit(schedule=None)), NOW) == (None, {
        "source": "EMS", "evse_id": 0, "local_generation": True, "grid_critical": False,
        "reported_at": "2026-10-26T01:55:00+00:00", "active_count": 1})


@pytest.mark.parametrize("elapsed,expected", [
    (-1, None), (0, 4000), (599, 4000), (600, 2500), (1199.5, 2500), (1200, 1000), (3599, 1000), (3600, None),
])
def test_sensor_relative_to_reported_at_period_boundaries(elapsed, expected):
    start = datetime(2026, 10, 26, 1, 55, tzinfo=UTC)
    value, attrs = charging_limit_now(reply(limit()), start + timedelta(seconds=elapsed))
    assert value == expected
    assert attrs["schedule_start"] == start.isoformat()


def test_sensor_uses_start_schedule_and_newest_limit():
    sched = {"id": 2, "startSchedule": "2026-10-26T11:30:00+10:00", "chargingRateUnit": "W",
             "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 7000}, {"startPeriod": 1800, "limit": 0}]}
    newest = limit(source="SO", grid_critical=True, evse_id=None, reported_at="2026-10-26T00:00:00+00:00",
                   schedule=[sched])
    # 02:00 UTC is 30 minutes after the 01:30 UTC start: the second period.
    value, attrs = charging_limit_now(reply(newest, limit()), NOW)
    assert value == 0
    assert attrs == {"source": "SO", "evse_id": None, "local_generation": True, "grid_critical": True,
                     "reported_at": "2026-10-26T00:00:00+00:00", "active_count": 2, "charging_rate_unit": "W",
                     "schedule_start": "2026-10-26T01:30:00+00:00"}
    assert charging_limit_now(reply(newest), NOW - timedelta(seconds=1))[0] == 7000
    # No duration: open ended.
    assert charging_limit_now(reply(newest), NOW + timedelta(days=2))[0] == 0


def test_sensor_amp_limit_is_unknown_with_its_unit():
    amps = limit(schedule=[{"id": 1, "chargingRateUnit": "A", "chargingSchedulePeriod": [
        {"startPeriod": 0, "limit": 16}]}])
    value, attrs = charging_limit_now(reply(amps), NOW)
    assert value is None and attrs["charging_rate_unit"] == "A" and attrs["schedule_limit"] == 16


@pytest.mark.asyncio
async def test_sensor_entity_created_only_with_charging_limits():
    for pol, expected in ((policy(local_policy()), True), (policy(), False),
                          (policy(local_policy(charging_limits=None)), False)):
        c = client(pol);await c.control_policy()
        coordinator = MagicMock();coordinator.data = {"stations": [gen_station()]}
        coordinator.last_update_success = True
        ctx = {"client": c, "coordinator": coordinator}
        hass = MagicMock();hass.data = {"citrine_ha": {"entry": ctx}}
        entry = MagicMock();entry.entry_id = "entry"
        added = []
        await setup_lab(hass, entry, added.extend, "sensor")
        sensors = [e for e in added if isinstance(e, LabChargingLimitSensor)]
        assert bool(sensors) is expected
        if sensors:
            s = sensors[0]
            assert s._attr_name == "EMS charging limit" and s.unique_id == f"entry_{STATION}_lab_ems_charging_limit"
            assert s.available is False
            await c.poll_charging_limits()
            assert s.available is True and s.extra_state_attributes["active_count"] == 1


def test_service_names_pinned():
    import json
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1] / "custom_components" / "citrine_ha"
    names = {SERVICE_LOCAL_SET, SERVICE_LOCAL_CLEAR, SERVICE_LIMITS}
    assert names == {"set_local_generation_profile", "clear_local_generation_profile", "get_charging_limits"}
    assert names <= set(yaml.safe_load((root / "services.yaml").read_text()))
    for strings in ("strings.json", "translations/en.json"):
        data = json.loads((root / strings).read_text())
        assert names <= set(data["services"]), strings
        assert data["entity"]["sensor"]["ems_charging_limit"]["name"] == "EMS charging limit"
    assert json.loads((root / "manifest.json").read_text())["version"] == "0.8.0"
    assert (root / "translations/en.json").read_bytes().count(b"\r\n") == \
        (root / "translations/en.json").read_bytes().count(b"\n")
