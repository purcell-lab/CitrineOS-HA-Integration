from datetime import UTC, datetime, timedelta
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock
import pytest

from custom_components.citrine_ha.citrine_api import CitrineApiError, CitrineCompatibilityError, CitrineReadOnlyError
from custom_components.citrine_ha.compatibility import PROFILE_LAB, automatic_control_disabled
from custom_components.citrine_ha.lab_control import ScopedLabClient, STATION, PROTOCOL, PATHS, build_profile, power_period, charge_period
from custom_components.citrine_ha.lab_telemetry import parse_meter
from custom_components.citrine_ha.lab_entities import setup_lab, command
from custom_components.citrine_ha.config_flow import CitrineOptionsFlow
from custom_components.citrine_ha.load_controller import CitrineLoadController


def policy():
    return {"station_id": STATION, "protocol": PROTOCOL, "can_control": True,
            "max_import_w": 11000, "max_export_w": 11000, "max_profile_seconds": 600,
            "max_periods": 10, "automatic_control": False, "real_devices_enabled": False,
            "clear_requires_closed_transaction": True, "commands": list(PATHS.values())}


def client(**kwargs):
    return ScopedLabClient(MagicMock(), "http://lab.invalid", 1, "reader", True, 3,
                           command_token="commander", read_only=False, **kwargs)


def test_bounded_absolute_schedule_and_transaction():
    p = build_profile("uuid-session", [power_period(i*60, w) for i, w in enumerate([6000,-5000,3000,0])])
    c = p["chargingProfile"]
    assert c["transactionId"] == "uuid-session"
    assert c["chargingProfilePurpose"] == "TxProfile"
    assert c["chargingProfileKind"] == "Absolute"
    assert [x["setpoint"] for x in c["chargingSchedule"][0]["chargingSchedulePeriod"]] == [6000,-5000,3000,0]
    assert len(c["chargingSchedule"][0]["startSchedule"].split(".")[1].split("+")[0]) == 3


def test_real_client_wire_payload_passes_pinned_gateway_policy():
    source=os.environ.get("CITRINE_APP_SOURCE")
    if not source:
        pytest.skip("Set CITRINE_APP_SOURCE for the cross-repository wire contract")
    policy_file=(Path(source)/"citrineos_server/rootfs/control-policy.mjs").resolve()
    script="""const {validateCommand,ROUTES,STATION}=await import(process.argv[1]);
let text='';for await(const c of process.stdin)text+=c;
const p=JSON.parse(text);
const u=new URL('http://lab'+ROUTES[2]+'?identifier='+STATION+'&tenantId=1');
if(!validateCommand(u,p))process.exit(1);"""
    payload=build_profile("uuid-session", [power_period(i*60,w) for i,w in enumerate([6000,-5000,3000,0])])
    subprocess.run(["node","--input-type=module","-e",script,policy_file.as_uri()],
                   input=json.dumps(payload),text=True,check=True,capture_output=True)


def test_ac_charging_only_limits_and_phases():
    periods=[charge_period(0,7000,1),charge_period(60,3000),{"startPeriod":120,"limit":0,"numberPhases":1,"phaseToUse":2}]
    sched=build_profile("t",periods)["chargingProfile"]["chargingSchedule"][0]["chargingSchedulePeriod"]
    assert sched==periods
    assert all("setpoint" not in x and "operationMode" not in x for x in sched)


@pytest.mark.parametrize("bad", [
    {"startPeriod":0,"limit":11001},
    {"startPeriod":0,"limit":1000,"numberPhases":2},
    {"startPeriod":0,"limit":1000,"phaseToUse":1},
    {"startPeriod":0,"limit":1000,"numberPhases":3,"phaseToUse":1},
    {"startPeriod":0,"limit":1000,"setpoint":500},
    {"startPeriod":0,"limit":1000,"operationMode":"ChargingOnly"},
    {"startPeriod":0,"limit":1000,"dischargeLimit":0},
])
def test_invalid_charging_only_periods_rejected(bad):
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t",[bad])


def test_immediate_start_leaves_no_gap():
    before=datetime.now(UTC)
    p=build_profile("t",[charge_period(0,5000,1)],immediate=True)
    start=datetime.fromisoformat(p["chargingProfile"]["chargingSchedule"][0]["startSchedule"])
    assert before-timedelta(seconds=3)<start<=before
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t",[charge_period(0,5000)],immediate=True,start_at=datetime.now(UTC)+timedelta(seconds=5))
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t",[charge_period(0,5000)],immediate=1)


def test_charging_only_wire_payload_passes_pinned_gateway_policy():
    source=os.environ.get("CITRINE_APP_SOURCE")
    if not source:
        pytest.skip("Set CITRINE_APP_SOURCE for the cross-repository wire contract")
    policy_file=(Path(source)/"citrineos_server/rootfs/control-policy.mjs").resolve()
    script="""const {validateCommand,ROUTES,STATION}=await import(process.argv[1]);
let text='';for await(const c of process.stdin)text+=c;
const p=JSON.parse(text);
const u=new URL('http://lab'+ROUTES[2]+'?identifier='+STATION+'&tenantId=1');
if(!validateCommand(u,p))process.exit(1);"""
    payload=build_profile("uuid-session",[charge_period(0,7000,1),charge_period(60,0,1)],immediate=True)
    subprocess.run(["node","--input-type=module","-e",script,policy_file.as_uri()],
                   input=json.dumps(payload),text=True,check=True,capture_output=True)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "5000", -11001, 11001])
def test_invalid_setpoints_rejected(bad):
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t", [power_period(0,bad)])


@pytest.mark.parametrize("kwargs", [{"duration":601},{"duration":True},{"transaction_id":""},
    {"periods":[power_period(1,0)]},{"periods":[power_period(0,0),power_period(0,1)]},
    {"start_at":datetime.now(UTC)-timedelta(seconds=5)}, {"start_at":datetime.now()}])
def test_invalid_schedule_never_repaired(kwargs):
    args={"transaction_id":"t","periods":[power_period(0,0)],**kwargs}
    with pytest.raises(CitrineCompatibilityError):
        build_profile(**args)


@pytest.mark.asyncio
async def test_one_attempt_no_201_downgrade_or_numeric_transaction_retry():
    c=client()
    c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=CitrineApiError("timeout"))
    with pytest.raises(CitrineApiError):
        await c.request_stop_transaction(protocol=PROTOCOL,station_id=STATION,transaction_id="123")
    assert c._command_client._request.await_count==1
    call=c._command_client._request.call_args
    assert call.args[:2]==("POST",PATHS["stop"])
    assert call.kwargs["json"]=={"transactionId":"123"}
    assert c.last_command["state"]=="outcome_unknown"
    with pytest.raises(CitrineApiError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c._command_client._request.await_count==1


@pytest.mark.asyncio
async def test_dispatch_is_not_station_acceptance_and_duplicate_start_blocked():
    c=client()
    c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    result=await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert result["state"]=="dispatched_awaiting_station_evidence"
    assert "accepted_at" not in result
    assert c._command_client._request.call_args.kwargs["json"]["idToken"]["type"]=="ISO14443"
    with pytest.raises(CitrineApiError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c._command_client._request.await_count==1


@pytest.mark.asyncio
@pytest.mark.parametrize("response",[[],{},[{"success":False}],[{"status":"Accepted"}],"OK"])
async def test_ambiguous_dispatch_response_is_not_success(response):
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(return_value=response)
    with pytest.raises(CitrineApiError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c.last_command["state"]=="dispatch_not_confirmed"


@pytest.mark.asyncio
async def test_mismatched_policy_and_foreign_station_fail_before_send():
    c=client();p=policy();p["max_export_w"]=500000
    c._command_client._request=AsyncMock(return_value=p)
    with pytest.raises(CitrineCompatibilityError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert all(x.args[0]=="GET" for x in c._command_client._request.call_args_list)
    c._command_client._request.reset_mock()
    with pytest.raises(CitrineCompatibilityError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id="REAL")
    c._command_client._request.assert_not_called()
    with pytest.raises(CitrineReadOnlyError):
        await c._request("POST",PATHS["start"])


def test_read_only_and_automatic_control_remain_separate():
    assert automatic_control_disabled({"api_profile":PROFILE_LAB,"read_only":False})
    with pytest.raises(CitrineApiError):
        ScopedLabClient(MagicMock(),"http://lab",1,"same",True,2,command_token="same",read_only=False)
    entry=MagicMock();entry.data={"api_profile":PROFILE_LAB};entry.options={}
    schema=CitrineOptionsFlow(entry)._options_schema(entry.data)
    fields=[k.schema for k in schema.schema]
    assert "command_token" in fields and "controller_mode" not in fields


def meter_rows(now=None):
    return [{"tenantId":1,"transactionDatabaseId":7,"timestamp":(now or datetime.now(UTC)).isoformat(),
             "sampledValue":[
                 {"value":-5000,"measurand":"Power.Active.Import","unitOfMeasure":{"unit":"W"}},
                 {"value":1500,"measurand":"Energy.Active.Import.Register","unitOfMeasure":{"unit":"Wh"}},
                 {"value":900,"measurand":"Energy.Active.Export.Register","unitOfMeasure":{"unit":"Wh"}},
             ]}]


def test_meter_directions_energy_units_staleness_and_scope():
    data=parse_meter(meter_rows(),7,1)
    assert data["power_w"]==-5000 and data["import_kwh"]==1.5 and data["export_kwh"]==0.9
    data=parse_meter(meter_rows(datetime.now(UTC)-timedelta(seconds=60)),7,1)
    assert data["power_w"] is None and data["meter_fresh"] is False
    assert data["export_kwh"]==0.9
    with pytest.raises(ValueError):
        parse_meter(meter_rows(),8,1)
    rows=meter_rows();rows[0]["sampledValue"][0]["phase"]="L1"
    assert parse_meter(rows,7,1)["power_w"] is None


@pytest.mark.asyncio
async def test_ha_controls_do_not_create_legacy_entities_or_automatically_dispatch():
    c=client()
    coordinator=MagicMock();coordinator.data={"stations":[]};coordinator.last_update_success=True
    ctx={"client":c,"coordinator":coordinator,"controller":MagicMock()}
    hass=MagicMock();hass.data={"citrine_ha":{"entry":ctx}};hass.services.has_service.return_value=False
    entry=MagicMock();entry.entry_id="entry";entry.data={"api_profile":PROFILE_LAB,"read_only":False};entry.options={}
    entities=[]
    await setup_lab(hass,entry,entities.extend,"button")
    assert len(entities)==5
    assert all(not e.available for e in entities)
    await setup_lab(hass,entry,entities.extend,"number")
    power=entities[-1];power.async_write_ha_state=MagicMock()
    await power.async_set_native_value(-5000)
    assert ctx["lab_requested_w"]==-5000
    await setup_lab(hass,entry,entities.extend,"sensor")
    assert len(entities)==13
    controller=CitrineLoadController(hass,entry=entry,coordinator=coordinator,client=c)
    await controller.async_start()
    assert controller.state_status=="Manual lab controls"
    assert controller._unsub_timer is None


@pytest.mark.asyncio
async def test_ui_never_clears_active_profile():
    c=client();c.clear_charging_profile=AsyncMock()
    coordinator=MagicMock();coordinator.async_request_refresh=AsyncMock()
    coordinator.data={"stations":[{"id":STATION,"protocol":PROTOCOL,"isOnline":True,"activeTransactionId":"t"}]}
    coordinator.last_update_success=True
    from homeassistant.exceptions import HomeAssistantError
    with pytest.raises(HomeAssistantError,match="closure"):
        await command({"client":c,"coordinator":coordinator},"clear")
    c.clear_charging_profile.assert_not_called()


def test_lifecycle_observation_does_not_claim_profile_acceptance():
    c=client()
    c.last_command={"action":"start","state":"dispatched_awaiting_station_evidence"}
    s={"id":STATION,"protocol":PROTOCOL,"activeTransactionId":"t"}
    c.observe_transaction(s)
    assert c.last_command["state"]=="transaction_observed"
    assert c.last_command["transaction_id"]=="t"
    c.last_command={"action":"stop","state":"outcome_unknown","transaction_id":"t"}
    s.update(activeTransactionId=None,lab_meter={"transaction_closed":True,"meter_transaction_id":"other"})
    c.observe_transaction(s)
    assert c.last_command["state"]=="outcome_unknown"
    s["lab_meter"]["meter_transaction_id"]="t"
    c.observe_transaction(s)
    assert c.last_command["state"]=="transaction_closure_observed"
    c.last_command={"action":"profile","state":"outcome_unknown"}
    c.observe_transaction(s)
    assert c.last_command["state"]=="outcome_unknown"


@pytest.mark.asyncio
async def test_command_station_learned_from_server_policy_and_pinned():
    c=client()
    assert c.station == STATION
    other={**policy(),"station_id":"SIM-EV-EVEREST-V2G-02"}
    c._command_client._request=AsyncMock(side_effect=[other,other,[{"success":True}]])
    await c.control_policy()
    assert c.station == "SIM-EV-EVEREST-V2G-02"
    with pytest.raises(CitrineCompatibilityError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    await c.request_start_transaction(protocol=PROTOCOL,station_id="SIM-EV-EVEREST-V2G-02")
    method,path=c._command_client._request.await_args.args
    assert path == PATHS["start"]
    assert c._command_client._request.await_args.kwargs["params"] == {
        "identifier":"SIM-EV-EVEREST-V2G-02","tenantId":1}
    # A server that changes its command station needs a reload, not a retarget.
    c._command_client._request=AsyncMock(return_value={**policy(),"station_id":"SIM-EV-OTHER"})
    with pytest.raises(CitrineCompatibilityError):
        await c.control_policy()
    assert c.station == "SIM-EV-EVEREST-V2G-02"


@pytest.mark.asyncio
@pytest.mark.parametrize("station_id",["SIM-CITRINE-LAB-01","REAL-CHARGER","SIM-EV-",None,"SIM-EV-a/b"])
async def test_policy_station_outside_simulator_namespace_rejected(station_id):
    c=client()
    c._command_client._request=AsyncMock(return_value={**policy(),"station_id":station_id})
    with pytest.raises(CitrineCompatibilityError):
        await c.control_policy()
    assert c.station == STATION
