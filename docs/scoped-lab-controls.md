# Scoped CitrineOS / EVerest lab controls

Staged profile: `v2_scoped_lab_control`. Not enabled by installing the component.
Existing `v2_hasura_read_only` entries stay read-only. This is a simulator-only
manual control profile; the inherited solar controller and MQTT intent publisher
never start in this mode.

## Prerequisites

- CitrineOS isolated App with the matching scoped gateway policy.
- Explicit enrolment of the gateway's single command station on tenant 1
  (a `SIM-EV-EVEREST-*` simulator by default).
- Observed `ocpp2.1`, `ocpp2.0.1` or `ocpp1.6`, as listed in the gateway
  policy's `protocols`. Commands use the protocol the station actually
  connected with; there is no fallback between versions.
- Separate reader and command credentials, entered only in HA's protected configuration.
- Confirmed migration, simulator readiness and an agreed bounded test.
- BSV is excluded.

### Physical command station

A physical (third-party) charger can be the command station only when the
CitrineOS App sets `command_real_stations_enabled: true` and that station
connects through the plugfest listener. The gateway policy then reports
`real_devices_enabled: true`; the integration accepts a non-`SIM-` station ID
only in that case and labels the device "Physical charger (lab command
station)". On `ocpp2.0.1` and `ocpp1.6` stations schedules are charging-only:
`operationMode`, `setpoint` and `dischargeLimit` are rejected before anything
is sent, and the bidirectional capability is not offered.

On `ocpp1.6` the integration sends the 1.6 messages (citrineos_server 0.6.0):
RemoteStartTransaction on connector 1, RemoteStopTransaction and a
`csChargingProfiles` TxProfile with the integer transaction ID, and
ClearChargingProfile by `id`. Limits are rounded to whole watts and
`phaseToUse` is rejected (1.6 has none). Chargers that accept only amp
schedules reject the W profile, and the command state shows that.

Select the profile in Configure/options, provide its command credential, and
explicitly turn read-only off. Connection validation checks readiness, protected
read access, Hasura discovery and the exact command policy before accepting it.
The protected Hasura gateway always uses the reader role, never client-supplied
admin privileges.

## Controls

Six buttons: Start charging, Stop transaction, Apply requested power,
Request zero power, Clear test profile after closure, and Reconcile command
state (see below). One number stages
requested signed power from -11,000 W to +11,000 W; changing that number does
not send a command. Apply sends a finite 180-second absolute TxProfile starting
five seconds in the future, bound to the freshly observed transaction.

Seven sensors show measured power, imported/exported meter energy, transaction,
observed protocol, connector status and command dispatch state. They do not
include the legacy site/EMS placeholder metrics. Power is unknown after 45
seconds without a fresh meter frame; stopped does not fabricate a zero sample.
Energy values are raw meter-register readings, not session deltas, and retain
their source timestamp and transaction ID.

Every discovered station (the command station and any read-only station) also
gets Max discharge power and Max charge power sensors (0.6.2): the smaller of
the EV's ChargingNeeds limit and the EVSE rating, else the fallback option. They
stay available while the station is offline; see the README's "Station power
limits".

`citrine_ha.lab_schedule` submits a multi-period schedule:

```yaml
action: citrine_ha.lab_schedule
data:
  entry_id: "<the isolated CitrineOS entry ID>"
  duration: 240
  periods:
    - {startPeriod: 0, limit: 11000, dischargeLimit: -11000, setpoint: 6000, operationMode: CentralSetpoint}
    - {startPeriod: 60, limit: 11000, dischargeLimit: -11000, setpoint: -5000, operationMode: CentralSetpoint}
    - {startPeriod: 120, limit: 11000, dischargeLimit: -11000, setpoint: 3000, operationMode: CentralSetpoint}
    - {startPeriod: 180, limit: 11000, dischargeLimit: -11000, setpoint: 0, operationMode: CentralSetpoint}
```

Only W, one EVSE, ten periods and 600 seconds maximum are accepted. The exact
transaction ID is looked up, not selected by the user. There is no automatic
change of units, profile purpose, protocol or transaction-ID representation.
Use these scoped controls, not inherited legacy convenience services.

Price information (OCPP 2.1 absolutePriceSchedule, CostUpdated,
SetDefaultTariff) from Amber Electric sensors: see "Dynamic pricing" in the
README. It needs the App's `price_commands` policy and never changes power.

## Honest outcomes and stop behaviour

A successful HTTP/native API result is labelled
`dispatched_awaiting_station_evidence`, not Accepted or Charging. Actual station
CALLRESULT and metered behaviour are separate acceptance evidence. A transport
failure, a timeout or a gateway error (HTTP 502 to 504) is outcome unknown,
with no automatic resend. A subsequent non-stop command is blocked until the
unresolved outcome is investigated; resetting the integration is not proof the
original command failed.

A definite error reply is `rejected` and does not block the next command: any
4xx (the lab gateway and CitrineOS refuse these before dispatch), and HTTP 500
for a schedule. A schedule always uses profile 9001 at the same stack level, so
the next schedule replaces whatever a failed one left. HTTP 500 for start or
stop is still outcome unknown. In Cactus LOA-11 a single CitrineOS 500 during
a 1 s ramp otherwise blocked every later schedule for almost seven minutes.

### Evidence window

A dispatched command waits at most 90 seconds (`EVIDENCE_TIMEOUT_S`) for
station evidence. A Start with no observed transaction in that window, and a
Stop with no observed closure, end as `no_station_evidence` with a logged
warning and a `detail` reason; a new Start is then allowed. Inside the window
a second Start is still refused ("Start already dispatched; wait up to N s").
Profile and clear outcomes are never observed by the integration, so they
also end as `no_station_evidence` after the window instead of showing
`dispatched_awaiting_station_evidence` forever. A transaction that appears
after the window is still recorded as `transaction_observed`.

### Reconcile without a reload

`outcome_unknown` and `dispatch_not_confirmed` are not timed out: they block
every command except Stop until reconciled. Press **Reconcile command state**
or call:

```yaml
action: citrine_ha.lab_reconcile
data:
  entry_id: "<the isolated CitrineOS entry ID>"
response_variable: reconcile   # optional: the resolved command state
```

Reconcile refreshes discovery, checks `/health/ready` and the command policy,
and reads the command station's active transaction from the CSMS. It never
clears a command sent less than the evidence window ago, because it may still
land; the call fails with the remaining wait instead. After the window:

| Unresolved command | CSMS shows | Result |
| --- | --- | --- |
| Start | an active transaction | `transaction_observed` |
| Start | no active transaction | `reconciled`, resolution `no_transaction`; Start allowed |
| Stop | the same transaction still active | `reconciled`, resolution `transaction_still_active` |
| Stop | no active transaction | `transaction_closure_observed` if the meter record shows closure, else `reconciled` / `no_active_transaction` |
| Schedule or clear | anything | `reconciled`, resolution `outcome_not_observable` |

The CSMS does not expose a profile or clear outcome to this integration, so
that resolution says so rather than claiming success. The next schedule uses
profile 9001 at the same stack level and replaces whatever was applied. A
command state with nothing unresolved is returned unchanged. The previous
state, resolution, active transaction and time are kept in the command
dispatch status attributes and logged once.

### CSMS unavailable

A `/health/ready` that fails with HTTP 5xx, a timeout or a transport error, or
does not report `pass`, marks the CSMS unavailable (one warning per outage).
The same check runs before every command is sent. While unavailable, every lab
command and reconcile fails at once with "CSMS unavailable (...)": nothing is
sent and the command state does not become `outcome_unknown`. The next passing
poll re-enables commands (logged once). The command dispatch status sensor
exposes `csms_available`, `csms_unavailable_reason` and
`csms_unavailable_since`, so an automation such as the ODE bridge can skip a
cycle with a condition instead of failing.

Stop uses the exact active transaction. Clear uses only test profile 9001 and
is blocked while a transaction remains active. Do not clear to stop discharge:
removing a profile can restore a nonzero default import limit.

The finite schedule is not a safety watchdog. These staged controls do not
claim that expiry or communication loss produces zero power. The integration
trial must stop/unplug on failure and prove closure; deployment still requires
the separately agreed local-fallback gate. No autonomous site-control policy
or physical-device authority is introduced here.
