"""Synchronous observers are application-scoped and cannot disrupt publication."""
import asyncio
import logging

import pytest

from office import bus


def test_listener_matches_source_by_identity_and_unscoped_publications_stay_isolated():
    class EqualSource:
        def __eq__(self, other):
            return True
    first, second = EqualSource(), EqualSource()
    first_events, second_events = [], []
    remove_first = bus.add_listener(lambda event, data: first_events.append((event, data)), source=first)
    remove_second = bus.add_listener(lambda event, data: second_events.append((event, data)), source=second)
    try:
        bus.publish("task.updated", {"id": "first"}, source=first)
        bus.publish("task.updated", {"id": "second"}, source=second)
        bus.publish("task.updated", {"id": "unscoped"})
        assert first_events == [("task.updated", {"id": "first"})]
        assert second_events == [("task.updated", {"id": "second"})]
    finally:
        remove_first()
        remove_second()


def test_unsubscribe_is_idempotent_and_removes_only_its_registration():
    source = object()
    received = []
    callback = lambda event, data: received.append(event)
    remove_first = bus.add_listener(callback, source=source)
    remove_second = bus.add_listener(callback, source=source)
    try:
        remove_first()
        remove_first()
        bus.publish("state.changed", {}, source=source)
        assert received == ["state.changed"]
        remove_second()
        bus.publish("task.updated", {}, source=source)
        assert received == ["state.changed"]
    finally:
        remove_first()
        remove_second()


def test_none_source_is_rejected():
    with pytest.raises(ValueError, match="non-None source"):
        bus.add_listener(lambda event, data: None, source=None)


def test_failed_listener_cannot_interrupt_another_listener_and_logging_is_private(caplog):
    source = object()
    received = []
    def fail(event, data):
        raise RuntimeError("private exception content")
    remove_failure = bus.add_listener(fail, source=source)
    remove_success = bus.add_listener(lambda event, data: received.append(event), source=source)
    try:
        with caplog.at_level(logging.WARNING, logger="office.bus"):
            bus.publish("private event name", {"private": "private payload"}, source=source)
        assert received == ["private event name"]
        assert "publication continued" in caplog.text
        assert "private" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        remove_failure()
        remove_success()


def test_sse_wire_shape_is_unchanged_for_scoped_and_unscoped_events():
    async def run():
        initial_count = len(bus._subscribers)
        stream = bus.subscribe()
        try:
            assert await anext(stream) == "event: hello\ndata: {}\n\n"
            bus.publish("task.updated", {"id": "abc"}, source=object())
            assert await anext(stream) == 'event: task.updated\ndata: {"id": "abc"}\n\n'
            bus.publish("state.changed", {})
            assert await anext(stream) == "event: state.changed\ndata: {}\n\n"
        finally:
            await stream.aclose()
        assert len(bus._subscribers) == initial_count
    asyncio.run(run())
