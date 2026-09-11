"""Buffer GraphQL client for the Analytics tab.

Buffer's API is a single GraphQL endpoint at https://api.buffer.com, authenticated
with a personal API key sent as a bearer token.

Two things shape this module:

1. **The post-metrics queries are experimental.** Buffer says the data can change
   and doesn't recommend relying on it for production reporting, and metrics
   refresh daily so values run up to ~24h behind the network. So field and enum
   names are resolved by *introspecting the live schema* rather than hardcoded —
   a rename then shows up as a clear message instead of a 400 from a query built
   on stale assumptions.

2. **Request budgets are small** (3,000 per 30 days on the free plan, 7,500 on
   Essentials). Everything is cached, and a page load must not mean a fresh
   crawl. `posts_in_period(..., force=True)` is the explicit refresh.
"""

import hashlib
import re
import threading
import time
from datetime import datetime, time as dtime, timedelta, timezone

import requests

# Reporting weeks/months/years are in the team's own timezone, not UTC — a post
# at 11pm on the 30th belongs to that month, not the next one.
REPORT_TZ_NAME = "America/Chicago"
try:
    from zoneinfo import ZoneInfo
    REPORT_TZ = ZoneInfo(REPORT_TZ_NAME)
except Exception as e:                      # slim images without tzdata
    print(f"Warning: {REPORT_TZ_NAME} unavailable ({e}); period boundaries fall back to UTC.")
    REPORT_TZ = timezone.utc

API_URL = "https://api.buffer.com"
TIMEOUT = 30

# Buffer's own guidance: metrics land about a day behind the source network.
METRICS_LAG_NOTE = "Buffer refreshes post metrics daily, so today's numbers can run ~24h behind."

CACHE_TTL = 15 * 60          # seconds; the request budget is the reason
SCHEMA_TTL = 24 * 60 * 60

# First match wins. Buffer's enum differs per network, so the chosen name is
# reported back to the UI rather than assumed.
VIEW_METRIC_CANDIDATES = ["views", "videoViews", "playCount", "plays", "impressions", "reach"]
DATE_FIELD_CANDIDATES = ["sentAt", "dueAt", "createdAt"]
# externalLink is what Buffer's live schema calls the published-post link; it isn't
# formally documented, so it stays last behind the conventional names.
URL_FIELD_CANDIDATES = ["permalink", "postUrl", "serviceLink", "serviceUrl", "externalLink"]
# Buffer's Channel type exposes no group field (confirmed against the live schema),
# so attribution falls back to channel names plus the manual overrides below.
GROUP_FIELD_CANDIDATES = ["channelGroup", "group"]

_lock = threading.RLock()
_cache: dict[str, tuple[float, object]] = {}


class BufferError(Exception):
    """Something went wrong talking to Buffer, phrased for the UI."""


def _key(token: str, *parts: str) -> str:
    """Cache key that never contains the token itself."""
    return hashlib.sha256(("|".join((token, *parts))).encode()).hexdigest()


def _cached(key: str, ttl: int | None):
    """ttl=None means 'whatever we have, at any age' — used to render the last
    sync without spending a Buffer request."""
    with _lock:
        hit = _cache.get(key)
        if hit and (ttl is None or time.time() - hit[0] < ttl):
            return hit[1]
    return None


def _store(key: str, value):
    with _lock:
        _cache[key] = (time.time(), value)


def clear_cache():
    with _lock:
        _cache.clear()


def _gql(token: str, query: str, variables: dict | None = None) -> dict:
    try:
        resp = requests.post(
            API_URL,
            json={"query": query, "variables": variables or {}},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        raise BufferError(f"Could not reach Buffer: {e}") from e

    if resp.status_code in (401, 403):
        raise BufferError("Buffer rejected the API key. Check it in Connections — only a Buffer "
                          "organization owner can create one.")
    if resp.status_code == 429:
        raise BufferError("Buffer rate limit hit. Plans allow 3,000–15,000 requests per 30 days; "
                          "try again later.")
    if not resp.ok:
        raise BufferError(f"Buffer returned HTTP {resp.status_code}: {resp.text[:200]}")

    body = resp.json()
    if body.get("errors"):
        msg = "; ".join(e.get("message", "unknown") for e in body["errors"][:3])
        raise BufferError(f"Buffer query failed: {msg}")
    return body.get("data") or {}


# ── schema discovery ─────────────────────────────────────────────────────
_INTROSPECT = """
query RandcutIntrospect {
  queryType: __type(name: "Query") { fields { name } }
  post: __type(name: "Post") { fields { name } }
  channel: __type(name: "Channel") { fields { name } }
  results: __type(name: "PostsResults") { fields { name } }
  metricType: __type(name: "PostMetricType") { enumValues { name } }
}
"""


def schema(token: str, force: bool = False) -> dict:
    """What the live schema actually offers, so queries are built from fact."""
    key = _key(token, "schema")
    if not force:
        hit = _cached(key, SCHEMA_TTL)
        if hit is not None:
            return hit

    data = _gql(token, _INTROSPECT)

    def names(node, attr="fields"):
        if not node or not node.get(attr):
            return []
        return [f["name"] for f in node[attr]]

    found = {
        "query_fields": names(data.get("queryType")),
        "post_fields": names(data.get("post")),
        "channel_fields": names(data.get("channel")),
        "results_fields": names(data.get("results")),
        "metric_types": names(data.get("metricType"), "enumValues"),
    }
    _store(key, found)
    return found


_ID_SAFE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def _literal_id(value: str) -> str:
    """Inline an id into a query string.

    Buffer types organizationId as a custom scalar (OrganizationId!), so passing it
    as a String! variable is rejected outright. Their own examples inline it, which
    avoids having to name the scalar at all. Validated because it lands in a query
    unquoted-by-us — it always comes from Buffer, never from a user.
    """
    if not value or not _ID_SAFE.match(value):
        raise BufferError(f"Buffer returned an id in an unexpected format: {value!r}")
    return value


def _pick(candidates: list[str], available: list[str]) -> str | None:
    lower = {a.lower(): a for a in available}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def capability_report(token: str, force: bool = False) -> dict:
    """Plain-language answer to 'can Buffer actually give us this?'.

    Surfaced in the UI so an unavailable field reads as a known limitation rather
    than a bug.
    """
    s = schema(token, force=force)
    view_metric = _pick(VIEW_METRIC_CANDIDATES, s["metric_types"])
    return {
        "views_metric": view_metric,
        "date_field": _pick(DATE_FIELD_CANDIDATES, s["post_fields"]),
        "url_field": _pick(URL_FIELD_CANDIDATES, s["post_fields"]),
        "group_field": _pick(GROUP_FIELD_CANDIDATES, s["channel_fields"]),
        "has_metrics": "metrics" in s["post_fields"],
        "metric_types": sorted(s["metric_types"]),
        "channel_fields": sorted(s["channel_fields"]),
        "post_fields": sorted(s["post_fields"]),
    }


# ── account / channels ───────────────────────────────────────────────────
def organization_id(token: str) -> str:
    key = _key(token, "org")
    hit = _cached(key, SCHEMA_TTL)
    if hit:
        return hit
    data = _gql(token, "query RandcutOrg { account { organizations { id name } } }")
    orgs = ((data.get("account") or {}).get("organizations")) or []
    if not orgs:
        raise BufferError("That Buffer account has no organizations.")
    org_id = orgs[0]["id"]
    _store(key, org_id)
    return org_id


def channels(token: str, org_id: str) -> list[dict]:
    """Connected channels, with a group name when the schema exposes one."""
    key = _key(token, "channels", org_id)
    hit = _cached(key, CACHE_TTL)
    if hit is not None:
        return hit

    s = schema(token)
    group_field = _pick(GROUP_FIELD_CANDIDATES, s["channel_fields"])
    # a group is an object in every shape we've seen; ask for its name
    group_sel = f"{group_field} {{ name }}" if group_field else ""
    query = f"""
    query RandcutChannels {{
      channels(input: {{ organizationId: "{_literal_id(org_id)}" }}) {{
        id
        name
        service
        {group_sel}
      }}
    }}
    """
    rows = _gql(token, query).get("channels") or []
    out = []
    for c in rows:
        group = None
        if group_field:
            g = c.get(group_field)
            if isinstance(g, dict):
                group = g.get("name")
            elif isinstance(g, str):
                group = g
        out.append({"id": c.get("id"), "name": c.get("name"),
                    "service": c.get("service"), "group": group})
    _store(key, out)
    return out


# ── influencer mapping ───────────────────────────────────────────────────
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def map_channels(chans: list[dict], influencers: list[str],
                 overrides: dict | None = None) -> tuple[dict, list[dict]]:
    """channel id -> influencer name.

    Three passes, most explicit first:
      1. a manual override saved from the UI (channel id -> influencer),
      2. the Buffer channel group, if the schema ever exposes one,
      3. an influencer's name appearing inside the channel name.

    Name matching only works when the spellings agree — a channel called
    "ChristyPlaysVR" will never match an influencer named "Christie", which is
    exactly what the overrides are for. Anything still unmatched is returned so
    the UI can offer to assign it rather than quietly dropping its posts.
    """
    overrides = overrides or {}
    known = {name for name in influencers}
    by_norm = {_norm(name): name for name in influencers}
    mapping, unmapped = {}, []
    for c in chans:
        label = None
        override = overrides.get(c.get("id"))
        if override and override in known:
            label = override
        if not label and c.get("group"):
            label = by_norm.get(_norm(c["group"]))
        if not label:
            haystack = _norm(c.get("name") or "")
            # longest first so "Christie B" wins over a shorter contained name
            for norm_name in sorted(by_norm, key=len, reverse=True):
                if norm_name and norm_name in haystack:
                    label = by_norm[norm_name]
                    break
        if label:
            mapping[c["id"]] = label
        else:
            unmapped.append({"id": c.get("id"), "name": c.get("name"),
                             "service": c.get("service"), "group": c.get("group")})
    return mapping, unmapped


# ── month to date ────────────────────────────────────────────────────────
RANGES = ("week", "month", "year")


def _local_midnight(d) -> datetime:
    """Wall-clock midnight in the reporting zone, as an aware datetime.

    Built from a date rather than by adding timedeltas to an aware datetime, so
    a week that crosses a DST change still starts at 00:00 local on both sides.
    """
    return datetime.combine(d, dtime.min, tzinfo=REPORT_TZ)


def period_bounds(kind: str, offset: int = 0, now: datetime | None = None) -> dict:
    """Bounds and labels for week/month/year at `offset` periods from today.

    offset 0 is the current period, -1 one back, +1 one forward. Weeks run
    Monday–Sunday and are named for their Monday.
    """
    if kind not in RANGES:
        raise BufferError(f"Unknown range: {kind}")
    now_local = (now or datetime.now(timezone.utc)).astimezone(REPORT_TZ)
    today = now_local.date()

    if kind == "week":
        monday = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
        start_local, end_local = _local_midnight(monday), _local_midnight(monday + timedelta(weeks=1))
        label = f"Week of {start_local:%b %-d}"
        short = f"Week of {start_local.month}/{start_local.day}"
    elif kind == "year":
        year = today.year + offset
        start_local = _local_midnight(today.replace(year=year, month=1, day=1))
        end_local = _local_midnight(today.replace(year=year + 1, month=1, day=1))
        label = short = str(year)
    else:
        months = today.year * 12 + (today.month - 1) + offset
        year, month = divmod(months, 12)
        month += 1
        nxt_year, nxt_month = (year + 1, 1) if month == 12 else (year, month + 1)
        start_local = _local_midnight(today.replace(year=year, month=month, day=1))
        end_local = _local_midnight(today.replace(year=nxt_year, month=nxt_month, day=1))
        label = f"{start_local:%B %Y}"
        short = f"{start_local:%B}" if start_local.year == today.year else label

    return {
        "kind": kind,
        "offset": offset,
        "start": start_local.astimezone(timezone.utc),
        "end": end_local.astimezone(timezone.utc),
        "label": label,                       # concrete, for the headline
        "nav_label": "Current" if offset == 0 else short,   # for the selector
        "is_current": offset == 0,
    }


def _period_key(token: str, influencers: list[str], overrides: dict, period: dict) -> str:
    return _key(token, "posts", period["kind"], str(period["offset"]),
                "|".join(sorted(influencers)),
                "|".join(f"{k}={v}" for k, v in sorted((overrides or {}).items())))


def last_sync(token: str, influencers: list[str], overrides: dict | None = None,
              period: dict | None = None) -> dict | None:
    """The most recent sync for a period, at any age, without calling Buffer.

    Opening the tab shows this rather than spending a request from the budget.
    """
    period = period or period_bounds("month", 0)
    return _cached(_period_key(token, influencers, overrides or {}, period), None)


def _parse_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):        # unix seconds
        return datetime.fromtimestamp(value, tz=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _metric_value(metrics, wanted: str | None):
    """Pull the views-ish metric, remembering which name supplied it."""
    if not metrics or not wanted:
        return None, None
    for m in metrics:
        if (m.get("type") or "").lower() == wanted.lower():
            return m.get("value"), m.get("type")
    # fall back to the next best available on this particular post
    by_type = {(m.get("type") or "").lower(): m for m in metrics}
    for cand in VIEW_METRIC_CANDIDATES:
        if cand.lower() in by_type:
            m = by_type[cand.lower()]
            return m.get("value"), m.get("type")
    return None, None


def posts_in_period(token: str, influencers: list[str], period: dict | None = None,
                    force: bool = False, overrides: dict | None = None,
                    page_size: int = 50, max_pages: int = 40) -> dict:
    """Sent posts inside one period, one row per post.

    Paginates newest-first, skipping anything after the period, and stops once it
    passes the start. A date filter on PostsFilter isn't documented, so reaching
    an older period means paging through the newer ones — which is why each
    period is cached separately and why the page count is reported back.
    """
    overrides = overrides or {}
    period = period or period_bounds("month", 0)
    key = _period_key(token, influencers, overrides, period)
    if not force:
        hit = _cached(key, CACHE_TTL)
        if hit is not None:
            return {**hit, "cached": True}

    caps = capability_report(token)
    if not caps["has_metrics"]:
        raise BufferError("This Buffer schema exposes no post metrics — the metrics queries are "
                          "experimental and may not be enabled on your account.")

    org_id = organization_id(token)
    chans = channels(token, org_id)
    mapping, unmapped = map_channels(chans, influencers, overrides)
    by_id = {c["id"]: c for c in chans}

    s = schema(token)
    date_field = caps["date_field"] or "createdAt"
    url_field = caps["url_field"]
    has_page_info = "pageInfo" in s["results_fields"]
    # the post carries its own service, so platform survives a channel we can't look up
    service_field = "channelService" if "channelService" in s["post_fields"] else ""
    # exact metric freshness beats a generic "about a day behind" caveat
    updated_field = "metricsUpdatedAt" if "metricsUpdatedAt" in s["post_fields"] else ""

    selection = "\n".join(filter(None, [
        "id", "text", "channelId", date_field,
        url_field or "", service_field, updated_field,
        "metrics { type value name unit }",
    ]))
    page_info = "pageInfo { hasNextPage endCursor }" if has_page_info else ""
    sort_field = date_field if date_field in ("dueAt", "createdAt") else "createdAt"
    query = f"""
    query RandcutPosts($first: Int!, $after: String) {{
      posts(first: $first, after: $after, input: {{
        organizationId: "{_literal_id(org_id)}",
        sort: [{{ field: {sort_field}, direction: desc }}],
        filter: {{ status: sent }}
      }}) {{
        edges {{ node {{ {selection} }} }}
        {page_info}
      }}
    }}
    """

    start, end = period["start"], period["end"]
    rows, after, pages, reached_boundary = [], None, 0, False
    freshest = None
    skipped_newer = 0

    while pages < max_pages:
        data = _gql(token, query, {"first": page_size, "after": after})
        pages += 1
        result = data.get("posts") or {}
        edges = result.get("edges") or []
        if not edges:
            reached_boundary = True
            break

        for edge in edges:
            node = edge.get("node") or {}
            when = _parse_dt(node.get(date_field))
            if when and when < start:
                reached_boundary = True
                break
            if when and when >= end:
                skipped_newer += 1      # newer than the period; keep paging back
                continue
            chan = by_id.get(node.get("channelId")) or {}
            views, metric_used = _metric_value(node.get("metrics"), caps["views_metric"])
            if updated_field:
                upd = _parse_dt(node.get(updated_field))
                if upd and (freshest is None or upd > freshest):
                    freshest = upd
            rows.append({
                "influencer": mapping.get(node.get("channelId")),
                "date": when.isoformat() if when else None,
                "url": node.get(url_field) if url_field else None,
                "platform": chan.get("service") or node.get(service_field or "") or None,
                "channel": chan.get("name"),
                "channel_id": node.get("channelId"),
                "views": views,
                "metric": metric_used,
                "text": (node.get("text") or "")[:140],
            })

        if reached_boundary:
            break
        info = result.get("pageInfo") or {}
        if not has_page_info or not info.get("hasNextPage"):
            reached_boundary = True     # ran out of history, not out of budget
            break
        after = info.get("endCursor")

    # posts on channels we couldn't attribute stay visible but unlabelled
    rows.sort(key=lambda r: (r["date"] or ""), reverse=True)

    # timestamps go out raw; the UI renders them in the viewer's terms
    warnings = []
    if not freshest:
        warnings.append(METRICS_LAG_NOTE)
    if not url_field:
        warnings.append("This Buffer schema exposes no published-post URL field, so Post URL is "
                        "blank. The post text is shown instead.")
    if not caps["views_metric"]:
        warnings.append("No views-style metric found in this schema. Available metrics: "
                        + (", ".join(caps["metric_types"][:12]) or "none"))
    if unmapped:
        names = ", ".join(c["name"] or c["id"] for c in unmapped[:4])
        warnings.append(f"{len(unmapped)} channel(s) aren't assigned to an influencer ({names}) — "
                        "assign them below and their posts will be attributed.")
    if not reached_boundary and pages >= max_pages:
        # count what was actually scanned; page_size is what we asked for, not what came back
        warnings.append(f"Stopped after scanning {len(rows) + skipped_newer} posts over {pages} "
                        f"pages to stay inside Buffer's request budget, so earlier posts in "
                        f"{period['label']} may be missing.")

    payload = {
        "period": {
            "kind": period["kind"], "offset": period["offset"],
            "label": period["label"], "nav_label": period["nav_label"],
            "is_current": period["is_current"],
            "start": start.isoformat(), "end": end.isoformat(),
        },
        "rows": rows,
        "total_views": sum(r["views"] or 0 for r in rows),
        "post_count": len(rows),
        # when Buffer last recomputed the metrics vs when we last read them
        "metrics_updated_at": freshest.isoformat() if freshest else None,
        "synced_at": datetime.now(timezone.utc).isoformat(),
        # so the cost of reaching an older period is visible, not hidden
        "pages_fetched": pages,
        "skipped_newer": skipped_newer,
        "views_metric": caps["views_metric"],
        "group_field_available": bool(caps["group_field"]),
        "unmapped_channels": unmapped,
        "warnings": warnings,
        "cached": False,
    }
    _store(key, payload)
    return payload
