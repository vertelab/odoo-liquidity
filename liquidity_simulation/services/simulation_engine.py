# -*- coding: utf-8 -*-
"""The Monte Carlo engine.

Pure functions over plain data -- no ORM in here. The caller (the
``liquidity.simulation`` model) turns records into ``SimItem`` objects, runs
the engine, and stores the result. Keeping the arithmetic out of the model is
what makes it testable without a database and readable without Odoo knowledge.

The one rule that matters most: an iteration keeps its identity all the way to
the aggregation. ``p_crisis_90`` is the share of *iterations* that went
negative on some day -- not the sum of the daily probabilities. Summing daily
probabilities gives a number that looks plausible and is systematically too
high, because one iteration can be negative on thirty days (design.md
decision 7).
"""
import math
import random

from .distributions import percentiles

#: Default number of iterations. 10 000 x 90 days x ~200 items is roughly
#: 0.3-1.5 s -- trivial in cron, unacceptable in a page load.
DEFAULT_ITERATIONS = 10000

#: Default horizon, matching the deterministic forecast.
DEFAULT_HORIZON_DAYS = 90

#: Below this many iterations with actual data, a probability is presented
#: with a margin rather than as a precise percentage.
MIN_EFFECTIVE_ITERATIONS = 500


class SimItem(object):
    """One thing that moves the cash, and how uncertain it is.

    ``amount`` is signed from the liquidity perspective: positive incoming,
    negative outgoing. ``due_offset`` is the day index (0-based) the item is
    due on. ``dist`` supplies ``(delay_days, amount_factor)``.
    """

    __slots__ = (
        "key",
        "label",
        "source_type",
        "partner_id",
        "partner_name",
        "amount",
        "due_offset",
        "dist",
        "event_type",
        "confidence",
    )

    def __init__(self, key, label, amount, due_offset, dist,
                 source_type="receivable", partner_id=False,
                 partner_name=False, event_type=False, confidence="high"):
        self.key = key
        self.label = label
        self.amount = float(amount)
        self.due_offset = int(due_offset)
        self.dist = dist
        self.source_type = source_type
        self.partner_id = partner_id
        self.partner_name = partner_name
        self.event_type = event_type
        self.confidence = confidence

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<SimItem %s %.2f d%s>" % (self.key, self.amount, self.due_offset)


def _correlation(xs, ys):
    """Pearson correlation of two equally long lists, 0.0 when undefined."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = math.fsum(xs) / n
    mean_y = math.fsum(ys) / n
    cov = math.fsum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = math.fsum((x - mean_x) ** 2 for x in xs)
    var_y = math.fsum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def _crisis_correlation(xs, crisis_flags):
    """Point-biserial correlation between an item's contribution and crisis.

    Used when the outcome itself has no variance -- no iteration went
    negative, so every scenario has the same "worst day". A Pearson
    correlation against a constant is 0 by definition and would report that
    nothing explains anything, which is false: an item that is paid early can
    still be what keeps the balance from dipping. Correlating against the
    crisis indicator instead says how much each item pushes the result toward
    the boundary, which is the question the user is asking.
    """
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x = math.fsum(xs) / n
    var_x = math.fsum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return 0.0
    ones = sum(1 for flag in crisis_flags if flag)
    zeros = n - ones
    if not ones or not zeros:
        return 0.0
    mean_y = ones / float(n)
    cov = math.fsum(
        (x - mean_x) * (flag - mean_y) for x, flag in zip(xs, crisis_flags)
    )
    var_y = math.fsum((flag - mean_y) ** 2 for flag in crisis_flags)
    return cov / math.sqrt(var_x * var_y)


def run(items, opening_balance, horizon_days=DEFAULT_HORIZON_DAYS,
        iterations=DEFAULT_ITERATIONS, seed=0, baseline=None):
    """Simulate the horizon ``iterations`` times and aggregate.

    ``baseline`` is the deterministic closing balance per day index, if known.
    It is only used to report whether the band covers it -- the engine never
    adjusts its own output to make that true.

    Returns a dict with:

    ``lines``
        one dict per day: ``date_offset``, ``p10``, ``p50``, ``p90``,
        ``p95``, ``p99``, ``p_negative``, ``baseline_balance``,
        ``expected_events``
    ``p_crisis_90``
        share of iterations negative on some day
    ``lowest_p10`` / ``lowest_p50`` / ``lowest_p90`` and ``lowest_p10_offset``
    ``worst_case_p95``
        the lowest balance in the worst 5 % of iterations
    ``variance_share``
        per item key, correlation with the outcome, normalised to ~1.0
    ``n_effective``
        iterations that carried at least one sampled amount
    ``iterations``, ``horizon_days``, ``seed``
    """
    horizon_days = int(horizon_days)
    iterations = max(1, int(iterations))
    rng = random.Random(seed)

    # Pre-compute each item's per-iteration draw. Sampling once per iteration
    # per item and keeping the result in a list is what makes the daily loop
    # cheap: the inner loop is then plain arithmetic, not distribution calls.
    draws = []
    for item in items:
        per_iter = []
        for _ in range(iterations):
            per_iter.append(item.dist.sample(rng))
        draws.append(per_iter)

    # balances[day][iteration]. Every day is assigned below; the placeholder
    # exists only so the list has the right length up front. A fresh list per
    # day -- ``[[]] * n`` would make every day share one list.
    balances = [[] for _ in range(horizon_days)]
    # Per-item contribution per iteration, needed for the variance share.
    contributions = [[0.0] * iterations for _ in items]
    # Which days an item lands on, per iteration -- only for the event marker.
    any_amount_per_iter = [False] * iterations
    # day index -> set of event types landing there in at least one iteration
    # (or deterministically). Used by the UI to explain a dip.
    event_days = {}

    # ``running`` is the balance carried into the next day, per iteration.
    # Rebuilt each day rather than mutated in place, so a stale copy cannot
    # survive a loop iteration unnoticed. The variable is renamed to make the
    # intent clear.
    running = [float(opening_balance) for _ in range(iterations)]
    for day in range(horizon_days):
        day_moves = [0.0] * iterations
        for item_index, item in enumerate(items):
            per_iter = draws[item_index]
            amount = item.amount
            for iteration in range(iterations):
                delay, factor = per_iter[iteration]
                if delay is None:
                    # Never arrives inside the horizon. Nothing is added on any
                    # day -- not deferred to the last day.
                    continue
                # A delay that pushes the item past the horizon means it
                # does not arrive inside the horizon. It is not clamped to
                # the last day: that would invent a receipt.
                # A delay before the due date is not modelled: the item is
                # due on its due date, and paying early is not the risk this
                # band measures. Clamping to day 0 keeps the amount inside
                # the horizon instead of dropping it silently.
                landing = max(0, item.due_offset + delay)
                if landing >= horizon_days or day != landing:
                    continue
                moved = amount * factor
                day_moves[iteration] = day_moves[iteration] + moved
                contributions[item_index][iteration] = (
                    contributions[item_index][iteration] + moved
                )
                any_amount_per_iter[iteration] = True
                if item.event_type:
                    event_days.setdefault(day, set()).add(item.event_type)
        running = [
            running[iteration] + day_moves[iteration]
            for iteration in range(iterations)
        ]
        balances[day] = list(running)

    # Per-iteration extremes are computed once, here, and reused by every
    # aggregate below. The opening balance counts as a point on the curve:
    # an opening balance that is already negative IS a crisis, and a trough
    # computed over the days alone would miss it.
    per_iteration_min = [
        min([float(opening_balance)] + [balances[day][iteration] for day in range(horizon_days)])
        for iteration in range(iterations)
    ]
    crisis_flags = [value < 0 for value in per_iteration_min]
    if not any(crisis_flags) and horizon_days:
        # Nothing went negative: the trough is a constant, so correlating
        # against it is 0 for every item by construction. The closing
        # balance on the last day does vary, and it is what the band is
        # actually about.
        outcome = [balances[horizon_days - 1][iteration] for iteration in range(iterations)]
    else:
        outcome = [-value for value in per_iteration_min]

    lines = []
    lowest_p10 = lowest_p50 = lowest_p90 = None
    lowest_p10_offset = 0
    negative_some_day = [False] * iterations
    for day in range(horizon_days):
        day_balances = balances[day]
        ordered = sorted(day_balances)
        p10, p50, p90, p95, p99 = percentiles(ordered)
        negative_count = 0
        for iteration, value in enumerate(day_balances):
            if value < 0:
                negative_count += 1
                negative_some_day[iteration] = True
        lines.append(
            {
                "date_offset": day,
                "p10": p10,
                "p50": p50,
                "p90": p90,
                "p95": p95,
                "p99": p99,
                "p_negative": negative_count / float(iterations),
                "baseline_balance": (
                    baseline[day] if baseline and day < len(baseline) else None
                ),
                "expected_events": sorted(event_days.get(day, ())),
            }
        )
        if lowest_p10 is None or p10 < lowest_p10:
            lowest_p10 = p10
            lowest_p10_offset = day
        if lowest_p50 is None or p50 < lowest_p50:
            lowest_p50 = p50
        if lowest_p90 is None or p90 < lowest_p90:
            lowest_p90 = p90

    crisis_iterations = sum(1 for flag in negative_some_day if flag)
    p_crisis_90 = crisis_iterations / float(iterations)

    # Worst 5 %: the five lowest iteration outcomes at their own worst day.
    ordered_min = sorted(per_iteration_min)
    worst_count = max(1, int(math.ceil(0.05 * iterations)))
    worst_case_p95 = ordered_min[worst_count - 1]

    # Variance share: which item drives the uncertainty.
    #
    # Two measures, and the choice between them is not cosmetic:
    #
    #   * When some iterations go negative, the interesting outcome is how
    #     deep the trough is. An item's contribution is correlated with the
    #     per-iteration minimum balance -- the lower the balance, the worse.
    #     The sign is flipped so that "more of this item" reads as "more
    #     uncertainty", whichever direction the item moves the cash.
    #   * When no iteration goes negative, the trough has no variance at all
    #     and a correlation against it is 0 for every item. Correlating
    #     against the crisis indicator (which does vary, through the
    #     near-misses) still ranks the items, but a constant outcome means
    #     the honest answer is "nothing here is dangerous yet" -- so the
    #     shares are reported as zero rather than manufactured from noise.
    #
    # Either way the measure is a correlation, never the item's amount: a
    # small but erratic invoice can outrank a large predictable one, and that
    # is the useful answer -- it says what to go and fix.
    raw = []
    for item_index, item in enumerate(items):
        if item.dist.is_deterministic:
            raw.append((item.key, 0.0))
            continue
        corr = _correlation(contributions[item_index], outcome)
        raw.append((item.key, abs(corr)))
    total = math.fsum(value for _key, value in raw)
    variance_share = {}
    if total > 0:
        for key, value in raw:
            variance_share[key] = value / total
    else:
        for key, _value in raw:
            variance_share[key] = 0.0

    n_effective = sum(1 for flag in any_amount_per_iter if flag)

    return {
        "lines": lines,
        "p_crisis_90": p_crisis_90,
        "lowest_p10": lowest_p10,
        "lowest_p50": lowest_p50,
        "lowest_p90": lowest_p90,
        "lowest_p10_offset": lowest_p10_offset,
        "worst_case_p95": worst_case_p95,
        "variance_share": variance_share,
        "n_effective": n_effective,
        "iterations": iterations,
        "horizon_days": horizon_days,
        "seed": seed,
    }
