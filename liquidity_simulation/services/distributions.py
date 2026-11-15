# -*- coding: utf-8 -*-
"""Sampling distributions for the Monte Carlo engine.

Pure stdlib on purpose: ``random.Random`` and ``statistics`` only. Core Odoo
does not ship numpy, and adding it for one calculation would widen the install
surface and need compatibility work at every upgrade. See design.md decision 4.

Every distribution answers the same question, in the same shape::

    dist.sample(rng) -> (day_offset, amount_factor)

``day_offset`` is a whole number of days from the item's own due date, so a
positive value means "paid late" and a negative value means "paid early".
``amount_factor`` is ``0.0`` or ``1.0`` for now -- partial payment is not
modelled in this phase, and a factor that is always one is honest about that.
``day_offset is None`` means the item never arrives inside the horizon, and the
caller must then add **nothing** for it -- not defer it to the last day.
"""
import math
import statistics


#: Conservative fallback when a partner has too few payments to have a
#: distribution. Deliberately wide: an unknown customer is assumed to pay
#: late and irregularly, never on time. Being wrong in this direction makes
#: the band too wide, which is visible; being wrong the other way makes it
#: too narrow, which is dangerous.
FALLBACK_PROFILE = {
    "mean_delay_days": 25.0,
    "stdev_delay_days": 30.0,
    "p_default": 0.05,
}

#: Default cap on a single observed delay, as a multiple of the observed
#: standard deviation. One 400-day payment must not make the band 400 days
#: wide (design.md risks table).
OUTLIER_CAP_STDEV_FACTOR = 3.0

#: Absolute floor for the cap. A partner whose stdev is 2 days should still
#: tolerate a 30-day delay without being clipped at 6.
OUTLIER_CAP_MIN_DAYS = 90


def outlier_cap_days(stdev_days, cap=None):
    """Return the delay cap to apply at sampling time.

    ``cap`` wins when given. Otherwise ``3 x stdev``, floored at 90 days.
    """
    if cap:
        return float(cap)
    return max(OUTLIER_CAP_MIN_DAYS, OUTLIER_CAP_STDEV_FACTOR * (stdev_days or 0.0))


class Distribution(object):
    """Base class. Subclasses implement ``sample``."""

    #: Whether this distribution carries any variance at all.
    is_deterministic = False

    def sample(self, rng):
        raise NotImplementedError

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<%s>" % self.__class__.__name__


class EmpiricalDist(Distribution):
    """Bootstrap from the delays actually observed for one partner.

    Sampling *with replacement* from the observed set preserves the
    asymmetry of real payment behaviour for free: a customer who pays late but
    never early produces no early payments, which a normal distribution
    centred on the mean delay would invent (design.md decision 3).

    The cap is applied **at sampling**, not by clipping the observation list.
    ``sample_size`` therefore keeps reporting how much history exists, even
    when the most extreme observation is capped when drawn.
    """

    def __init__(self, observations, cap_days=None):
        self.observations = [float(o) for o in observations]
        if not self.observations:
            raise ValueError("EmpiricalDist needs at least one observation")
        self.cap_days = cap_days

    @property
    def sample_size(self):
        return len(self.observations)

    def sample(self, rng):
        delay = rng.choice(self.observations)
        if self.cap_days is not None and delay > self.cap_days:
            delay = self.cap_days
        return int(round(delay)), 1.0

    def __repr__(self):  # pragma: no cover
        return "<EmpiricalDist n=%s cap=%s>" % (self.sample_size, self.cap_days)


class BernoulliDist(Distribution):
    """Does this item arrive at all?

    ``p_pay`` is the probability that it does. When it does not, the amount is
    **never** placed on any day in the horizon -- not pushed to the last day.
    Pushing it would pretend the money arrives, only later; the honest model
    of a default is that the money does not arrive.

    The delay, when the item *does* arrive, comes from ``delay_dist`` -- so the
    "will they pay" and "when will they pay" questions stay separate.
    """

    def __init__(self, p_pay, delay_dist=None, fixed_delay=0):
        self.p_pay = max(0.0, min(1.0, float(p_pay)))
        self.delay_dist = delay_dist
        self.fixed_delay = fixed_delay

    def sample(self, rng):
        # ``rng.random() < p_pay`` -- not ``>=``. With p_pay = 0.8 the first
        # form pays out on 20 % of the draws, which is the kind of inversion
        # that produces a plausible-looking band with the risk backwards.
        if rng.random() < self.p_pay:
            if self.delay_dist is not None:
                return self.delay_dist.sample(rng)
            return self.fixed_delay, 1.0
        return None, 0.0

    def __repr__(self):  # pragma: no cover
        return "<BernoulliDist p_pay=%.3f>" % self.p_pay


class FixedDist(Distribution):
    """A statutory item: same day, same amount, every iteration.

    Used for tax, payroll and loan instalments -- things whose date is set by
    law or contract, not by behaviour. Contributes exactly zero to the
    variance, which is what makes a simulation of only fixed items collapse to
    a line.
    """

    is_deterministic = True

    def __init__(self, day, amount_factor=1.0):
        self.day = int(day)
        self.amount_factor = float(amount_factor)

    def sample(self, rng):
        return self.day, self.amount_factor

    def __repr__(self):  # pragma: no cover
        return "<FixedDist day=%s>" % self.day


class NormalDist(Distribution):
    """Fallback only -- never the default.

    Used when a partner has fewer than five payments, and then with a
    deliberately **higher** standard deviation than the empirics would give,
    so the band errs wide rather than narrow (design.md decision 3).

    Negative delays are clipped to zero: a normal distribution centred on a
    positive mean still produces early payments that were never observed, and
    an early payment makes the cash position look better than it is.
    """

    def __init__(self, mean, stdev, cap_days=None, floor_at_zero=True):
        self.mean = float(mean)
        self.stdev = max(0.0, float(stdev))
        self.cap_days = cap_days
        self.floor_at_zero = floor_at_zero

    def sample(self, rng):
        delay = rng.gauss(self.mean, self.stdev)
        if self.floor_at_zero and delay < 0:
            delay = 0.0
        if self.cap_days is not None and delay > self.cap_days:
            delay = self.cap_days
        return int(round(delay)), 1.0

    def __repr__(self):  # pragma: no cover
        return "<NormalDist mean=%.1f stdev=%.1f>" % (self.mean, self.stdev)


def build_delay_dist(distribution, mean_delay_days, stdev_delay_days,
                     observations=None, cap_days=None):
    """Return the delay distribution a profile should sample from.

    ``distribution`` is the profile's stored selection (``empirical`` or
    ``fallback``). An ``empirical`` profile without observations falls back to
    the conservative normal -- a profile that claims empirics but carries no
    data is a data problem, and guessing is worse than widening.
    """
    if distribution == "empirical" and observations:
        return EmpiricalDist(observations, cap_days=cap_days)
    mean = mean_delay_days if mean_delay_days is not None else FALLBACK_PROFILE["mean_delay_days"]
    stdev = stdev_delay_days if stdev_delay_days is not None else FALLBACK_PROFILE["stdev_delay_days"]
    # The fallback must not be narrower than the generic fallback profile.
    stdev = max(stdev, FALLBACK_PROFILE["stdev_delay_days"])
    return NormalDist(mean, stdev, cap_days=cap_days)


def mean_and_stdev(values):
    """Mean and sample standard deviation of ``values``, 0.0 when undefined."""
    values = [float(v) for v in values]
    if not values:
        return 0.0, 0.0
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, 0.0
    return mean, statistics.stdev(values)


def percentiles(sorted_values, fractions=(0.10, 0.50, 0.90, 0.95, 0.99)):
    """Percentiles of an already sorted list, by nearest-rank.

    Nearest-rank rather than interpolation: it always returns a value that
    actually occurred in the simulation, which is what an amount in a curve
    should be. Interpolating between two iterations produces a number no
    scenario produced.
    """
    n = len(sorted_values)
    if not n:
        return [0.0 for _ in fractions]
    out = []
    for frac in fractions:
        # Nearest-rank, 1-based: ceil(frac * n), clamped into range.
        rank = math.ceil(frac * n)
        index = min(n - 1, max(0, rank - 1))
        out.append(sorted_values[index])
    return out
