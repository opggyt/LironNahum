#!/usr/bin/env python3
"""Roblox Ads Manager -> Telegram status bot.

Reads the campaign list from the Roblox Open Cloud Ads API (read-only) and
sends a Hebrew summary to Telegram. Meant to run every 3 hours from GitHub
Actions, but works the same from a local shell.

Secrets come only from environment variables:
    ROBLOX_API_KEY      Open Cloud API key with the ad.campaign:read operation
    TELEGRAM_BOT_TOKEN  token from @BotFather
    TELEGRAM_CHAT_ID    chat that receives the report
Optional:
    ROBLOX_GROUP_ID     list a group's ads account instead of the personal one
    STATE_FILE          where the snapshot is kept (default: state/snapshot.json)

Usage:
    python roblox_ads_bot.py                  fetch, send, save snapshot
    python roblox_ads_bot.py --dry-run        fetch, print, save nothing
    python roblox_ads_bot.py --demo --dry-run print a report built from sample data
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = BASE_DIR / "sample_data"
DEFAULT_STATE_FILE = BASE_DIR / "state" / "snapshot.json"

ADS_API_BASE = "https://apis.roblox.com/ads-management/v1"
TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_LIMIT = 4096  # Telegram's hard limit per message

REPORT_TZ = ZoneInfo("Asia/Jerusalem")

REQUEST_TIMEOUT = 30  # seconds
MAX_ATTEMPTS = 5
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
BACKOFF_BASE = 2.0  # 2s, 4s, 8s, 16s (+ jitter)
BACKOFF_CAP = 60.0
RETRY_AFTER_CAP = 120.0
PAGE_SIZE = 100  # documented maximum for GET /campaigns
MAX_PAGES = 200

DELIVERY_EMOJI = {
    "SERVING": "🟢",
    "IN_REVIEW": "🟡",
    "REJECTED": "🔴",
    "NOT_SERVING": "🔴",
}
ALERT_STATUSES = frozenset({"REJECTED", "NOT_SERVING"})
# A campaign with one of these reasons is over for good, so it is alerted on
# once (when it stops) and then left out of the recurring list.
TERMINAL_REASONS = frozenset({"COMPLETED", "EXPIRED", "CANCELLED"})
DELIVERY_ORDER = {"REJECTED": 0, "NOT_SERVING": 1, "IN_REVIEW": 2, "SERVING": 3}

# Hebrew gloss for the documented deliveryStatusReasons values. The raw code is
# always shown next to it, so an unknown or changed code still gets through.
REASON_HE = {
    "SCHEDULED": "מתוזמן, עוד לא התחיל",
    "AUTO_PAUSED": "הושהה אוטומטית",
    "PAUSED": "מושהה",
    "INACTIVE": "לא פעיל",
    "COMPLETED": "הסתיים",
    "EXPIRED": "פג תוקף",
    "CANCELLED": "בוטל",
    "MODERATED": "נחסם במודרציה",
    "CLICK_BAIT": "סומן כקליקבייט",
    "GAME_FILTERED": "המשחק סונן",
    "PRIVATE": "המשחק פרטי",
    "PLACE_JOIN_RESTRICTED": "הכניסה למשחק מוגבלת",
    "LEARNING": "בשלב למידה",
}

BUDGET_SUFFIX = {"DAILY": "ליום", "LIFETIME": "לכל חיי הקמפיין"}

# Unicode isolates: keep "$30.00 -> $50.00" and "+$5.00" reading left to right
# inside a Hebrew (right-to-left) line.
LRI, PDI = "⁦", "⁩"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Metrics:
    """Performance totals for one campaign. All fields are optional.

    Roblox does not expose these through any API-key endpoint yet, so real runs
    never fill this in (see fetch_campaign_metrics). The rest of the bot is
    already able to show them, which is what the tests and --demo-metrics use.
    """

    spend_micros: int | None = None  # total spend since the campaign started
    spend_today_micros: int | None = None  # needed for daily-budget utilization
    impressions: int | None = None
    clicks: int | None = None
    plays: int | None = None


@dataclass
class Campaign:
    id: str
    name: str
    status: str
    delivery_status: str
    reasons: list[str] = field(default_factory=list)
    budget_type: str | None = None
    budget_micros: int | None = None
    scheduled_budget_micros: int | None = None
    scheduled_effective_time: str | None = None
    metrics: Metrics | None = None


@dataclass
class Snapshot:
    generated_at: datetime | None
    campaigns: dict[str, dict]


class RobloxApiError(Exception):
    def __init__(self, status: int | None, code: str, message: str, attempts: int):
        super().__init__(f"Roblox API error {status} {code} {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.attempts = attempts


class TelegramError(Exception):
    pass


# --------------------------------------------------------------------------
# Money and number helpers
# --------------------------------------------------------------------------


def parse_micros(value) -> int | None:
    """Parse a micro-USD amount ("5000000" -> 5000000). None if it isn't one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    return int(number)


def micros_to_usd(micros: int) -> Decimal:
    """Exact conversion: 1,000,000 micro-USD = $1. No floats involved."""
    return Decimal(micros).scaleb(-6)


def _money(usd: Decimal, places: str, signed: bool) -> str:
    usd = usd.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    sign = "-" if usd < 0 else ("+" if signed and usd > 0 else "")
    digits = len(places) - 2
    return f"{sign}${abs(usd):,.{digits}f}"


def fmt_usd(micros: int | None, signed: bool = False) -> str:
    if micros is None:
        return "לא ידוע"
    return _money(micros_to_usd(micros), "0.01", signed)


def fmt_int(value: int, signed: bool = False) -> str:
    return f"{value:+,}" if signed else f"{value:,}"


def ltr(text: str) -> str:
    return f"{LRI}{text}{PDI}"


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def ctr_percent(clicks: int | None, impressions: int | None) -> Decimal | None:
    if clicks is None or not impressions:
        return None
    return (Decimal(clicks) * 100 / Decimal(impressions)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def fmt_cost_per_play(spend_micros: int | None, plays: int | None) -> str | None:
    if spend_micros is None or not plays:
        return None
    cost = micros_to_usd(spend_micros) / Decimal(plays)
    # Sub-dollar costs need the third digit to be useful ($0.069 vs $0.07).
    return _money(cost, "0.001" if abs(cost) < 1 else "0.01", signed=False)


def utilization(campaign: Campaign) -> tuple[Decimal, str] | None:
    """Budget used, as (percent, label). None when it can't be computed."""
    metrics = campaign.metrics
    if metrics is None or not campaign.budget_micros or campaign.budget_micros <= 0:
        return None
    if campaign.budget_type == "LIFETIME":
        spent, label = metrics.spend_micros, "מהתקציב הכולל"
    elif campaign.budget_type == "DAILY":
        spent, label = metrics.spend_today_micros, "מהתקציב היומי"
    else:
        return None
    if spent is None:
        return None
    percent = (Decimal(spent) * 100 / Decimal(campaign.budget_micros)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP
    )
    return percent, label


# --------------------------------------------------------------------------
# HTTP with retry
# --------------------------------------------------------------------------


def _retry_after_seconds(resp) -> float | None:
    header = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            pass
    try:  # Telegram puts it in the body: {"parameters": {"retry_after": 5}}
        value = resp.json()["parameters"]["retry_after"]
        return max(0.0, float(value))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _retry_delay(attempt: int, resp) -> float:
    if resp is not None:
        retry_after = _retry_after_seconds(resp)
        if retry_after is not None:
            return min(retry_after, RETRY_AFTER_CAP)
    return min(BACKOFF_BASE * 2 ** (attempt - 1), BACKOFF_CAP) + random.uniform(0, 1)


def request_with_retry(session, method: str, url: str, *, sleep=time.sleep, **kwargs):
    """Send a request, retrying on 429 / 5xx / network errors with backoff.

    Returns (response, attempts). The response may still be an error; the
    caller decides. Network errors that outlast the retries are re-raised.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = None
        try:
            resp = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == MAX_ATTEMPTS:
                raise
        else:
            if resp.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                return resp, attempt
        sleep(_retry_delay(attempt, resp))
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Roblox Ads API (read-only)
# --------------------------------------------------------------------------


def _roblox_error(resp, attempts: int) -> RobloxApiError:
    code = message = ""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            code = str(errors[0].get("code") or "")
            message = str(errors[0].get("message") or "")
        code = code or str(body.get("code") or "")
        message = message or str(
            body.get("detail") or body.get("message") or body.get("title") or ""
        )
    return RobloxApiError(resp.status_code, code, message[:200], attempts)


def list_campaigns(session, group_id: str | None = None, *, sleep=time.sleep) -> list[dict]:
    """GET /ads-management/v1/campaigns, following nextPageToken to the end."""
    url = f"{ADS_API_BASE}/campaigns"
    campaigns: list[dict] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(MAX_PAGES):
        params: dict[str, object] = {"maxPageSize": PAGE_SIZE}
        if group_id:
            params["groupId"] = group_id
        if page_token:
            params["pageToken"] = page_token
        try:
            resp, attempts = request_with_retry(session, "GET", url, params=params, sleep=sleep)
        except requests.RequestException as exc:
            raise RobloxApiError(None, "NETWORK_ERROR", type(exc).__name__, MAX_ATTEMPTS) from None
        if resp.status_code != 200:
            raise _roblox_error(resp, attempts)
        try:
            data = resp.json()
        except ValueError:
            raise RobloxApiError(200, "BAD_RESPONSE", "response is not JSON", attempts) from None
        if not isinstance(data, dict):
            raise RobloxApiError(200, "BAD_RESPONSE", "unexpected response shape", attempts)
        campaigns.extend(c for c in (data.get("campaigns") or []) if isinstance(c, dict))
        page_token = data.get("nextPageToken") or None
        if not page_token:
            return campaigns
        if page_token in seen_tokens:
            raise RobloxApiError(200, "BAD_RESPONSE", "repeated page token", attempts)
        seen_tokens.add(page_token)
    raise RobloxApiError(200, "BAD_RESPONSE", "too many pages", 1)


def fetch_campaign_metrics(session, campaigns: list[Campaign]) -> dict[str, Metrics]:
    """Per-campaign spend / impressions / clicks / plays, keyed by campaign id.

    Returns nothing for now, on purpose. As of 2026-11-09 Roblox has no
    API-key endpoint for campaign performance:
      * Campaign objects in the Ads API carry no metrics.
      * The Analytics Query API only has publisher-side ad metrics
        (AdsPublisherReporting*) and no campaign dimension.
    Roblox has said campaign metrics will come through the Analytics API. When
    they do, fill this function in (and add the scope it needs to the key);
    the report, the deltas and the snapshot already handle a Metrics value.
    """
    return {}


def parse_campaign(raw: dict) -> Campaign:
    budget = raw.get("budget") if isinstance(raw.get("budget"), dict) else {}
    reasons = raw.get("deliveryStatusReasons")
    return Campaign(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or "(ללא שם)"),
        status=str(raw.get("status") or "UNKNOWN"),
        delivery_status=str(raw.get("deliveryStatus") or "UNKNOWN"),
        reasons=[str(r) for r in reasons] if isinstance(reasons, list) else [],
        budget_type=str(budget["type"]) if budget.get("type") else None,
        budget_micros=parse_micros(budget.get("amountMicros")),
        scheduled_budget_micros=parse_micros(budget.get("scheduledAmountMicros")),
        scheduled_effective_time=budget.get("scheduledEffectiveTime") or None,
    )


def metrics_from_dict(data) -> Metrics | None:
    if not isinstance(data, dict):
        return None
    return Metrics(
        spend_micros=parse_micros(data.get("spendMicros")),
        spend_today_micros=parse_micros(data.get("spendTodayMicros")),
        impressions=parse_micros(data.get("impressions")),
        clicks=parse_micros(data.get("clicks")),
        plays=parse_micros(data.get("plays")),
    )


def metrics_to_dict(metrics: Metrics) -> dict:
    def text(value):
        return None if value is None else str(value)

    data = {
        "spendMicros": text(metrics.spend_micros),
        "spendTodayMicros": text(metrics.spend_today_micros),
        "impressions": metrics.impressions,
        "clicks": metrics.clicks,
        "plays": metrics.plays,
    }
    return {key: value for key, value in data.items() if value is not None}


# --------------------------------------------------------------------------
# Snapshot (state between runs)
# --------------------------------------------------------------------------


def load_state(path: Path) -> Snapshot | None:
    """Previous snapshot, or None on the first run or if the file is unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring unreadable state file ({type(exc).__name__})", file=sys.stderr)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("campaigns"), dict):
        print("warning: ignoring state file with unexpected shape", file=sys.stderr)
        return None
    generated_at = None
    try:
        generated_at = datetime.fromisoformat(str(data.get("generatedAt")))
    except ValueError:
        pass
    campaigns = {str(k): v for k, v in data["campaigns"].items() if isinstance(v, dict)}
    return Snapshot(generated_at=generated_at, campaigns=campaigns)


def build_state(campaigns: list[Campaign], now: datetime) -> dict:
    entries = {}
    for campaign in campaigns:
        entry = {
            "name": campaign.name,
            "status": campaign.status,
            "deliveryStatus": campaign.delivery_status,
            "deliveryStatusReasons": campaign.reasons,
            "budgetType": campaign.budget_type,
            "budgetMicros": None if campaign.budget_micros is None else str(campaign.budget_micros),
        }
        if campaign.metrics is not None:
            entry["metrics"] = metrics_to_dict(campaign.metrics)
        entries[campaign.id] = entry
    return {
        "version": 1,
        "generatedAt": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "campaigns": entries,
    }


def save_state(path: Path, state: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)  # atomic: never leaves a half-written snapshot


# --------------------------------------------------------------------------
# Report building
# --------------------------------------------------------------------------


def is_reportable(campaign: Campaign) -> bool:
    """Active campaigns: status ACTIVE or delivery SERVING, and not finished."""
    if set(campaign.reasons) & TERMINAL_REASONS:
        return False
    return campaign.status == "ACTIVE" or campaign.delivery_status == "SERVING"


def fmt_reasons(reasons: list[str]) -> str:
    parts = []
    for reason in reasons:
        gloss = REASON_HE.get(reason)
        parts.append(f"{esc(reason)} ({gloss})" if gloss else esc(reason))
    return ", ".join(parts)


def fmt_time_il(moment: datetime, with_date: bool = True) -> str:
    local = moment.astimezone(REPORT_TZ)
    return local.strftime("%d/%m/%Y %H:%M" if with_date else "%H:%M")


def find_alerts(campaigns: list[Campaign], previous: Snapshot | None) -> list[tuple[Campaign, str | None]]:
    """Campaigns that moved to REJECTED / NOT_SERVING since the last snapshot."""
    if previous is None:
        return []
    alerts = []
    for campaign in campaigns:
        if campaign.delivery_status not in ALERT_STATUSES:
            continue
        old = previous.campaigns.get(campaign.id)
        if old is None:
            # First time we see it and it is already rejected: worth a flag.
            if campaign.delivery_status == "REJECTED":
                alerts.append((campaign, None))
        elif old.get("deliveryStatus") != campaign.delivery_status:
            alerts.append((campaign, str(old.get("deliveryStatus") or "UNKNOWN")))
    return alerts


def describe_changes(campaign: Campaign, previous: Snapshot) -> list[str]:
    """What changed for this campaign since the previous snapshot (HTML bits)."""
    old = previous.campaigns.get(campaign.id)
    if old is None:
        return ["🆕 קמפיין חדש"]
    changes = []
    old_status = str(old.get("status") or "UNKNOWN")
    if old_status != campaign.status:
        changes.append("סטטוס " + ltr(f"{esc(old_status)} → {esc(campaign.status)}"))
    old_delivery = str(old.get("deliveryStatus") or "UNKNOWN")
    if old_delivery != campaign.delivery_status:
        changes.append("הגשה " + ltr(f"{esc(old_delivery)} → {esc(campaign.delivery_status)}"))
    else:
        old_reasons = old.get("deliveryStatusReasons")
        old_reasons = [str(r) for r in old_reasons] if isinstance(old_reasons, list) else []
        if sorted(old_reasons) != sorted(campaign.reasons):
            before = ", ".join(old_reasons) or "-"
            after = ", ".join(campaign.reasons) or "-"
            changes.append("סיבה " + ltr(f"{esc(before)} → {esc(after)}"))
    old_budget = parse_micros(old.get("budgetMicros"))
    if old_budget is not None and campaign.budget_micros is not None and old_budget != campaign.budget_micros:
        diff = campaign.budget_micros - old_budget
        changes.append(
            "תקציב "
            + ltr(f"{fmt_usd(old_budget)} → {fmt_usd(campaign.budget_micros)}")
            + " ("
            + ltr(fmt_usd(diff, signed=True))
            + ")"
        )
    changes.extend(_metric_deltas(campaign.metrics, metrics_from_dict(old.get("metrics"))))
    return changes


def _metric_deltas(current: Metrics | None, old: Metrics | None) -> list[str]:
    if current is None or old is None:
        return []
    deltas = []
    if current.spend_micros is not None and old.spend_micros is not None:
        diff = current.spend_micros - old.spend_micros
        if diff:
            deltas.append("הוצאה " + ltr(fmt_usd(diff, signed=True)))
    for label, now_value, old_value in (
        ("חשיפות", current.impressions, old.impressions),
        ("קליקים", current.clicks, old.clicks),
        ("כניסות", current.plays, old.plays),
    ):
        if now_value is not None and old_value is not None and now_value != old_value:
            deltas.append(f"{label} " + ltr(fmt_int(now_value - old_value, signed=True)))
    return deltas  # unchanged metrics are left out; the caller prints "no change"


def _metric_lines(campaign: Campaign) -> list[str]:
    metrics = campaign.metrics
    if metrics is None:
        return []
    lines = []
    if metrics.spend_micros is not None:
        line = f"הוצאה: {ltr(fmt_usd(metrics.spend_micros))}"
        used = utilization(campaign)
        if used is not None:
            percent, label = used
            line += f" · ניצול: {ltr(f'{percent}%')} {label}"
        lines.append(line)
    parts = []
    if metrics.impressions is not None:
        parts.append(f"חשיפות: {fmt_int(metrics.impressions)}")
    if metrics.clicks is not None:
        parts.append(f"קליקים: {fmt_int(metrics.clicks)}")
    ctr = ctr_percent(metrics.clicks, metrics.impressions)
    if ctr is not None:
        parts.append(f"CTR: {ltr(f'{ctr}%')}")
    if parts:
        lines.append(" · ".join(parts))
    parts = []
    if metrics.plays is not None:
        parts.append(f"כניסות: {fmt_int(metrics.plays)}")
    cost = fmt_cost_per_play(metrics.spend_micros, metrics.plays)
    if cost is not None:
        parts.append(f"עלות לכניסה: {ltr(cost)}")
    if parts:
        lines.append(" · ".join(parts))
    return lines


def _budget_line(campaign: Campaign) -> str:
    if campaign.budget_micros is None:
        return "תקציב: לא ידוע"
    suffix = BUDGET_SUFFIX.get(campaign.budget_type or "", esc(campaign.budget_type or ""))
    return f"תקציב: {ltr(fmt_usd(campaign.budget_micros))} {suffix}".rstrip()


def _campaign_block(campaign: Campaign, previous: Snapshot | None) -> str:
    emoji = DELIVERY_EMOJI.get(campaign.delivery_status, "⚪")
    lines = [
        f"{emoji} <b>{esc(campaign.name)}</b>",
        f"סטטוס: <code>{esc(campaign.status)}</code> · הגשה: <code>{esc(campaign.delivery_status)}</code>",
    ]
    if campaign.reasons:
        lines.append(f"סיבה: {fmt_reasons(campaign.reasons)}")
    lines.append(_budget_line(campaign))
    if campaign.scheduled_budget_micros is not None:
        line = f"⏬ ירידת תקציב מתוזמנת ל-{ltr(fmt_usd(campaign.scheduled_budget_micros))}"
        try:
            when = datetime.fromisoformat(str(campaign.scheduled_effective_time).replace("Z", "+00:00"))
            line += f" ב-{fmt_time_il(when)}"
        except ValueError:
            pass
        lines.append(line)
    lines.extend(_metric_lines(campaign))
    if previous is not None:
        changes = describe_changes(campaign, previous)
        lines.append("שינוי מהדיווח הקודם: " + (" · ".join(changes) if changes else "ללא שינוי"))
    return "\n".join(lines)


def _alert_block(alerts: list[tuple[Campaign, str | None]]) -> str:
    lines = ["🚨🚨 <b>התראה: קמפיין הפסיק לרוץ</b> 🚨🚨"]
    for campaign, old_delivery in alerts:
        if old_delivery is None:
            move = ltr(esc(campaign.delivery_status)) + " (קמפיין חדש)"
        else:
            move = ltr(f"{esc(old_delivery)} → {esc(campaign.delivery_status)}")
        lines.append(f"🔴 <b>{esc(campaign.name)}</b>: {move}")
        if campaign.reasons:
            lines.append(f"סיבה: {fmt_reasons(campaign.reasons)}")
    return "\n".join(lines)


def _summary_block(shown: list[Campaign], hidden: int, previous: Snapshot | None, now: datetime) -> str:
    counts = {"🟢": 0, "🟡": 0, "🔴": 0}
    for campaign in shown:
        emoji = DELIVERY_EMOJI.get(campaign.delivery_status)
        if emoji in counts:
            counts[emoji] += 1
    lines = [
        "📋 <b>סיכום</b>",
        f"קמפיינים פעילים: {len(shown)} (🟢 {counts['🟢']} · 🟡 {counts['🟡']} · 🔴 {counts['🔴']})",
    ]
    for budget_type, label in (("DAILY", "סך התקציב היומי"), ("LIFETIME", "סך התקציבים הכוללים")):
        amounts = [
            c.budget_micros for c in shown if c.budget_type == budget_type and c.budget_micros is not None
        ]
        if amounts:
            lines.append(f"{label}: {ltr(fmt_usd(sum(amounts)))}")

    with_metrics = [c.metrics for c in shown if c.metrics is not None]
    if with_metrics:
        def total(attr):
            values = [getattr(m, attr) for m in with_metrics if getattr(m, attr) is not None]
            return sum(values) if values else None

        spend, impressions = total("spend_micros"), total("impressions")
        clicks, plays = total("clicks"), total("plays")
        if spend is not None:
            lines.append(f"הוצאה כוללת: {ltr(fmt_usd(spend))}")
        parts = []
        if impressions is not None:
            parts.append(f"חשיפות: {fmt_int(impressions)}")
        if plays is not None:
            parts.append(f"כניסות: {fmt_int(plays)}")
        ctr = ctr_percent(clicks, impressions)
        if ctr is not None:
            parts.append(f"CTR: {ltr(f'{ctr}%')}")
        cost = fmt_cost_per_play(spend, plays)
        if cost is not None:
            parts.append(f"עלות לכניסה: {ltr(cost)}")
        if parts:
            lines.append(" · ".join(parts))
    else:
        lines.append("ℹ️ הוצאה, חשיפות, כניסות ו-CTR: Roblox עדיין לא חושפת מדדי קמפיין ב-API.")

    if hidden:
        lines.append(f"לא מוצגים: {hidden} קמפיינים לא פעילים (מושהים, מבוטלים או שהסתיימו)")
    if previous is None:
        lines.append("זה הדיווח הראשון. ההשוואה לדיווח הקודם תתחיל מהריצה הבאה.")
    elif previous.generated_at is not None:
        lines.append(f"ההשוואה היא מול הדיווח מ-{fmt_time_il(previous.generated_at)}")
    lines.append(f"🕒 {fmt_time_il(now)} (שעון ישראל)")
    return "\n".join(lines)


def build_report(
    campaigns: list[Campaign], previous: Snapshot | None, now: datetime, *, demo: bool = False
) -> list[str]:
    """The report as a list of self-contained HTML blocks (see split_blocks)."""
    blocks = []
    alerts = find_alerts(campaigns, previous)
    if alerts:
        blocks.append(_alert_block(alerts))
    title = "📊 <b>דוח קמפיינים · Roblox Ads</b>"
    if demo:
        title += "\n🧪 נתוני דמה, לא מהחשבון שלך"
    blocks.append(title)

    shown = sorted(
        (c for c in campaigns if is_reportable(c)),
        key=lambda c: (DELIVERY_ORDER.get(c.delivery_status, 9), c.name.casefold()),
    )
    if shown:
        blocks.extend(_campaign_block(c, previous) for c in shown)
    else:
        blocks.append("אין כרגע קמפיינים פעילים.")
    blocks.append(_summary_block(shown, len(campaigns) - len(shown), previous, now))
    return blocks


def build_error_message(error: RobloxApiError, now: datetime) -> str:
    title = "⚠️ <b>שגיאה במשיכת נתוני הקמפיינים מ-Roblox</b>"
    footer = f"🕒 {fmt_time_il(now)} (שעון ישראל)"
    if error.code == "MISSING_API_KEY":
        return "\n".join([
            title,
            "MISSING_API_KEY · המשתנה ROBLOX_API_KEY לא מוגדר",
            "💡 בדוק את ה-Secret ב-GitHub, או את קובץ ה-env בהרצה מקומית.",
            footer,
        ])
    head = "HTTP " + str(error.status) if error.status is not None else "אין תשובה מהשרת"
    if error.code:
        head += f" · {esc(error.code)}"
    lines = [title, head]
    if error.message:
        lines.append(esc(error.message))
    if error.status in (401, 403):
        lines.append("💡 בדוק שה-API key תקף ושיש לו את הפעולה ad.campaign:read.")
    elif error.status == 429:
        lines.append("💡 חריגה ממגבלת הקצב של Roblox.")
    elif error.status is None or error.status >= 500:
        lines.append("💡 נראה כמו תקלה זמנית בצד של Roblox או ברשת.")
    if error.attempts > 1:
        lines.append(f"בוצעו {error.attempts} ניסיונות. הניסיון הבא בריצה המתוזמנת הבאה.")
    lines.append(footer)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------


def tg_len(text: str) -> int:
    """Length the way Telegram counts it (UTF-16 code units, so emoji = 2)."""
    return len(text.encode("utf-16-le")) // 2


def _hard_split(line: str, limit: int) -> list[str]:
    """Last resort for a single line longer than the limit: drop its tags."""
    plain = html.unescape(re.sub(r"<[^>]+>", "", line))
    pieces, current = [], ""
    for char in plain:
        escaped = esc(char)
        if current and tg_len(current) + tg_len(escaped) > limit:
            pieces.append(current)
            current = ""
        current += escaped
    if current:
        pieces.append(current)
    return pieces


def _fit_block(block: str, limit: int) -> list[str]:
    if tg_len(block) <= limit:
        return [block]
    # Every line the report builds has balanced tags, so lines are safe cut points.
    pieces, current = [], ""
    for line in block.split("\n"):
        for part in ([line] if tg_len(line) <= limit else _hard_split(line, limit)):
            candidate = part if not current else f"{current}\n{part}"
            if tg_len(candidate) <= limit:
                current = candidate
            else:
                pieces.append(current)
                current = part
    if current:
        pieces.append(current)
    return pieces


def split_blocks(blocks: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Pack blocks into messages of at most `limit` chars, cutting between blocks."""
    messages, current = [], ""
    for block in blocks:
        for piece in _fit_block(block, limit):
            candidate = piece if not current else f"{current}\n\n{piece}"
            if tg_len(candidate) <= limit:
                current = candidate
            else:
                messages.append(current)
                current = piece
    if current:
        messages.append(current)
    return messages


def send_telegram(token: str, chat_id: str, text: str, *, session=None, sleep=time.sleep) -> None:
    """sendMessage with parse_mode=HTML. Errors never include the bot token."""
    session = session or requests.Session()
    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    try:
        resp, _ = request_with_retry(session, "POST", url, json=payload, sleep=sleep)
    except requests.RequestException as exc:
        # str(exc) would contain the URL, and the URL contains the token.
        raise TelegramError(f"network error ({type(exc).__name__})") from None
    description = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            if resp.status_code == 200 and body.get("ok"):
                return
            description = str(body.get("description") or "")
    except ValueError:
        pass
    raise TelegramError(f"HTTP {resp.status_code} {description}".strip())


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def load_dotenv_file(path: Path) -> None:
    """Tiny .env reader for local runs. Real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and not os.environ.get(key):
            os.environ[key] = value


def deliver(messages: list[str], *, dry_run: bool, token: str, chat_id: str) -> None:
    if dry_run:
        for index, message in enumerate(messages, 1):
            print(f"----- message {index}/{len(messages)} ({tg_len(message)} chars) -----")
            print(message)
        print("----- dry run: nothing was sent -----")
        return
    for message in messages:
        send_telegram(token, chat_id, message)


def _load_demo(with_metrics: bool) -> tuple[list[Campaign], Snapshot | None]:
    raw = json.loads((SAMPLE_DIR / "campaigns.json").read_text(encoding="utf-8"))
    campaigns = [parse_campaign(item) for item in raw["campaigns"]]
    if with_metrics:
        metrics = json.loads((SAMPLE_DIR / "metrics.json").read_text(encoding="utf-8"))
        for campaign in campaigns:
            campaign.metrics = metrics_from_dict(metrics.get(campaign.id))
    name = "previous_snapshot_with_metrics.json" if with_metrics else "previous_snapshot.json"
    return campaigns, load_state(SAMPLE_DIR / name)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Roblox Ads -> Telegram status report")
    parser.add_argument("--dry-run", action="store_true", help="print the message, send and save nothing")
    parser.add_argument("--demo", action="store_true", help="use the bundled sample data instead of calling Roblox")
    parser.add_argument(
        "--demo-metrics",
        action="store_true",
        help="like --demo, plus made-up performance metrics (checks the money maths)",
    )
    parser.add_argument("--state-file", help="snapshot path (default: state/snapshot.json)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    demo = args.demo or args.demo_metrics
    for stream in (sys.stdout, sys.stderr):  # Hebrew + emoji on a Windows console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv_file(BASE_DIR / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and not (token and chat_id):
        print("error: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run)", file=sys.stderr)
        return 2

    state_file = Path(args.state_file or os.environ.get("STATE_FILE") or DEFAULT_STATE_FILE)
    now = datetime.now(timezone.utc)

    def send(messages: list[str]) -> None:
        deliver(messages, dry_run=args.dry_run, token=token, chat_id=chat_id)

    try:
        if demo:
            campaigns, previous = _load_demo(args.demo_metrics)
        else:
            api_key = os.environ.get("ROBLOX_API_KEY", "").strip()
            if not api_key:
                raise RobloxApiError(None, "MISSING_API_KEY", "ROBLOX_API_KEY is not set", 1)
            session = requests.Session()
            session.headers.update({"x-api-key": api_key, "Accept": "application/json"})
            group_id = os.environ.get("ROBLOX_GROUP_ID", "").strip() or None
            campaigns = [parse_campaign(item) for item in list_campaigns(session, group_id)]
            metrics = fetch_campaign_metrics(session, campaigns)
            for campaign in campaigns:
                campaign.metrics = metrics.get(campaign.id)
            previous = load_state(state_file)

        send(split_blocks(build_report(campaigns, previous, now, demo=demo)))
        # Saved only after a successful send, so a failed delivery can't swallow an alert.
        if not args.dry_run and not demo:
            save_state(state_file, build_state(campaigns, now))
        return 0
    except RobloxApiError as error:
        print(f"error: {error}", file=sys.stderr)
        try:
            send([build_error_message(error, now)])
        except TelegramError as tg_error:
            print(f"error: could not report the failure to Telegram: {tg_error}", file=sys.stderr)
        return 1
    except TelegramError as error:
        print(f"error: Telegram delivery failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # a bug or an unexpected response: still don't stay silent
        traceback.print_exc()
        try:
            send([
                "⚠️ <b>שגיאה לא צפויה בבוט הקמפיינים</b>\n"
                f"{esc(type(error).__name__)}\n"
                f"🕒 {fmt_time_il(now)} (שעון ישראל)"
            ])
        except TelegramError as tg_error:
            print(f"error: could not report the failure to Telegram: {tg_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
