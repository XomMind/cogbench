#!/usr/bin/env python3
"""
Learn how fast a hack trace climbs, because nobody knows.

Every other number this harness uses is looked up: bot stats, item stats, hack
odds all come from Cog-Minder. The trace increment does not, and not because it
was missed -- it is not published anywhere, and per the player it is part of the
game's nuance that it stays unknown. So it has to be learned from play, which
makes it the first genuinely *learned* quantity here rather than a retrieved
one.

## The shape of the problem

Partial trace is free. There is no penalty for sitting at 90%. Being **fully**
traced is severe. So this is not a budget to stay under, it is a cliff to stop
short of, and the optimal play is to keep hacking right up to the point where
one more attempt might cross 100.

That makes stopping at some fixed percentage strictly wrong in both directions:
too low and free attempts are left unspent, too high and the run eats a full
trace. What matters is the *increment* distribution, and specifically its upper
tail -- the worst single jump ever observed, since that is what can take 88% to
100%.

## Cold start

With no observations, attempts are allowed only while the trace is low enough
that no plausible increment could reach 100. That is safe under almost any
model, and it is also how the observations get collected: explore where it is
cheap, exploit once the tail is known. The estimate tightens on its own as the
log fills.

The log is append-only JSONL and accumulates across runs, so this gets better
the more the benchmark is used -- which is the point.
"""

import json
import os

DEFAULT_LOG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "trace_observations.jsonl"
)

# Until the tail is known, only hack while the trace is below this. Chosen to be
# safe rather than clever: no single increment observed in Cogmind discussion is
# anywhere near this large, so crossing 100 from below it is implausible.
COLD_START_CEILING = 25

# Margin over the worst observed jump, decaying with sample count.
#
# The worst case seen is a sample, not a bound, so it gets padded -- but a fixed
# pad is wrong in both directions as data arrives. At 1.5x with two failures
# clustered at 56 and 60, the model refused to hack at a trace of 10, where even
# the worst observed jump only reaches 70. That leaves free attempts unspent,
# and partial trace costs nothing.
#
# So the pad shrinks as the tail gets better characterised: wide when the
# distribution is a guess, narrow once it is measured. It never reaches zero,
# because the largest jump has not necessarily been seen yet.
# The pad is ADDITIVE. A multiplicative one is wrong on a bounded 0-100 scale:
# 60 x 2 is 120, which exceeds the range, so the model refused to hack even at
# a trace of 0 -- where no observed jump can possibly reach 100. Scaling a jump
# that is already most of the scale produces nonsense.
PAD_SCALE = 20.0  # padding at one sample, decaying as 1/sqrt(n)
PAD_FLOOR = 3  # never trust the observed maximum as a hard bound
MAX_HEADROOM = 99  # a first attempt from 0 is always allowed, since no jump
# can reach 100 from zero

# Below this many samples the learned estimate is not allowed to be *bolder*
# than the cold-start rule.
#
# This is not caution for its own sake -- it fixes an observed failure. A run
# hacked once for +7, concluded the worst jump was 7, padded that to 27, and so
# permitted an attempt at 57%. The next jump was +43 and completed the trace.
# Cold start would have refused at 57. One small benign observation made the
# model *less* safe than knowing nothing, which is the characteristic way a
# learner with a tiny sample walks off a cliff: the sample says the cliff is
# not there.
#
# So the learned headroom is floored at the cold-start ceiling until the tail
# has actually been sampled. Learning may tighten the bound only after it has
# had a fair chance to see a bad outcome.
MIN_SAMPLES_TO_LOOSEN = 15


class TraceModel:
    def __init__(self, path=DEFAULT_LOG):
        self.path = path
        self.increments = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                # Records that never consumed a turn are rejected commands,
                # not attempts, and say nothing about the trace curve.
                if rec.get("attempted") is False:
                    continue
                d = rec.get("trace_after")
                b = rec.get("trace_before")
                if isinstance(d, int) and isinstance(b, int) and d >= b:
                    self.increments.append(d - b)

    def observe(self, rec):
        """Append one attempt. `rec` wants hack, depth, detect_chance,
        trace_before, trace_after, success."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if (
            rec.get("attempted") is not False
            and rec.get("trace_after") is not None
            and rec.get("trace_before") is not None
        ):
            self.increments.append(rec["trace_after"] - rec["trace_before"])

    @property
    def samples(self):
        return len(self.increments)

    def worst(self):
        return max(self.increments) if self.increments else None

    def pad(self):
        """Buffer above the worst observed jump, shrinking as the tail gets
        better characterised: wide when it is a guess, narrow once measured."""
        if not self.increments:
            return None
        return max(PAD_FLOOR, int(round(PAD_SCALE / (self.samples**0.5))))

    def headroom(self):
        """How much trace to leave unspent before stopping."""
        w = self.worst()
        if w is None:
            return None
        return min(MAX_HEADROOM, w + self.pad())

    def should_continue(self, trace):
        """Is one more attempt safe at this trace level?

        Returns (bool, reason) so the caller can log why it stopped -- a policy
        that quits early for the wrong reason is otherwise indistinguishable
        from one that quits for the right one.
        """
        if trace is None:
            return False, "trace unreadable"
        if trace >= 100:
            return False, "already fully traced"
        if self.samples == 0:
            if trace < COLD_START_CEILING:
                return True, "cold start: below the %d%% ceiling" % COLD_START_CEILING
            return False, (
                "cold start: %d%% with no increment data -- "
                "stopping rather than guessing" % trace
            )
        room = self.headroom()
        # Never let a thin sample authorise something cold start would refuse.
        if self.samples < MIN_SAMPLES_TO_LOOSEN and trace >= COLD_START_CEILING:
            return False, (
                "%d%% with only %d samples -- holding the cold-start "
                "ceiling of %d%% until the tail is sampled"
                % (trace, self.samples, COLD_START_CEILING)
            )
        if trace + room >= 100:
            return False, (
                "%d%% + worst jump %d + pad %d (= %d) reaches 100"
                % (trace, self.worst(), self.pad(), room)
            )
        return True, (
            "%d%%, headroom %d (worst %d + pad %d over %d samples)"
            % (trace, room, self.worst(), self.pad(), self.samples)
        )

    def summary(self):
        if not self.increments:
            return "no observations yet (cold start ceiling %d%%)" % COLD_START_CEILING
        nz = [i for i in self.increments if i > 0]
        return "%d attempts, %d moved the trace, worst jump %d, mean move %.1f" % (
            self.samples,
            len(nz),
            max(self.increments),
            sum(nz) / len(nz) if nz else 0.0,
        )


if __name__ == "__main__":
    m = TraceModel()
    print(m.summary())
    for t in (0, 10, 24, 30, 60, 90):
        ok, why = m.should_continue(t)
        print("  trace %3d%% -> %-5s %s" % (t, "hack" if ok else "STOP", why))
