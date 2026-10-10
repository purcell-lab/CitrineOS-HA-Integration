"""Closed-loop behavioral tests for CitrineLoadController."""

from unittest.mock import AsyncMock, MagicMock
from datetime import UTC, datetime
import pytest
from custom_components.citrine_ha.const import (
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CONTROLLER_MODE,
    CONF_DEADBAND_W,
    CONF_DOE_EXPORT_LIMIT_SENSOR,
    CONF_DOE_IMPORT_LIMIT_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_MAIN_FUSE_LIMIT_W,
    CONF_MIN_CHARGE_CURRENT_A,
    CONF_MIN_DWELL_SECS,
    CONF_NOMINAL_VOLTAGE,
    CONF_RAMP_RATE_W_S,
    CONF_SITE_EXPORT_LIMIT_W,
    CONF_SITE_PHASES,
    CONF_SOLAR_POWER_SENSOR,
    CONF_SOLAR_START_BUFFER_W,
    MODE_DYNAMIC_DOE,
    MODE_EMERGENCY_SAFE,
    MODE_GRID_CAPPED,
    MODE_OFF,
    MODE_SOLAR_BATTERY,
    MODE_SOLAR_ONLY,
)
from custom_components.citrine_ha.load_controller import CitrineLoadController


@pytest.fixture
def mock_controller_env():
    """Build mock HA environment with coordinator and client."""
    hass = MagicMock()
    entry = MagicMock()
    coordinator = MagicMock()
    client = MagicMock()

    entry.data = {
        "read_only": False,
        CONF_CONTROLLER_MODE: MODE_SOLAR_ONLY,
        CONF_MAIN_FUSE_LIMIT_W: 14400.0,
        CONF_SITE_EXPORT_LIMIT_W: 5000.0,
        CONF_SOLAR_START_BUFFER_W: 250.0,
        CONF_MIN_CHARGE_CURRENT_A: 6.0,
        CONF_SITE_PHASES: "1",
        CONF_NOMINAL_VOLTAGE: 230.0,
        CONF_DEADBAND_W: 250.0,
        CONF_MIN_DWELL_SECS: 30,
        CONF_GRID_POWER_SENSOR: "sensor.grid_power",
        CONF_SOLAR_POWER_SENSOR: "sensor.solar_power",
        CONF_BATTERY_POWER_SENSOR: "sensor.battery_power",
        CONF_BATTERY_SOC_SENSOR: "sensor.battery_soc",
    }
    entry.options = {}

    coordinator.data = {
        "stations": [
            {
                "id": "CS1",
                "isOnline": True,
                "protocol": "ocpp2.0.1",
                "defaultEvseId": 1,
                "activeTransactionId": "tx-1",
            }
        ]
    }
    coordinator.get_station_protocol.return_value = "ocpp2.0.1"
    coordinator.get_station_capabilities.return_value = {}

    client.set_station_limit = AsyncMock()
    hass.services.has_service.return_value = False  # Test REST dispatch path

    controller = CitrineLoadController(
        hass=hass,
        entry=entry,
        coordinator=coordinator,
        client=client,
    )
    return controller, hass, client


@pytest.mark.asyncio
async def test_solar_tracking_charging_allocation(mock_controller_env):
    """Scenario 1: Excess solar charges active EVSE above the start buffer."""
    controller, hass, client = mock_controller_env

    # 4000W solar generation, 1000W grid export (-1000W), 0W battery
    # non_ev_house_w = -1000 + 4000 + 0 - 0 = 3000W house load
    # raw_surplus = 4000 - 3000 = 1000W
    # surplus is above 250W buffer -> available budget = 750W (below 6A/1380W pilot) -> allocates 0W
    def get_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.solar_power":
            state.state = "4000.0"
        elif entity_id == "sensor.grid_power":
            state.state = "-1000.0"
        elif entity_id == "sensor.battery_power":
            state.state = "0.0"
        elif entity_id == "sensor.battery_soc":
            state.state = "80.0"
        return state

    hass.states.get.side_effect = get_state

    # 1st run: 1000W surplus -> budget 750W (< 1380W min pilot floor) -> 0W
    await controller.async_recompute()
    assert controller.solar_surplus_w == 1000.0
    assert controller.allocated_ev_power_w == 0.0

    # 2nd run: Solar jumps to 6000W, grid export -3000W
    # non_ev_house_w = -3000 + 6000 = 3000W
    # raw surplus = 6000 - 3000 = 3000W
    # budget = 3000 - 250 = 2750W (>= 1380W min pilot) -> allocates 2750W
    def get_higher_solar_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.solar_power":
            state.state = "6000.0"
        elif entity_id == "sensor.grid_power":
            state.state = "-3000.0"
        elif entity_id == "sensor.battery_power":
            state.state = "0.0"
        elif entity_id == "sensor.battery_soc":
            state.state = "80.0"
        return state

    hass.states.get.side_effect = get_higher_solar_state

    # Force time forward past min dwell time
    controller._last_state_change_time.clear()

    await controller.async_recompute()
    assert controller.solar_surplus_w == 3000.0
    assert controller.allocated_ev_power_w == 2750.0
    client.set_station_limit.assert_called_with(
        protocol="ocpp2.0.1",
        station_id="CS1",
        limit=2750.0,
        unit="W",
        evse_id=1,
        duration=300,
    )


@pytest.mark.asyncio
async def test_sudden_load_spike_curtailment(mock_controller_env):
    """Scenario 2: Sudden home load spike (>92% fuse rating) forces instant curtailment."""
    controller, hass, client = mock_controller_env
    controller.set_mode(MODE_GRID_CAPPED)

    # Main fuse is 14400W. Grid import spikes to 13500W (> 92% of 14400W = 13248W)
    def get_spike_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.solar_power":
            state.state = "0.0"
        elif entity_id == "sensor.grid_power":
            state.state = "13500.0"
        elif entity_id == "sensor.battery_power":
            state.state = "0.0"
        return state

    hass.states.get.side_effect = get_spike_state

    await controller.async_recompute()
    assert controller.state_status == "Curtailing (Grid Alert)"


@pytest.mark.asyncio
async def test_dynamic_doe_envelope_tracking(mock_controller_env):
    """Scenario 3: CSIP-Aus DOE limits dynamically constrain available EV budget."""
    controller, hass, client = mock_controller_env
    controller.entry.options[CONF_DOE_IMPORT_LIMIT_SENSOR] = "sensor.doe_import_limit"
    controller.entry.options[CONF_DOE_EXPORT_LIMIT_SENSOR] = "sensor.doe_export_limit"
    controller.set_mode(MODE_DYNAMIC_DOE)

    # Dynamic DOE import cap = 6000W, house load = 2000W, 0W solar -> remaining headroom = 4000W
    def get_doe_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.doe_import_limit":
            state.state = "6000.0"
        elif entity_id == "sensor.doe_export_limit":
            state.state = "1500.0"
        elif entity_id == "sensor.grid_power":
            state.state = "2000.0"
        elif entity_id == "sensor.solar_power":
            state.state = "0.0"
        elif entity_id == "sensor.battery_power":
            state.state = "0.0"
        return state

    hass.states.get.side_effect = get_doe_state
    controller._last_state_change_time.clear()

    await controller.async_recompute()
    assert controller.state_status == "CSIP-Aus Envelope Active"
    assert controller.allocated_ev_power_w == 4000.0


@pytest.mark.asyncio
async def test_three_phase_unbalance_monitoring(mock_controller_env):
    """Scenario 4: 3-phase grid current monitoring calculates phase unbalance."""
    controller, hass, client = mock_controller_env
    controller.entry.options[CONF_GRID_PHASE_A_CURRENT_SENSOR] = "sensor.phase_a_current"
    controller.entry.options[CONF_GRID_PHASE_B_CURRENT_SENSOR] = "sensor.phase_b_current"
    controller.entry.options[CONF_GRID_PHASE_C_CURRENT_SENSOR] = "sensor.phase_c_current"

    def get_phase_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.phase_a_current":
            state.state = "25.0"
        elif entity_id == "sensor.phase_b_current":
            state.state = "10.0"
        elif entity_id == "sensor.phase_c_current":
            state.state = "5.0"
        elif entity_id == "sensor.grid_power":
            state.state = "9200.0"
        elif entity_id == "sensor.solar_power":
            state.state = "0.0"
        elif entity_id == "sensor.battery_power":
            state.state = "0.0"
        return state

    hass.states.get.side_effect = get_phase_state
    controller._last_state_change_time.clear()

    await controller.async_recompute()
    assert controller.phase_a_current_a == 25.0
    assert controller.phase_b_current_a == 10.0
    assert controller.phase_c_current_a == 5.0
    # Unbalance = max(25, 10, 5) - min(25, 10, 5) = 20.0A
    assert controller.phase_unbalance_a == 20.0


@pytest.mark.asyncio
async def test_active_charging_connector_without_transaction_id_counts_as_active(
    mock_controller_env,
):
    """A charging connector is active even when transaction ID telemetry is absent."""
    controller, hass, client = mock_controller_env
    controller.coordinator.data["stations"][0].pop("activeTransactionId", None)
    controller.coordinator.data["stations"][0]["connectors"] = [
        {
            "connectorId": 1,
            "evseId": 1,
            "status": "Charging",
            "isOnline": True,
        }
    ]

    def get_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.grid_power":
            state.state = "-3000.0"
        else:
            state.state = "0.0" if entity_id != "sensor.battery_soc" else "80.0"
        return state

    hass.states.get.side_effect = get_state

    await controller.async_recompute()

    assert controller.active_ev_count == 1


@pytest.mark.asyncio
async def test_unavailable_grid_sensor_forces_safe_fallback(mock_controller_env):
    """Configured unavailable grid telemetry must prevent EV allocation."""
    controller, hass, client = mock_controller_env

    def get_unavailable_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.grid_power":
            state.state = "unavailable"
        else:
            state.state = "0.0"
        return state

    hass.states.get.side_effect = get_unavailable_state

    await controller.async_recompute()

    assert controller.telemetry_health == "stale"
    assert controller.allocated_ev_power_w == 0.0
    assert controller.state_status == "Safe Fallback: telemetry unavailable"
    assert "grid power sensor state is unavailable" in controller.telemetry_error


@pytest.mark.asyncio
async def test_limit_command_status_is_recorded(mock_controller_env):
    """Accepted station limit commands are visible to HA diagnostics."""
    controller, hass, client = mock_controller_env

    def get_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.grid_power":
            state.state = "-3000.0"
        else:
            state.state = "0.0" if entity_id != "sensor.battery_soc" else "80.0"
        return state

    hass.states.get.side_effect = get_state
    await controller.async_recompute()

    assert controller.last_command_status == "dispatched"
    assert controller.last_command_station_id == "CS1"
    assert controller.last_command_limit_w == 2750.0
    assert controller.last_command_error is None


@pytest.mark.asyncio
async def test_export_is_surplus_and_does_not_inflate_site_headroom(mock_controller_env):
    """Grid export supplies surplus, while headroom remains bounded by the import cap."""
    controller, hass, client = mock_controller_env

    def get_export_state(entity_id):
        state = MagicMock()
        if entity_id == "sensor.grid_power":
            state.state = "-6000.0"
        elif entity_id == "sensor.solar_power":
            state.state = "0.0"
        elif entity_id == "sensor.battery_power":
            state.state = "0.0"
        elif entity_id == "sensor.battery_soc":
            state.state = "80.0"
        else:
            state.state = "0.0"
        return state

    hass.states.get.side_effect = get_export_state
    controller._last_state_change_time.clear()

    await controller.async_recompute()

    assert controller.solar_surplus_w == 6000.0
    assert controller.site_headroom_w == 14400.0

