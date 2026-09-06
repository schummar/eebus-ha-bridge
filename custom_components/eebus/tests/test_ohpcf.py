"""Tests for OHPCF state conversion and controls."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import grpc
import pytest
from grpc.aio import AioRpcError, Metadata
from homeassistant.exceptions import ServiceValidationError

from custom_components.eebus import proto_stubs
from custom_components.eebus.coordinator import EebusCoordinator
from custom_components.eebus.device_session import DeviceSession
from custom_components.eebus.generated.eebus.v1 import ohpcf_service_pb2 as ohpcf_pb2
from custom_components.eebus.models import CapabilityState, CompressorFlexibilityState
from custom_components.eebus.select import EebusCompressorFlexibilitySelect
from custom_components.eebus.sensor import STATE_SENSORS, EebusMeasurementSensor
from custom_components.eebus.snapshot import _async_read_compressor_flexibility
from custom_components.eebus.state import (
    REASON_LOCAL_DISABLED,
    CapabilitiesState,
    CapabilityKey,
    CapabilityMetadata,
    ConnectionState,
    DeviceState,
    DeviceStateStore,
    OHPCFState,
    StateField,
    ohpcf_locally_disabled,
)


def _flex(state: str = "COMPRESSOR_STATE_AVAILABLE") -> CompressorFlexibilityState:
    return CompressorFlexibilityState(True, state, None, None, True, False, 600, 300)


def _coordinator(flex: CompressorFlexibilityState | None = None) -> EebusCoordinator:
    coordinator = EebusCoordinator.__new__(EebusCoordinator)
    coordinator.ski = "test-ski"
    initial = DeviceState(
        connection=ConnectionState(connected=True),
        ohpcf=OHPCFState(compressor_flexibility=flex),
        capabilities=CapabilitiesState(ohpcf=(CapabilityState.AVAILABLE if flex else CapabilityState.UNKNOWN)),
        fresh_fields=(frozenset({StateField.COMPRESSOR_FLEXIBILITY}) if flex else frozenset()),
    )
    coordinator.data = initial
    coordinator.last_update_success = True
    coordinator._ensure_channel = AsyncMock(return_value=None)
    coordinator._state_store = DeviceStateStore(lambda value: setattr(coordinator, "data", value), initial)
    coordinator._device_session = DeviceSession(coordinator.ski, coordinator._ensure_channel)
    return coordinator


def test_read_compressor_flexibility_maps_fields() -> None:
    flex = ohpcf_pb2.CompressorFlexibility(
        available=True,
        requested_power_estimate_w=1500.0,
        is_pausable=True,
        state=ohpcf_pb2.COMPRESSOR_STATE_RUNNING,
        minimal_run_seconds=600,
    )
    stub = SimpleNamespace(GetCompressorFlexibility=AsyncMock(return_value=flex))
    with patch.object(proto_stubs, "ohpcf_service_stub", return_value=stub):
        result = asyncio.run(
            _async_read_compressor_flexibility(None, proto_stubs.DeviceRequest(ski="test-ski"), "test-ski")
        )
    assert result.status is None
    assert result.value == CompressorFlexibilityState(
        True,
        "COMPRESSOR_STATE_RUNNING",
        1500.0,
        None,
        True,
        False,
        600,
        0,
    )


def test_read_unavailable_defers_status_to_reducer() -> None:
    error = AioRpcError(grpc.StatusCode.UNAVAILABLE, Metadata(), Metadata(), details="off")
    stub = SimpleNamespace(GetCompressorFlexibility=AsyncMock(side_effect=error))
    with patch.object(proto_stubs, "ohpcf_service_stub", return_value=stub):
        result = asyncio.run(
            _async_read_compressor_flexibility(None, proto_stubs.DeviceRequest(ski="test-ski"), "test-ski")
        )
    assert result.value is None
    assert result.status == grpc.StatusCode.UNAVAILABLE


def _select_with(flex: CompressorFlexibilityState | None):
    coordinator = _coordinator(flex)
    coordinator.async_control_compressor = AsyncMock()
    coordinator.async_request_refresh = AsyncMock()
    return EebusCompressorFlexibilitySelect(coordinator)


@pytest.mark.parametrize(
    ("raw", "option"),
    [
        ("COMPRESSOR_STATE_RUNNING", "on"),
        ("COMPRESSOR_STATE_SCHEDULED", "on"),
        ("COMPRESSOR_STATE_PAUSED", "paused"),
        ("COMPRESSOR_STATE_AVAILABLE", "off"),
        ("COMPRESSOR_STATE_STOPPED", "off"),
    ],
)
def test_select_current_option(raw: str, option: str) -> None:
    assert _select_with(_flex(raw)).current_option == option


@pytest.mark.parametrize(
    ("flex", "expected"),
    [
        # An offered but not yet started process: schedule it, nothing to pause or abort.
        (_flex(), ["on", "off"]),
        # Running: pausable per the offer, but not stoppable, so no abort.
        (_flex("COMPRESSOR_STATE_RUNNING"), ["on", "paused"]),
        (
            replace(_flex("COMPRESSOR_STATE_RUNNING"), is_stoppable=True),
            ["on", "paused", "off"],
        ),
        # Scheduled: OHPCF-022/2 needs a *running* process, so no pause.
        (_flex("COMPRESSOR_STATE_SCHEDULED"), ["on"]),
        (
            replace(_flex("COMPRESSOR_STATE_SCHEDULED"), is_stoppable=True),
            ["on", "off"],
        ),
        # Paused: resume is always permitted, abort only when stoppable.
        (_flex("COMPRESSOR_STATE_PAUSED"), ["on", "paused"]),
        (
            replace(_flex("COMPRESSOR_STATE_PAUSED"), is_stoppable=True),
            ["on", "paused", "off"],
        ),
        # No offer at all: nothing to schedule, pause or abort.
        (
            replace(
                _flex("COMPRESSOR_STATE_STOPPED"),
                available=False,
                is_pausable=False,
                is_stoppable=False,
            ),
            ["off"],
        ),
    ],
)
def test_select_offers_only_permitted_transitions(
    flex: CompressorFlexibilityState, expected: list[str]
) -> None:
    select = _select_with(flex)
    assert select.options == expected
    assert select.current_option in select.options


def test_select_options_fall_back_to_all_transitions_without_an_offer() -> None:
    assert _select_with(None).options == ["on", "paused", "off"]


def test_select_is_unavailable_when_retained_offer_is_stale() -> None:
    coordinator = _coordinator(_flex())
    coordinator.data = DeviceState(
        connection=ConnectionState(connected=True),
        ohpcf=coordinator.data.ohpcf,
        capabilities=CapabilitiesState(ohpcf=CapabilityState.TEMPORARILY_UNAVAILABLE),
    )
    assert EebusCompressorFlexibilitySelect(coordinator).available is False


def test_select_remains_available_when_no_process_is_offered() -> None:
    flex = replace(
        _flex("COMPRESSOR_STATE_STOPPED"),
        available=False,
        is_pausable=False,
        is_stoppable=False,
    )
    select = _select_with(flex)
    assert select.available is True
    assert select.current_option == "off"


def test_select_option_on_schedules_or_resumes() -> None:
    select = _select_with(_flex())
    asyncio.run(select.async_select_option("on"))
    select.coordinator.async_control_compressor.assert_awaited_once_with(proto_stubs.OHPCFAction.OHPCF_ACTION_SCHEDULE)
    select = _select_with(_flex("COMPRESSOR_STATE_PAUSED"))
    asyncio.run(select.async_select_option("on"))
    select.coordinator.async_control_compressor.assert_awaited_once_with(proto_stubs.OHPCFAction.OHPCF_ACTION_RESUME)


def test_select_only_sends_permitted_pause_and_abort_actions() -> None:
    select = _select_with(_flex("COMPRESSOR_STATE_RUNNING"))
    asyncio.run(select.async_select_option("paused"))
    select.coordinator.async_control_compressor.assert_awaited_once_with(
        proto_stubs.OHPCFAction.OHPCF_ACTION_PAUSE
    )

    select = _select_with(
        replace(_flex("COMPRESSOR_STATE_RUNNING"), is_stoppable=True)
    )
    asyncio.run(select.async_select_option("off"))
    select.coordinator.async_control_compressor.assert_awaited_once_with(
        proto_stubs.OHPCFAction.OHPCF_ACTION_ABORT
    )


@pytest.mark.parametrize(
    ("flex", "option"),
    [
        (replace(_flex(), available=False), "on"),
        (_flex("COMPRESSOR_STATE_SCHEDULED"), "paused"),
        (replace(_flex("COMPRESSOR_STATE_RUNNING"), is_pausable=False), "paused"),
        (_flex("COMPRESSOR_STATE_RUNNING"), "off"),
        (replace(_flex("COMPRESSOR_STATE_STOPPED"), is_stoppable=True), "off"),
        (_flex(), "invalid"),
    ],
)
def test_select_rejects_actions_disallowed_by_ohpcf(
    flex: CompressorFlexibilityState, option: str
) -> None:
    select = _select_with(flex)
    with pytest.raises(ServiceValidationError):
        asyncio.run(select.async_select_option(option))
    select.coordinator.async_control_compressor.assert_not_awaited()
    select.coordinator.async_request_refresh.assert_not_awaited()


def test_status_sensor_maps_raw_state() -> None:
    description = next(item for item in STATE_SENSORS if item.key == "compressor_flexibility_status")
    sensor = EebusMeasurementSensor(_coordinator(_flex("COMPRESSOR_STATE_RUNNING")), description)
    assert sensor.native_value == "running"


def test_control_rejection_is_validation_error() -> None:
    coordinator = _coordinator()
    error = AioRpcError(
        grpc.StatusCode.INTERNAL,
        Metadata(),
        Metadata(),
        details="ohpcf control: data not available",
    )
    stub = SimpleNamespace(ControlCompressorFlexibility=AsyncMock(side_effect=error))
    with patch.object(proto_stubs, "ohpcf_service_stub", return_value=stub), pytest.raises(ServiceValidationError):
        asyncio.run(coordinator.async_control_compressor(proto_stubs.OHPCFAction.OHPCF_ACTION_SCHEDULE))


@pytest.mark.parametrize(
    ("error_status", "expected"),
    [
        (None, CapabilityState.AVAILABLE),
        (grpc.StatusCode.UNAVAILABLE, CapabilityState.TEMPORARILY_UNAVAILABLE),
        (grpc.StatusCode.UNIMPLEMENTED, CapabilityState.UNSUPPORTED),
    ],
)
def test_write_status_updates_authoritative_capability(error_status, expected) -> None:
    coordinator = _coordinator()
    side_effect = None
    if error_status is not None:
        side_effect = AioRpcError(error_status, Metadata(), Metadata(), details="failure")
    stub = SimpleNamespace(ControlCompressorFlexibility=AsyncMock(side_effect=side_effect))
    with patch.object(proto_stubs, "ohpcf_service_stub", return_value=stub):
        if error_status == grpc.StatusCode.UNAVAILABLE:
            with pytest.raises(ServiceValidationError):
                asyncio.run(coordinator.async_control_compressor(proto_stubs.OHPCFAction.OHPCF_ACTION_SCHEDULE))
        else:
            asyncio.run(coordinator.async_control_compressor(proto_stubs.OHPCFAction.OHPCF_ACTION_SCHEDULE))
    assert coordinator.data.capabilities.ohpcf == expected


def _locally_disabled_state() -> DeviceState:
    return DeviceState(
        connection=ConnectionState(connected=True),
        capabilities=CapabilitiesState(ohpcf=CapabilityState.UNSUPPORTED),
        capability_metadata=(
            CapabilityMetadata(CapabilityKey.OHPCF, REASON_LOCAL_DISABLED, None),
        ),
    )


def test_ohpcf_locally_disabled_requires_explicit_local_reason() -> None:
    assert ohpcf_locally_disabled(_locally_disabled_state())
    assert not ohpcf_locally_disabled(None)
    assert not ohpcf_locally_disabled(DeviceState())
    # Remote non-advertisement is transient, so the entities must stay.
    assert not ohpcf_locally_disabled(
        replace(
            _locally_disabled_state(),
            capability_metadata=(
                CapabilityMetadata(CapabilityKey.OHPCF, "remote_not_advertised", None),
            ),
        )
    )
    # A legacy bridge without the explicit contract reports no reason.
    assert not ohpcf_locally_disabled(replace(_locally_disabled_state(), capability_metadata=()))


def test_select_setup_skips_entity_when_ohpcf_locally_disabled() -> None:
    from custom_components.eebus import select as select_platform

    entry = SimpleNamespace(runtime_data=SimpleNamespace(data=_locally_disabled_state()))
    added = MagicMock()
    asyncio.run(select_platform.async_setup_entry(None, entry, added))
    added.assert_not_called()


def test_select_setup_creates_entity_when_ohpcf_enabled() -> None:
    from custom_components.eebus import select as select_platform

    entry = SimpleNamespace(runtime_data=_coordinator(_flex()))
    added = MagicMock()
    asyncio.run(select_platform.async_setup_entry(None, entry, added))
    (entities,) = added.call_args.args
    assert len(entities) == 1
    assert isinstance(entities[0], EebusCompressorFlexibilitySelect)


def test_sensor_setup_drops_ohpcf_diagnostics_when_locally_disabled() -> None:
    from custom_components.eebus import sensor as sensor_platform

    coordinator = _coordinator()
    coordinator.data = _locally_disabled_state()
    entry = SimpleNamespace(runtime_data=coordinator)
    added = MagicMock()
    asyncio.run(sensor_platform.async_setup_entry(None, entry, added))
    keys = {entity.entity_description.key for entity in added.call_args.args[0]}
    all_keys = {
        description.key
        for description in (*sensor_platform.STATE_SENSORS, *sensor_platform.MEASUREMENT_SENSORS)
    }
    assert sensor_platform.OHPCF_SENSOR_KEYS <= all_keys
    assert keys == all_keys - sensor_platform.OHPCF_SENSOR_KEYS


def test_sensor_setup_keeps_ohpcf_diagnostics_when_enabled() -> None:
    from custom_components.eebus import sensor as sensor_platform

    entry = SimpleNamespace(runtime_data=_coordinator(_flex()))
    added = MagicMock()
    asyncio.run(sensor_platform.async_setup_entry(None, entry, added))
    keys = {entity.entity_description.key for entity in added.call_args.args[0]}
    assert sensor_platform.OHPCF_SENSOR_KEYS <= keys


def test_remove_ohpcf_entities_removes_stale_registry_entries() -> None:
    from custom_components.eebus import _remove_ohpcf_entities

    entity_registry = MagicMock()
    entity_registry.async_get_entity_id.side_effect = [
        "select.eebus_compressor_flexibility",
        None,
        "sensor.eebus_compressor_flexibility_power_estimate",
        None,
    ]
    with patch("custom_components.eebus.er.async_get", return_value=entity_registry):
        _remove_ohpcf_entities(MagicMock(), "test-ski")
    assert entity_registry.async_remove.call_args_list == [
        call("select.eebus_compressor_flexibility"),
        call("sensor.eebus_compressor_flexibility_power_estimate"),
    ]
