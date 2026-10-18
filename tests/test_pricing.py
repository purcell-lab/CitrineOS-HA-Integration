"""Dynamic pricing: Amber forecast parsing, OCPP price builders, client and services."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.citrine_ha.citrine_api import CitrineCompatibilityError, CitrineHttpError, CitrineTransportError
from custom_components.citrine_ha.lab_control import (
    PATHS, PROTOCOL, STATION, ScopedLabClient, charge_period, parse_price_commands, power_period)
from custom_components.citrine_ha.lab_entities import (
    SERVICE, SERVICE_COST, SERVICE_TARIFF, setup_lab)
from custom_components.citrine_ha.pricing import (
    PriceInterval, amber_intervals, build_cost_updated, build_price_schedule, build_tariff,
    contiguous, current_and_next, local_parts, openadr_forecasts, price_text, rational, tariff_status,
    valid_cost_updated, valid_price_schedule, valid_tariff)

BNE = ZoneInfo("Australia/Brisbane")
PLUGFEST = "ACME-WB-0042"
COST_21 = "/ocpp/2.1/transactions/costUpdated"
COST_201 = "/ocpp/2.0.1/transactions/costUpdated"
TARIFF = "/ocpp/2.1/transactions/setDefaultTariff"
T0 = datetime(2026, 10, 18, 21, 30, tzinfo=UTC)  # 07:30 in Brisbane


def item(start, price, minutes=30, offset=1, end=True):
    """An Amber forecast item: starts one second past the boundary, like the API."""
    x = {"per_kwh": price, "start_time": (start + timedelta(seconds=offset)).isoformat(),
         "duration": minutes, "descriptor": "neutral", "spot_per_kwh": 0.1}
    if end:
        x["end_time"] = (start + timedelta(minutes=minutes)).isoformat()
    return x


def core(start, prices, minutes=30, entity="sensor.amber_general_forecast"):
    items = [item(start + timedelta(minutes=i * minutes), p, minutes) for i, p in enumerate(prices)]
    return SimpleNamespace(entity_id=entity, state=str(prices[0]),
                           attributes={"channel_type": "general", "forecasts": items})


def iv(start, minutes, price):
    return PriceInterval(start, start + timedelta(minutes=minutes), price)


def price_policy(**kw):
    return {"enabled": True, "currency": "AUD",
            "price_schedule": {"max_rule_stacks": 288, "max_level_entries": 100, "max_horizon_s": 172800},
            "cost_updated_by_protocol": {"ocpp2.1": COST_21, "ocpp2.0.1": COST_201},
            "default_tariff": TARIFF,
            "tariff_limits": {"max_prices": 96, "min_price_kwh": -5, "max_price_kwh": 20},
            "tariff_stations": [STATION, PLUGFEST], **kw}


def policy(prices=None, **kw):
    p = {"station_id": STATION, "protocol": PROTOCOL, "can_control": True,
         "max_import_w": 11000, "max_export_w": 11000, "max_profile_seconds": 600,
         "max_periods": 10, "automatic_control": False, "real_devices_enabled": False,
         "clear_requires_closed_transaction": True, "commands": list(PATHS.values()),
         "protocols": ["ocpp2.1", "ocpp2.0.1"],
         "commands_by_protocol": {"ocpp2.1": list(PATHS.values()),
                                  "ocpp2.0.1": [x.replace("/2.1/", "/2.0.1/") for x in PATHS.values()]}, **kw}
    if prices is not None:
        p["price_commands"] = prices
    return p


def client(pol=None, reply=None):
    """A lab client whose transport answers the policy GET and records POSTs."""
    c = ScopedLabClient(MagicMock(), "http://lab.invalid", 1, "reader", True, 3,
                        command_token="commander", read_only=False)
    c.ready = AsyncMock()
    c.posts = []
    state = {"policy": policy(price_policy()) if pol is None else pol,
             "reply": [{"success": True}] if reply is None else reply}

    async def request(method, path, **kw):
        if method == "GET":
            return state["policy"]
        c.posts.append((path, kw["params"], kw["json"]))
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]
    c._command_client._request = AsyncMock(side_effect=request)
    c.fake = state
    return c


# Parsing

def test_core_forecast_skips_past_and_missing_keeps_negative_and_floors_start():
    now = T0 + timedelta(minutes=10)
    s = core(T0 - timedelta(minutes=30), [0.10, 0.225, 0.202, None, -0.05, "x", True])
    got = amber_intervals(s, now)
    assert [x.price for x in got] == [0.225, 0.202, -0.05]
    assert got[0] == PriceInterval(T0, T0 + timedelta(minutes=30), 0.225)
    assert all(x.start.second == 0 and x.start.tzinfo is UTC for x in got)
    # The None interval leaves a gap: the contiguous run stops before it.
    assert len(contiguous(got)) == 2
    assert current_and_next(got, now) == (got[0], got[1])


def test_amber_express_detailed_forecast_and_end_from_duration():
    items = [item(T0, 0.3, end=False), item(T0 + timedelta(minutes=30), -0.01, end=False)]
    s = SimpleNamespace(entity_id="sensor.amber_express_general_price", state="0.3",
                        attributes={"detailedForecast": items})
    got = amber_intervals(s, T0 + timedelta(minutes=1))
    assert [(x.start, x.end, x.price) for x in got] == [
        (T0, T0 + timedelta(minutes=30), 0.3),
        (T0 + timedelta(minutes=30), T0 + timedelta(minutes=60), -0.01)]


def test_amber_express_future_only_forecast_uses_state_for_current_interval():
    items = [item(T0 + timedelta(minutes=5 * i), 0.2, minutes=5) for i in range(1, 4)]
    s = SimpleNamespace(entity_id="sensor.x", state="25.4",
                        attributes={"detailedForecast": items, "unit_of_measurement": "c/kWh"})
    got = amber_intervals(s, T0 + timedelta(minutes=2))
    assert got[0] == PriceInterval(T0, T0 + timedelta(minutes=5), 0.254)
    assert len(contiguous(got)) == 4
    # The core forecast sensor's state is the NEXT price, never used as current.
    c = core(T0 + timedelta(minutes=30), [0.2, 0.3])
    got = amber_intervals(c, T0 + timedelta(minutes=2))
    assert got[0].start == T0 + timedelta(minutes=30)
    assert current_and_next(got, T0 + timedelta(minutes=2)) == (None, None)


def test_core_price_sensor_current_interval_from_own_attributes():
    attrs = {**item(T0, 0.181), "channel_type": "general", "estimate": True}
    s = SimpleNamespace(entity_id="sensor.amber_general_price", state="0.181", attributes=attrs)
    got = amber_intervals(s, T0 + timedelta(minutes=29))
    assert got == [PriceInterval(T0, T0 + timedelta(minutes=30), 0.181)]
    assert current_and_next(got, T0 + timedelta(minutes=29)) == (got[0], None)
    assert amber_intervals(s, T0 + timedelta(minutes=30)) == []


@pytest.mark.parametrize("state", [
    None, SimpleNamespace(), SimpleNamespace(attributes=None), SimpleNamespace(attributes={}),
    SimpleNamespace(attributes={"forecasts": None}),
    SimpleNamespace(attributes={"forecasts": [None, 1, {"per_kwh": 0.2}, {"start_time": "x", "per_kwh": 0.2},
                                              {"start_time": "2026-10-18T21:30:00", "end_time": "2026-10-18T22:00:00",
                                               "per_kwh": 0.2},
                                              {**item(T0, 0.2), "end_time": T0.isoformat()},
                                              {**item(T0, float("nan"))}]}),
])
def test_missing_or_malformed_forecasts_give_no_intervals(state):
    assert amber_intervals(state, T0) == []


def test_overlapping_items_are_dropped():
    s = core(T0, [0.2, 0.3])
    s.attributes["forecasts"].append(item(T0 + timedelta(minutes=10), 9.9))
    assert [x.price for x in amber_intervals(s, T0)] == [0.2, 0.3]


# B: absolutePriceSchedule

@pytest.mark.parametrize("value,expected", [
    (0.2245, {"value": 225, "exponent": -3}), (-0.0504, {"value": -50, "exponent": -3}),
    (32.767, {"value": 32767, "exponent": -3}), (-32.768, {"value": -32768, "exponent": -3}),
    (32.768, {"value": 3277, "exponent": -2}), (17.5, {"value": 17500, "exponent": -3}),
    (0, {"value": 0, "exponent": -3}), (1e9, {"value": 32767, "exponent": 3}),
])
def test_rational_number_int16_and_exponent_bounds(value, expected):
    assert rational(value) == expected


def test_price_schedule_shape_and_policy_bounds():
    intervals = [iv(T0 + timedelta(minutes=5 * i), 5, 0.1 + i / 100) for i in range(300)]
    s = build_price_schedule(intervals, max_rule_stacks=288)
    assert set(s) == {"timeAnchor", "priceScheduleID", "currency", "language", "priceAlgorithm",
                      "priceRuleStacks", "priceScheduleDescription"}
    assert s["timeAnchor"] == T0.isoformat() and s["currency"] == "AUD" and s["language"] == "en"
    assert s["priceAlgorithm"] == "urn:iso:std:iso:15118:-20:PriceAlgorithm:1-Power"
    assert len(s["priceRuleStacks"]) == 288
    # 5-minute intervals kept as is, one rule per stack.
    assert all(x["duration"] == 300 and len(x["priceRule"]) == 1 for x in s["priceRuleStacks"])
    assert s["priceRuleStacks"][1]["priceRule"][0] == {
        "energyFee": {"value": 110, "exponent": -3}, "powerRangeStart": {"value": 0, "exponent": 0}}
    assert valid_price_schedule(s)
    assert not valid_price_schedule(s, max_rule_stacks=287)
    assert len(s["priceScheduleDescription"]) <= 160 and s["priceScheduleID"] >= 0


def test_price_schedule_horizon_clips_and_gap_stops():
    intervals = [iv(T0 + timedelta(minutes=30 * i), 30, 0.2) for i in range(4)]
    s = build_price_schedule(intervals, max_horizon_s=4000)
    assert [x["duration"] for x in s["priceRuleStacks"]] == [1800, 1800, 400]
    assert valid_price_schedule(s, max_horizon_s=4000)
    gap = intervals[:2] + [iv(T0 + timedelta(hours=3), 30, 0.2)]
    assert len(build_price_schedule(gap)["priceRuleStacks"]) == 2
    with pytest.raises(ValueError):
        build_price_schedule([])


@pytest.mark.parametrize("change", [
    {"currency": "USD"}, {"language": "de"}, {"taxRules": []}, {"priceScheduleID": -1},
    {"priceScheduleDescription": "x" * 161}, {"timeAnchor": "2026-10-18T21:30:00"},
    {"priceRuleStacks": []},
    {"priceRuleStacks": [{"duration": 0, "priceRule": [{"energyFee": {"value": 1, "exponent": 0},
                                                       "powerRangeStart": {"value": 0, "exponent": 0}}]}]},
    {"priceRuleStacks": [{"duration": 60, "priceRule": [{"energyFee": {"value": 40000, "exponent": 0},
                                                        "powerRangeStart": {"value": 0, "exponent": 0}}]}]},
    {"priceRuleStacks": [{"duration": 60, "priceRule": [{"energyFee": {"value": 1, "exponent": -4},
                                                        "powerRangeStart": {"value": 0, "exponent": 0}}]}]},
])
def test_price_schedule_validator_mirrors_gateway(change):
    s = {**build_price_schedule([iv(T0, 30, 0.2)]), **change}
    assert not valid_price_schedule(s)


# C: CostUpdated

def test_cost_updated_body_and_price_text_local_time():
    cur, nxt = iv(T0 - timedelta(minutes=30), 30, 0.2249), iv(T0, 30, 0.20151)
    body = build_cost_updated("tx-1", cur, nxt, BNE, total_cost=1.5)
    assert body == {"totalCost": 1.5, "transactionId": "tx-1", "customData": {
        "vendorId": "org.openchargealliance.costmsg", "chargingPrice": {"kWhPrice": 0.2249},
        "nextPeriod": {"atTime": T0.isoformat(), "chargingPrice": {"kWhPrice": 0.20151}},
        "priceText": "Amber 22.5 c/kWh now, 20.2 c/kWh from 07:30"}}
    assert valid_cost_updated(body)


def test_cost_updated_negative_clamped_and_no_next():
    body = build_cost_updated("t", iv(T0, 30, -0.031), None, BNE)
    assert body["customData"] == {"vendorId": "org.openchargealliance.costmsg",
                                  "chargingPrice": {"kWhPrice": -0.031}, "priceText": "Amber -3.1 c/kWh now"}
    spike = build_cost_updated("t", iv(T0, 30, 25.0), iv(T0, 30, -9.0), BNE)["customData"]
    assert spike["chargingPrice"] == {"kWhPrice": 20} and spike["nextPeriod"]["chargingPrice"] == {"kWhPrice": -5}
    assert spike["priceText"].startswith("Amber 2500.0 c/kWh now, -900.0 c/kWh")
    assert valid_cost_updated(build_cost_updated("t", iv(T0, 30, 25.0), None, BNE))
    for bad in (-1, 10001, float("nan")):
        with pytest.raises(ValueError):
            build_cost_updated("t", iv(T0, 30, 0.2), None, BNE, total_cost=bad)


@pytest.mark.parametrize("change", [
    {"totalCost": -1}, {"totalCost": 10001}, {"extra": 1}, {"customData": {"vendorId": "x", "chargingPrice": {"kWhPrice": 1}}},
    {"customData": {"vendorId": "org.openchargealliance.costmsg", "chargingPrice": {"kWhPrice": 21}}},
    {"customData": {"vendorId": "org.openchargealliance.costmsg", "chargingPrice": {"kWhPrice": 1}, "priceText": "x" * 256}},
    {"customData": {"vendorId": "org.openchargealliance.costmsg", "chargingPrice": {"kWhPrice": 1},
                    "nextPeriod": {"atTime": "soon", "chargingPrice": {"kWhPrice": 1}}}},
])
def test_cost_updated_validator_mirrors_gateway(change):
    assert not valid_cost_updated({**build_cost_updated("t", iv(T0, 30, 0.2), None, BNE), **change})


# D: SetDefaultTariff

def test_tariff_gst_maths_local_time_and_dates():
    t = build_tariff([iv(T0, 30, 0.22), iv(T0 + timedelta(minutes=30), 30, 0.3), iv(T0 + timedelta(hours=1), 30, -0.011)], BNE)
    assert t["tariffId"] == "amber-20261019-0730" and len(t["tariffId"]) <= 60
    assert t["currency"] == "AUD" and t["validFrom"] == T0.isoformat()
    assert t["energy"]["taxRates"] == [{"type": "GST", "tax": 10}]
    assert [p["priceKwh"] for p in t["energy"]["prices"]] == [0.2, 0.27273, -0.01]
    assert t["energy"]["prices"][0]["conditions"] == {
        "startTimeOfDay": "07:30", "endTimeOfDay": "08:00",
        "validFromDate": "2026-10-19", "validToDate": "2026-10-20"}
    assert valid_tariff(t)


def test_tariff_interval_crossing_midnight_is_split_and_midnight_end():
    late = datetime(2026, 10, 19, 13, 45, tzinfo=UTC)  # 23:45 Brisbane
    assert local_parts(iv(late, 30, 0.2), BNE) == [
        (datetime(2026, 10, 19).date(), "23:45", "00:00"), (datetime(2026, 10, 20).date(), "00:00", "00:15")]
    t = build_tariff([iv(late, 30, 0.2), iv(late + timedelta(minutes=30), 30, 0.3)], BNE)
    assert [p["conditions"] for p in t["energy"]["prices"]] == [
        {"startTimeOfDay": "23:45", "endTimeOfDay": "00:00", "validFromDate": "2026-10-19", "validToDate": "2026-10-20"},
        {"startTimeOfDay": "00:00", "endTimeOfDay": "00:15", "validFromDate": "2026-10-20", "validToDate": "2026-10-21"},
        {"startTimeOfDay": "00:15", "endTimeOfDay": "00:45", "validFromDate": "2026-10-20", "validToDate": "2026-10-21"}]
    assert valid_tariff(t)


def test_tariff_export_entries_station_pays_driver():
    t = build_tariff([iv(T0, 30, 0.22)], BNE, export_intervals=[iv(T0, 30, 0.055), iv(T0, 30, -0.011)])
    imp, exp, neg = t["energy"]["prices"]
    assert "minPower" not in imp["conditions"]
    assert exp == {"priceKwh": -0.05, "conditions": {
        "startTimeOfDay": "07:30", "endTimeOfDay": "08:00", "validFromDate": "2026-10-19",
        "validToDate": "2026-10-20", "minPower": -1, "maxPower": -1000000}}
    # A negative feed-in (customer pays to export) becomes a positive price.
    assert neg["priceKwh"] == 0.01
    assert valid_tariff(t)


def test_tariff_truncated_to_intervals_and_max_prices():
    many = [iv(T0 + timedelta(minutes=30 * i), 30, 0.2) for i in range(100)]
    assert len(build_tariff(many, BNE)["energy"]["prices"]) == 48
    assert len(build_tariff(many, BNE, count=5)["energy"]["prices"]) == 5
    t = build_tariff(many, BNE, export_intervals=many, count=96, limits={"max_prices": 96})
    prices = t["energy"]["prices"]
    assert len(prices) == 96 and sum("minPower" in p["conditions"] for p in prices) == 48
    assert valid_tariff(t)
    assert not valid_tariff(t, limits={"max_prices": 95})
    clamped = build_tariff([iv(T0, 30, 30.0), iv(T0 + timedelta(minutes=30), 30, -8.0)], BNE)
    assert [p["priceKwh"] for p in clamped["energy"]["prices"]] == [20, -5]


@pytest.mark.parametrize("change", [
    {"currency": "USD"}, {"tariffId": "x" * 61}, {"tariffId": "amber\u00e9"}, {"tariffId": "HA-LAB-ZERO"},
    {"fixedFee": {}},
    {"energy": {"prices": [{"priceKwh": 1, "conditions": {"minPower": -1000001}}]}},
    {"description": [{"format": "UTF8", "language": "en", "content": "x"}] * 4},
    {"energy": {"prices": []}},
    {"energy": {"prices": [{"priceKwh": 21}]}},
    {"energy": {"prices": [{"priceKwh": 1, "conditions": {"chargingTime": 1}}]}},
    {"energy": {"prices": [{"priceKwh": 1, "conditions": {"startTimeOfDay": "24:00"}}]}},
    {"energy": {"prices": [{"priceKwh": 1, "conditions": {"validToDate": "2026-02-30"}}]}},
    {"energy": {"prices": [{"priceKwh": 1}], "taxRates": [{"type": "VAT", "tax": 20}]}},
])
def test_tariff_validator_mirrors_gateway(change):
    assert not valid_tariff({**build_tariff([iv(T0, 30, 0.2)], BNE), **change})


@pytest.mark.parametrize("response,status", [
    ({"status": "Accepted"}, "Accepted"), ([{"success": True, "payload": {"status": "Rejected"}}], "Rejected"),
    ({"errorCode": "NotImplemented", "errorDescription": "x"}, "NotImplemented"),
    ({"_messageId": "m", "_errorCode": "NotImplemented", "_errorDetails": {}, "name": "OcppError",
      "message": "x"}, "CALLERROR NotImplemented"),
    ({"status": "TooManyElements", "statusInfo": {"reasonCode": "x"}}, "TooManyElements"),
    ([{"success": True}], None), ("ok", None), (None, None),
])
def test_tariff_status_from_verbatim_reply(response, status):
    assert tariff_status(response) == status


def test_price_text_truncated_and_local():
    assert price_text(iv(T0, 30, 0.1), iv(T0 + timedelta(minutes=30), 30, 0.05), BNE) == \
        "Amber 10.0 c/kWh now, 5.0 c/kWh from 08:00"


# Policy: price_commands is optional and never fails compatibility

@pytest.mark.parametrize("prices,enabled", [
    (None, False), ({"enabled": False}, False), ("yes", False), (price_policy(), True),
    (price_policy(currency="aud"), False), (price_policy(cost_updated_by_protocol={"ocpp2.1": "/evil"}), False),
    (price_policy(default_tariff="/other"), False), (price_policy(tariff_stations=["a/b"]), False),
    (price_policy(price_schedule={"max_rule_stacks": 0}), False), (price_policy(tariff_limits=[]), False),
])
@pytest.mark.asyncio
async def test_policy_price_commands_optional(prices, enabled):
    c = client(policy(prices))
    await c.control_policy()
    assert c.price_commands["enabled"] is enabled
    assert parse_price_commands(policy(prices)) == c.price_commands
    if enabled:
        assert c.price_commands["tariff_stations"] == [STATION, PLUGFEST]
        assert c.price_commands["price_schedule"]["max_rule_stacks"] == 288


# Client dispatch

def cost_body(tx="tx-1"):
    return build_cost_updated(tx, iv(T0, 30, 0.225), iv(T0 + timedelta(minutes=30), 30, 0.2), BNE)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol,path", [("ocpp2.1", COST_21), ("ocpp2.0.1", COST_201)])
async def test_cost_updated_uses_protocol_route_and_awaits_evidence(protocol, path):
    c = client();await c.control_policy()
    lc = await c.cost_updated(protocol=protocol, station_id=STATION, body=cost_body())
    assert c.posts == [(path, {"identifier": STATION, "tenantId": 1}, cost_body())]
    assert lc["action"] == "cost_update" and lc["state"] == "dispatched_awaiting_station_evidence"
    assert lc["transaction_id"] == "tx-1"


@pytest.mark.asyncio
async def test_price_commands_refused_before_send():
    c = client(policy());await c.control_policy()
    with pytest.raises(CitrineCompatibilityError, match="disabled"):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body=cost_body())
    c = client();await c.control_policy()
    with pytest.raises(CitrineCompatibilityError):
        await c.cost_updated(protocol="ocpp1.6", station_id=STATION, body=cost_body())
    with pytest.raises(CitrineCompatibilityError):
        await c.cost_updated(protocol=PROTOCOL, station_id=PLUGFEST, body=cost_body())
    with pytest.raises(CitrineCompatibilityError):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body={**cost_body(), "totalCost": -1})
    tariff = build_tariff([iv(T0, 30, 0.2)], BNE)
    with pytest.raises(CitrineCompatibilityError, match="tariff_stations"):
        await c.set_default_tariff(protocol=PROTOCOL, station_id="OTHER-1", evse_id=0, tariff=tariff)
    with pytest.raises(CitrineCompatibilityError):
        await c.set_default_tariff(protocol="ocpp2.0.1", station_id=PLUGFEST, evse_id=0, tariff=tariff)
    with pytest.raises(CitrineCompatibilityError):
        await c.set_default_tariff(protocol=PROTOCOL, station_id=PLUGFEST, evse_id=-1, tariff=tariff)
    assert c.posts == [] and c.last_command == {"state": "not_sent"}


@pytest.mark.asyncio
async def test_price_switch_turned_off_since_last_poll_is_honoured():
    c = client();await c.control_policy()
    c.fake["policy"] = policy({"enabled": False})
    with pytest.raises(CitrineCompatibilityError):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body=cost_body())
    assert c.posts == [] and c.last_command == {"state": "not_sent"}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,state,status", [
    ({"status": "Accepted"}, "station_responded", "Accepted"),
    ({"_messageId": "m", "_errorCode": "NotImplemented", "_errorDetails": {}, "name": "OcppError",
      "message": "SetDefaultTariff"}, "station_responded", "CALLERROR NotImplemented"),
    ({"status": "Rejected", "statusInfo": {"reasonCode": "x"}}, "station_responded", "Rejected"),
    ([{"success": True}], "dispatched_awaiting_station_evidence", None),
])
async def test_default_tariff_to_plugfest_station_returns_station_reply(reply, state, status):
    c = client(reply=reply);await c.control_policy()
    tariff = build_tariff([iv(T0, 30, 0.2)], BNE)
    lc = await c.set_default_tariff(protocol=PROTOCOL, station_id=PLUGFEST, evse_id=0, tariff=tariff)
    assert c.posts == [(TARIFF, {"identifier": PLUGFEST, "tenantId": 1}, {"evseId": 0, "tariff": tariff})]
    assert lc["state"] == state and lc.get("station_status") == status and lc["station_id"] == PLUGFEST
    if status:
        assert lc["station_response"] == reply


@pytest.mark.asyncio
async def test_price_errors_500_rejected_502_unknown():
    c = client(reply=CitrineHttpError("x", 500));await c.control_policy()
    with pytest.raises(CitrineHttpError):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body=cost_body())
    assert c.last_command["state"] == "rejected"
    c.fake["reply"] = CitrineTransportError("timeout")
    with pytest.raises(CitrineTransportError):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body=cost_body())
    assert c.last_command["state"] == "outcome_unknown"
    # A tariff 502 means CitrineOS did not send it; 504 (no station reply) stays unknown.
    t = client(reply=CitrineHttpError("x", 502));await t.control_policy()
    tariff = build_tariff([iv(T0, 30, 0.2)], BNE)
    with pytest.raises(CitrineHttpError):
        await t.set_default_tariff(protocol=PROTOCOL, station_id=PLUGFEST, evse_id=0, tariff=tariff)
    assert t.last_command["state"] == "rejected"
    t.fake["reply"] = CitrineHttpError("x", 504)
    with pytest.raises(CitrineHttpError):
        await t.set_default_tariff(protocol=PROTOCOL, station_id=PLUGFEST, evse_id=0, tariff=tariff)
    assert t.last_command["state"] == "outcome_unknown"
    # Unresolved: price commands are blocked like every other non-stop command.
    with pytest.raises(Exception, match="reconcile"):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body=cost_body())


@pytest.mark.asyncio
async def test_price_message_never_replaces_awaited_start_evidence():
    c = client();await c.control_policy()
    await c.request_start_transaction(protocol=PROTOCOL, station_id=STATION)
    with pytest.raises(Exception, match="awaits station evidence"):
        await c.cost_updated(protocol=PROTOCOL, station_id=STATION, body=cost_body())
    assert c.last_command["action"] == "start" and len(c.posts) == 1


@pytest.mark.asyncio
async def test_schedule_carries_price_schedule_only_on_2_1_with_prices_enabled():
    c = client();await c.control_policy()
    ps = build_price_schedule([iv(T0, 30, 0.2), iv(T0 + timedelta(minutes=30), 30, -0.01)])
    await c.submit_schedule(protocol=PROTOCOL, station_id=STATION, transaction_id="t",
                            periods=[power_period(0, 3000)], immediate=True, price_schedule=ps)
    path, _, body = c.posts[-1]
    assert path == PATHS["profile"]
    sched = body["chargingProfile"]["chargingSchedule"][0]
    assert sched["absolutePriceSchedule"] == ps and sched["chargingSchedulePeriod"] == [power_period(0, 3000)]
    with pytest.raises(CitrineCompatibilityError, match="OCPP 2.1"):
        await c.submit_schedule(protocol="ocpp2.0.1", station_id=STATION, transaction_id="t",
                                periods=[charge_period(0, 3000)], immediate=True, price_schedule=ps)
    with pytest.raises(CitrineCompatibilityError):
        await c.submit_schedule(protocol=PROTOCOL, station_id=STATION, transaction_id="t",
                                periods=[power_period(0, 3000)], immediate=True,
                                price_schedule={**ps, "currency": "USD"})
    off = client(policy());await off.control_policy()
    with pytest.raises(CitrineCompatibilityError):
        await off.submit_schedule(protocol=PROTOCOL, station_id=STATION, transaction_id="t",
                                  periods=[power_period(0, 3000)], immediate=True, price_schedule=ps)
    assert len(c.posts) == 1 and off.posts == []


# Services

def station(sid=STATION, protocol=PROTOCOL, tx="tx-1", online=True):
    return {"id": sid, "protocol": protocol, "isOnline": online, "activeTransactionId": tx, "lab_meter": {}}


def now_forecast(minutes=30, prices=(0.225, 0.202, -0.03), entity="sensor.amber_express_general_price"):
    now = datetime.now(UTC)
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % minutes)
    items = [item(start + timedelta(minutes=i * minutes), p, minutes) for i, p in enumerate(prices)]
    return SimpleNamespace(entity_id=entity, state=str(prices[0]), attributes={"detailedForecast": items}), start


async def services(c, stations, states, openadr=None):
    coordinator = MagicMock();coordinator.async_request_refresh = AsyncMock()
    coordinator.data = {"stations": stations};coordinator.last_update_success = True
    ctx = {"client": c, "coordinator": coordinator}
    hass = MagicMock();hass.data = {"citrine_ha": {"entry": ctx}}
    # openadr: {entity_id: get_forecast rows}; the openadr3_ven service exists only then.
    hass.services.has_service.side_effect = lambda domain, name: openadr is not None and domain == "openadr3_ven"

    async def get_forecast(domain, name, data, target=None, blocking=False, return_response=False):
        assert (domain, name, blocking, return_response) == ("openadr3_ven", "get_forecast", True, True)
        assert data["end"] - data["start"] == timedelta(hours=49)
        eid = target["entity_id"]
        return {eid: {"payload_type": "PRICE", "unit": "AUD/kWh", "forecast": openadr[eid]}}
    hass.services.async_call = AsyncMock(side_effect=get_forecast)
    hass.config.time_zone = "Australia/Brisbane"
    hass.states.get.side_effect = states.get
    entry = MagicMock();entry.entry_id = "entry"
    await setup_lab(hass, entry, [].extend, "button")
    calls = {call.args[1]: call for call in hass.services.async_register.call_args_list}
    for name in (SERVICE, SERVICE_COST, SERVICE_TARIFF):
        assert calls[name].kwargs["supports_response"] == SupportsResponse.OPTIONAL

    def call(name, **data):
        schema = calls[name].kwargs["schema"]
        return calls[name].args[2](MagicMock(data=schema({"entry_id": "entry", **data})))
    return call


@pytest.mark.asyncio
async def test_cost_update_service_sends_current_and_next_price():
    c = client();await c.control_policy()
    state, start = now_forecast()
    call = await services(c, [station()], {state.entity_id: state})
    out = await call(SERVICE_COST, price_entity=state.entity_id, total_cost="2.5")
    path, params, body = c.posts[-1]
    nxt = (start + timedelta(minutes=30)).astimezone(BNE)
    assert path == COST_21 and params["identifier"] == STATION
    assert body["transactionId"] == "tx-1" and body["totalCost"] == 2.5
    assert body["customData"]["chargingPrice"] == {"kWhPrice": 0.225}
    assert body["customData"]["nextPeriod"]["chargingPrice"] == {"kWhPrice": 0.202}
    assert body["customData"]["priceText"] == f"Amber 22.5 c/kWh now, 20.2 c/kWh from {nxt:%H:%M}"
    assert out["sent"]["price_text"] == body["customData"]["priceText"]
    assert out["command_state"] == "dispatched_awaiting_station_evidence" and out["station_id"] == STATION


@pytest.mark.asyncio
async def test_cost_update_validation_errors():
    state, _ = now_forecast()
    future, _ = now_forecast()
    future.attributes = {"forecasts": future.attributes["detailedForecast"][1:]}
    states = {state.entity_id: state, "sensor.future": SimpleNamespace(**{**vars(future), "entity_id": "sensor.future"})}
    off = client(policy());await off.control_policy()
    call = await services(off, [station()], states)
    with pytest.raises(ServiceValidationError, match="Price commands are disabled"):
        await call(SERVICE_COST, price_entity=state.entity_id)
    c = client();await c.control_policy()
    call = await services(c, [station(tx=None)], states)
    with pytest.raises(ServiceValidationError, match="active transaction"):
        await call(SERVICE_COST, price_entity=state.entity_id)
    call = await services(c, [station()], states)
    with pytest.raises(ServiceValidationError, match="current interval"):
        await call(SERVICE_COST, price_entity="sensor.future")
    with pytest.raises(ServiceValidationError, match="not found"):
        await call(SERVICE_COST, price_entity="sensor.missing")
    with pytest.raises(Exception):
        await call(SERVICE_COST, price_entity=state.entity_id, total_cost=10001)
    c16 = client(policy(price_policy(), protocols=["ocpp2.1", "ocpp1.6"], commands_by_protocol={
        "ocpp2.1": list(PATHS.values()), "ocpp1.6": ["/ocpp/1.6/evdriver/remoteStartTransaction",
        "/ocpp/1.6/evdriver/remoteStopTransaction", "/ocpp/1.6/smartcharging/setChargingProfile",
        "/ocpp/1.6/smartcharging/clearChargingProfile"]}))
    await c16.control_policy()
    call = await services(c16, [station(protocol="ocpp1.6", tx="42")], states)
    with pytest.raises(ServiceValidationError, match="2.0.1 only"):
        await call(SERVICE_COST, price_entity=state.entity_id)
    assert off.posts == c.posts == c16.posts == []


@pytest.mark.asyncio
async def test_default_tariff_service_to_plugfest_station_with_export():
    c = client(reply={"status": "Accepted"});await c.control_policy()
    imp, start = now_forecast(prices=[0.22] * 60)
    exp, _ = now_forecast(prices=[0.055] * 60, entity="sensor.amber_express_feed_in_price")
    call = await services(c, [station(), station(PLUGFEST, tx=None)],
                          {imp.entity_id: imp, exp.entity_id: exp})
    out = await call(SERVICE_TARIFF, price_entity=imp.entity_id, export_price_entity=exp.entity_id,
                     station_id=PLUGFEST, evse_id=1, intervals=10)
    path, params, body = c.posts[-1]
    assert path == TARIFF and params["identifier"] == PLUGFEST and body["evseId"] == 1
    prices = body["tariff"]["energy"]["prices"]
    assert len(prices) == 20 and prices[0]["priceKwh"] == 0.2 and prices[10]["priceKwh"] == -0.05
    assert prices[0]["conditions"]["startTimeOfDay"] == f"{start.astimezone(BNE):%H:%M}"
    assert out["station_status"] == "Accepted" and out["command_state"] == "station_responded"
    assert out["sent"]["import_prices"] == 10 and out["sent"]["export_prices"] == 10
    assert out["station_response"] == {"status": "Accepted"}


@pytest.mark.asyncio
async def test_default_tariff_validation_errors():
    state, _ = now_forecast()
    states = {state.entity_id: state}
    c = client();await c.control_policy()
    call = await services(c, [station(), station("OTHER-1"), station(PLUGFEST, protocol="ocpp2.0.1")], states)
    with pytest.raises(ServiceValidationError, match="not a tariff station"):
        await call(SERVICE_TARIFF, price_entity=state.entity_id, station_id="OTHER-1")
    with pytest.raises(ServiceValidationError, match="needs OCPP 2.1"):
        await call(SERVICE_TARIFF, price_entity=state.entity_id, station_id=PLUGFEST)
    with pytest.raises(HomeAssistantError, match="online"):
        call2 = await services(c, [station(online=False)], states)
        await call2(SERVICE_TARIFF, price_entity=state.entity_id)
    empty = SimpleNamespace(entity_id="sensor.empty", state="0.2", attributes={"forecasts": []})
    call = await services(c, [station()], {**states, "sensor.empty": empty})
    with pytest.raises(ServiceValidationError, match="no current or future price interval"):
        await call(SERVICE_TARIFF, price_entity="sensor.empty")
    off = client(policy());await off.control_policy()
    call = await services(off, [station()], states)
    with pytest.raises(ServiceValidationError, match="disabled"):
        await call(SERVICE_TARIFF, price_entity=state.entity_id)
    assert c.posts == off.posts == []
    # Default: the command station (listed in tariff_stations), EVSE 0, 48 intervals.
    call = await services(c, [station()], states)
    out = await call(SERVICE_TARIFF, price_entity=state.entity_id)
    assert c.posts[-1][1]["identifier"] == STATION and c.posts[-1][2]["evseId"] == 0
    assert out["sent"]["import_prices"] == 3 and out["command_state"] == "dispatched_awaiting_station_evidence"


@pytest.mark.asyncio
async def test_lab_schedule_price_entity_attaches_absolute_price_schedule():
    c = client();await c.control_policy()
    state, start = now_forecast(minutes=5, prices=[0.1 + i / 1000 for i in range(400)])
    call = await services(c, [station()], {state.entity_id: state})
    periods = [power_period(0, 3000)]
    out = await call(SERVICE, periods=periods, immediate=True, price_entity=state.entity_id)
    sched = c.posts[-1][2]["chargingProfile"]["chargingSchedule"][0]
    ps = sched["absolutePriceSchedule"]
    assert ps["timeAnchor"] == start.isoformat() and len(ps["priceRuleStacks"]) == 288
    assert ps["priceRuleStacks"][0]["duration"] == 300
    assert ps["priceRuleStacks"][0]["priceRule"][0]["energyFee"] == {"value": 100, "exponent": -3}
    assert out["sent"]["price_schedule"]["rule_stacks"] == 288
    # Without price_entity the schedule is unchanged.
    await call(SERVICE, periods=periods, immediate=True)
    assert "absolutePriceSchedule" not in c.posts[-1][2]["chargingProfile"]["chargingSchedule"][0]
    c201 = client();await c201.control_policy()
    call = await services(c201, [station(protocol="ocpp2.0.1")], {state.entity_id: state})
    with pytest.raises(ServiceValidationError, match="OCPP 2.1"):
        await call(SERVICE, periods=[charge_period(0, 3000)], immediate=True, price_entity=state.entity_id)
    off = client(policy());await off.control_policy()
    call = await services(off, [station()], {state.entity_id: state})
    with pytest.raises(ServiceValidationError, match="disabled"):
        await call(SERVICE, periods=periods, immediate=True, price_entity=state.entity_id)
    assert c201.posts == off.posts == []


def vtn_rows(start, prices, first_minutes=5):
    """openadr3_ven get_forecast rows: a 5-minute current interval, then 30-minute ones."""
    rows, at = [], start
    for i, price in enumerate(prices):
        minutes = first_minutes if i == 0 else 30
        rows.append({"datetime": at.astimezone(BNE).isoformat(), "value": price, "interval_minutes": minutes})
        at += timedelta(minutes=minutes)
    return rows


def test_openadr_rows_become_intervals_current_first():
    now = T0 + timedelta(minutes=2)
    rows = vtn_rows(T0, [0.355155, 0.383078, -0.02])
    rows.insert(0, {"datetime": (T0 - timedelta(minutes=30)).isoformat(), "value": 0.3, "interval_minutes": 30})
    rows += [{"datetime": "bad", "value": 1, "interval_minutes": 30}, {"datetime": T0.isoformat(), "value": None,
             "interval_minutes": 30}, {"datetime": T0.isoformat(), "value": 1, "interval_minutes": 0}, "row"]
    state = SimpleNamespace(entity_id="sensor.x", state="0.355155", attributes={"forecasts": openadr_forecasts(rows)})
    got = amber_intervals(state, now)
    assert [(i.start, i.end, i.price) for i in got] == [
        (T0, T0 + timedelta(minutes=5), 0.355155),
        (T0 + timedelta(minutes=5), T0 + timedelta(minutes=35), 0.383078),
        (T0 + timedelta(minutes=35), T0 + timedelta(minutes=65), -0.02)]
    current, nxt = current_and_next(got, now)
    assert current.price == 0.355155 and nxt.price == 0.383078
    assert openadr_forecasts(None) == openadr_forecasts("x") == []


@pytest.mark.asyncio
async def test_openadr_ven_sensor_feeds_cost_update_and_tariff():
    c = client();await c.control_policy()
    now = datetime.now(UTC)
    start = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
    imp, exp = "sensor.openadr3_vtn_au_qld1_price", "sensor.openadr3_vtn_au_qld1_export_price"
    attrs = {"payload_type": "PRICE", "unit_of_measurement": "AUD/kWh", "forecast_rows": 3}
    states = {imp: SimpleNamespace(entity_id=imp, state="0.355155", attributes=attrs),
              exp: SimpleNamespace(entity_id=exp, state="0.216571", attributes={**attrs, "payload_type": "EXPORT_PRICE"})}
    call = await services(c, [station()], states,
                          openadr={imp: vtn_rows(start, [0.355155, 0.383078, 0.390571]),
                                   exp: vtn_rows(start, [0.216571, 0.239819, 0.25])})
    out = await call(SERVICE_COST, price_entity=imp)
    body = c.posts[-1][2]
    assert body["customData"]["chargingPrice"] == {"kWhPrice": 0.35516}
    assert body["customData"]["nextPeriod"]["chargingPrice"] == {"kWhPrice": 0.38308}
    assert body["customData"]["priceText"].startswith("OpenADR 35.5 c/kWh now, 38.3 c/kWh from ")
    assert out["sent"]["price_text"] == body["customData"]["priceText"]
    await call(SERVICE_TARIFF, price_entity=imp, export_price_entity=exp, intervals=3)
    tariff = c.posts[-1][2]["tariff"]
    assert tariff["tariffId"].startswith("openadr-")
    assert tariff["description"][0]["content"].startswith("OpenADR forecast, 3 intervals")
    prices = tariff["energy"]["prices"]
    assert prices[0]["priceKwh"] == round(0.355155 / 1.1, 5)
    assert any(p["priceKwh"] == -round(0.216571 / 1.1, 5) for p in prices)


@pytest.mark.asyncio
async def test_openadr_forecast_without_rows_is_a_clear_error():
    c = client();await c.control_policy()
    imp = "sensor.openadr3_vtn_au_qld1_price"
    states = {imp: SimpleNamespace(entity_id=imp, state="0.3", attributes={"payload_type": "PRICE"})}
    call = await services(c, [station()], states, openadr={imp: []})
    with pytest.raises(ServiceValidationError, match="no current or future price interval"):
        await call(SERVICE_COST, price_entity=imp)
    assert c.posts == []
