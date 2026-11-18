"""Chargetronic webhooks: signature, de-duplication, sessions and the EV charge limit."""
import asyncio
import base64
import json
from unittest.mock import MagicMock

import pytest

from custom_components.citrine_ha import chargetronic
from custom_components.citrine_ha.chargetronic import (
    APPLIED, DUPLICATE, STALE, ChargetronicSessions, departure, ev_max_power_w, parse_devices, sign,
    verify_signature)
from custom_components.citrine_ha.config_flow import _keep_chargetronic_secret
from custom_components.citrine_ha.const import CONF_CHARGETRONIC_SECRET
from custom_components.citrine_ha.power_limits import (
    CHARGE, DISCHARGE, SOURCE_CHARGETRONIC, SOURCE_EV, SOURCE_FALLBACK, PowerLimitReader)

SECRET = "whsec_" + base64.b64encode(b"chargetronic-test-key").decode()
NOW = 1_794_816_000
STATION = "SIM-EV-EVEREST-V2G-03"

# The examples from the Chargetronic partner reference (16 November 2026).
ISO2 = {"protocol": "iso15118-2", "e_amount_wh": 42000, "departure_time_s": 36000, "phases": 3,
        "ev_max_current_a": 32, "ev_min_current_a": 6, "ev_max_voltage_v": 400,
        "onboard_charger_w": 22080, "energy_need_wh": 42000, "energy_need_source": "reported"}
DIN = {"protocol": "din70121", "soc": 30, "capacity_wh": 60000, "energy_request_wh": 42000,
       "full_soc": 100, "bulk_soc": 80, "ev_max_power_w": 11000, "energy_need_wh": 46700,
       "energy_need_source": "reported"}


def event(n, kind, data=None, session="s_1", device="sp-dev-01"):
    return {"id": f"evt_{n}", "type": kind, "time": "2026-11-16T08:12:03Z", "device_id": device,
            "session_id": session, "data": data or {}}


def test_signature_matches_standard_webhooks():
    body = b'{"id":"evt_1"}'
    good = sign(SECRET, "evt_1", NOW, body)
    assert verify_signature(SECRET, "evt_1", str(NOW), f"v1,{good}", body, NOW)
    # Several signatures (secret rotation): one match is enough.
    assert verify_signature(SECRET, "evt_1", str(NOW), f"v1,bad= v1,{good}", body, NOW)
    # A plain (not whsec_) secret is used as given.
    assert verify_signature("plain", "evt_1", str(NOW), f"v1,{sign('plain', 'evt_1', NOW, body)}", body, NOW)


@pytest.mark.parametrize("change", ["body", "id", "version", "old", "missing", "badsecret"])
def test_signature_refusals(change):
    body = b'{"id":"evt_1"}'
    good = sign(SECRET, "evt_1", NOW, body)
    args = [SECRET, "evt_1", str(NOW), f"v1,{good}", body, NOW]
    if change == "body":
        args[4] = b'{"id":"evt_2"}'
    elif change == "id":
        args[1] = "evt_2"
    elif change == "version":
        args[3] = f"v2,{good}"
    elif change == "old":
        args[5] = NOW + 301
    elif change == "missing":
        args[3] = None
    else:
        args[0] = "whsec_!!not-base64"
    assert not verify_signature(*args)


def test_iso2_session_gives_need_departure_and_power():
    sessions = ChargetronicSessions({"sp-dev-01": STATION})
    assert sessions.apply(event(1, "session.plugged", {"plugged_at": "2026-11-16T08:12:00Z"})) == APPLIED
    assert sessions.charge_limits() == {}  # plugged, no request yet
    assert sessions.apply(event(2, "ev.energy_request", ISO2)) == APPLIED
    s = sessions.active("sp-dev-01")
    assert s["energy_need_wh"] == 42000 and s["soc"] is None  # ISO 15118-2 reports no SoC
    assert s["departure"] == "2026-11-16T18:12:03+00:00"  # 36000 s after the request
    # 3 x 32 A x 230 V = 22080 W, the same as the on-board charger.
    assert sessions.charge_limits() == {STATION: 22080}
    assert sessions.apply(event(3, "session.ended", {"energy_wh": 33300, "reason": "ev_stopped"})) == APPLIED
    assert sessions.active("sp-dev-01") is None and sessions.charge_limits() == {}
    assert sessions.sessions["sp-dev-01"]["energy_wh"] == 33300


def test_din_session_and_soc_final():
    sessions = ChargetronicSessions({"sp-dev-01": STATION})
    sessions.apply(event(1, "session.plugged"))
    sessions.apply(event(2, "ev.energy_request", DIN))
    s = sessions.active("sp-dev-01")
    assert (s["soc"], s["energy_need_wh"], s["ev_max_power_w"]) == (30, 46700, 11000)
    sessions.apply(event(3, "ev.soc_final", {"soc_start": 30, "soc_final": 80, "energy_wh": 33300}))
    assert s["soc_final"] == 80


def test_duplicates_late_retries_and_new_sessions():
    sessions = ChargetronicSessions({"sp-dev-01": STATION})
    sessions.apply(event(1, "session.plugged"))
    assert sessions.apply(event(1, "session.plugged")) == DUPLICATE
    sessions.apply(event(2, "session.ended"))
    # A late, retried request for the ended session must not plug the car back in.
    assert sessions.apply(event(3, "ev.energy_request", ISO2)) == STALE
    assert sessions.charge_limits() == {}
    # A request for a new session, its plug-in event lost, starts that session.
    assert sessions.apply(event(4, "ev.energy_request", ISO2, session="s_2")) == APPLIED
    assert sessions.charge_limits() == {STATION: 22080}
    assert sessions.received == 4


def test_unmapped_device_is_kept_but_limits_no_station():
    sessions = ChargetronicSessions({})
    sessions.apply(event(1, "ev.energy_request", ISO2, device="sp-dev-99"))
    assert sessions.active("sp-dev-99")["energy_need_wh"] == 42000
    assert sessions.charge_limits() == {}


@pytest.mark.parametrize("bad", [[], {"type": "session.plugged"}, {"id": "e", "type": "x", "device_id": ""}])
def test_malformed_envelope(bad):
    with pytest.raises(ValueError):
        ChargetronicSessions().apply(bad)


def test_helpers():
    assert parse_devices("sp-dev-01=A; sp-dev-02 = B\nbad, =C") == {"sp-dev-01": "A", "sp-dev-02": "B"}
    assert ev_max_power_w({"protocol": "iso15118-2", "ev_max_current_a": 16}) == 3680  # one phase assumed
    assert ev_max_power_w({"protocol": "iso15118-2", "ev_max_current_a": 32, "phases": 3,
                           "onboard_charger_w": 11000}) == 11000
    assert ev_max_power_w({"protocol": "iso15118-2"}) is None
    assert departure("2026-11-16T08:12:03Z", None) is None


def test_power_limits_prefer_charging_needs_then_chargetronic():
    station = {"id": STATION, "databaseId": 7, "protocol": "ocpp1.6", "activeTransactionId": None}
    reader = PowerLimitReader(None)
    data = {"stations": [dict(station)], "transactions": []}
    asyncio.run(reader.async_attach(data, 7400, {STATION: 22080}))
    charge = data["stations"][0]["power_limits"][CHARGE]
    assert (charge["value"], charge["source"]) == (22080, SOURCE_CHARGETRONIC)
    # Chargetronic gives no discharge limit.
    assert data["stations"][0]["power_limits"][DISCHARGE]["source"] == SOURCE_FALLBACK
    # Without an override the fallback stands.
    data = {"stations": [dict(station)], "transactions": []}
    asyncio.run(reader.async_attach(data, 7400))
    assert data["stations"][0]["power_limits"][CHARGE]["source"] == SOURCE_FALLBACK


def test_charging_needs_win_over_chargetronic():
    reader = PowerLimitReader(None)
    reader._needs = {42: {CHARGE: 11000, DISCHARGE: None}}
    reader._active_transactions = lambda stations, txs: {STATION: {"db": 42, "start": None, "evses": [11]}}
    data = {"stations": [{"id": STATION, "protocol": "ocpp2.1", "activeTransactionId": "uuid-1"}]}
    asyncio.run(reader.async_attach(data, 7400, {STATION: 22080}))
    charge = data["stations"][0]["power_limits"][CHARGE]
    assert (charge["value"], charge["source"]) == (11000, SOURCE_EV)


def test_blank_secret_keeps_the_saved_one():
    saved = {CONF_CHARGETRONIC_SECRET: SECRET}
    assert _keep_chargetronic_secret({CONF_CHARGETRONIC_SECRET: ""}, saved) == saved
    assert _keep_chargetronic_secret({}, {}) == {}
    assert _keep_chargetronic_secret({CONF_CHARGETRONIC_SECRET: "new"}, saved) == {CONF_CHARGETRONIC_SECRET: "new"}


class FakeRequest:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body, self.headers = body, headers

    async def read(self) -> bytes:
        return self._body


def test_webhook_handler(monkeypatch):
    """Signed events are applied once, fire an HA event and refresh; others are refused."""
    from homeassistant.components import webhook

    registered = {}
    monkeypatch.setattr(webhook, "async_register",
                        lambda hass, domain, name, hook_id, handler, **kw: registered.update(
                            handler=handler, hook_id=hook_id, **kw))
    sent = []
    monkeypatch.setattr("homeassistant.helpers.dispatcher.async_dispatcher_send",
                        lambda hass, sig, device: sent.append((sig, device)))
    monkeypatch.setattr(chargetronic.time, "time", lambda: NOW)
    hass, entry, refreshes = MagicMock(), MagicMock(entry_id="entry1"), []

    async def run():
        sessions = await chargetronic.async_setup(
            hass, entry, secret=SECRET, webhook_id="hook1", devices={"sp-dev-01": STATION},
            on_update=lambda: refreshes.append(1))
        handler = registered["handler"]

        async def post(evt, sig=None):
            body = json.dumps(evt).encode()
            headers = {"webhook-id": evt["id"], "webhook-timestamp": str(NOW),
                       "webhook-signature": sig or f"v1,{sign(SECRET, evt['id'], NOW, body)}"}
            return (await handler(hass, "hook1", FakeRequest(body, headers))).status

        assert await post(event(1, "ev.energy_request", ISO2)) == 200
        assert await post(event(1, "ev.energy_request", ISO2)) == 200  # retry: 2xx, applied once
        assert await post(event(2, "session.plugged"), sig="v1,forged") == 401
        bad = FakeRequest(b"not json", {"webhook-id": "evt_3", "webhook-timestamp": str(NOW),
                                        "webhook-signature": f"v1,{sign(SECRET, 'evt_3', NOW, b'not json')}"})
        assert (await handler(hass, "hook1", bad)).status == 400
        assert await post(event(4, "test.ping")) == 200  # unknown type: counted, connector refreshed
        return sessions

    sessions = asyncio.run(run())
    assert registered["hook_id"] == "hook1" and registered["local_only"] is False
    assert registered["allowed_methods"] == ["POST"]
    assert sessions.received == 2 and refreshes == [1]
    assert sent == [("citrine_ha_chargetronic_entry1", "sp-dev-01"), ("citrine_ha_chargetronic_entry1", None)]
    fired = hass.bus.async_fire.call_args.args
    assert fired[0] == "citrine_ha_chargetronic" and fired[1]["station_id"] == STATION


# Live events from Chargetronic (18 November 2026): max_power_w and an absolute
# departure_time, not the partner reference's field names.
LIVE_ISO2 = {"phases": 3, "protocol": "iso15118-2", "e_amount_wh": 30000, "max_power_w": 22080,
             "departure_time": "2026-11-19T04:59:59Z", "energy_need_wh": 30000, "ev_max_current_a": 32,
             "ev_min_current_a": 6, "energy_need_source": "ev"}


def test_live_iso2_event():
    sessions = ChargetronicSessions({"sp-dev-01": STATION})
    evt = {"id": "8929d788-0dfa-5d8e-b179-3131555d5c20", "type": "ev.energy_request",
           "time": "2026-11-18T05:56:41.959Z", "device_id": "sp-dev-01",
           "session_id": "01a1ed3a-87d4-7260-97f3-f70b6ac4f574", "data": LIVE_ISO2}
    assert sessions.apply(evt) == APPLIED
    s = sessions.active("sp-dev-01")
    assert s["departure"] == "2026-11-19T04:59:59+00:00"
    assert (s["energy_need_wh"], s["energy_need_source"], s["ev_max_power_w"]) == (30000, "ev", 22080)
    assert sessions.charge_limits() == {STATION: 22080}


def test_live_din_max_power_and_smallest_wins():
    din = {"protocol": "din70121", "soc": 30, "capacity_wh": 60000, "max_power_w": 11000,
           "energy_need_wh": 46666, "energy_need_source": "soc_capacity"}
    assert ev_max_power_w(din) == 11000
    assert ev_max_power_w({"protocol": "iso15118-2", "max_power_w": 22080, "ev_max_current_a": 16,
                           "phases": 1}) == 3680
    assert departure("2026-11-18T05:56:41Z", 3600, "not a time") == "2026-11-18T06:56:41+00:00"
