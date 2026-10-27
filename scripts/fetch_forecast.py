#!/usr/bin/env python3
"""Read the next-day PSI forecast from https://www.haze.gov.sg/ and store it in the repo.

The forecast is only published as a picture (the "PSI Gauge" image), so this script
downloads that image and reads the numbers from it with Tesseract OCR. It cross-checks
several OCR passes and refuses to store anything it is not sure about.

Outputs (relative to the repo root):
  data/psi_forecast.csv          one row per forecast date (newest value wins)
  data/forecast_latest.json      the newest row, read by the mobile page
  data/forecast_images/*.png     the gauge image each row was read from (audit trail)

Usage:
  fetch_forecast.py --when-due   only act after 18:15 SGT, and only if tomorrow's
                                 forecast has not been stored yet (used by the relay)
  fetch_forecast.py              fetch now and store whatever forecast the page shows
  fetch_forecast.py --image F    read a local gauge image instead of the website (testing)

Needs: tesseract-ocr and Pillow.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from PIL import Image, ImageOps

SGT = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("HAZE_DATA_DIR", ROOT / "data"))
PAGE = "https://www.haze.gov.sg/"
DUE = (18, 15)  # 18:15 SGT
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
BANDS = [(50, "Good"), (100, "Moderate"), (200, "Unhealthy"), (300, "Very Unhealthy"), (10**9, "Hazardous")]
FIELDS = ["forecast_date", "low", "high", "band_low", "band_high", "checked_at"]


STATUS = {}


def write_status(**kw):
    """Record what the last attempt saw, so a failure can be diagnosed from the repo."""
    STATUS.update(kw)
    STATUS["attempted_at"] = datetime.now(SGT).isoformat(timespec="seconds")
    try:
        DATA.mkdir(exist_ok=True)
        (DATA / "forecast_status.json").write_text(json.dumps(STATUS, indent=2) + "\n")
    except Exception:  # noqa: BLE001
        pass


def band(v):
    return next(name for top, name in BANDS if v <= top)


# ---------- fetching ----------
def http_get(url, retries=3):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept": "text/html,image/png,image/*;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-SG,en;q=0.9",
        "Cache-Control": "no-cache",
    }
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


class _Imgs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.imgs = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.imgs.append(dict(attrs))


def find_images(html):
    p = _Imgs()
    p.feed(html)
    gauge = header = None
    for a in p.imgs:
        alt, src = (a.get("alt") or "").lower(), a.get("src") or ""
        if not src:
            continue
        low = src.lower()
        if ("gauge" in alt or "new-chart" in low or "psi-forecast" in low or "chart_" in low
                or re.search(r"psi forecast for .*\d+\s*[-–—]\s*\d+", alt)):
            gauge = gauge or src
            STATUS.setdefault("gauge_alt", a.get("alt"))
        elif ("forecast header" in alt or "header-image" in low or "psi-header" in low
                or alt.startswith("psi forecast for planning")):
            header = header or src
    if not gauge:
        raise RuntimeError("PSI gauge image not found on the page (layout may have changed)")
    return urljoin(PAGE, gauge), (urljoin(PAGE, header) if header else None)


def date_from_filename(url):
    m = re.search(r"(\d{1,2})-([a-z]{3})[a-z]*-(\d{4})", (url or "").lower())
    if m and m.group(2) in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).date()
    return None


# ---------- reading the image ----------
def tesseract(img, psm):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "x.png"
        img.save(f)
        out = subprocess.run(["tesseract", str(f), "stdout", "--psm", str(psm)],
                             capture_output=True, text=True, timeout=120)
    return out.stdout


RANGE_RE = re.compile(r"^\W*(\d{2,3})\s*[-–—−~+=_:]\s*(\d{2,3})\W*$")
DATE_RE = re.compile(r"Forecast\s*for\s*(\d{1,2})\s*([A-Za-z]{3})", re.I)


def text_on_pill(img):
    """The range sits in a two-colour pill whose text colour varies (white on dark green,
    black on yellow...). Crop the pill area, find each half's background colour (the most
    common clearly-coloured shade), and mark pixels inside the pill that differ strongly
    from it as text. Returns black text on white."""
    from collections import Counter as C
    w, h = img.size
    box = img.crop((int(w * .28), int(h * .55), int(w * .72), int(h * .88))).convert("RGB")
    bw, bh = box.size
    px = box.load()
    out = Image.new("L", box.size, 255)
    op = out.load()

    def saturated(p):
        return max(p) - min(p) > 80

    bgs = []
    for x0, x1 in ((0, bw // 2), (bw // 2, bw)):
        cnt = C()
        for y in range(0, bh, 2):
            for x in range(x0, x1, 2):
                p = px[x, y]
                if saturated(p):
                    cnt[(p[0] // 24, p[1] // 24, p[2] // 24)] += 1
        if not cnt:
            return out
        bgs.append(tuple(v * 24 + 12 for v in cnt.most_common(1)[0][0]))

    def dist(p, q):
        return abs(p[0] - q[0]) + abs(p[1] - q[1]) + abs(p[2] - q[2])

    xs, ys = [], []
    for y in range(bh):
        for x in range(bw):
            if min(dist(px[x, y], bgs[0]), dist(px[x, y], bgs[1])) < 70:
                xs.append(x); ys.append(y)
    if not xs:
        return out
    # trim the bounding box to the dense core so stray arc pixels do not widen it
    xs.sort(); ys.sort()
    n = len(xs)
    bx0, bx1, by0, by1 = xs[int(n * .02)], xs[int(n * .98)], ys[int(n * .02)], ys[int(n * .98)]
    for y in range(by0, by1 + 1):
        for x in range(bx0, bx1 + 1):
            bg = bgs[0] if x < bw // 2 else bgs[1]
            if dist(px[x, y], bg) > 200:
                op[x, y] = 0
    return out.crop((max(bx0 - 10, 0), max(by0 - 10, 0), min(bx1 + 10, bw), min(by1 + 10, bh)))


def read_gauge(path):
    base = Image.open(path).convert("RGB")
    ranges, dates = Counter(), Counter()
    passes = 0
    variants = []
    for scale in (2, 3):
        big = base.resize((base.width * scale, base.height * scale), Image.LANCZOS)
        variants.append(ImageOps.autocontrast(ImageOps.grayscale(big)))
        # The range sits in a coloured pill (dark green is hard for plain greyscale OCR):
        # keep only the near-black text pixels, on white.
        dark = big.convert("L").point(lambda v: 0 if v < 80 else 255)
        variants.append(dark)
        w, h = dark.size
        variants.append(dark.crop((int(w * .25), int(h * .50), int(w * .75), int(h * .82))))
        variants.append(text_on_pill(big))
    if True:
        for big in variants:
          for psm in (11, 6, 7):
            passes += 1
            text = tesseract(big, psm)
            for line in text.splitlines():
                m = RANGE_RE.match(line.strip())
                if m:
                    lo, hi = int(m.group(1)), int(m.group(2))
                    if lo <= hi <= 500 and hi - lo <= 300:
                        ranges[(lo, hi)] += 1
                d = DATE_RE.search(line)
                if d and d.group(2).lower() in MONTHS:
                    dates[(int(d.group(1)), MONTHS[d.group(2).lower()])] += 1
    print(f"OCR candidates over {passes} passes: ranges={dict(ranges)} dates={dict(dates)}")
    if not ranges or ranges.most_common(1)[0][1] < 2:
        raise RuntimeError("could not read the PSI range reliably from the gauge image")
    if len(ranges) > 1 and ranges.most_common(2)[0][1] == ranges.most_common(2)[1][1]:
        raise RuntimeError(f"OCR passes disagree on the range: {dict(ranges)}")
    if not dates:
        raise RuntimeError("could not read the forecast date from the gauge image")
    (lo, hi), _ = ranges.most_common(1)[0]
    (day, month), _ = dates.most_common(1)[0]
    now = datetime.now(SGT).date()
    fdate = None
    for y in (now.year - 1, now.year, now.year + 1):
        try:
            c = datetime(y, month, day).date()
        except ValueError:
            continue
        if fdate is None or abs((c - now).days) < abs((fdate - now).days):
            fdate = c
    return lo, hi, fdate


# ---------- storing ----------
def load_rows():
    p = DATA / "psi_forecast.csv"
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def store(fdate, lo, hi, image_path=None):
    DATA.mkdir(exist_ok=True)
    now = datetime.now(SGT).isoformat(timespec="seconds")
    row = {"forecast_date": fdate.isoformat(), "low": lo, "high": hi,
           "band_low": band(lo), "band_high": band(hi), "checked_at": now}
    rows = [r for r in load_rows() if r["forecast_date"] != row["forecast_date"]] + [row]
    rows.sort(key=lambda r: r["forecast_date"])
    rows = rows[-120:]  # keep roughly four months
    with (DATA / "psi_forecast.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    (DATA / "forecast_latest.json").write_text(json.dumps(
        {**row, "low": lo, "high": hi}, indent=2) + "\n")
    if image_path:
        d = DATA / "forecast_images"
        d.mkdir(exist_ok=True)
        (d / f"gauge_{row['forecast_date']}.png").write_bytes(Path(image_path).read_bytes())
    print(f"stored forecast for {row['forecast_date']}: {lo}-{hi} ({row['band_low']} to {row['band_high']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--when-due", action="store_true")
    ap.add_argument("--image")
    args = ap.parse_args()

    now = datetime.now(SGT)
    tomorrow = (now + timedelta(days=1)).date()
    if args.when_due:
        if (now.hour, now.minute) < DUE:
            print(f"not due yet ({now:%H:%M} SGT, due {DUE[0]}:{DUE[1]})")
            return 0
        if any(r["forecast_date"] == tomorrow.isoformat() for r in load_rows()):
            print(f"forecast for {tomorrow} already stored")
            return 0

    header_date = None
    with tempfile.TemporaryDirectory() as tmp:
        if args.image:
            img = Path(args.image)
        else:
            html = http_get(PAGE).decode("utf-8", "replace")
            write_status(result="page fetched", html_bytes=len(html),
                         imgs=[i for i in re.findall(r"<img[^>]*>", html) if "health-advisory" in i][:6])
            gauge_url, header_url = find_images(html)
            header_date = date_from_filename(header_url)
            img = Path(tmp) / "gauge.png"
            sep = "&" if "?" in gauge_url else "?"
            img.write_bytes(http_get(f"{gauge_url}{sep}t={int(time.time())}"))
            write_status(result="gauge downloaded", gauge_url=gauge_url, header_url=header_url, image_bytes=img.stat().st_size)
            (DATA / "forecast_images").mkdir(parents=True, exist_ok=True)
            (DATA / "forecast_images" / "last_attempt.png").write_bytes(img.read_bytes())
            print(f"gauge image: {gauge_url}  header date: {header_date}")
        try:
            lo, hi, fdate = read_gauge(img)
        except Exception as e:  # noqa: BLE001
            write_status(result="ocr failed", error=str(e))
            raise
        if header_date and header_date != fdate:
            print(f"page is mid-update: gauge says {fdate}, header says {header_date}; will retry", file=sys.stderr)
            write_status(result="mid-update", gauge_date=str(fdate), header_date=str(header_date))
            return 1
        if args.when_due and fdate != tomorrow:
            print(f"page still shows the forecast for {fdate}, not {tomorrow} yet; will retry next cycle")
            write_status(result="page still shows old forecast", gauge_date=str(fdate), lo=lo, hi=hi)
            return 0
        store(fdate, lo, hi, img)
        write_status(result="stored", forecast_date=str(fdate), lo=lo, hi=hi)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        write_status(result="error", error=str(e))
        sys.exit(1)
