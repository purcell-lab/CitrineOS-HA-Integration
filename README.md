# CitrineOS Home Assistant Energy Controller

> Staged compatibility work: see [CitrineOS 2.x read-only setup and upgrade
> precautions](docs/v2-readonly-compatibility.md). New and upgraded entries default
> to read-only; 2.x control remains disabled. This branch is not a deployment.

<p align="center">
  <img src="https://avatars.githubusercontent.com/u/132117031?s=200&v=4" alt="CitrineOS Logo" width="120" height="120" />
</p>

A powerful, user-friendly Home Assistant integration and local load controller for CitrineOS. It bridges Home Assistant's local sensing (grid meter, solar inverter, home battery) with CitrineOS's OCPP smart charging layer to provide automated solar surplus tracking, main fuse protection, priority EV power allocation, and CSIP-Aus dynamic operating envelopes.

- Domain: `citrine_ha`
- Path: `custom_components/citrine_ha`
- Supports UI Config Flow & full Options Flow
- Multi-mode real-time closed loop energy governor (1-2s cycle)
- Automated solar surplus & battery discharge allocation
- Dynamic multi-station priority weighting and minimum pilot current ($6\text{ A}$) protection
- Dual device architecture: **Citrine Energy Controller Hub** & **Per-Station Charger Controls**

---

## Key Features

### 1. Automated Local Load Controller
* **Real-time Closed Loop:** Continuously evaluates grid import/export, solar generation, and home battery power every 1–3 seconds.
* **Main Fuse Protection:** Enforces hard site import limits with instantaneous asymmetric curtailment if household loads spike.
* **Anti-Chatter & Dwell Protection:** Enforces a $6\text{ A}$ minimum pilot floor ($1.38\text{ kW}$ 1-ph / $4.14\text{ kW}$ 3-ph) and minimum 30-second dwell time to protect EV contactors against intermittent cloud cover or appliance cycling.
* **Deadband Slew Limiting:** Power changes smaller than the deadband (default $250\text{ W}$) are filtered out to prevent unnecessary network overhead and relay wear.

### 2. Intelligent Operating Modes
Selectable on the fly via `select.citrine_controller_mode`:
* **Solar Only:** Charges strictly from excess solar generation above a configurable start buffer.
* **Solar + Battery:** Uses excess solar and allows drawing from the home battery down to a minimum configurable SOC.
* **Grid Capped (Fast):** Maximizes EV charging speed while ensuring total site load stays safely below the main grid fuse rating.
* **Dynamic Envelope (DOE):** Follows dynamic utility operating envelopes (CSIP-Aus / IEEE 2030.5). Ingests real-time import/export caps from your CSIP gateway/client sensor (e.g. Edge zero, Wattwatchers, SwitchDin) and dynamically limits EV share while co-optimizing with other household loads.
* **Off / Emergency Safe:** Suspends all charging or drops power to safe minimums immediately.

### 3. Multi-Station Power & 3-Phase Balancing
* Dynamically balances available site power across multiple connected EVSEs.
* Supports **Per-Station Priority (1 to 5)**: higher-priority vehicles receive power first.
* Supports **Per-Station Phase Wiring (`select.<id>_phase_wiring`)**: configure chargers as `3-Phase (L1+L2+L3)`, `1-Phase (L1 / Phase A)`, `1-Phase (L2 / Phase B)`, or `1-Phase (L3 / Phase C)`.
* **Phase-Unbalance Protection**: Ingests Phase A, B, and C grid currents and enforces maximum phase-to-phase unbalance limits (e.g., AS/NZS 4777 20A limit) so high 1-phase EV loads do not trip main neutral or phase fuses.
* Supports **Per-Station Mode Overrides**: `auto` (follows site controller), `boost` (max power override), and `pause` (temporarily suspend).

---

## Entities Provided

### Site Hub: `Citrine Energy Controller Hub`
| Entity | Type | Description |
| :--- | :--- | :--- |
| `select.citrine_controller_mode` | Select | Active site mode (`Solar Only`, `Solar + Battery`, `Grid Capped`, `Off`, etc.) |
| `sensor.citrine_controller_state` | Sensor | State summary (`Tracking Solar`, `Curtailing`, `Normal`, `Safe Fallback`) |
| `sensor.citrine_allocated_ev_power` | Power Sensor | Real-time Watts allocated across all EVSEs |
| `sensor.citrine_site_headroom_power` | Power Sensor | Remaining Watts before reaching main fuse limit |
| `sensor.citrine_solar_surplus_power` | Power Sensor | Available excess solar generation in Watts |
| `sensor.citrine_phase_unbalance_a` | Current Sensor | Real-time phase-to-phase current unbalance (Amps) |
| `sensor.citrine_phase_a_current` | Current Sensor | Grid Phase A / L1 current (Amps) |
| `sensor.citrine_phase_b_current` | Current Sensor | Grid Phase B / L2 current (Amps) |
| `sensor.citrine_phase_c_current` | Current Sensor | Grid Phase C / L3 current (Amps) |
| `sensor.citrine_active_ev_count` | Sensor | Number of actively charging / connected EVs |
| `number.citrine_main_fuse_limit` | Number | Site main fuse import limit (Watts) |
| `number.citrine_solar_start_buffer` | Number | Surplus buffer required before starting charging (Watts) |
| `number.citrine_min_charge_current` | Number | Pilot current floor ($6\text{ A} - 16\text{ A}$) |
| `number.citrine_max_ramp_rate` | Number | Maximum power ramp rate ($\text{W/s}$) |
| `button.citrine_recompute_load_control` | Button | Trigger an immediate controller calculation cycle |
| `button.citrine_emergency_safe_mode` | Button | Immediate emergency power curtailment |

### Discovered Chargers: `Citrine Charger <ID>`
* **Status Sensors:** Online state, Session state, Connector count, Protocol, OCPP Heartbeat age.
* **Power limit sensors:** Max discharge power and Max charge power (W), see [Station power limits](#station-power-limits-062).
* **Control Selects:** Control Override (`auto`, `boost`, `pause`), Station Priority (`1 - Lowest` to `5 - Highest`), Phase Wiring (`3-Phase`, `L1`, `L2`, `L3`).
* **Buttons:** Start Charging, Stop Charging, Apply Profile, Clear Profile, Dynamic Session Start/Stop.

---

## Services

* `citrine_ha.set_controller_mode` — Change active site control mode.
* `citrine_ha.recompute_load_control` — Force an immediate control cycle.
* `citrine_ha.emergency_safe_mode` — Immediately curtail EV loads.
* `citrine_ha.start_charging` / `citrine_ha.stop_charging` — Remote transaction controls.
* `citrine_ha.set_station_limit` / `citrine_ha.set_group_limit` — Set static limits.
* `citrine_ha.set_charging_profile` / `citrine_ha.clear_charging_profile` — Advanced OCPP profile dispatch.

---

## Dynamic pricing (0.6.0, scoped lab profile)

Amber Electric prices in Home Assistant can be sent to the lab command station
as OCPP price information. Nothing here changes charging power. It needs
citrineos_server 0.7.0 with `price_commands_enabled: true`: the integration
reads the optional `price_commands` key of `GET /lab/control-policy`. When the key
is missing, disabled or malformed, price features are off and everything else
works as before.

| Service | Message | Stations |
| --- | --- | --- |
| `citrine_ha.lab_schedule` + `price_entity` | TxProfile with `absolutePriceSchedule` (one rule stack per Amber interval, current first, up to `max_rule_stacks` and the next 24 h) | command station, OCPP 2.1 |
| `citrine_ha.lab_cost_update` | `CostUpdated` with California Pricing `customData`: current price, the next interval as `nextPeriod`, and text such as "Amber 22.5 c/kWh now, 20.2 c/kWh from 08:00" | command station, OCPP 2.1 or 2.0.1, active transaction |
| `citrine_ha.lab_default_tariff` | `SetDefaultTariff`: Amber intervals as `energy.prices[]` with local start/end time of day and dates, priceKwh = Amber ÷ 1.1 (5 dp), GST 10 %; an optional feed-in sensor adds export entries (`minPower: -1, maxPower: -1000000`, priceKwh = −feed-in ÷ 1.1) | a station in `tariff_stations` (default the command station), OCPP 2.1 |

**OpenADR 3 VEN sensors (preferred).** A price sensor from the `openadr3_ven`
integration (for example `sensor.openadr3_vtn_..._price` and `..._export_price`,
`payload_type` PRICE / EXPORT_PRICE) is read through its `openadr3_ven.get_forecast`
service: the next 24 h at the VTN's native granularity (5- then 30-minute intervals).
Values are AUD/kWh including GST. The price text, price schedule description and
tariff ID then say "OpenADR" (`openadr-YYYYMMDD-HHMM`).

Amber sensors: the core `amberelectric` forecast sensors (`forecasts[]`) and
price sensors (one current interval), and `amber_express`
(`detailedForecast[]`, current price in the state). Past intervals and
intervals with no price are skipped; negative prices are kept. The core
forecast sensors list future intervals only, so `lab_cost_update` needs the
core *price* sensor or `amber_express`. Price schedule fees are RationalNumbers
with exponent −3 (0.1 c/kWh precision, AUD/kWh incl. GST). CostUpdated and
tariff prices outside the server's bounds (−5 to 20 AUD/kWh) are clamped; the
price text keeps the real Amber price. Tariff times use HA's time zone, and an
interval crossing midnight is split in two (validToDate is exclusive).

Each service returns `sent` (a summary), `command_state` and, for a tariff,
`station_status` (`Accepted`, `Rejected`, `CALLERROR NotImplemented`, ...)
with the verbatim `station_response`. Price messages go through the same
single-command path as the other lab commands: a timeout or 502 to 504 is
`outcome_unknown` and needs `lab_reconcile` (a tariff 502 means CitrineOS did
not send it, so it is `rejected`). Price features off, a non-2.1 station for a
price schedule or tariff, no active transaction for a cost update, or a
station outside `tariff_stations` fail with a validation error before
anything is sent.

```yaml
alias: CitrineOS lab - OpenADR prices to the station
mode: queued
triggers:
  - trigger: state
    entity_id: sensor.openadr3_vtn_example_price
actions:
  - action: citrine_ha.lab_cost_update
    continue_on_error: true   # no active transaction is a validation error
    data:
      entry_id: "<the isolated CitrineOS entry ID>"
      price_entity: sensor.openadr3_vtn_example_price
  - action: citrine_ha.lab_default_tariff
    data:
      entry_id: "<the isolated CitrineOS entry ID>"
      price_entity: sensor.openadr3_vtn_example_price
      export_price_entity: sensor.openadr3_vtn_example_export_price
      intervals: 48
    response_variable: tariff
  - action: system_log.write
    data:
      message: "SetDefaultTariff {{ tariff.sent.tariff_id }}: {{ tariff.station_status }}"
```

A schedule with the forecast attached as an absolutePriceSchedule:

```yaml
action: citrine_ha.lab_schedule
data:
  entry_id: "<the isolated CitrineOS entry ID>"
  immediate: true
  duration: 240
  price_entity: sensor.openadr3_vtn_example_price
  periods:
    - {startPeriod: 0, limit: 11000, dischargeLimit: -11000, setpoint: 6000, operationMode: CentralSetpoint}
response_variable: schedule   # schedule.sent.price_schedule: id, rule stacks, horizon
```

---

## DER control (0.7.0, scoped lab profile)

OCPP 2.1 DER control messages to the command station, through the CitrineOS
Isolated Lab App 0.10.0 DER routes (`der_commands_enabled`, which needs
`command_control_enabled`). The services are available only while
`/lab/control-policy` reports `der_commands` enabled with exactly the known
routes and the command station; when the key is missing, disabled or
malformed, DER controls are off and everything else works as before. Requests
go only to the routes the policy names, with the command token.

| Service | Message | Fields |
| --- | --- | --- |
| `citrine_ha.lab_set_der_control` | `SetDERControl` with exactly one control matching `control_type` | `control_type`, `control_id`, `is_default` (false), `priority` (1), the type's fields, optional `start_time` and `duration` (s) |
| `citrine_ha.lab_get_der_control` | `GetDERControl`; the reply carries the `ReportDERControl` reports for the request ID | optional `request_id` (random when empty), `is_default`, `control_type`, `control_id` |
| `citrine_ha.lab_clear_der_control` | `ClearDERControl` | `is_default` (required), optional `control_type`, `control_id` |
| `citrine_ha.lab_der_events` | `GET /lab/der-events`: the newest 20 DER reports and notifications | none |

| `control_type` | Fields (all required for the type, no others) |
| --- | --- |
| `LimitMaxDischarge` | `pct_max_discharge_power` (0 to 100 %) |
| `FixedPFInject`, `FixedPFAbsorb` | `displacement` (above 0, at most 1), `excitation` |
| `FixedVar` | `setpoint` (-100 to 100 %), `unit` (`PctMaxW`, `PctMaxVar`, `PctVarAvail`) |

Missing or extra fields for the type, values outside the policy's `limits`, a
control type the policy does not offer, or a command station that is offline
or not on OCPP 2.1 fail with a validation error before anything is sent. The
set, get and clear services return `sent`, `station_status` (`Accepted`,
`Rejected`, `NotSupported`, `NotFound` or `CALLERROR ...`) and the verbatim
`station_response`. Gateway errors come back with the gateway's error text; a
502 (not sent) or 504 (no station reply in 8 s) means the outcome may be
unknown: check with `lab_get_der_control` and do not retry automatically. DER
messages do not change the command state that Start, Stop and schedules use.

```yaml
action: citrine_ha.lab_set_der_control
data:
  entry_id: "<the isolated CitrineOS entry ID>"
  control_type: LimitMaxDischarge
  control_id: lab-limit-1
  pct_max_discharge_power: 50
  duration: 3600
response_variable: der   # der.station_status, der.station_response.supersededIds
```

---

## Station power limits (0.6.2)

Every discovered station gets two read-only sensors in W (device class power,
state class measurement): **Max discharge power** and **Max charge power**. A
bridge automation can use them to cap V2G discharge and charge and to publish
the DER nameplate, instead of a hard-coded 25 kW.

| Entry | Entity IDs |
| --- | --- |
| Scoped lab profile, simulator `SIM-EV-EVEREST-V2G-03` | `sensor.citrineos_everest_v2g_lab_sim_ev_everest_v2g_03_max_discharge_power`, `..._max_charge_power` |
| Scoped lab profile, other stations | `sensor.citrineos_lab_<station>_max_discharge_power` (a `SIM-EV-` station: `sensor.citrineos_everest_v2g_lab_<station>_...`) |
| Legacy and 2.x read-only profiles | `sensor.<station>_max_discharge_power`, `sensor.<station>_max_charge_power` |

The value is the smallest of the sources CitrineOS has, else the fallback:

1. `ev_charging_needs`: the EV's limits from the latest `ChargingNeeds` row
   (NotifyEVChargingNeeds) of the station's active transaction. Charge: DC
   `evMaxPower`, else `evMaxCurrent` × `evMaxVoltage`; AC `evMaxCurrent` ×
   `evMaxVoltage` × phases (one phase unless the parameters carry a phase
   count). Discharge (OCPP 2.1): `evMaxDischargePower` or V2X
   `maxDischargePower` (per-phase `_L2`/`_L3` added), else the discharge
   current × `evMaxVoltage`. `energyAmount`, SoC and departure time are ignored.
   A row without a transaction link counts when it is on the transaction's
   EVSE and newer than the transaction start.
2. `evse_rating` / `evse_max_limit_shared`: device model variable `Power` of
   component `EVSE` (the largest EVSE), else `ChargingStation`. The station's
   own `MaxSet` attribute value wins (`evse_rating`); only when the station has
   none is `VariableCharacteristics.maxLimit` used (`evse_max_limit_shared`).
   Discharge, in order: a discharge-specific `MaxSet` (`DischargePower`,
   `MaxDischargePower`, ...), the station's negative `Power` `MinSet` (OCPP
   2.1 signs discharge negative; `evse_rating`), the discharge-specific
   maxLimit, a negative `Power` minLimit, then the charge rating. Each step
   tries the EVSE component, then ChargingStation, before the next step: the
   EVerest V2G simulator reports `MaxSet` +25000 / `MinSet` −25000 on
   ChargingStation `Power` while its EVSE `Power` has only `Actual` (and a
   shared 88,320 W maxLimit), so it reads 25,000 W both ways.
3. `fallback`: the option *Fallback max charge/discharge power* (default
   25,000 W, 1,000 to 350,000 W). OCPP 1.6 stations always use it.

Attributes: `source`, `ev_limit_w`, `evse_rating_w`, `fallback_w` and
`transaction_id`. ChargingNeeds is read with the discovery poll (at most every
15 s, or at once when a transaction starts or ends); the device model every
60 s. All five queries are flat reads by id, without relationships.

The reads need select permission for the App's `ha_reader` on
`VariableAttributes`, `Components`, `Variables`, `VariableCharacteristics` and
`ChargingNeeds`. On an older CitrineOS App without them, or when a column is
missing, the source is dropped and the sensors keep a value (the next source,
at worst the fallback); they never go unavailable for it. The first refusal of
each table is logged as a warning, repeats at debug, and the table is retried
every 10 minutes. A timeout keeps the previous values until the next poll.

CitrineOS keeps one `VariableCharacteristics` row per variable name and
tenant, so a `Power` maxLimit (and minLimit) is the last value any station
reported, not this station's. At a mixed-charger event the per-station
`MaxSet` is therefore preferred, and `source: evse_max_limit_shared` marks a
value taken from the shared characteristic.

---

## Lovelace Dashboard & Automation Blueprints

### Turnkey Dashboard Card
A pre-configured Lovelace dashboard view is available in [assets/dashboard.yaml](assets/dashboard.yaml) featuring:
- **Real-Time Gauges**: Allocated EV Power, Available Solar Surplus, and Grid Fuse Headroom.
- **Operating Mode Switcher & Sliders**: Switch between `Solar Only`, `Solar + Battery`, and `Grid Capped` modes on the fly.
- **Action Buttons**: Instant Recompute and Emergency Safe Mode triggers.
- **Health Indicators**: Real-time MQTT stream status and CitrineOS API telemetry freshness.

### Automation Blueprints
- **Dynamic Tariff Mode Switcher**: [blueprints/citrine_tariff_automator.yaml](blueprints/citrine_tariff_automator.yaml) automatically toggles between `Grid Capped (Fast)` during cheap/negative spot price windows (e.g. Amber Electric, Nord Pool, Tibber) and `Solar Only` during peak price hours.



## Implemented capabilities

1. UI configuration (Config Entry)
- CitrineOS base URL
- Tenant ID
- Bearer token
- SSL verify toggle
- Request timeout
- Optional Hasura URL/token/query
- Discovery scan interval (1 to 3600 s; 1 to 60 s in the v2 options). The value chosen at setup is used until the options change it.
- Default idTag and EVSE id for quick start commands

2. Discovery and entities
- Polls Hasura with GraphQL and maps discovered stations into HA devices
- Sensor per station for online state + metadata
- Additional diagnostics sensors for protocol, connector count, session state, and OCPP heartbeat age
- Additional per-station EMS diagnostic sensor for charging-profile eligibility (`eligible` / `ineligible`)
- Integration-level EMS diagnostics sensors for intake totals, accepted count, and rejected count
- Integration-level EMS telemetry freshness diagnostic sensor (seconds since last successful telemetry fetch)
- Integration-level EMS telemetry health status sensor (`ok`, `stale`, `error`, `unknown`)
- Number per station for max limit (W)
- Start/Stop button entities per station
- Dedicated charging profile UI entities (numbers, selects, and action buttons) so users can apply/clear profiles from dashboards without manual service calls

3. Control services
- `citrine_ha.start_charging`
- `citrine_ha.stop_charging`
- `citrine_ha.set_station_limit`
- `citrine_ha.set_group_limit`
- `citrine_ha.set_charging_profile`
- `citrine_ha.clear_charging_profile`
- `citrine_ha.sync_discovery_now`
- `citrine_ha.sync_ems_telemetry_now`
- `citrine_ha.clear_ems_telemetry_error`

Bidirectional note:
- `set_charging_profile` supports negative `limit` values for stations that advertise bidirectional profile support in the integration capability cache (typically OCPP 2.x).
- Stations mapped as non-bidirectional (for example OCPP 1.6 by default) will reject negative profile limits with a clear error.
- Set `duration` to `0` for an indefinite profile (the integration omits duration in the OCPP schedule payload).
- Charging profile controls and service calls support explicit `profile_kind` selection. OCPP 2.0.1 exposes `Absolute` and `Relative`; OCPP 2.1 additionally exposes `Dynamic`.
- OCPP 2.1 stations additionally support `Dynamic` profile kind and can push both `limit` and `setpoint` values.
- `set_charging_profile` accepts optional `setpoint` and `profile_periods` for advanced multi-period dynamic schedules.
- OCPP 2.1 dynamic profiles support optional `operation_mode` and `discharge_limit` controls.
- Supported OCPP 2.1 operation modes in HA are: `ChargingOnly`, `ExternalLimits`, `CentralSetpoint`, `ExternalSetpoint`, `LocalFrequency`, `LocalLoadBalancing`, and `Idle`.
- Operation-mode controls are capability-gated: non-2.1 stations do not expose the `Profile Operation Mode` select in HA.
- Service input normalization matches UI capability rules: if `set_charging_profile` is called with an unsupported `operation_mode`, the integration automatically falls back to the station default mode.
- Changing `Profile Limit`, `Profile Setpoint`, or `Profile Discharge Limit` immediately pushes an updated profile.
- If a charger applies the profile but drives power in the opposite sign direction, set `Profile Sign Mode` to `invert_negative` for that station.
- `Profile Tx Mode` controls TxProfile behavior: use `safe_fallback` for charger compatibility fallbacks, or `strict_txprofile` to keep `TxProfile` unchanged.

4. Protocol-aware API calls
- OCPP 2.0.1 and OCPP 1.6 start/stop mappings
- OCPP 2.0.1 and OCPP 1.6 smart charging profile mappings for limits
- OCPP protocol normalization for mixed station metadata formats (for example `ocpp16`, `1.6`, `OCPP 2.0`)
- Endpoint fallback workarounds for deployments exposing `/ocpp/2.0/*` instead of `/ocpp/2.0.1/*`
- Retry workarounds for common profile issues (connector `0` rejection on OCPP 1.6, unit compatibility fallback)
- OCPP 2.0.1 `remoteStartId` can be sourced from transactions and incremented per station
- Per-station protocol and capability cache guides entity options and command payload selection
- EMS profile-control entities are now guarded by the same CSMS-facing support rules used by the EMS policy slice
- If a charger is ineligible for EMS profile control, the `EMS Profile Eligibility` diagnostic sensor exposes the exact support reason and the profile-control entities are unavailable in HA
- EMS telemetry consumption via Citrine Data API endpoint `GET /data/<ems-endpoint-prefix>/emsIntakeTelemetry`
- Options now support `ems_endpoint_prefix`, `ems_telemetry_site_id`, and `ems_telemetry_limit` for telemetry scoping
- Options now support `ems_telemetry_stale_secs` for telemetry stale detection threshold
- `sync_ems_telemetry_now` also supports optional per-call overrides for `site_id` and `telemetry_limit`
- EMS intake total attributes now include `fetched_at` and `last_success_at` timestamps

EMS profile eligibility rules:
- `eligible` means the integration believes the charger can participate in Citrine EMS charging-profile control.
- `ineligible` means one of the current conservative guards blocked the charger, for example:
- protocol is not `ocpp2.0.1` or `ocpp2.1`
- station advertised capabilities exist but do not include `ChargingProfileCapable`
- the integration could not derive a compatible EMS profile-control path

The `EMS Profile Eligibility` sensor attributes include:
- `ems_profile_support_reason`
- `advertised_capabilities`
- `normalized_protocol`
- `supports_dynamic_profiles`
- `supports_set_charging_profile`

Example Lovelace card:

```yaml
type: entities
title: Citrine EMS Profile Eligibility
entities:
  - entity: sensor.station_a_ems_profile_eligibility
    name: Station A EMS Profile Eligibility
  - entity: sensor.station_b_ems_profile_eligibility
    name: Station B EMS Profile Eligibility
```

If you want to expose the support reason inline, use an attribute row card or template entity. For example with Mushroom template cards:

```yaml
type: vertical-stack
cards:
  - type: entities
    title: Citrine EMS Profile Eligibility
    entities:
      - entity: sensor.station_a_ems_profile_eligibility
      - entity: sensor.station_b_ems_profile_eligibility
  - type: markdown
    content: >
      **Station A reason:**
      {{ state_attr('sensor.station_a_ems_profile_eligibility', 'ems_profile_support_reason') or 'Eligible' }}

      **Station B reason:**
      {{ state_attr('sensor.station_b_ems_profile_eligibility', 'ems_profile_support_reason') or 'Eligible' }}
```

Entity naming note:
- Replace `station_a` / `station_b` with your actual Home Assistant entity ids.
- The integration unique id suffix for this sensor is `_ems_profile_eligibility`.

## Hasura query for stations, connectors, and transactions

Use this as the discovery query value in the config flow/options:

```graphql
query ChargingStations($tenantId: Int!) {
  ChargingStations(where: {tenantId: {_eq: $tenantId}}) {
    id
    protocol
    isOnline
    chargePointVendor
    chargePointModel
    chargePointSerialNumber
    firmwareVersion
    tenantId
    locationId
    latestOcppMessageTimestamp
    updatedAt
    capabilities
  }

  Connectors(where: {tenantId: {_eq: $tenantId}}) {
    id
    stationId
    chargingStationId
    connectorId
    evseId
    status
    isOnline
    updatedAt
  }

  Transactions(where: {tenantId: {_eq: $tenantId}}, order_by: {updatedAt: desc}, limit: 500) {
    id
    stationId
    chargingStationId
    transactionId
    isActive
    active
    startedAt
    stoppedAt
    updatedAt
  }
}
```

Notes:
- The integration now merges station + connector + transaction rows.
- Include `capabilities` in the station query if your Hasura schema exposes it; this allows HA to distinguish explicit lack of charging-profile support from missing metadata.
- Stop button and stop service can use discovered `active/current/previous` transaction id automatically.
- Start command can auto-select EVSE from connector rows.
- For OCPP 2.0.1, `remoteStartId` is derived from station transactions as `max(transactionId) + 1` when numeric, then incremented after each start.
- If your table/column names differ, adjust the query in options; the merge logic accepts station references from `stationId`, `chargingStationId`, or `identifier`.

## Load into Home Assistant

1. Copy this folder into your HA config path:
- `<config>/custom_components/citrine_ha`

2. Restart Home Assistant.

3. Add integration:
- Settings -> Devices & Services -> Add Integration -> "CitrineOS HA"

4. Fill config fields.

5. Verify devices are created for discovered chargers.

## Install via HACS

This repository is now HACS-ready with `hacs.json` at the repository root.

1. In Home Assistant, open HACS.
2. Go to Integrations.
3. Open the menu and select Custom repositories.
4. Add your repository URL and choose category Integration.
5. Search for CitrineOS HA in HACS and install it.
6. Restart Home Assistant.
7. Add the integration from Settings -> Devices & Services.

## Next hardening tasks (recommended)

1. Add explicit capability mapping per station (supported units, profile purpose, connector model).
2. Persist outbox/idempotency keys for retries and restart-safe command execution.
3. Replace equal-split group allocator with weighted/floor-based policy.
4. Add reconciliation loop using `getChargingProfiles`/`getCompositeSchedule`.
5. Add test suite (unit + integration mocks).
