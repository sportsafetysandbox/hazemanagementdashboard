#!/usr/bin/env python3
"""Fetch 1-hr PM2.5 and 24-hr PSI from data.gov.sg and store them in the repo.

Run every 15 minutes by .github/workflows/update-data.yml. Uses only the
Python standard library.

Outputs (relative to the repo root):
  data/pm25_1hr.csv   one row per poll: fetched_at, reading_timestamp, 5 regions
  data/psi_24hr.csv   same layout for 24-hour PSI
  data/latest.json    newest reading of each, read by the mobile page

Optional: set DATA_GOV_SG_API_KEY (GitHub secret) to get higher rate limits.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))
REGIONS = ["north", "south", "east", "west", "central"]
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
KEEP_DAYS = 30

# (v2 URL, v1 fallback URL, reading key inside "readings")
SOURCES = {
    "pm25_1hr": (
        "https://api-open.data.gov.sg/v2/real-time/api/pm25",
        "https://api.data.gov.sg/v1/environment/pm25",
        "pm25_one_hourly",
    ),
    "psi_24hr": (
        "https://api-open.data.gov.sg/v2/real-time/api/psi",
        "https://api.data.gov.sg/v1/environment/psi",
        "psi_twenty_four_hourly",
    ),
}


def http_json(url, retries=3):
    headers = {"User-Agent": "sportsg-haze-dashboard/1.0"}
    key = os.environ.get("DATA_GOV_SG_API_KEY")
    if key:
        headers["x-api-key"] = key
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def parse(payload, reading_key):
    """Return (timestamp, {region: value}) from a v2 or v1 response."""
    body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = body.get("items") or []
    if not items:
        raise ValueError("no items in response")
    item = max(items, key=lambda i: i.get("timestamp") or i.get("updatedTimestamp") or "")
    readings = item["readings"][reading_key]
    values = {}
    for region in REGIONS:
        v = readings.get(region)
        values[region] = None if v is None else int(round(float(v)))
    if all(v is None for v in values.values()):
        raise ValueError("all regions empty")
    ts = item.get("timestamp") or item.get("updatedTimestamp") or item.get("update_timestamp")
    return ts, values


def fetch(name):
    v2, v1, key = SOURCES[name]
    errors = []
    for url in (v2, v1):
        try:
            return parse(http_json(url), key)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    raise RuntimeError("; ".join(errors))


def append_row(name, fetched_at, ts, values):
    path = DATA / f"{name}.csv"
    header = ["fetched_at", "reading_timestamp"] + REGIONS
    rows = []
    if path.exists():
        with path.open(newline="") as f:
            rows = list(csv.reader(f))[1:]
    rows.append([fetched_at, ts] + ["" if values[r] is None else values[r] for r in REGIONS])
    cutoff = (datetime.now(SGT) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    rows = [r for r in rows if r and r[0] >= cutoff]
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def main():
    DATA.mkdir(exist_ok=True)
    now = datetime.now(SGT).isoformat(timespec="seconds")
    latest_path = DATA / "latest.json"
    latest = {}
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text())
        except Exception:  # noqa: BLE001
            latest = {}
    latest.pop("sample", None)  # real data replaces the shipped placeholder

    failures = 0
    for name in SOURCES:
        try:
            ts, values = fetch(name)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"[{name}] FAILED: {e}", file=sys.stderr)
            continue
        append_row(name, now, ts, values)
        latest[name] = {"timestamp": ts, **values}
        print(f"[{name}] {ts} {values}")

    if failures < len(SOURCES):
        latest["fetched_at"] = now
        latest_path.write_text(json.dumps(latest, indent=2) + "\n")
    # Fail the run only if nothing could be fetched.
    sys.exit(1 if failures == len(SOURCES) else 0)


if __name__ == "__main__":
    main()
