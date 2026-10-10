# CitrineOS 2.x read-only compatibility

This staged client change supports an explicit, read-only discovery path for the
CitrineOS server v2.0.1 schema. It does not enable or deploy a server and does not
establish live OCPP command compatibility. It complements the
[disconnected packaging PR](https://github.com/purcell-lab/ha-addons/pull/12).

## Profiles

This staged branch declares Home Assistant **2026.10.0** as its minimum and
tests against that published runtime. Older HA releases are not claimed
compatible; coordinator setup passes its config entry explicitly.

| Profile | Connection validation | Discovery | Control |
| --- | --- | --- | --- |
| `legacy` | Existing `/data/ocpprouter/systemConfig` | Configured Hasura, otherwise legacy REST | Read-only by default; explicit option needed to enable existing control |
| `v2_hasura_read_only` | `/health/ready`, then protected tenant-scoped `GET /commands/tariff` | Fixed, tenant-filtered Hasura query | Always disabled in this PR |

The profiles are selected during setup; there is no automatic protocol downgrade
or silent switch of discovery backend. Existing entries without a `read_only`
setting become read-only on upgrade. This is a deliberate safety change: users
of existing legacy control must review Options and explicitly disable read-only
after validating their control policy. There is no change until this PR is
installed and the entry reloaded. Controller **Off** still means zero allocation,
not read-only.

Read-only setup skips the energy-controller configuration step, loads only sensor
entities, starts no controller timer and blocks mutating services and client HTTP
methods. A second guard covers manual controller recompute and direct limit
application. Read-only Hasura access permits queries, not mutations/subscriptions.
API and Hasura requests refuse redirects.

## Readiness is not authentication

The 2.x profile requires a bearer token. Public readiness must return a successful
JSON health result, but that alone is insufficient. An anonymous request to the
protected tariff-read endpoint must be rejected with 401/403; the same request
with the configured token must return a collection. A deployment that allows
anonymous access fails validation rather than reporting authenticated access.

This proves only access to that protected read endpoint. It does not prove
permission to issue OCPP commands, enforcement of all tenant policy, station
registration, meter accuracy or actual charging response. The configured API
role needs permission for the selected tariff-read probe; this PR does not
broaden its permissions or fetch a privileged token.

Hasura is checked separately using the actual discovery query, not `__typename`
alone. For the 2.x profile, configure the read-only gateway's `/v1/graphql` URL
and bearer token, not a Hasura administrator secret. The legacy profile retains
its existing admin-secret-header convention. No endpoint or secret is generated.
Server-side tenant isolation and a read-only Hasura role remain
deployment requirements. Query filters are defence in depth, not a substitute
for server-side authorisation.

## Data model and error handling

The 2.x database station ID is a numeric foreign key. The OCPP connection identity
is `ocppConnectionName`; this client preserves the former as `databaseId` and
normalises display/relationship references to the latter. Duplicate identities,
cross-tenant data, missing collections and orphan relationships fail discovery.
No command routing is enabled by this mapping.

EVSE and connector database keys are also distinct from OCPP numbers. The fixed
query joins the `Evse` relationship and maps `evseTypeId` and
`evseTypeConnectorId` for 2.x stations, retaining database keys separately.
Missing or ambiguous mappings fail rather than substituting database IDs.

The fixed query reads stations, connectors and active transactions, bounded at
500 rows per collection. A 501st row produces an explicit failure; pagination
and historical transaction loading are deferred. Transaction `startTime` and
`endTime` are aliased to the client names. Unsupported legacy fields are not
silently deleted from the 2.x query.

Valid empty collections are successful empty discovery. Missing endpoints,
authentication failures and invalid data are errors, not empty success. HA marks
failed discovery unavailable; retaining previous data does not authorise control.
Manual telemetry refresh/error clearing cannot mark failed discovery healthy.
Multiple entries require an explicit service `entry_id`.

The legacy controller now reports a successful HTTP dispatch as `dispatched`,
not `accepted`. Actual station acceptance and measured response still require
separate evidence. The 2.x profile does not poll the legacy EMS-intake endpoint.

## Verification and remaining gates

The focused suite covers profile selection, identity mapping, unsafe discovery,
protected read validation, HTTP media types and redirects, read-only REST/MQTT
guards, unavailable-discovery guards and existing controller behaviour.
All responses and stations in these tests are fixtures. No live HA, Hasura,
broker, server or charger is contacted. CI installs a pinned Home Assistant
test runtime; it does not install this integration on an operator's system.

The suite also checks query fields against the pinned server model files.
This is a static schema check, not a live GraphQL query. The existing allocator
suite has three known failing assertions; CI runs it against both the unmodified
client baseline and this branch and requires the exact same failure set.
These failures remain documented debt, not passing tests. No allocation
algorithm is changed by this PR.

Before activation:

1. Review this draft and the separate App package. Keep the live HA entry disabled.
2. Prove the fixed query against a real isolated Hasura schema and least-privilege
   credentials, including negative tenant and permission tests.
3. Resolve the packaging PR's inline-schema issue and run default-mode server
   startup, migrations, broker health, persistence and restore checks.
4. Request separate installation approval and perform a read-only live test.
5. Prepare a separate command-compatibility change before enabling 2.x control.

## Pinned evidence

The server baseline is
[CitrineOS v2.0.1, commit 1a83638](https://github.com/citrineos/citrineos-core/tree/1a83638c31eebdb1dd8d6cdb0f5b8a9f142a1372).
Readiness comes from its
[server health routes](https://github.com/citrineos/citrineos-core/blob/1a83638c31eebdb1dd8d6cdb0f5b8a9f142a1372/packages/ocpp/src/server/citrineos-server.ts);
the protected read is its
[tariff GET API](https://github.com/citrineos/citrineos-core/blob/1a83638c31eebdb1dd8d6cdb0f5b8a9f142a1372/packages/ocpp/src/apis/commands/tariff-endpoints.ts).
The fixed discovery query follows the
[station](https://github.com/citrineos/citrineos-core/blob/1a83638c31eebdb1dd8d6cdb0f5b8a9f142a1372/packages/dal/src/db/drizzle/schema/charging-station.ts),
[connector](https://github.com/citrineos/citrineos-core/blob/1a83638c31eebdb1dd8d6cdb0f5b8a9f142a1372/packages/dal/src/db/drizzle/schema/connector.ts)
and [transaction](https://github.com/citrineos/citrineos-core/blob/1a83638c31eebdb1dd8d6cdb0f5b8a9f142a1372/packages/dal/src/db/drizzle/schema/transaction.ts)
models. The client baseline is
[a98cd33](https://github.com/Stuart0411/CitrineOS-HA-Integration/tree/a98cd336e4197f3875be238bdfe5c4784194efb1).
