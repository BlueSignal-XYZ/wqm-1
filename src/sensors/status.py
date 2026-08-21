"""
Why a channel has no number this cycle.

A driver that returns `None` says only "no reading". Everything downstream then
has to guess what that means, and every layer guesses differently: the DB stores
NULL, `cloud/client.py` omits the key entirely, the cloud's mirror loses the
channel, and the customer's dashboard loses the tile. A probe that is working
perfectly in clean water and a probe lying on the floor produce the identical
result — nothing.

That cost a real customer their TDS tile on 2026-08-21. The tile did not go
blank, it ceased to exist, and the founder's question was the right one: the
probe is still connected, so how did this happen?

It happened because `tds.py` rejected any ADC reading at or below 0.05 V as an
open input. With the 0.3125 divider in front of it and the default 500 ppm/V
calibration, that is **80 ppm** — well inside the range the sensor exists to
measure. Clean water was being discarded as a disconnected probe.

So a driver now answers with a REASON, and two rules follow from that:

  1. **Clean water is a result, not an absence.** A genuinely low reading is a
     number, and it travels as a number. Only a signal the electronics could
     not have produced is refused.
  2. **A refusal is still a report.** The channel is transmitted carrying its
     status, so the cloud can say "check the probe" instead of showing nothing
     and letting silence be read as "this unit doesn't measure that".

Status codes are stable strings — they are stored in SQLite, sent to the cloud,
and rendered in a customer-facing UI, so renaming one silently changes what a
dashboard displays. Add codes; do not repurpose them.
"""

from dataclasses import dataclass

# A real measurement. The value is present and trustworthy.
OK = "ok"

# No conduction path: the electrode sees effectively nothing. On this hardware
# a disconnected probe and a probe sitting in air are electrically the same
# thing — both leave the input at essentially zero — so this ONE code covers
# both and the customer-facing text names both possibilities. Claiming to tell
# them apart would be inventing a distinction the electronics cannot make.
NO_CONDUCTION = "no_conduction"

# The signal is outside the band this channel can represent — railed high, or
# past the sensor's documented full scale. Real, but not a measurement.
OUT_OF_RANGE = "out_of_range"

# The maths produced something impossible (negative ppm, NTU outside 0-max)
# from a plausible voltage. That is a calibration or signal-path fault, not a
# property of the water.
UNCALIBRATED = "uncalibrated"

# The bus read itself threw. Says nothing about the probe.
READ_FAILED = "read_failed"

# Every code a driver may emit. Used by tests and by the cloud payload builder
# to reject a typo before it reaches a dashboard.
ALL_STATUSES = frozenset({OK, NO_CONDUCTION, OUT_OF_RANGE, UNCALIBRATED, READ_FAILED})

# Codes that mean "a human should look at this probe". NO_CONDUCTION is the one
# that matters in the field: it is the difference between "your water is clean"
# and "your sensor is out of the water".
NEEDS_ATTENTION = frozenset({NO_CONDUCTION, OUT_OF_RANGE, UNCALIBRATED})


@dataclass(frozen=True)
class SensorResult:
    """One channel's outcome for one sampling cycle.

    `value` is None whenever `status` is not OK, and non-None whenever it is.
    Nothing enforces that at runtime beyond the drivers themselves; the tests
    assert it per driver, which is where a mistake would actually be made.
    """

    value: float | None
    status: str
    # Short, factual, safe to show an installer — "input at 0.002 V". Never a
    # verdict about the water.
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    @property
    def needs_attention(self) -> bool:
        return self.status in NEEDS_ATTENTION
