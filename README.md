# SportSG Haze Management Dashboard

Mobile dashboard showing 1-hour PM2.5 and 24-hour PSI for Singapore's five regions
(North, West, Central, East, South). Pill colours follow the NEA bands.

## How it works

```
data.gov.sg ──(every 15 min, GitHub Actions)──> data/*.csv + data/latest.json (this repo)
                                                        │
                                            index.html (GitHub Pages) reads latest.json
```

| File | Purpose |
|---|---|
| `scripts/fetch_data.py` | Fetches both readings, appends to the tables, writes `latest.json` |
| `.github/workflows/update-data.yml` | Runs the script every 15 minutes and commits changes |
| `data/pm25_1hr.csv` | Table of 1-hr PM2.5 readings (last 30 days) |
| `data/psi_24hr.csv` | Table of 24-hr PSI readings (last 30 days) |
| `data/latest.json` | Newest reading of each; the only file the page loads |
| `index.html` | The mobile page. It never calls data.gov.sg |

Each CSV row holds `fetched_at` (when we polled), `reading_timestamp` (NEA's own timestamp)
and the five regions. NEA updates these readings hourly, so several consecutive rows can
share the same `reading_timestamp`.

## One-time setup

1. Push this folder to a **public** GitHub repo.
2. Repo **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, folder = `/ (root)`.
3. Repo **Settings → Actions → General → Workflow permissions**: choose *Read and write permissions*.
4. Open the **Actions** tab, select *Update haze data*, click **Run workflow** once to fetch the first real reading.
5. (Optional) Get a free key from data.gov.sg and save it as repo secret `DATA_GOV_SG_API_KEY` for higher rate limits.

Until the first run completes, the page shows the shipped sample values with a "sample data" banner.

## Notes

- GitHub's scheduler can start runs a few minutes late, and may pause schedules on repos with no activity for 60 days. Re-enable from the Actions tab if that happens.
- Colour bands: PM2.5 Normal 0–55, Elevated 56–150, High 151–250, Very high ≥251.
  PSI Good 0–50, Moderate 51–100, Unhealthy 101–200, Very unhealthy 201–300, Hazardous >300.
