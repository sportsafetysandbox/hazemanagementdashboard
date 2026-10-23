#!/usr/bin/env python3
"""Fill gaps in data/pm25_1hr.csv and data/psi_24hr.csv from data.gov.sg history.

For each of the last N days it asks data.gov.sg for every reading of that day and adds
any reading_timestamp that is not already in the table. Safe to run repeatedly.
Backfilled rows use the reading's own timestamp as `fetched_at`.
Does not touch latest.json.

Usage: backfill.py [days]     (default 2)
"""
import csv
import sys
import urllib.parse
from datetime import datetime, timedelta

import fetch_data as F

REGIONS, SGT, DATA = F.REGIONS, F.SGT, F.DATA


def items_for_day(name, day):
    v2, v1, key = F.SOURCES[name]
    errors = []
    # v2: paginated
    try:
        out, token = [], None
        for _ in range(50):
            q = {"date": day}
            if token:
                q["paginationToken"] = token
            payload = F.http_json(f"{v2}?{urllib.parse.urlencode(q)}")
            body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            out += body.get("items") or []
            token = body.get("paginationToken")
            if not token:
                break
        if out:
            return out, key
    except Exception as e:  # noqa: BLE001
        errors.append(f"v2: {e}")
    try:
        payload = F.http_json(f"{v1}?{urllib.parse.urlencode({'date': day})}")
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        out = body.get("items") or []
        if out:
            return out, key
    except Exception as e:  # noqa: BLE001
        errors.append(f"v1: {e}")
    raise RuntimeError(f"no data for {day}: {'; '.join(errors)}")


def norm(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(SGT).isoformat(timespec="seconds")


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    today = datetime.now(SGT).date()
    for name in F.SOURCES:
        path = DATA / f"{name}.csv"
        with path.open(newline="") as f:
            rows = list(csv.reader(f))
        header, rows = rows[0], rows[1:]
        have = {r[1] for r in rows if len(r) > 1}
        added = 0
        for d in range(days, -1, -1):
            day = (today - timedelta(days=d)).isoformat()
            try:
                items, key = items_for_day(name, day)
            except Exception as e:  # noqa: BLE001
                print(f"[{name}] {day}: {e}", file=sys.stderr)
                continue
            print(f"[{name}] {day}: {len(items)} readings from data.gov.sg")
            for it in items:
                try:
                    ts = norm(it.get("timestamp") or it.get("updatedTimestamp") or it.get("update_timestamp"))
                    rd = it["readings"][key]
                except Exception:  # noqa: BLE001
                    continue
                if ts in have:
                    continue
                vals = [rd.get(r) for r in REGIONS]
                if all(v is None for v in vals):
                    continue
                rows.append([ts, ts] + ["" if v is None else int(round(float(v))) for v in vals])
                have.add(ts)
                added += 1
                print(f"  + {ts} {vals}")
        rows.sort(key=lambda r: (r[1], r[0]))
        with path.open("w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(header)
            w.writerows(rows)
        print(f"[{name}] added {added} missing readings")


if __name__ == "__main__":
    main()
