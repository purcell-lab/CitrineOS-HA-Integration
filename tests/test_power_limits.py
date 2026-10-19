"""Max charge/discharge power per station: EV limit, EVSE rating, else the fallback."""
import logging
import re
from unittest.mock import MagicMock

import pytest
from graphql import parse
from homeassistant.util import slugify

from custom_components.citrine_ha.compatibility import CONF_API_PROFILE, PROFILE_LAB, PROFILE_V2
from custom_components.citrine_ha.config_flow import CitrineOptionsFlow
from custom_components.citrine_ha.const import CONF_DEFAULT_MAX_POWER_W, CONF_TENANT_ID
from custom_components.citrine_ha.coordinator import default_max_power_w
from custom_components.citrine_ha.hasura_client import HasuraError
from custom_components.citrine_ha.lab_entities import LabMaxPowerSensor, max_power_sensors
from custom_components.citrine_ha.power_limits import (
    ATTRIBUTES_QUERY, CHARACTERISTICS_QUERY, COMPONENTS_QUERY, NEEDS_QUERY, VARIABLES_QUERY,
    PowerLimitReader, ev_limits, sensor_state)
from custom_components.citrine_ha.sensor import CitrineStationMaxPowerSensor

LAB = "SIM-EV-EVEREST-V2G-03"


def discovery(protocol="ocpp2.1", tx=True, station=LAB, db=7):
    """A normalized CitrineOS 2.x discovery payload with one station."""
    return {
        "stations": [{"id": station, "databaseId": db, "protocol": protocol, "tenantId": 1,
                      "activeTransactionId": "uuid-1" if tx else None,
                      "connectors": [{"databaseEvseId": 11, "evseId": 1}]}],
        "transactions": [{"id": 42, "stationId": station, "transactionId": "uuid-1", "isActive": True,
                          "startedAt": "2026-10-19T01:00:00+00:00"}] if tx else [],
    }


class FakeHasura:
    """Answers the flat power queries; a table in `refuse` fails like a missing permission."""

    def __init__(self, needs=(), characteristics=None, attributes=None, refuse=(), transient=()):
        self.calls = []
        self.refuse, self.transient = set(refuse), set(transient)
        self.tables = {
            "Variables": [{"id": 1, "name": "Power", "instance": None}],
            "VariableAttributes": attributes if attributes is not None else [
                {"id": 5, "stationId": 7, "componentId": 3, "variableId": 1, "type": "Actual", "value": "0"}],
            "Components": [{"id": 3, "name": "EVSE", "instance": None, "evseDatabaseId": 2}],
            "VariableCharacteristics": characteristics if characteristics is not None else [
                {"id": 9, "variableId": 1, "maxLimit": 22000, "minLimit": None, "unit": "W"}],
            "ChargingNeeds": list(needs),
        }

    async def query(self, query, variables=None):
        root = re.search(r"\{\s*(\w+)\(", query).group(1)
        self.calls.append((root, variables))
        if root in self.refuse:
            raise HasuraError("Hasura query failed", errors=[
                {"message": f"field '{root}' not found in type: 'query_root'",
                 "extensions": {"code": "validation-failed"}}])
        if root in self.transient:
            raise HasuraError("Hasura request timed out")
        return {"data": {root: self.tables[root]}}


def needs(dc=None, ac=None, tx=42, row_id=1, evse=11, created="2026-10-19T01:00:05+00:00"):
    return {"id": row_id, "evseId": evse, "transactionDatabaseId": tx, "createdAt": created,
            "dcChargingParameters": dc, "acChargingParameters": ac}


async def attach(hasura, data=None, fallback=25000):
    data = data or discovery()
    await PowerLimitReader(hasura).async_attach(data, fallback)
    return data["stations"][0]["power_limits"]


@pytest.mark.asyncio
async def test_ev_limit_wins_when_smaller():
    hasura = FakeHasura(needs=[needs(dc={"evMaxPower": 11000, "evMaxDischargePower": 7000,
                                         "evMaxCurrent": 30, "evMaxVoltage": 400})])
    limits = await attach(hasura)
    assert limits["charge"] == {"value": 11000, "source": "ev_charging_needs", "ev_limit_w": 11000,
                                "evse_rating_w": 22000, "fallback_w": 25000, "transaction_id": "uuid-1"}
    assert limits["discharge"]["value"] == 7000
    assert limits["discharge"]["source"] == "ev_charging_needs"


@pytest.mark.asyncio
async def test_evse_rating_wins_when_ev_limit_is_larger():
    hasura = FakeHasura(needs=[needs(dc={"evMaxPower": 150000, "evMaxDischargePower": 50000})])
    limits = await attach(hasura)
    assert limits["charge"]["value"] == 22000 and limits["charge"]["source"] == "evse_rating"
    assert limits["discharge"]["ev_limit_w"] == 50000 and limits["discharge"]["value"] == 22000


@pytest.mark.asyncio
async def test_evse_max_limit_used_when_no_session():
    hasura = FakeHasura()
    limits = await attach(hasura, discovery(tx=False))
    for direction in ("charge", "discharge"):
        assert limits[direction]["value"] == 22000
        assert limits[direction]["source"] == "evse_rating"
        assert limits[direction]["ev_limit_w"] is None
        assert limits[direction]["transaction_id"] is None
    assert "ChargingNeeds" not in [c[0] for c in hasura.calls]


@pytest.mark.asyncio
async def test_max_set_attribute_when_no_max_limit():
    hasura = FakeHasura(characteristics=[{"id": 9, "variableId": 1, "maxLimit": None, "unit": "kW"}],
                        attributes=[{"id": 5, "stationId": 7, "componentId": 3, "variableId": 1,
                                     "type": "MaxSet", "value": "11"}])
    limits = await attach(hasura, discovery(tx=False))
    assert limits["charge"]["value"] == 11000 and limits["charge"]["source"] == "evse_rating"


@pytest.mark.asyncio
async def test_max_set_attribute_when_characteristics_are_refused():
    hasura = FakeHasura(refuse={"VariableCharacteristics"},
                        attributes=[{"id": 5, "stationId": 7, "componentId": 3, "variableId": 1,
                                     "type": "MaxSet", "value": "7400"}])
    limits = await attach(hasura, discovery(tx=False))
    assert limits["charge"]["evse_rating_w"] == 7400


@pytest.mark.asyncio
async def test_discharge_specific_rating_and_negative_min_limit():
    hasura = FakeHasura()
    hasura.tables["Variables"].append({"id": 2, "name": "DischargePower", "instance": None})
    hasura.tables["VariableAttributes"].append(
        {"id": 6, "stationId": 7, "componentId": 3, "variableId": 2, "type": "MaxSet", "value": "10000"})
    limits = await attach(hasura, discovery(tx=False))
    assert limits["charge"]["value"] == 22000 and limits["discharge"]["value"] == 10000

    hasura = FakeHasura(characteristics=[{"id": 9, "variableId": 1, "maxLimit": 22000,
                                          "minLimit": -15000, "unit": "W"}])
    limits = await attach(hasura, discovery(tx=False))
    assert limits["discharge"]["evse_rating_w"] == 15000


@pytest.mark.asyncio
async def test_ignores_other_components_and_unknown_variables():
    hasura = FakeHasura()
    hasura.tables["Components"] = [{"id": 3, "name": "ConnectedEV", "instance": None}]
    hasura.tables["Variables"].append({"id": 8, "name": "VehicleId", "instance": None})
    limits = await attach(hasura, discovery(tx=False))
    assert limits["charge"]["source"] == "fallback"


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", ["Variables", "VariableAttributes", "Components"])
async def test_fallback_when_device_model_is_refused(refused, caplog):
    hasura = FakeHasura(refuse={refused, "ChargingNeeds"})
    reader = PowerLimitReader(hasura)
    with caplog.at_level(logging.DEBUG, logger="custom_components.citrine_ha.power_limits"):
        for _ in range(2):
            data = discovery()
            await reader.async_attach(data, 18000)
    limits = data["stations"][0]["power_limits"]
    assert limits["charge"] == {"value": 18000, "source": "fallback", "ev_limit_w": None,
                                "evse_rating_w": None, "fallback_w": 18000, "transaction_id": "uuid-1"}
    assert limits["discharge"]["source"] == "fallback"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert {r.args[0] for r in warnings} == {refused, "ChargingNeeds"}  # once each
    # A refused query is not repeated every poll.
    assert [c[0] for c in hasura.calls].count(refused) == 1


@pytest.mark.asyncio
async def test_refused_query_is_retried_later():
    now = [0.0]
    hasura = FakeHasura(refuse={"ChargingNeeds"}, needs=[needs(dc={"evMaxPower": 9000})])
    reader = PowerLimitReader(hasura, clock=lambda: now[0])
    data = discovery()
    await reader.async_attach(data, 25000)
    assert data["stations"][0]["power_limits"]["charge"]["source"] == "evse_rating"
    now[0] = 100  # due again, but a refused query waits 10 minutes
    await reader.async_attach(discovery(), 25000)
    assert [c[0] for c in hasura.calls].count("ChargingNeeds") == 1
    hasura.refuse.clear()  # the App's ha_reader was extended
    now[0] = 601
    data = discovery()
    await reader.async_attach(data, 25000)
    assert data["stations"][0]["power_limits"]["charge"]["value"] == 9000


@pytest.mark.asyncio
async def test_transient_error_keeps_previous_values():
    now = [0.0]
    hasura = FakeHasura(needs=[needs(dc={"evMaxPower": 9000})])
    reader = PowerLimitReader(hasura, clock=lambda: now[0])
    await reader.async_attach(discovery(), 25000)
    hasura.transient = {"ChargingNeeds", "Variables"}
    now[0] = 100
    data = discovery()
    await reader.async_attach(data, 25000)
    assert data["stations"][0]["power_limits"]["charge"]["value"] == 9000
    assert data["stations"][0]["power_limits"]["charge"]["evse_rating_w"] == 22000


@pytest.mark.asyncio
async def test_no_hasura_or_malformed_rows_use_fallback():
    data = discovery()
    await PowerLimitReader(None).async_attach(data, 25000)
    assert data["stations"][0]["power_limits"]["discharge"]["value"] == 25000
    hasura = FakeHasura(attributes=[{"stationId": 7, "componentId": 3, "variableId": 1}])
    hasura.tables["VariableCharacteristics"] = "not a list"
    limits = await attach(hasura, discovery(tx=False))
    assert limits["charge"]["source"] == "fallback"


@pytest.mark.asyncio
async def test_ocpp16_station_always_uses_fallback():
    hasura = FakeHasura(needs=[needs(dc={"evMaxPower": 5000})])
    limits = await attach(hasura, discovery(protocol="ocpp1.6"), fallback=11000)
    assert limits["charge"]["source"] == limits["discharge"]["source"] == "fallback"
    assert limits["charge"]["value"] == 11000
    assert hasura.calls == []


@pytest.mark.asyncio
async def test_unlinked_charging_needs_on_the_evse_since_the_transaction_started():
    rows = [needs(dc={"evMaxPower": 8000}, tx=None, row_id=3),
            needs(dc={"evMaxPower": 1000}, tx=None, row_id=2, created="2026-10-18T23:00:00+00:00"),
            needs(dc={"evMaxPower": 2000}, tx=None, row_id=4, evse=99)]
    limits = await attach(FakeHasura(needs=rows))
    assert limits["charge"]["ev_limit_w"] == 8000
    # A row linked to the transaction wins over unlinked rows.
    limits = await attach(FakeHasura(needs=rows + [needs(dc={"evMaxPower": 6000}, row_id=1)]))
    assert limits["charge"]["ev_limit_w"] == 6000


@pytest.mark.parametrize(("row", "charge", "discharge"), [
    # OCPP 2.0.1 DCChargingParametersType: evMaxPower, else evMaxCurrent x evMaxVoltage.
    ({"dcChargingParameters": {"evMaxPower": 50000, "evMaxCurrent": 125, "evMaxVoltage": 500}}, 50000, None),
    ({"dcChargingParameters": {"evMaxCurrent": 100, "evMaxVoltage": 400, "energyAmount": 30000}}, 40000, None),
    # OCPP 2.1 DC discharge, power or current x voltage.
    ({"dcChargingParameters": {"evMaxPower": 11000, "evMaxDischargePower": -9000}}, 11000, 9000),
    ({"dcChargingParameters": {"evMaxCurrent": 30, "evMaxVoltage": 400, "evMaxDischargeCurrent": 20}},
     12000, 8000),
    # OCPP 2.1 V2XChargingParametersType, nested or on the row, with per-phase values.
    ({"dcChargingParameters": {"v2xChargingParameters": {"maxChargePower": 7000, "maxDischargePower": 5000}}},
     7000, 5000),
    ({"v2xChargingParameters": {"maxChargePower": 3700, "maxChargePower_L2": 3700, "maxChargePower_L3": 3700,
                                "maxDischargeCurrent": 16, "maxVoltage": 230}}, 11100, 3680),
    # ACChargingParametersType: per-phase current x voltage, x phases when given.
    ({"acChargingParameters": {"evMaxCurrent": 32, "evMaxVoltage": 230, "evMinCurrent": 6,
                               "energyAmount": 20000}}, 7360, None),
    ({"acChargingParameters": '{"evMaxCurrent": "16", "evMaxVoltage": 230, "numberOfPhases": 3}'}, 11040, None),
    # Missing is not available, never zero.
    ({"dcChargingParameters": {"evMaxCurrent": 100}, "acChargingParameters": None}, None, None),
    ({"dcChargingParameters": "not json", "acChargingParameters": {"evMaxCurrent": True}}, None, None),
])
def test_charging_parameter_key_tolerance(row, charge, discharge):
    assert ev_limits(row) == {"charge": charge, "discharge": discharge}


def test_queries_are_flat_read_only_and_parse():
    for query in (VARIABLES_QUERY, ATTRIBUTES_QUERY.replace("STATION_KEY", "Int"), COMPONENTS_QUERY,
                  CHARACTERISTICS_QUERY, NEEDS_QUERY):
        operation = parse(query).definitions[0]
        assert operation.operation.value == "query"
        for root in operation.selection_set.selections:
            assert all(field.selection_set is None for field in root.selection_set.selections)


def test_fallback_option_bounds_and_default():
    assert default_max_power_w({}, {}) == 25000
    assert default_max_power_w({}, {CONF_DEFAULT_MAX_POWER_W: 7400}) == 7400
    assert default_max_power_w({}, {CONF_DEFAULT_MAX_POWER_W: 10}) == 1000
    assert default_max_power_w({}, {CONF_DEFAULT_MAX_POWER_W: 10**9}) == 350000
    assert default_max_power_w({}, {CONF_DEFAULT_MAX_POWER_W: "bad"}) == 25000
    assert sensor_state({"id": LAB}, "discharge", 25000) == (25000, {
        "source": "fallback", "ev_limit_w": None, "evse_rating_w": None, "fallback_w": 25000,
        "transaction_id": None})


@pytest.mark.parametrize("profile", [PROFILE_V2, PROFILE_LAB, None])
def test_options_flow_offers_the_fallback(profile):
    entry = MagicMock()
    entry.data = {CONF_TENANT_ID: 1, **({CONF_API_PROFILE: profile} if profile else {})}
    entry.options = {CONF_DEFAULT_MAX_POWER_W: 11000}
    schema = CitrineOptionsFlow(entry)._options_schema({**entry.data, **entry.options})
    key = next(k for k in schema.schema if k.schema == CONF_DEFAULT_MAX_POWER_W)
    assert key.default() == 11000
    assert schema({CONF_DEFAULT_MAX_POWER_W: 25000})[CONF_DEFAULT_MAX_POWER_W] == 25000


def coordinator(data):
    c = MagicMock()
    c.data = data
    c.default_max_power_w = 25000.0
    return c


def test_lab_entity_ids_for_every_discovered_station():
    data = discovery()
    data["stations"].append({"id": "REAL-01", "protocol": "ocpp1.6"})
    ctx = {"coordinator": coordinator(data), "client": MagicMock(station=LAB, protocols=("ocpp2.1",))}
    entry = MagicMock(entry_id="entry")
    known = set()
    sensors = max_power_sensors(ctx, entry, known)
    assert max_power_sensors(ctx, entry, known) == []
    ids = [f"sensor.{slugify(s.device_info['name'] + ' ' + s.name)}" for s in sensors]
    assert ids == [
        "sensor.citrineos_everest_v2g_lab_sim_ev_everest_v2g_03_max_discharge_power",
        "sensor.citrineos_everest_v2g_lab_sim_ev_everest_v2g_03_max_charge_power",
        "sensor.citrineos_lab_real_01_max_discharge_power",
        "sensor.citrineos_lab_real_01_max_charge_power",
    ]
    assert sensors[2].device_info["model"] == "Physical charger (read only)"
    sensor = sensors[0]
    assert isinstance(sensor, LabMaxPowerSensor)
    assert sensor.unique_id == f"entry_{LAB}_lab_max_discharge_power"
    assert (sensor.device_class, sensor.state_class, sensor.native_unit_of_measurement) == (
        "power", "measurement", "W")
    assert sensor.native_value == 25000 and sensor.extra_state_attributes["source"] == "fallback"


def test_read_only_profile_entity_names():
    data = discovery()
    data["stations"][0]["power_limits"] = {"charge": {
        "value": 11000, "source": "ev_charging_needs", "ev_limit_w": 11000, "evse_rating_w": 22000,
        "fallback_w": 25000, "transaction_id": "uuid-1"}}
    sensor = CitrineStationMaxPowerSensor(coordinator(data), MagicMock(entry_id="entry"),
                                          data["stations"][0], "charge")
    assert f"sensor.{slugify(sensor.name)}" == "sensor.sim_ev_everest_v2g_03_max_charge_power"
    assert sensor.native_value == 11000
    assert sensor.extra_state_attributes["source"] == "ev_charging_needs"
