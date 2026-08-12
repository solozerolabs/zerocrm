"""Sender abstraction the executor drives. Real drivers (Smartlead, Expandi,
Postbeam) implement Sender; tests use FakeSender.

`already_sent` is what makes exactly-once possible across a crash: on
redelivery the executor asks the provider "did this already go out?" instead of
blindly re-sending.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Sender(Protocol):
    channel: str

    def send(self, item: dict) -> str:
        """Perform the send. Return a provider message/enrollment id. May raise."""
        ...

    def already_sent(self, item: dict) -> bool:
        """Idempotency probe for redelivery: has this item's send already landed?"""
        ...


class FakeSender:
    """In-memory sender for tests. `crash_after_send` simulates worker death
    between the provider call and the DB confirm."""

    channel = "email"

    def __init__(self, crash_after_send: bool = False):
        self.sent: list[int] = []
        self.crash_after_send = crash_after_send

    def send(self, item: dict) -> str:
        self.sent.append(item["item_no"])
        if self.crash_after_send:
            raise RuntimeError("simulated crash after provider send, before DB confirm")
        return f"fake-{item['item_no']}"

    def already_sent(self, item: dict) -> bool:
        return item["item_no"] in self.sent
