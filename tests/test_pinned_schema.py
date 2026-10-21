"""Static contract checks on pinned upstream files, not live Hasura validation."""
import os
from pathlib import Path
import re

import pytest
from graphql import parse

from custom_components.citrine_ha.compatibility import V2_DISCOVERY_QUERY


@pytest.mark.skipif(not os.getenv("CITRINE_SERVER_SOURCE"), reason="pinned source supplied in CI")
def test_query_fields_exist_in_pinned_v2_models():
    root = Path(os.environ["CITRINE_SERVER_SOURCE"])
    models = {"ChargingStations": "charging-station", "Connectors": "connector",
              "Transactions": "transaction", "Evse": "evse"}
    def check(selection, model):
        text = (root / "packages/dal/src/db/drizzle/schema" / f"{models[model]}.ts").read_text()
        fields = set(re.findall(r"^\s{4}(\w+):", text, re.MULTILINE))
        for field in selection.selections:
            name = field.name.value
            if field.selection_set:
                assert model == "Connectors" and name == "Evse"
                metadata = (root / "apps/ocpp-server/db/hasura-metadata/databases/default/tables/public_Connectors.yaml").read_text()
                assert "- name: Evse" in metadata and "foreign_key_constraint_on: evseId" in metadata
                check(field.selection_set, name)
            else:
                assert name in fields, f"{model}.{name} is absent from pinned source"
    operation = parse(V2_DISCOVERY_QUERY).definitions[0]
    for collection in operation.selection_set.selections:
        check(collection.selection_set, collection.name.value)


@pytest.mark.skipif(not os.getenv("CITRINE_SERVER_SOURCE"), reason="pinned source supplied in CI")
def test_power_limit_query_fields_exist_in_pinned_v2_models():
    from custom_components.citrine_ha.power_limits import (
        ATTRIBUTES_QUERY, CHARACTERISTICS_QUERY, COMPONENTS_QUERY, NEEDS_QUERY, VARIABLES_QUERY)
    root = Path(os.environ["CITRINE_SERVER_SOURCE"])
    models = {"Variables": "variable", "VariableAttributes": "variable-attribute",
              "Components": "component", "VariableCharacteristics": "variable-characteristics",
              "ChargingNeeds": "charging-needs"}
    for query in (VARIABLES_QUERY, ATTRIBUTES_QUERY.replace("STATION_KEY", "Int"), COMPONENTS_QUERY,
                  CHARACTERISTICS_QUERY, NEEDS_QUERY):
        for collection in parse(query).definitions[0].selection_set.selections:
            model = collection.name.value
            text = (root / "packages/dal/src/db/drizzle/schema" / f"{models[model]}.ts").read_text()
            fields = set(re.findall(r"^\s{4}(\w+):", text, re.MULTILINE))
            for field in collection.selection_set.selections:
                assert field.selection_set is None, "power queries use no relationships"
                assert field.name.value in fields, f"{model}.{field.name.value} is absent from pinned source"
