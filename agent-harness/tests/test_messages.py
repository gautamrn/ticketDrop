from __future__ import annotations

from datetime import UTC

import pytest

from conftest import stream_fields
from harness.messages import MalformedMessage, parse


def test_parses_the_publisher_field_set():
    msg = parse(stream_fields())

    assert msg.source == "ra"
    assert msg.source_event_id == "90001"
    assert msg.artists == ("Prospa",)
    assert msg.attending == 412
    assert msg.queue_it is False
    assert msg.detected_at.tzinfo is not None


def test_nanosecond_timestamps_from_go_are_accepted():
    msg = parse(stream_fields(detected_at="2026-08-09T12:34:56.123456789Z"))

    assert msg.detected_at.year == 2026
    assert msg.detected_at.tzinfo == UTC


def test_deep_link_prefers_ticket_link_unmodified():
    msg = parse(stream_fields(ticket_link="https://seller.example/9876?utm=x"))

    assert msg.deep_link == "https://seller.example/9876?utm=x"


def test_deep_link_falls_back_to_event_url():
    msg = parse(stream_fields(ticket_link=""))

    assert msg.deep_link == "https://ra.co/events/90001"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("drop_id", ""),
        ("source", "ticketmaster"),
        ("change", "announced"),
        ("change", "price_drop"),
        ("event_id", ""),
        ("detected_at", "not-a-timestamp"),
    ],
)
def test_unroutable_messages_are_rejected(field: str, value: str):
    with pytest.raises(MalformedMessage):
        parse(stream_fields(**{field: value}))


def test_unparseable_attending_degrades_to_absent():
    assert parse(stream_fields(attending="lots")).attending is None
