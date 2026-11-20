"""EV SoC from CitrineOS MeterValues: active transaction only, never fails discovery."""
import pytest
from graphql import parse

from custom_components.citrine_ha.ev_soc import SOC_QUERY, EvSocReader, soc_value


def payload(tx=True):
    return {
        "stations": [{"id": "IMATRA-harness-01", "databaseId": 178, "activeTransactionId": "uuid-1" if tx else None},
                     {"id": "IDLE-01", "databaseId": 9, "activeTransactionId": None}],
        "transactions": [{"id": 42, "stationId": "IMATRA-harness-01", "transactionId": "uuid-1",
                          "isActive": True}] if tx else [],
    }


class Hasura:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or [], fail, []

    async def query(self, query, variables=None):
        self.calls.append(variables)
        if self.fail:
            raise RuntimeError("permission denied")
        return {"data": {"MeterValues": self.rows}}


SOC = {"value": 47, "measurand": "SoC", "unitOfMeasure": {"unit": "Percent"}, "location": "EV"}


def test_soc_value():
    assert soc_value([SOC]) == 47
    assert soc_value([{"value": 1, "measurand": "Power.Active.Import"}, {**SOC, "value": 101}]) is None
    assert soc_value([{**SOC, "location": "Outlet", "value": 5}, SOC]) == 47
    assert soc_value([{**SOC, "unitOfMeasure": {"unit": "Wh"}}, {**SOC, "value": True}]) is None
    parse(SOC_QUERY)


@pytest.mark.asyncio
async def test_latest_soc_of_the_active_transaction():
    hasura = Hasura([{"transactionDatabaseId": 42, "timestamp": "t2", "sampledValue": [SOC]},
                     {"transactionDatabaseId": 42, "timestamp": "t1", "sampledValue": [{**SOC, "value": 10}]}])
    data = payload()
    await EvSocReader(hasura).async_attach(data, 1)
    assert hasura.calls == [{"transactions": [42], "tenant": 1}]
    assert data["stations"][0]["ev_soc"] == {"value": 47, "timestamp": "t2", "transaction_id": "uuid-1"}
    assert data["stations"][1]["ev_soc"]["value"] is None


@pytest.mark.asyncio
async def test_no_query_without_a_session_and_errors_do_not_fail():
    hasura = Hasura([{"transactionDatabaseId": 42, "timestamp": "t", "sampledValue": [SOC]}])
    data = payload(tx=False)
    await EvSocReader(hasura).async_attach(data, 1)
    assert hasura.calls == [] and data["stations"][0]["ev_soc"]["value"] is None
    data, now = payload(), [0.0]
    reader = EvSocReader(Hasura(fail=True), clock=lambda: now[0])
    await reader.async_attach(data, 1)
    await reader.async_attach(data, 1)
    assert data["stations"][0]["ev_soc"]["value"] is None and len(reader._hasura.calls) == 1
