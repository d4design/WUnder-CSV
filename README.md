# WUnder CSV

Export 5-minute interval table data from a Weather Underground (WU) Personal Weather Station (PWS) dashboard to CSV, by station ID and date range. Data is scraped from the public dashboard (no API key) — the old WU API is deprecated, and the newer PWS Historical API only offers hourly/daily data, not 5-minute.

Python + Playwright (headless Chromium) for scraping, Tkinter for a console-free desktop UI (`.pyw`).

## Quick start

One-time setup on a new machine:

```bash
pip install -r requirements.txt
playwright install chromium
```

Then double-click `app.pyw` (or run `pythonw app.pyw`).

Enter a Station ID (from the dashboard URL, e.g. `IGOLDC915` from `wunderground.com/dashboard/pws/IGOLDC915`), a start/end date, units (metric/english), and time resolution (original 5-min, or resampled to 15/30/60-min), then click **Download CSV**.

## Files

- `wunder_csv.py` — scraping/CSV engine (Playwright + parsing/unit-conversion/resampling logic)
- `app.pyw` — Tkinter desktop UI
- `wu.ico` — app icon

## Building a standalone .exe

For machines without Python installed. The tricky part is that Playwright's Chromium binary isn't part of the pip package — it has to be bundled separately.

```bash
pip install pyinstaller
pyinstaller --onedir --windowed --name "WUnder CSV" --icon=wu.ico --collect-all playwright --collect-all tkcalendar app.pyw
```

Then copy both Chromium builds from the local Playwright cache into the dist folder (both are needed — headless launches use the headless-shell build by default, but Playwright falls back to the regular build in some paths):

```bash
cp -r "$LOCALAPPDATA/ms-playwright/chromium-1234" "dist/WUnder CSV/ms-playwright/"
cp -r "$LOCALAPPDATA/ms-playwright/chromium_headless_shell-1234" "dist/WUnder CSV/ms-playwright/"
```

(Version numbers in the folder names may differ — check `%LOCALAPPDATA%\ms-playwright` and match what's actually installed.)

`app.pyw` detects when it's frozen and points `PLAYWRIGHT_BROWSERS_PATH` at that bundled `ms-playwright` folder instead of the normal pip-managed cache.

Result is `dist/WUnder CSV/` — an ~850MB folder (mostly the two Chromium builds) that runs standalone via `WUnder CSV.exe`, no Python or pip install required on the target machine. Zip the whole folder to distribute it; the exe won't run correctly on its own without the `_internal` and `ms-playwright` folders alongside it. Not code-signed, so Windows SmartScreen will likely warn on first run on another machine ("Windows protected your PC" → More info → Run anyway).

## Notes

- Long date ranges can take a few minutes to scrape; a progress bar and Cancel button are provided.
- WU's dashboard paints the table once in a default unit set, then re-renders a second or so later once it applies the requested unit preference. The scraper waits for that render to settle and cross-checks with the scraped pressure reading (inHg vs mb ranges never overlap) rather than relying on a fixed delay or a temperature-only heuristic, to avoid picking up the pre-switch render.

## License

MIT.
