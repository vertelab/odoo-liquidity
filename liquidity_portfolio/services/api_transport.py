# -*- coding: utf-8 -*-
"""Transport shared by the API drivers: one place for the failure modes.

Everything here exists because of a specific way external APIs misbehave when
you have two hundred customers instead of two:

**Rate limits are not errors.** A 429 means "ask again shortly", not "this
customer is broken". Without this distinction, a portfolio of any size shows a
wall of red rows the moment the quota is reached -- and the reader learns to
ignore red, which is worse than showing nothing.

**Expired tokens are not "not answering".** A token that expired at midnight
makes every call fail with 401. If that is rendered as "the source does not
answer", the reader goes looking for a network problem that does not exist. It
has its own outcome so the row can say "the connection needs renewing".

**Backoff is per source and bounded.** We spread the calls; we do not retry
forever. A retry loop that keeps a failing customer at the front of the queue
turns one slow API into a slow portfolio.

Nothing in this module raises to the caller. Every failure is classified.
"""
import logging
import random
import time

import requests

_logger = logging.getLogger(__name__)

# Outcomes. Same vocabulary as rpc_client, extended with the two that only
# token-authenticated APIs can produce, plus the one that is not a failure.
OK = "ok"
AUTH_EXPIRED = "auth_expired"     # token expired or revoked; needs renewing
RATE_LIMITED = "rate_limited"     # quota reached; ask later, do not alarm
UNREACHABLE = "unreachable"
API_ERROR = "api_error"
PARTIAL = "partial"               # answered for some sections, not all

DEFAULT_TIMEOUT = 20

#: How many times one call is retried after a 429 before it gives up. Small on
#: purpose: the answer to a rate limit is to wait, and the portfolio can show
#: "delayed" honestly for one cycle instead of stalling the whole view.
MAX_RATE_LIMIT_RETRIES = 2

#: Base backoff in seconds; doubled per retry, with jitter so two hundred
#: customers that hit the limit at the same moment do not retry in lockstep.
BACKOFF_BASE = 1.0

#: How many sources may be in flight at once. The quota is shared by all of a
#: customer's calls, so the limit has to be enforced where the whole portfolio
#: is fetched, not per call.
MAX_PARALLEL_SOURCES = 4


class ApiResult(object):
    """Outcome of one API call. Never an exception."""

    __slots__ = ("outcome", "value", "message", "status_code", "retry_after")

    def __init__(self, outcome, value=None, message="", status_code=None,
                 retry_after=None):
        self.outcome = outcome
        self.value = value
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after

    @property
    def ok(self):
        return self.outcome == OK

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<ApiResult %s%s>" % (
            self.outcome,
            " %s" % self.message if self.message else "",
        )


def _classify_status(response):
    """Map an HTTP status to one of our outcomes.

    The distinction that matters: 401 is *our* credential problem, 429 is
    *temporary*, and 5xx is *theirs*. All three are failures, but only one of
    them is worth telling the user about.
    """
    status = response.status_code
    if status in (401, 403):
        return AUTH_EXPIRED, "the connection needs renewing"
    if status == 429:
        return RATE_LIMITED, "the source asked us to wait"
    if 500 <= status < 600:
        return API_ERROR, "the source reported a server error (HTTP %s)" % status
    if 400 <= status < 500:
        return API_ERROR, "HTTP %s" % status
    return OK, ""


def request_json(session, method, url, messages=None, params=None, json_body=None,
                 timeout=DEFAULT_TIMEOUT, max_retries=MAX_RATE_LIMIT_RETRIES,
                 sleep=time.sleep):
    """One HTTP call, with rate-limit handling. Returns ApiResult.

    ``sleep`` is injectable so tests do not actually wait. The retry is bounded
    and only ever triggered by 429: a 500 is not retried, because a source that
    is genuinely broken should be reported, not hammered.
    """
    messages = messages or {}
    last = None
    for attempt in range(max_retries + 1):
        try:
            response = session.request(
                method, url, params=params, json=json_body, timeout=timeout
            )
        except requests.exceptions.Timeout:
            return ApiResult(
                UNREACHABLE,
                message=messages.get(
                    "timeout",
                    "no answer within %s seconds" % timeout,
                ),
            )
        except requests.exceptions.RequestException as exc:
            return ApiResult(UNREACHABLE, message=str(exc)[:200])

        outcome, message = _classify_status(response)

        if outcome == RATE_LIMITED:
            retry_after = _retry_after_seconds(response)
            last = ApiResult(
                RATE_LIMITED,
                message=message,
                status_code=response.status_code,
                retry_after=retry_after,
            )
            if attempt < max_retries:
                # Jitter prevents two hundred sources from retrying in
                # lockstep and hitting the quota again at the same instant.
                delay = (retry_after or BACKOFF_BASE * (2 ** attempt))
                delay += random.uniform(0, 0.5)
                _logger.info(
                    "Rate limited on %s, retrying in %.1fs (attempt %s/%s)",
                    url, delay, attempt + 1, max_retries,
                )
                sleep(delay)
                continue
            return last

        if outcome != OK:
            return ApiResult(
                outcome,
                message=message,
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError:
            return ApiResult(
                API_ERROR,
                message="HTTP %s, not JSON" % response.status_code,
                status_code=response.status_code,
            )

        return ApiResult(OK, value=body, status_code=response.status_code)

    return last or ApiResult(API_ERROR, message="no attempt was made")


def _retry_after_seconds(response):
    """Honour Retry-After when the server sends one. Seconds, not dates."""
    header = response.headers.get("Retry-After")
    if not header:
        return None
    try:
        return float(header)
    except (TypeError, ValueError):
        # A date-formatted Retry-After is legal but rare; fall back to backoff.
        return None


class RateLimiter(object):
    """A minimum interval between calls to one source.

    The point is not politeness, it is correctness: a portfolio that fires two
    hundred calls at once gets two hundred rate-limit answers, which then all
    retry at once. Spreading the calls is cheaper than recovering from the
    quota being hit.
    """

    def __init__(self, min_interval=0.2, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._last = {}

    def wait(self, key):
        """Block until ``key`` may be called again."""
        now = self._clock()
        last = self._last.get(key)
        if last is not None:
            elapsed = now - last
            remaining = self.min_interval - elapsed
            if remaining > 0:
                self._sleep(remaining)
        self._last[key] = self._clock()
