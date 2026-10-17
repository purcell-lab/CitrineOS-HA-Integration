from datetime import UTC, datetime, timedelta
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock
import pytest

from custom_components.citrine_ha.citrine_api import CitrineApiError, CitrineCompatibilityError, CitrineHttpError, CitrineReadOnlyError
from custom_components.citrine_ha.compatibility import PROFILE_LAB, automatic_control_disabled
from custom_components.citrine_ha.lab_control import ScopedLabClient, STATION, PROTOCOL, PATHS, build_profile, power_period, charge_period, paths_for, transaction, to_ocpp16
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
    c = ScopedLabClient(MagicMock(), "http://lab.invalid", 1, "reader", True, 3,
                        command_token="commander", read_only=False, **kwargs)
    c.ready = AsyncMock()
    return c


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
    {"startPeriod":0,"limit":25001},
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


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "5000", -25001, 25001])
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


async def _profile(c):
    return await c.submit_schedule(protocol=PROTOCOL, station_id=STATION, transaction_id="t",
                                   periods=[charge_period(0, 7000, 3)], immediate=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 409, 500])
async def test_profile_error_reply_is_rejected_and_next_profile_goes_out(status):
    # LOA-11 (17 Oct 2026): a CitrineOS 500 during a 1 s ramp locked every later profile.
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=[CitrineHttpError("refused", status), [{"success":True}]])
    with pytest.raises(CitrineApiError):
        await _profile(c)
    assert c.last_command["state"]=="rejected" and c.last_command["http_status"]==status
    result=await _profile(c)
    assert result["state"]=="dispatched_awaiting_station_evidence"
    assert c._command_client._request.await_count==2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [CitrineHttpError("gateway", 502), CitrineHttpError("gateway", 504),
                                   CitrineApiError("Citrine API timeout calling /x")])
async def test_profile_outcome_unknown_still_locks(error):
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=error)
    with pytest.raises(CitrineApiError):
        await _profile(c)
    assert c.last_command["state"]=="outcome_unknown"
    with pytest.raises(CitrineApiError, match="Previous outcome unresolved"):
        await _profile(c)
    assert c._command_client._request.await_count==1


@pytest.mark.asyncio
async def test_start_500_still_locks_but_start_409_is_rejected():
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=CitrineHttpError("server", 500))
    with pytest.raises(CitrineApiError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c.last_command["state"]=="outcome_unknown"
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=[CitrineHttpError("conflict", 409), [{"success":True}]])
    with pytest.raises(CitrineApiError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c.last_command["state"]=="rejected"
    result=await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert result["state"]=="dispatched_awaiting_station_evidence"


def real_policy(**kw):
    return {**policy(), "station_id": "ACME-WB-0042", "real_devices_enabled": True,
            "protocols": ["ocpp2.1", "ocpp2.0.1"],
            "commands_by_protocol": {x: list(paths_for(x).values()) for x in ("ocpp2.1", "ocpp2.0.1")}, **kw}


@pytest.mark.asyncio
async def test_physical_station_only_when_server_allows_real_devices():
    c=client();c._command_client._request=AsyncMock(return_value=real_policy())
    await c.control_policy()
    assert c.station=="ACME-WB-0042" and c.real_devices is True
    assert c.protocols==("ocpp2.1","ocpp2.0.1")
    for bad in [real_policy(real_devices_enabled=False), real_policy(station_id="a/b"),
                real_policy(real_devices_enabled="yes"), real_policy(protocols=["ocpp2.0.1"]),
                real_policy(protocols=["ocpp2.1","ocpp1.5"]),
                real_policy(commands_by_protocol={"ocpp2.1": list(PATHS.values())})]:
        c=client();c._command_client._request=AsyncMock(return_value=bad)
        with pytest.raises(CitrineCompatibilityError):
            await c.control_policy()


@pytest.mark.asyncio
async def test_older_server_policy_means_ocpp_2_1_only():
    c=client();c._command_client._request=AsyncMock(return_value=policy())
    await c.control_policy()
    assert c.protocols==("ocpp2.1",) and c.real_devices is False
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    c.control_policy=AsyncMock(return_value=policy())
    with pytest.raises(CitrineCompatibilityError):
        await c.submit_schedule(protocol="ocpp2.0.1", station_id=STATION, transaction_id="t",
                                periods=[charge_period(0, 7000, 3)], immediate=True)
    assert c._command_client._request.await_count==0


@pytest.mark.asyncio
async def test_ocpp_2_0_1_station_uses_2_0_1_routes_and_charging_only_periods():
    c=client();c._command_client._request=AsyncMock(return_value=real_policy())
    await c.control_policy()
    c.control_policy=AsyncMock(return_value=real_policy())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    await c.submit_schedule(protocol="ocpp2.0.1", station_id="ACME-WB-0042", transaction_id="TX:42.a",
                            periods=[charge_period(0, 7000, 1)], immediate=True)
    call=c._command_client._request.call_args
    assert call.args[:2]==("POST","/ocpp/2.0.1/smartcharging/setChargingProfile")
    assert call.kwargs["params"]=={"identifier":"ACME-WB-0042","tenantId":1}
    with pytest.raises(CitrineCompatibilityError):
        await c.submit_schedule(protocol="ocpp2.0.1", station_id="ACME-WB-0042", transaction_id="TX:42.a",
                                periods=[power_period(0, 5000)], immediate=True)
    assert c._command_client._request.await_count==1


def test_paths_and_transaction_ids_for_physical_chargers():
    assert paths_for("ocpp2.0.1")["stop"]=="/ocpp/2.0.1/evdriver/requestStopTransaction"
    assert paths_for("ocpp2.1")==PATHS
    for ok in ["a1:b2.c3","TX@1|2+3=4","*abc*","uuid-0-1"]:
        assert transaction(ok)
    for bad in ["a b","a/b","x"*65,"é",""]:
        assert not transaction(bad)


def test_power_period_keeps_11_kw_bounds_unless_setpoint_exceeds():
    assert power_period(0,6000)["limit"]==11000 and power_period(0,-6000)["dischargeLimit"]==-11000
    assert power_period(0,20000)["limit"]==20000 and power_period(0,-20000)["dischargeLimit"]==-20000
    build_profile("t",[power_period(0,25000),power_period(60,-25000)])


def test_three_phase_ac_and_v2g_limits_up_to_25_kw():
    sched=build_profile("t",[charge_period(0,22080,3)])["chargingProfile"]["chargingSchedule"][0]
    assert sched["chargingSchedulePeriod"][0]["limit"]==22080
    build_profile("t",[{"startPeriod":0,"limit":25000,"dischargeLimit":-25000,"setpoint":-25000,"operationMode":"CentralSetpoint"}])
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t",[charge_period(0,25001,3)])
    with pytest.raises(CitrineCompatibilityError):
        build_profile("t",[{"startPeriod":0,"limit":20000,"dischargeLimit":-25001,"setpoint":0,"operationMode":"CentralSetpoint"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("bounds", [(11000, 11000), (25000, 25000)])
async def test_policy_accepts_old_and_raised_bounds(bounds):
    c=client();p=policy();p["max_import_w"],p["max_export_w"]=bounds
    c._command_client._request=AsyncMock(return_value=p)
    assert (await c.control_policy())["max_export_w"]==bounds[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("bounds", [(25000, 11000), (11000, 25000), (23000, 11000)])
async def test_policy_rejects_mixed_bounds(bounds):
    c=client();p=policy();p["max_import_w"],p["max_export_w"]=bounds
    c._command_client._request=AsyncMock(return_value=p)
    with pytest.raises(CitrineCompatibilityError):
        await c.control_policy()


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
    assert len(entities)==6
    assert all(not e.available for e in entities)
    await setup_lab(hass,entry,entities.extend,"number")
    power=entities[-1];power.async_write_ha_state=MagicMock()
    await power.async_set_native_value(-5000)
    assert ctx["lab_requested_w"]==-5000
    await setup_lab(hass,entry,entities.extend,"sensor")
    assert len(entities)==14
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


def policy_16(**kw):
    protocols = ["ocpp2.1", "ocpp2.0.1", "ocpp1.6"]
    return real_policy(protocols=protocols,
                       commands_by_protocol={x: list(paths_for(x).values()) for x in protocols}, **kw)


@pytest.mark.asyncio
async def test_ocpp_1_6_station_gets_1_6_messages():
    c=client();c._command_client._request=AsyncMock(return_value=policy_16())
    await c.control_policy()
    assert c.protocols==("ocpp2.1","ocpp2.0.1","ocpp1.6")
    c.control_policy=AsyncMock(return_value=policy_16())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    sid="ACME-WB-0042"
    await c.request_start_transaction(protocol="ocpp1.6", station_id=sid)
    call=c._command_client._request.call_args
    assert call.args[1]=="/ocpp/1.6/evdriver/remoteStartTransaction"
    assert call.kwargs["json"]=={"connectorId":1,"idTag":"C17E0001"}
    c.last_command={"state":"transaction_observed"}
    await c.submit_schedule(protocol="ocpp1.6", station_id=sid, transaction_id="42",
                            periods=[charge_period(0, 7200.4, 3), charge_period(60, 0, 3)], immediate=True)
    call=c._command_client._request.call_args
    assert call.args[1]=="/ocpp/1.6/smartcharging/setChargingProfile"
    body=call.kwargs["json"];cp=body["csChargingProfiles"]
    assert body["connectorId"]==1 and cp["transactionId"]==42 and cp["chargingProfileId"]==9001
    assert cp["chargingProfilePurpose"]=="TxProfile" and cp["chargingProfileKind"]=="Absolute"
    assert cp["chargingSchedule"]["chargingRateUnit"]=="W"
    assert cp["chargingSchedule"]["chargingSchedulePeriod"]==[
        {"startPeriod":0,"limit":7200,"numberPhases":3},{"startPeriod":60,"limit":0,"numberPhases":3}]
    assert c.last_command["transaction_id"]=="42"
    await c.request_stop_transaction(protocol="ocpp1.6", station_id=sid, transaction_id="42")
    assert c._command_client._request.call_args.kwargs["json"]=={"transactionId":42}
    await c.clear_charging_profile(protocol="ocpp1.6", station_id=sid, profile_id=9001)
    call=c._command_client._request.call_args
    assert call.args[1]=="/ocpp/1.6/smartcharging/clearChargingProfile" and call.kwargs["json"]=={"id":9001}
    sent=c._command_client._request.await_count
    for periods, tx in [([power_period(0, 5000)], "42"), ([charge_period(0, 7000, 3)], "uuid-1"),
                        ([{"startPeriod":0,"limit":0,"numberPhases":1,"phaseToUse":2}], "42")]:
        with pytest.raises(CitrineCompatibilityError):
            await c.submit_schedule(protocol="ocpp1.6", station_id=sid, transaction_id=tx,
                                    periods=periods, immediate=True)
    assert c._command_client._request.await_count==sent


def test_real_client_ocpp16_wire_payloads_pass_pinned_gateway_policy():
    source=os.environ.get("CITRINE_APP_SOURCE")
    if not source:
        pytest.skip("Set CITRINE_APP_SOURCE for the cross-repository wire contract")
    policy_file=(Path(source)/"citrineos_server/rootfs/control-policy.mjs").resolve()
    script="""const {validateCommand,routesFor,STATION}=await import(process.argv[1]);
let text='';for await(const c of process.stdin)text+=c;
for(const [i,p] of JSON.parse(text)) {
  const u=new URL('http://lab'+routesFor('ocpp1.6')[i]+'?identifier='+STATION+'&tenantId=1');
  if(!validateCommand(u,p))process.exit(1);
}"""
    start={"remoteStartId":1,"evseId":1,"idToken":{"idToken":"C17E0001","type":"ISO14443"}}
    profile=build_profile("42",[charge_period(0,7000.3,3),charge_period(60,0)],immediate=True)
    payloads=[[0,to_ocpp16("start",start)],[1,to_ocpp16("stop",{"transactionId":"42"})],
              [2,to_ocpp16("profile",profile)],[3,to_ocpp16("clear",{"chargingProfileId":9001})]]
    subprocess.run(["node","--input-type=module","-e",script,policy_file.as_uri()],
                   input=json.dumps(payloads),text=True,check=True,capture_output=True)


# purcell-lab/ha-addons#69: lock-outs that needed an integration reload.
import logging
from custom_components.citrine_ha.citrine_api import CitrineAuthError, CitrineTransportError
from custom_components.citrine_ha.lab_control import EVIDENCE_TIMEOUT_S, CitrineUnavailableError
from custom_components.citrine_ha.lab_entities import reconcile, SERVICE_RECONCILE


def aged(c, seconds, key="dispatch_response_at"):
    c.last_command[key]=(datetime.now(UTC)-timedelta(seconds=seconds)).isoformat()


def lab_station(tx=None, **meter):
    return {"id":STATION,"protocol":PROTOCOL,"isOnline":True,"activeTransactionId":tx,"lab_meter":meter}


@pytest.mark.asyncio
async def test_start_without_station_evidence_times_out_and_allows_new_start(caplog):
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    # Duplicate protection inside the window is kept.
    c.observe_transaction(lab_station())
    assert c.last_command["state"]=="dispatched_awaiting_station_evidence"
    with pytest.raises(CitrineApiError,match="Start already dispatched; wait up to"):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c._command_client._request.await_count==1
    aged(c,EVIDENCE_TIMEOUT_S+1)
    with caplog.at_level(logging.WARNING):
        c.observe_transaction(lab_station())
    assert c.last_command["state"]=="no_station_evidence"
    assert "new Start is allowed" in c.last_command["detail"]
    assert "No station evidence within 90 s" in caplog.text
    result=await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert result["state"]=="dispatched_awaiting_station_evidence"
    assert c._command_client._request.await_count==2
    # A late transaction is still recorded as evidence.
    aged(c,EVIDENCE_TIMEOUT_S+1);c.observe_transaction(lab_station())
    c.observe_transaction(lab_station("tx-late"))
    assert c.last_command["state"]=="transaction_observed"


@pytest.mark.asyncio
async def test_stale_start_expires_at_dispatch_without_a_poll_and_profiles_end_too():
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    aged(c,EVIDENCE_TIMEOUT_S+5)
    await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    assert c._command_client._request.await_count==2
    await _profile(c)
    aged(c,EVIDENCE_TIMEOUT_S+1)
    c.observe_transaction(lab_station("t"))
    # purcell-lab/ha-addons#32: the status no longer stays stale.
    assert c.last_command["state"]=="no_station_evidence"


@pytest.mark.asyncio
async def test_reconcile_never_clears_a_command_that_may_still_land():
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=CitrineHttpError("gateway",502))
    with pytest.raises(CitrineApiError):
        await _profile(c)
    assert c.last_command["state"]=="outcome_unknown"
    with pytest.raises(CitrineApiError,match="may still be pending"):
        await c.reconcile(lab_station("t"))
    assert c.last_command["state"]=="outcome_unknown"
    with pytest.raises(CitrineApiError,match="Previous outcome unresolved"):
        await _profile(c)


@pytest.mark.asyncio
async def test_reconcile_resolves_unknown_profile_and_next_profile_goes_out(caplog):
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=[CitrineHttpError("gateway",502),[{"success":True}]])
    with pytest.raises(CitrineApiError):
        await _profile(c)
    aged(c,EVIDENCE_TIMEOUT_S+1,"dispatched_at")
    with caplog.at_level(logging.WARNING):
        result=await c.reconcile(lab_station("t"))
    assert result["state"]=="reconciled" and result["previous_state"]=="outcome_unknown"
    assert result["resolution"]=="outcome_not_observable"
    assert "reconciled from outcome_unknown" in caplog.text
    assert (await _profile(c))["state"]=="dispatched_awaiting_station_evidence"
    assert c._command_client._request.await_count==2


@pytest.mark.asyncio
async def test_reconcile_start_and_stop_from_csms_transaction_state():
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(side_effect=CitrineHttpError("server",500))
    with pytest.raises(CitrineApiError):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    aged(c,EVIDENCE_TIMEOUT_S+1,"dispatched_at")
    assert (await c.reconcile(lab_station("tx-9")))["state"]=="transaction_observed"
    c.last_command={"action":"start","state":"outcome_unknown","station_id":STATION}
    aged(c,EVIDENCE_TIMEOUT_S+1,"dispatched_at")
    result=await c.reconcile(lab_station())
    assert (result["state"],result["resolution"])==("reconciled","no_transaction")
    c.last_command={"action":"stop","state":"outcome_unknown","transaction_id":"tx-9"}
    aged(c,EVIDENCE_TIMEOUT_S+1,"dispatched_at")
    assert (await c.reconcile(lab_station("tx-9")))["resolution"]=="transaction_still_active"
    # Nothing to resolve is reported, not invented.
    c.last_command={"action":"profile","state":"rejected","http_status":409}
    assert (await c.reconcile(lab_station("tx-9")))["state"]=="rejected"


@pytest.mark.asyncio
async def test_reconcile_needs_a_reachable_csms():
    c=client();c.control_policy=AsyncMock(side_effect=CitrineHttpError("gateway",502))
    c.last_command={"action":"profile","state":"outcome_unknown","transaction_id":"t"}
    aged(c,EVIDENCE_TIMEOUT_S+1,"dispatched_at")
    with pytest.raises(CitrineUnavailableError):
        await c.reconcile(lab_station("t"))
    assert c.last_command["state"]=="outcome_unknown" and c.csms_available is False
    c.control_policy=AsyncMock(return_value=policy())
    with pytest.raises(CitrineUnavailableError):
        await c.reconcile(lab_station("t"))
    c.control_policy.assert_not_called()


@pytest.mark.asyncio
async def test_health_failure_marks_csms_unavailable_once_and_commands_fail_fast(caplog):
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    c.ready=AsyncMock(side_effect=CitrineHttpError("Citrine API GET /health/ready failed (502)",502))
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            with pytest.raises(CitrineUnavailableError,match="CSMS unavailable"):
                await c.ping()
    assert caplog.text.count("CSMS unavailable")==1
    assert c.csms_available is False and "502" in c.csms_unavailable_reason
    with pytest.raises(CitrineUnavailableError,match="CSMS unavailable"):
        await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION)
    c._command_client._request.assert_not_called()
    assert c.last_command=={"state":"not_sent"}
    # Recovery is automatic on the next passing poll.
    c.ready=AsyncMock();c._request=AsyncMock(side_effect=[CitrineAuthError("denied"),[]])
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await c.ping()
    assert c.csms_available is True and "available again" in caplog.text
    assert (await c.request_start_transaction(protocol=PROTOCOL,station_id=STATION))["state"]=="dispatched_awaiting_station_evidence"


@pytest.mark.asyncio
@pytest.mark.parametrize("error",[CitrineTransportError("Citrine API transport error"),
                                  CitrineHttpError("Citrine API GET /health/ready failed (503)",503)])
async def test_readiness_failure_at_dispatch_is_not_an_unknown_outcome(error):
    c=client();c.control_policy=AsyncMock(return_value=policy())
    c._command_client._request=AsyncMock(return_value=[{"success":True}])
    c.ready=AsyncMock(side_effect=error)
    with pytest.raises(CitrineUnavailableError):
        await _profile(c)
    c._command_client._request.assert_not_called()
    assert c.last_command=={"state":"not_sent"} and c.csms_available is False


@pytest.mark.asyncio
async def test_auth_and_policy_errors_do_not_mark_csms_unavailable():
    c=client();c.ready=AsyncMock(side_effect=CitrineAuthError("denied"))
    with pytest.raises(CitrineAuthError):
        await c.ping()
    assert c.csms_available is True


def lab_ctx(c,data,ok=True):
    coordinator=MagicMock();coordinator.async_request_refresh=AsyncMock();coordinator.async_refresh=AsyncMock()
    coordinator.data=data;coordinator.last_update_success=ok
    return {"client":c,"coordinator":coordinator}


@pytest.mark.asyncio
async def test_ha_command_reports_csms_unavailable_and_reconcile_service():
    from homeassistant.core import SupportsResponse
    from homeassistant.exceptions import HomeAssistantError
    c=client();c.csms_available=False;c.csms_unavailable_reason="GET /health/ready failed (502)"
    c.request_start_transaction=AsyncMock()
    ctx=lab_ctx(c,{"stations":[lab_station()]},ok=False)
    with pytest.raises(HomeAssistantError,match="CSMS unavailable"):
        await command(ctx,"start")
    c.request_start_transaction.assert_not_called()
    with pytest.raises(HomeAssistantError,match="CSMS unavailable"):
        await reconcile(ctx)
    c.csms_available=True;c.control_policy=AsyncMock(return_value=policy())
    c.last_command={"action":"profile","state":"outcome_unknown","transaction_id":"t"}
    aged(c,EVIDENCE_TIMEOUT_S+1,"dispatched_at")
    ctx=lab_ctx(c,{"stations":[lab_station("t")]})
    assert (await reconcile(ctx))["state"]=="reconciled"
    ctx["coordinator"].async_refresh.assert_awaited_once()
    hass=MagicMock();hass.data={"citrine_ha":{"entry":ctx}};hass.services.has_service.return_value=False
    entry=MagicMock();entry.entry_id="entry"
    buttons=[]
    await setup_lab(hass,entry,buttons.extend,"button")
    assert buttons[-1].action=="reconcile"
    calls={call.args[1]:call for call in hass.services.async_register.call_args_list}
    assert calls[SERVICE_RECONCILE].kwargs["supports_response"]==SupportsResponse.OPTIONAL
    handler=calls[SERVICE_RECONCILE].args[2]
    assert (await handler(MagicMock(data={"entry_id":"entry"})))["state"]=="reconciled"
