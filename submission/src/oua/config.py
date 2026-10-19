"""All tunables in one place. Everything here is configurable from the CLI (see cli.py)."""
from dataclasses import dataclass, field
from datetime import timedelta


@dataclass
class Config:
    # --- budgets (assignment section 7) -------------------------------------------------
    max_model_calls: int = 8
    max_tool_calls: int = 20
    latency_target_s: float = 60.0      # publish within this of the trigger
    latency_fail_s: float = 300.0       # anything later is a failure
    decision_deadline_s: float = 20.0   # internal soft deadline: past this we stop optional work

    # --- trigger freshness -------------------------------------------------------------
    stale_after_s: float = 600.0        # an app_open older than this (phone-side buffer age) is not a live session

    # --- dependencies ------------------------------------------------------------------
    model_timeout_s: float = 2.0
    model_retries: int = 1              # extra attempts after a malformed answer
    store_timeout_s: float = 1.0
    store_retries: int = 1
    breaker_open_s: float = 60.0        # model circuit breaker stays open this long (virtual seconds)
    model_policy: str = 'on_new_goal'   # on_new_goal | never
    live_call_cap: int = 0              # max real provider calls per run (0 = unlimited); free tiers are rate-limited
    workers: int = 8
    cache_max_age_s: float = 6 * 3600.0  # cached facts older than this are not used for anything but a generic page

    # --- data freshness rules ----------------------------------------------------------
    profile_max_age_for_consent_s: float = 72 * 3600.0
    page_ttl: timedelta = timedelta(minutes=120)
    page_availability_margin: timedelta = timedelta(minutes=120)  # a title must stay available for the page's life
    observation_days: int = 14          # simulator horizon after the replay day

    # --- proportionality ---------------------------------------------------------------
    max_messages_per_goal: int = 3
    default_send_hour_ist: int = 19
    min_send_delay: timedelta = timedelta(minutes=30)

    # --- load shedding -----------------------------------------------------------------
    shed_backlog_s: float = 10.0        # when queue wait exceeds this, optional model calls are skipped
