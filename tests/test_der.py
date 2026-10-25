"""OCPP 2.1 DER control: der_commands policy, client routes and the four services."""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import voluptuous as vol  # after homeassistant: the module the service schemas use

from custom_components.citrine_ha.citrine_api import (
    CitrineAuthError, CitrineClient, CitrineCompatibilityError, CitrineHttpError,
    CitrineTransportError, error_detail)
from custom_components.citrine_ha.lab_control import (
    DER_EVENTS, DER_ROUTES, PATHS, PROTOCOL, STATION, DerOutcomeUnknownError, ScopedLabClient,
    parse_der_commands)
from custom_components.citrine_ha.lab_entities import (
    SERVICE_DER_CLEAR, SERVICE_DER_EVENTS, SERVICE_DER_GET, SERVICE_DER_SET, setup_lab)

TYPES = ["LimitMaxDischarge", "FixedPFInject", "FixedPFAbsorb", "FixedVar"]
PARAMS = {"identifier": STATION, "tenantId": 1}
EVENTS = {"station_id": STATION, "events": [
    {"action": "NotifyDERStartStop", "timestamp": "2026-10-25T01:00:00Z",
     "payload": {"controlId": "lim-1", "started": True}}]}


def der_policy(**kw):
    return {"enabled": True, "station_id": STATION, "protocol": PROTOCOL,
            "routes": dict(DER_ROUTES), "events": DER_EVENTS, "control_types": list(TYPES),
            "limits": {"pct_max_discharge_power": [0, 100], "displacement": [0, 1],
                       "var_setpoint_pct": [-100, 100], "max_duration_s": 86400}, **kw}


def policy(der=None, **kw):
    p = {"station_id": STATION, "protocol": PROTOCOL, "can_control": True,
         "max_import_w": 11000, "max_export_w": 11000, "max_profile_seconds": 600,
         "max_periods": 10, "automatic_control": False, "real_devices_enabled": False,
         "clear_requires_closed_transaction": True, "commands": list(PATHS.values()), **kw}
    if der is not None:
        p["der_commands"] = der
    return p


def client(pol=None, reply=None):
    """A lab client whose transport answers GETs and records POSTs."""
    c = ScopedLabClient(MagicMock(), "http://lab.invalid", 1, "reader", True, 3,
                        command_token="commander", read_only=False)
    c.ready = AsyncMock()
    c.posts = []
    state = {"policy": policy(der_policy()) if pol is None else pol,
             "reply": {"status": "Accepted"} if reply is None else reply, "events": EVENTS}

    async def request(method, path, **kw):
        if method == "GET":
            return state["policy"] if path == "/lab/control-policy" else state["events"]
        c.posts.append((path, kw["params"], kw["json"]))
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]
    c._command_client._request = AsyncMock(side_effect=request)
    c.fake = state
    return c


# Policy: der_commands is optional and never fails compatibility

@pytest.mark.parametrize("der,enabled", [
    (None, False), ({"enabled": False}, False), ("yes", False), ({"enabled": "true"}, False),
    (der_policy(), True), (der_policy(control_types=["FixedVar"]), True),
    (der_policy(limits={"max_duration_s": 3600}), True),
    (der_policy(station_id="SIM-EV-OTHER"), False), (der_policy(protocol="ocpp2.0.1"), False),
    (der_policy(routes={**DER_ROUTES, "set": "/evil"}), False),
    (der_policy(routes={"set": DER_ROUTES["set"]}), False), (der_policy(events="/lab/other"), False),
    (der_policy(control_types=[]), False), (der_policy(control_types=["Curve"]), False),
    (der_policy(control_types=["FixedVar", "FixedVar"]), False), (der_policy(control_types="FixedVar"), False),
    (der_policy(limits=[]), False), (der_policy(limits={"pct_max_discharge_power": [0, 101]}), False),
    (der_policy(limits={"displacement": [1, 0]}), False), (der_policy(limits={"max_duration_s": 0}), False),
    (der_policy(limits={"other": 1}), False),
])
@pytest.mark.asyncio
async def test_policy_der_commands_optional(der, enabled):
    c = client(policy(der))
    await c.control_policy()
    assert c.der_commands["enabled"] is enabled
    assert parse_der_commands(policy(der)) == c.der_commands
    # Malformed is flagged; absent or disabled is simply off.
    assert c.der_commands.get("invalid", False) is (not enabled and isinstance(der, dict)
                                                    and der.get("enabled") is True)
    if enabled:
        assert c.der_commands["station_id"] == STATION and c.der_commands["routes"] == DER_ROUTES


@pytest.mark.asyncio
async def test_malformed_der_policy_warns_once(caplog):
    c = client(policy(der_policy(routes={})))
    await c.control_policy()
    await c.control_policy()
    assert caplog.text.count("der_commands policy is malformed") == 1


# Client: only the policy's routes, bounds checked before send

def set_body(ctype="LimitMaxDischarge", **control):
    field = {"LimitMaxDischarge": "limitMaxDischarge", "FixedPFInject": "fixedPFInject",
             "FixedPFAbsorb": "fixedPFAbsorb", "FixedVar": "fixedVar"}[ctype]
    default = {"LimitMaxDischarge": {"pctMaxDischargePower": 50},
               "FixedVar": {"setpoint": -20, "unit": "PctMaxVar"}}.get(
        ctype, {"displacement": 0.95, "excitation": True})
    return {"isDefault": False, "controlId": "lim-1", "controlType": ctype,
            field: {"priority": 1, **default, **control}}


@pytest.mark.asyncio
async def test_client_routes_and_verbatim_replies():
    c = client();await c.control_policy()
    assert await c.set_der_control(set_body()) == {"status": "Accepted"}
    c.fake["reply"] = {"status": "Accepted", "reports": [{"requestId": 7}]}
    assert (await c.get_der_control({"requestId": 7}))["reports"] == [{"requestId": 7}]
    await c.clear_der_control({"isDefault": True, "controlType": "FixedVar"})
    assert c.posts == [(DER_ROUTES["set"], PARAMS, set_body()), (DER_ROUTES["get"], PARAMS, {"requestId": 7}),
                       (DER_ROUTES["clear"], PARAMS, {"isDefault": True, "controlType": "FixedVar"})]
    assert await c.der_events() == EVENTS
    assert c._command_client._request.call_args_list[-1].args == ("GET", DER_EVENTS)
    # DER messages never replace the Start/Stop/profile outcome.
    assert c.last_command == {"state": "not_sent"}


@pytest.mark.parametrize("body", [
    set_body(pctMaxDischargePower=101), set_body(pctMaxDischargePower=True),
    set_body("FixedPFInject", displacement=0), set_body("FixedPFAbsorb", displacement=1.1),
    set_body("FixedPFInject", excitation="yes"), set_body("FixedVar", setpoint=-101),
    set_body("FixedVar", unit="W"), set_body(priority=-1), set_body(duration=0),
    set_body(duration=86401), set_body(startTime="2026-10-25T10:00:00"),
    set_body(setpoint=1), {**set_body(), "controlId": "bad id"},
    {**set_body(), "fixedVar": {"priority": 1, "setpoint": 0, "unit": "PctMaxW"}},
    {**set_body(), "controlType": "FixedVar"}, {**set_body(), "isDefault": "no"},
])
@pytest.mark.asyncio
async def test_client_refuses_set_outside_bounds_before_send(body):
    c = client();await c.control_policy()
    with pytest.raises(CitrineCompatibilityError):
        await c.set_der_control(body)
    assert c.posts == []


@pytest.mark.asyncio
async def test_client_refuses_when_disabled_or_switched_off_since_last_poll():
    c = client(policy());await c.control_policy()
    with pytest.raises(CitrineCompatibilityError, match="DER controls are disabled"):
        await c.set_der_control(set_body())
    with pytest.raises(CitrineCompatibilityError, match="disabled"):
        await c.der_events()
    c = client();await c.control_policy()
    c.fake["policy"] = policy({"enabled": False})
    with pytest.raises(CitrineCompatibilityError, match="disabled"):
        await c.clear_der_control({"isDefault": False})
    # Limits narrowed since the last poll: checked again against the fresh policy.
    n = client();await n.control_policy()
    n.fake["policy"] = policy(der_policy(limits={"pct_max_discharge_power": [0, 40]}))
    with pytest.raises(CitrineCompatibilityError, match="outside"):
        await n.set_der_control(set_body())
    assert n.posts == []
    for bad in ({"requestId": -1}, {"requestId": 2147483648}, {"requestId": 1, "controlType": "Curve"},
                {"requestId": 1, "extra": 1}):
        with pytest.raises(CitrineCompatibilityError):
            await client().get_der_control(bad)
    assert c.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error,match", [
    (CitrineHttpError("x", 502), "CitrineOS did not send it"),
    (CitrineHttpError("x", 504), "no station reply in 8 s"),
    (CitrineTransportError("timeout"), "no gateway reply"),
])
async def test_502_504_and_timeouts_are_outcome_unknown(error, match):
    c = client(reply=error);await c.control_policy()
    with pytest.raises(DerOutcomeUnknownError, match=match) as err:
        await c.set_der_control(set_body())
    assert "outcome may be unknown, do not retry automatically" in str(err.value)
    # One attempt, and a DER 5xx does not mark the CSMS unavailable.
    assert len(c.posts) == 1 and c.csms_available and c.last_command == {"state": "not_sent"}


@pytest.mark.asyncio
async def test_gateway_error_text_carried_on_http_errors():
    assert error_detail('{"error": "der_commands_enabled is off"}') == "der_commands_enabled is off"
    assert error_detail("<html>") is None and error_detail('{"error": 3}') is None and error_detail(None) is None

    class Response:
        def __init__(self, status, body):
            self.status, self.body, self.headers = status, body, {"Content-Type": "application/json"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def text(self):
            return self.body

    session = MagicMock()
    api = CitrineClient(session, "http://lab.invalid", 1, "t", True, 3, read_only=False)
    session.request.return_value = Response(409, '{"error": "station not online on OCPP 2.1"}')
    with pytest.raises(CitrineHttpError, match=r"failed \(409\): station not online on OCPP 2.1") as err:
        await api._request("POST", DER_ROUTES["set"])
    assert err.value.status == 409 and err.value.detail == "station not online on OCPP 2.1"
    session.request.return_value = Response(403, '{"error": "der_commands_enabled is off"}')
    with pytest.raises(CitrineAuthError, match="authorization failed: der_commands_enabled is off"):
        await api._request("POST", DER_ROUTES["set"])
    session.request.return_value = Response(500, "")
    with pytest.raises(CitrineHttpError, match=r"failed \(500\)$"):
        await api._request("GET", DER_EVENTS)


# Services

def station(sid=STATION, protocol=PROTOCOL, online=True):
    return {"id": sid, "protocol": protocol, "isOnline": online, "activeTransactionId": None, "lab_meter": {}}


async def services(c, stations):
    coordinator = MagicMock();coordinator.async_request_refresh = AsyncMock()
    coordinator.data = {"stations": stations};coordinator.last_update_success = True
    hass = MagicMock();hass.data = {"citrine_ha": {"entry": {"client": c, "coordinator": coordinator}}}
    hass.services.has_service.return_value = False
    entry = MagicMock();entry.entry_id = "entry"
    await setup_lab(hass, entry, [].extend, "button")
    calls = {call.args[1]: call for call in hass.services.async_register.call_args_list}
    for name in (SERVICE_DER_SET, SERVICE_DER_GET, SERVICE_DER_CLEAR, SERVICE_DER_EVENTS):
        assert calls[name].kwargs["supports_response"] == SupportsResponse.OPTIONAL

    def call(name, **data):
        schema = calls[name].kwargs["schema"]
        return calls[name].args[2](MagicMock(data=schema({"entry_id": "entry", **data})))
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,control", [
    ({"control_type": "LimitMaxDischarge", "pct_max_discharge_power": 40},
     ("limitMaxDischarge", {"priority": 1, "pctMaxDischargePower": 40.0})),
    ({"control_type": "FixedPFInject", "displacement": 0.95, "excitation": True, "priority": 0},
     ("fixedPFInject", {"priority": 0, "displacement": 0.95, "excitation": True})),
    ({"control_type": "FixedPFAbsorb", "displacement": "0.9", "excitation": "false"},
     ("fixedPFAbsorb", {"priority": 1, "displacement": 0.9, "excitation": False})),
    ({"control_type": "FixedVar", "setpoint": -25, "unit": "PctVarAvail", "duration": 900,
      "start_time": "2026-10-25T10:00:00+10:00"},
     ("fixedVar", {"priority": 1, "setpoint": -25.0, "unit": "PctVarAvail",
                   "startTime": "2026-10-25T00:00:00+00:00", "duration": 900})),
])
async def test_set_service_builds_exactly_one_control(fields, control):
    c = client(reply={"status": "Accepted", "supersededIds": ["old-1"]});await c.control_policy()
    call = await services(c, [station()])
    out = await call(SERVICE_DER_SET, control_id="lab-1", **fields)
    body = {"isDefault": False, "controlId": "lab-1", "controlType": fields["control_type"], control[0]: control[1]}
    assert c.posts == [(DER_ROUTES["set"], PARAMS, body)]
    assert out == {"sent": body, "station_id": STATION, "station_status": "Accepted",
                   "station_response": {"status": "Accepted", "supersededIds": ["old-1"]}}


@pytest.mark.asyncio
async def test_set_service_default_control_and_callerror_reply():
    reply = {"_messageId": "m", "_errorCode": "NotImplemented", "_errorDetails": {}, "name": "OcppError"}
    c = client(reply=reply);await c.control_policy()
    call = await services(c, [station()])
    out = await call(SERVICE_DER_SET, control_type="LimitMaxDischarge", control_id="d.1:x-y_z",
                     is_default=True, pct_max_discharge_power=0)
    assert c.posts[-1][2]["isDefault"] is True
    assert out["station_status"] == "CALLERROR NotImplemented" and out["station_response"] == reply


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,match", [
    ({"control_type": "LimitMaxDischarge"}, "missing pct_max_discharge_power"),
    ({"control_type": "FixedPFInject", "displacement": 0.9}, "missing excitation"),
    ({"control_type": "FixedVar", "setpoint": 10}, "missing unit"),
    ({"control_type": "FixedVar", "unit": "PctMaxW"}, "missing setpoint"),
    ({"control_type": "LimitMaxDischarge", "pct_max_discharge_power": 10, "unit": "PctMaxW"},
     "not used by LimitMaxDischarge: unit"),
    ({"control_type": "FixedPFAbsorb", "displacement": 0.9, "excitation": True, "setpoint": 3,
      "pct_max_discharge_power": 5}, "not used by FixedPFAbsorb: pct_max_discharge_power, setpoint"),
    ({"control_type": "FixedVar", "setpoint": 1, "unit": "PctMaxW", "excitation": False},
     "not used by FixedVar: excitation"),
])
async def test_set_service_rejects_missing_and_extra_fields(fields, match):
    c = client();await c.control_policy()
    call = await services(c, [station()])
    with pytest.raises(ServiceValidationError, match=match):
        await call(SERVICE_DER_SET, control_id="lab-1", **fields)
    assert c.posts == []


@pytest.mark.asyncio
async def test_set_service_policy_limits_types_and_schema_bounds():
    c = client(policy(der_policy(control_types=["LimitMaxDischarge"], limits={
        "pct_max_discharge_power": [0, 50], "max_duration_s": 600})));await c.control_policy()
    call = await services(c, [station()])
    with pytest.raises(ServiceValidationError, match="FixedVar is not offered"):
        await call(SERVICE_DER_SET, control_type="FixedVar", control_id="a", setpoint=1, unit="PctMaxW")
    with pytest.raises(ServiceValidationError, match="0 to 50 %"):
        await call(SERVICE_DER_SET, control_type="LimitMaxDischarge", control_id="a", pct_max_discharge_power=60)
    with pytest.raises(ServiceValidationError, match="1 to 600 s"):
        await call(SERVICE_DER_SET, control_type="LimitMaxDischarge", control_id="a",
                   pct_max_discharge_power=10, duration=601)
    for bad in ({"control_type": "Curve"}, {"control_id": "x" * 37}, {"control_id": "a b"},
                {"pct_max_discharge_power": 101}, {"displacement": 0}, {"setpoint": -101},
                {"unit": "W"}, {"priority": -1}, {"duration": 0}):
        with pytest.raises(vol.Invalid):
            await call(SERVICE_DER_SET, **{"control_type": "LimitMaxDischarge", "control_id": "a",
                                           "pct_max_discharge_power": 10, **bad})
    assert c.posts == []


@pytest.mark.asyncio
async def test_get_service_random_or_given_request_id_and_filters():
    reply = {"status": "Accepted", "reports": [{"requestId": 42, "tbc": False}]}
    c = client(reply=reply);await c.control_policy()
    call = await services(c, [station()])
    out = await call(SERVICE_DER_GET, request_id=42, is_default=False, control_type="FixedVar", control_id="v-1")
    assert c.posts[-1] == (DER_ROUTES["get"], PARAMS, {"requestId": 42, "isDefault": False,
                                                       "controlType": "FixedVar", "controlId": "v-1"})
    assert out["station_response"] == reply and out["station_status"] == "Accepted"
    await call(SERVICE_DER_GET)
    await call(SERVICE_DER_GET)
    a, b = c.posts[-2][2], c.posts[-1][2]
    assert set(a) == set(b) == {"requestId"} and all(0 <= x["requestId"] <= 2147483647 for x in (a, b))
    with pytest.raises(vol.Invalid):
        await call(SERVICE_DER_GET, request_id=2147483648)


@pytest.mark.asyncio
async def test_clear_service_body_and_required_is_default():
    c = client();await c.control_policy()
    call = await services(c, [station()])
    out = await call(SERVICE_DER_CLEAR, is_default=True)
    assert c.posts[-1] == (DER_ROUTES["clear"], PARAMS, {"isDefault": True})
    await call(SERVICE_DER_CLEAR, is_default=False, control_type="FixedPFInject", control_id="pf-1")
    assert c.posts[-1][2] == {"isDefault": False, "controlType": "FixedPFInject", "controlId": "pf-1"}
    assert out["station_status"] == "Accepted"
    with pytest.raises(vol.Invalid):
        await call(SERVICE_DER_CLEAR)


@pytest.mark.asyncio
async def test_events_service_returns_gateway_reply_even_when_station_offline():
    c = client();await c.control_policy()
    call = await services(c, [station(online=False)])
    assert await call(SERVICE_DER_EVENTS) == EVENTS
    c.fake["events"] = ["not", "a", "reply"]
    with pytest.raises(HomeAssistantError, match="Unexpected"):
        await call(SERVICE_DER_EVENTS)


@pytest.mark.asyncio
async def test_services_refuse_before_send():
    off = client(policy());await off.control_policy()
    call = await services(off, [station()])
    with pytest.raises(ServiceValidationError, match="DER controls are disabled"):
        await call(SERVICE_DER_CLEAR, is_default=True)
    bad = client(policy(der_policy(events="/x")));await bad.control_policy()
    call = await services(bad, [station()])
    with pytest.raises(ServiceValidationError, match="malformed"):
        await call(SERVICE_DER_EVENTS)
    c = client();await c.control_policy()
    call = await services(c, [station(online=False)])
    with pytest.raises(HomeAssistantError, match="online"):
        await call(SERVICE_DER_GET)
    call = await services(c, [station(protocol="ocpp2.0.1")])
    with pytest.raises(ServiceValidationError, match="needs OCPP 2.1"):
        await call(SERVICE_DER_CLEAR, is_default=True)
    assert off.posts == bad.posts == c.posts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error,match", [
    (CitrineHttpError("Citrine API POST setDERControl failed (400): pctMaxDischargePower outside lab bounds",
                      400, "pctMaxDischargePower outside lab bounds"), "outside lab bounds"),
    (CitrineHttpError("Citrine API POST setDERControl failed (409): station not online on OCPP 2.1",
                      409, "station not online on OCPP 2.1"), "not online on OCPP 2.1"),
    (CitrineAuthError("Authentication or authorization failed: der_commands_enabled is off"),
     "der_commands_enabled is off"),
    (CitrineHttpError("x", 502, "not sent"), r"did not send it \(not sent\); the outcome may be unknown, "
                                             "do not retry automatically"),
    (CitrineHttpError("x", 504), "no station reply in 8 s; the outcome may be unknown, do not retry automatically"),
])
async def test_gateway_errors_map_to_home_assistant_error_with_their_text(error, match):
    c = client(reply=error);await c.control_policy()
    call = await services(c, [station()])
    with pytest.raises(HomeAssistantError, match=match) as err:
        await call(SERVICE_DER_SET, control_type="LimitMaxDischarge", control_id="a", pct_max_discharge_power=10)
    assert not isinstance(err.value, ServiceValidationError)
    assert len(c.posts) == 1
