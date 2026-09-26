"""Recorded debug requests."""

from typing import Literal

from oam.contracts.evidence import FrozenContract, NonBlank


class ReplayRequest(FrozenContract):
    trace_id: NonBlank
    mode: Literal["inspect_recorded", "reassemble_recorded"]
