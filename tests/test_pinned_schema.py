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
