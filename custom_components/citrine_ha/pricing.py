"""Amber Electric prices as OCPP 2.1 price messages. Pure functions, no I/O.

Three messages, all information only (they never change the charging power):
- B: absolutePriceSchedule inside the lab TxProfile's chargingSchedule[0].
- C: CostUpdated with California Pricing customData (current and next price).
- D: SetDefaultTariff (OCPP 2.1 Block I) built from the forecast.

Amber prices are AUD/kWh including GST. B and C carry them as is; D carries
them excluding GST with a GST 10 % tax rate. Bounds mirror the lab gateway's
price_commands policy, which refuses anything outside them.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
import math
import re
from typing import Any, NamedTuple

GST = Decimal("1.1")
CURRENCY = "AUD"
PRICE_ALGORITHM = "urn:iso:std:iso:15118:-20:PriceAlgorithm:1-Power"
COST_VENDOR = "org.openchargealliance.costmsg"
# CostUpdated kWhPrice and totalCost bounds (contract: AUD/kWh incl. GST).
COST_PRICE_MIN, COST_PRICE_MAX = -5, 20
TOTAL_COST_MAX = 10000
INT16 = (-32768, 32767)
# Defaults when the policy omits a limit; the policy's own values always win.
SCHEDULE_LIMITS = {"max_rule_stacks": 288, "max_level_entries": 100, "max_horizon_s": 172800}
TARIFF_LIMITS = {"max_prices": 96, "min_price_kwh": -5, "max_price_kwh": 20}
COST_PATHS = {"ocpp2.1": "/ocpp/2.1/transactions/costUpdated",
              "ocpp2.0.1": "/ocpp/2.0.1/transactions/costUpdated"}
TARIFF_PATH = "/ocpp/2.1/transactions/setDefaultTariff"
# Export (discharge) entries: the contract's power condition, copied as given.
EXPORT_POWER = {"minPower": -1, "maxPower": -1000000}


class PriceInterval(NamedTuple):
    start: datetime  # UTC, floored to the minute (Amber starts at :00:01)
    end: datetime    # UTC
    price: float     # AUD/kWh incl. GST; negative is valid


def _finite(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(UTC)


def iso(stamp):
    """Whole-second ISO 8601 with offset (the gateway allows at most 3 fraction digits)."""
    return stamp.isoformat(timespec="seconds")


def _interval(item, price=None):
    """One forecast item as a PriceInterval, or None when unusable."""
    if not isinstance(item, dict):
        return None
    price = item.get("per_kwh") if price is None else price
    start = _utc(item.get("start_time"))
    if not _finite(price) or start is None:
        return None
    start = start.replace(second=0, microsecond=0)
    end = _utc(item.get("end_time"))
    if end is None and _finite(item.get("duration")) and item["duration"] > 0:
        end = start + timedelta(minutes=item["duration"])
    if end is None or end <= start:
        return None
    return PriceInterval(start, end, float(price))


def _state_price(state):
    """The entity state as AUD/kWh (amber_express reports the current price there)."""
    try:
        value = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    unit = str((getattr(state, "attributes", None) or {}).get("unit_of_measurement") or "")
    return value / 100 if re.match(r"\s*(c|¢)", unit) else value


def amber_intervals(state, now=None):
    """Amber price intervals from a HA State, current interval first.

    Supports the core amberelectric forecast sensor (attributes.forecasts[]),
    its price sensor (one interval in its own attributes) and amber_express
    (attributes.detailedForecast[], current price in the state). Past
    intervals, items without a finite price and overlaps are skipped; prices
    may be negative. Intervals after a gap are kept: callers that need a
    contiguous run (the price schedule) stop at the gap.
    """
    attrs = getattr(state, "attributes", None)
    if not isinstance(attrs, dict):
        return []
    now = now or datetime.now(UTC)
    express = isinstance(attrs.get("detailedForecast"), list)
    items = attrs.get("detailedForecast") if express else attrs.get("forecasts")
    found = [x for x in map(_interval, items if isinstance(items, list) else []) if x]
    own = _interval(attrs)
    if own:
        found.append(own)
    found.sort(key=lambda x: x.start)
    intervals = []
    for x in found:
        if x.end <= now or (intervals and x.start < intervals[-1].end):
            continue
        intervals.append(x)
    if express and intervals and intervals[0].start > now:
        # amber_express may list only future intervals; the state is the current price.
        first = intervals[0]
        start = first.start - (first.end - first.start)
        price = _state_price(state)
        if price is not None and start <= now:
            intervals.insert(0, PriceInterval(start, first.start, price))
    return intervals


def current_and_next(intervals, now=None):
    """The interval containing now and the one that follows it without a gap."""
    now = now or datetime.now(UTC)
    if not intervals or not intervals[0].start <= now < intervals[0].end:
        return None, None
    nxt = intervals[1] if len(intervals) > 1 and intervals[1].start <= intervals[0].end else None
    return intervals[0], nxt


def _round(value, places):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def clamp(value, low, high):
    return min(max(value, low), high)


def rational(value):
    """An ISO 15118-20 RationalNumber: int16 value, exponent -3..3.

    Exponent -3 (0.1 c/kWh for AUD/kWh) is used when the value fits int16,
    i.e. within ±32.767; larger magnitudes use the next exponent up. The
    value is clamped only beyond 32767e3.
    """
    for exponent in range(-3, 4):
        scaled = int(Decimal(str(value)).scaleb(-exponent).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if INT16[0] <= scaled <= INT16[1]:
            return {"value": scaled, "exponent": exponent}
    return {"value": clamp(scaled, *INT16), "exponent": 3}


def contiguous(intervals):
    """The leading run of intervals with no gap (Amber's 1 s start offset is floored away)."""
    run = intervals[:1]
    for x in intervals[1:]:
        if x.start != run[-1].end:
            break
        run.append(x)
    return run


# B: absolutePriceSchedule

def build_price_schedule(intervals, *, currency=CURRENCY, max_rule_stacks=288, max_horizon_s=172800):
    """An absolutePriceSchedule from the contiguous intervals, current one first.

    One rule stack per Amber interval (5-minute intervals are kept as is), up
    to max_rule_stacks and max_horizon_s. energyFee is AUD/kWh incl. GST at
    0.1 c/kWh precision.
    """
    run = contiguous(intervals)[:max_rule_stacks]
    if not run:
        raise ValueError("No price interval")
    stacks, horizon = [], 0
    for x in run:
        duration = min(int((x.end - x.start).total_seconds()), max_horizon_s - horizon)
        if duration <= 0:
            break
        horizon += duration
        stacks.append({"duration": duration, "priceRule": [{
            "energyFee": rational(x.price), "powerRangeStart": {"value": 0, "exponent": 0}}]})
    anchor = run[0].start
    return {
        "timeAnchor": iso(anchor),
        # Changes with each interval; the station shows the newest schedule.
        "priceScheduleID": int(anchor.timestamp() // 60) % 2147483647,
        "currency": currency, "language": "en", "priceAlgorithm": PRICE_ALGORITHM,
        "priceRuleStacks": stacks,
        "priceScheduleDescription": f"Amber import forecast, {currency}/kWh incl. GST",
    }


def valid_rational(r):
    return (isinstance(r, dict) and set(r) == {"value", "exponent"}
            and type(r["value"]) is int and INT16[0] <= r["value"] <= INT16[1]
            and type(r["exponent"]) is int and -3 <= r["exponent"] <= 3)


def valid_price_schedule(s, *, currency=CURRENCY, max_rule_stacks=288, max_horizon_s=172800):
    """The gateway's absolutePriceSchedule bounds."""
    allowed = {"timeAnchor", "priceScheduleID", "currency", "language", "priceAlgorithm",
               "priceRuleStacks", "priceScheduleDescription", "minimumCost", "maximumCost"}
    if (not isinstance(s, dict) or set(s) - allowed or _utc(s.get("timeAnchor")) is None
            or type(s.get("priceScheduleID")) is not int or s["priceScheduleID"] < 0
            or s.get("currency") != currency or s.get("language") != "en"
            or s.get("priceAlgorithm") != PRICE_ALGORITHM
            or not isinstance(s.get("priceScheduleDescription", ""), str)
            or len(s.get("priceScheduleDescription", "")) > 160
            or any(k in s and not valid_rational(s[k]) for k in ("minimumCost", "maximumCost"))
            or not isinstance(s.get("priceRuleStacks"), list)
            or not 1 <= len(s["priceRuleStacks"]) <= max_rule_stacks):
        return False
    total = 0
    for stack in s["priceRuleStacks"]:
        rules = stack.get("priceRule") if isinstance(stack, dict) else None
        if (set(stack) != {"duration", "priceRule"} or type(stack["duration"]) is not int
                or stack["duration"] <= 0 or not isinstance(rules, list) or not 1 <= len(rules) <= 8
                or not all(isinstance(r, dict) and set(r) == {"energyFee", "powerRangeStart"}
                           and valid_rational(r["energyFee"]) and valid_rational(r["powerRangeStart"])
                           for r in rules)):
            return False
        total += stack["duration"]
    return total <= max_horizon_s


def schedule_summary(s):
    first = s["priceRuleStacks"][0]["priceRule"][0]["energyFee"]
    return {"price_schedule_id": s["priceScheduleID"], "time_anchor": s["timeAnchor"],
            "rule_stacks": len(s["priceRuleStacks"]),
            "horizon_s": sum(x["duration"] for x in s["priceRuleStacks"]),
            "first_price_kwh": float(Decimal(first["value"]).scaleb(first["exponent"])),
            "currency": s["currency"]}


# C: CostUpdated customData (California Pricing)

def cents(price):
    return f"{_round(price * 100, 1):.1f} c/kWh"


def price_text(current, nxt, tz):
    """E.g. "Amber 22.5 c/kWh now, 20.2 c/kWh from 08:00" (local time)."""
    text = f"Amber {cents(current.price)} now"
    if nxt is not None:
        text += f", {cents(nxt.price)} from {nxt.start.astimezone(tz):%H:%M}"
    return text[:255]


def build_cost_updated(transaction_id, current, nxt, tz, *, total_cost=0):
    """The CostUpdated body: current price, the next interval and a price text.

    kWhPrice is AUD/kWh incl. GST at 5 dp, clamped to -5..20; the text keeps
    the actual Amber price.
    """
    if not _finite(total_cost) or not 0 <= total_cost <= TOTAL_COST_MAX:
        raise ValueError(f"total_cost must be 0 to {TOTAL_COST_MAX}")

    def kwh(price):
        return {"kWhPrice": _round(clamp(price, COST_PRICE_MIN, COST_PRICE_MAX), 5)}

    custom = {"vendorId": COST_VENDOR, "chargingPrice": kwh(current.price)}
    if nxt is not None:
        custom["nextPeriod"] = {"atTime": iso(current.end), "chargingPrice": kwh(nxt.price)}
    custom["priceText"] = price_text(current, nxt, tz)
    return {"totalCost": total_cost, "transactionId": transaction_id, "customData": custom}


def valid_cost_updated(body):
    custom = body.get("customData") if isinstance(body, dict) else None

    def price(p, keys):
        return (isinstance(p, dict) and "kWhPrice" in p and not set(p) - keys
                and all(_finite(v) and COST_PRICE_MIN <= v <= COST_PRICE_MAX for v in p.values()))

    nxt = custom.get("nextPeriod") if isinstance(custom, dict) else None
    return (isinstance(custom, dict) and set(body) == {"totalCost", "transactionId", "customData"}
            and _finite(body["totalCost"]) and 0 <= body["totalCost"] <= TOTAL_COST_MAX
            and isinstance(body["transactionId"], str)
            and not set(custom) - {"vendorId", "chargingPrice", "nextPeriod", "priceText"}
            and custom.get("vendorId") == COST_VENDOR
            and price(custom.get("chargingPrice"), {"kWhPrice", "hourPrice", "flatFee"})
            and (nxt is None or (isinstance(nxt, dict) and set(nxt) == {"atTime", "chargingPrice"}
                                 and _utc(nxt["atTime"]) is not None
                                 and price(nxt["chargingPrice"], {"kWhPrice"})))
            and isinstance(custom.get("priceText", ""), str) and len(custom.get("priceText", "")) <= 255)


# D: SetDefaultTariff

def local_parts(interval, tz):
    """Split an interval at local midnight into (date, start "HH:MM", end "HH:MM").

    End is exclusive; a part ending at midnight ends "00:00". Dates follow
    OCPP/OCPI: validFromDate inclusive, validToDate exclusive (the next day).
    """
    start, end = interval.start.astimezone(tz), interval.end.astimezone(tz)
    parts = []
    while start < end:
        midnight = datetime.combine(start.date() + timedelta(days=1), time(), tz)
        stop = min(end, midnight)
        parts.append((start.date(), f"{start:%H:%M}", f"{stop:%H:%M}"))
        start = stop
    # A sub-minute remainder would read as a whole day ("00:00"-"00:00").
    return [p for p in parts if p[1] != p[2]]


def _excl_gst(price, limits):
    value = float((Decimal(str(price)) / GST).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))
    return clamp(value, limits["min_price_kwh"], limits["max_price_kwh"])


def _entries(intervals, tz, limits, *, export=False):
    entries = []
    for x in intervals:
        for day, start, end in local_parts(x, tz):
            conditions = {"startTimeOfDay": start, "endTimeOfDay": end,
                          "validFromDate": day.isoformat(),
                          "validToDate": (day + timedelta(days=1)).isoformat()}
            if export:
                conditions.update(EXPORT_POWER)
            # Export: the station pays the driver the feed-in price.
            entries.append({"priceKwh": _excl_gst(-x.price if export else x.price, limits),
                            "conditions": conditions})
    return entries


def build_tariff(intervals, tz, *, export_intervals=(), count=48, currency=CURRENCY, limits=None):
    """A TariffType: Amber import (and optional feed-in) intervals as energy prices.

    priceKwh is AUD/kWh excl. GST (Amber ÷ 1.1, 5 dp) with taxRates GST 10.
    Time-of-day and date conditions are in tz (HA's time zone). At most count
    intervals of each forecast; both are shortened together until the entries
    (midnight splits included) fit max_prices.
    """
    limits = {**TARIFF_LIMITS, **(limits or {})}
    if not intervals:
        raise ValueError("No price interval")
    count = max(1, count)
    while True:
        imports = _entries(intervals[:count], tz, limits)
        exports = _entries(list(export_intervals)[:count], tz, limits, export=True)
        if len(imports) + len(exports) <= limits["max_prices"] or count == 1:
            break
        count -= 1
    prices = (imports + exports)[:limits["max_prices"]]
    first = intervals[0].start
    local = first.astimezone(tz)
    used = intervals[:count]
    description = (f"Amber forecast, {len(used)} intervals from {local:%Y-%m-%d %H:%M}"
                   f"{' with feed-in' if exports else ''} ({currency}/kWh excl. GST, GST 10 %)")
    return {"tariffId": f"amber-{local:%Y%m%d-%H%M}", "currency": currency,
            "description": [{"format": "UTF8", "language": "en", "content": description[:255]}],
            "validFrom": iso(first),
            "energy": {"prices": prices, "taxRates": [{"type": "GST", "tax": 10}]}}


def _date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_HHMM = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def _valid_conditions(c):
    allowed = {"startTimeOfDay", "endTimeOfDay", "dayOfWeek", "validFromDate", "validToDate",
               "minPower", "maxPower"}
    if not isinstance(c, dict) or set(c) - allowed:
        return False
    for k in ("startTimeOfDay", "endTimeOfDay"):
        if k in c and not (isinstance(c[k], str) and _HHMM.fullmatch(c[k])):
            return False
    for k in ("validFromDate", "validToDate"):
        if k in c and not (isinstance(c[k], str) and _DATE.fullmatch(c[k]) and _date(c[k])):
            return False
    if "dayOfWeek" in c and not (isinstance(c["dayOfWeek"], list) and set(c["dayOfWeek"]) <= DAYS):
        return False
    return all(_finite(c[k]) and -1000000 <= c[k] <= 1000000 for k in ("minPower", "maxPower") if k in c)


def valid_tariff(t, *, currency=CURRENCY, limits=None):
    """The gateway's TariffType bounds (energy prices only)."""
    limits = {**TARIFF_LIMITS, **(limits or {})}
    energy = t.get("energy") if isinstance(t, dict) else None
    prices = energy.get("prices") if isinstance(energy, dict) else None
    description = t.get("description", []) if isinstance(t, dict) else None
    return (isinstance(prices, list)
            and not set(t) - {"tariffId", "currency", "description", "validFrom", "energy"}
            and isinstance(t.get("tariffId"), str) and re.fullmatch(r"[ -~]{1,60}", t["tariffId"]) is not None
            and t["tariffId"] != "HA-LAB-ZERO"
            and t.get("currency") == currency
            and ("validFrom" not in t or _utc(t["validFrom"]) is not None)
            and isinstance(description, list) and len(description) <= 3
            and all(isinstance(d, dict) and d.get("format") == "UTF8" and d.get("language") == "en"
                    and set(d) == {"format", "language", "content"}
                    and isinstance(d["content"], str) and len(d["content"]) <= 255 for d in description)
            and not set(energy) - {"prices", "taxRates"}
            and 1 <= len(prices) <= limits["max_prices"]
            and all(isinstance(p, dict) and not set(p) - {"priceKwh", "conditions"}
                    and _finite(p.get("priceKwh"))
                    and limits["min_price_kwh"] <= p["priceKwh"] <= limits["max_price_kwh"]
                    and ("conditions" not in p or _valid_conditions(p["conditions"])) for p in prices)
            and energy.get("taxRates", []) in ([], [{"type": "GST", "tax": 10}]))


def tariff_summary(body):
    prices = body["tariff"]["energy"]["prices"]
    exports = sum(1 for p in prices if "minPower" in p.get("conditions", {}))
    return {"evse_id": body["evseId"], "tariff_id": body["tariff"]["tariffId"],
            "valid_from": body["tariff"].get("validFrom"),
            "import_prices": len(prices) - exports, "export_prices": exports,
            "first_price_kwh_excl_gst": prices[0]["priceKwh"]}


def tariff_status(response):
    """The station's SetDefaultTariff status from the gateway's verbatim reply, if any.

    A station CALLERROR comes back (HTTP 200) as CitrineOS's OcppError object
    with _errorCode, reported as e.g. "CALLERROR NotImplemented".
    """
    item = response[0] if isinstance(response, list) and len(response) == 1 else response
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("_errorCode"), str):
        return f"CALLERROR {item['_errorCode']}"
    for source in (item, item.get("payload")):
        if isinstance(source, dict):
            for key in ("status", "errorCode"):
                if isinstance(source.get(key), str):
                    return source[key]
    return None
