"""
WUnder CSV - core scraping/CSV engine.

Fetches 5-minute interval table data from the WU dashboard (public, no API
key) and returns CSV text.
"""

import csv
import io
import json
import math
import re
import time
from datetime import date, datetime, timedelta

from playwright.sync_api import sync_playwright

BASE_URL = "https://www.wunderground.com/dashboard/pws"
DELAY_S = 2.0  # be polite between page/date changes

# WU's Angular app reads its unit preference from this localStorage key on
# init ("metric" or "english"). Pre-setting it before any navigation makes
# every page load (each date's table) render natively in the requested
# units, instead of relying on a UI toggle click.
UNIT_STORAGE_KEY = "wu.Units"

COLUMNS = [
    "Temperature", "Dew Point", "Humidity", "Wind Direction",
    "Wind Speed", "Wind Gust", "Pressure", "Precip. Rate", "Precip. Accum.",
]

COMPASS_DEGREES = {
    "N": 0, "NORTH": 0,
    "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "EAST": 90,
    "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SOUTH": 180,
    "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WEST": 270,
    "WNW": 292.5, "NW": 315, "NNW": 337.5,
}
COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                   "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]

# Fixed reference point used only for bucketing arithmetic (day-of-week/
# timezone irrelevant, matches original's Date(ms)-round-trip trick).
_EPOCH = datetime(1970, 1, 1)


class ScrapeCancelled(Exception):
    pass


def table_url(station_id, date_str):
    return f"{BASE_URL}/{station_id}/table/{date_str}/{date_str}/daily"


def parse_date(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def date_range(start, end):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def strip_units(value):
    if value is None or value in ("", "--", "N/A"):
        return ""
    s = str(value).strip()
    # Degree symbols first, then trailing unit suffixes (order matters -
    # "72.3 °F" has its ° removed before the F/C/mph/etc suffix pass).
    s = re.sub(r"\s*°\s*", "", s)
    s = re.sub(r"\s*(mph|km/h|in|mm|%|mb|inHg|F|C)\s*$", "", s, flags=re.IGNORECASE)
    s = s.strip()
    return "" if s in ("", "°") else s


def to_number_or_null(v):
    s = strip_units(v)
    if not s:
        return None
    try:
        return float(s.replace(",", "").strip())
    except ValueError:
        return None


def round2(n):
    return round(n, 2)


# --- browser-side scraping -------------------------------------------------

_SCRAPE_JS = r"""
(dateStr) => {
  const result = { headers: [], rows: [], date: dateStr };
  const container = document.querySelector('lib-history-table');
  if (!container) {
    const tables = document.querySelectorAll('table');
    for (const table of tables) {
      const headerCells = table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td');
      const headers = Array.from(headerCells).map(c => c.textContent.trim());
      const dataRows = table.querySelectorAll('tbody tr, tr:not(:first-child)');
      const rows = [];
      dataRows.forEach(tr => {
        const cells = tr.querySelectorAll('td, th');
        if (cells.length) rows.push(Array.from(cells).map(c => c.textContent.trim()));
      });
      if (headers.length && rows.length > 0) {
        result.headers = headers;
        result.rows = rows;
        return result;
      }
    }
    return result;
  }
  const tbodies = container.querySelectorAll('tbody');
  const timeTbody = tbodies[0];
  const dataTbody = tbodies[1];
  if (!timeTbody || !dataTbody) return result;
  const times = Array.from(timeTbody.querySelectorAll('tr')).map(tr => tr.textContent.trim());
  const columns = ['Temperature', 'Dew Point', 'Humidity', 'Wind Direction', 'Wind Speed', 'Wind Gust', 'Pressure', 'Precip. Rate', 'Precip. Accum.'];
  const numCols = columns.length;
  const dataRows = dataTbody.querySelectorAll('tr');
  if (dataRows.length === 0) return result;
  result.headers = ['Time'].concat(columns);
  for (let r = 0; r < dataRows.length && r < times.length; r++) {
    const cells = dataRows[r].querySelectorAll('td');
    const row = [times[r] || ''];
    for (let c = 0; c < numCols; c++) {
      const cell = cells[c + 1]; // cells[0] is a duplicate AM/PM time column - skip it
      if (!cell) { row.push(''); continue; }
      if (c === 3) {
        row.push(cell.textContent.trim());
      } else {
        const span = cell.querySelector('span.wu-value, span.wu-value-to')
                  || cell.querySelector('span.wu-unit-no-value');
        row.push(span ? span.textContent.trim() : cell.textContent.trim());
      }
    }
    result.rows.push(row);
  }
  return result;
}
"""


def preset_unit_preference(page, units):
    key_json = json.dumps(UNIT_STORAGE_KEY)
    val_json = json.dumps(units)
    page.add_init_script(
        f"try {{ localStorage.setItem({key_json}, {val_json}); }} catch (e) {{}}"
    )


def scrape_date(page, station_id, date_str, settle_checks=5, settle_interval=1.0):
    url = table_url(station_id, date_str)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        pass
    try:
        page.wait_for_selector("lib-history-table, table", timeout=20000)
    except Exception:
        pass
    time.sleep(1)
    data = page.evaluate(_SCRAPE_JS, date_str)
    # WU's Angular app paints the table once in a default/cached unit set, then
    # re-renders a second or so later once it picks up the localStorage unit
    # preference set in preset_unit_preference() - a race, not a fixed delay.
    # Poll until two consecutive reads of the first row agree, so we capture
    # the settled render instead of a mid-switch one.
    for _ in range(settle_checks):
        time.sleep(settle_interval)
        next_data = page.evaluate(_SCRAPE_JS, date_str)
        if data["rows"] and next_data["rows"] and data["rows"][0] == next_data["rows"][0]:
            return next_data
        data = next_data
    return data


# --- CSV / units -------------------------------------------------------

def build_csv_header_with_units(headers, units):
    is_metric = units == "metric"
    unit_map = (
        [
            ("time", "Time"),
            ("temperature", "Temperature (°C)"),
            ("temp", "Temperature (°C)"),
            ("dew", "Dew Point (°C)"),
            ("point", "Dew Point (°C)"),
            ("humidity", "Humidity (%)"),
            ("speed", "Wind Speed (km/h)"),
            ("direction", "Wind Direction"),
            ("gust", "Wind Gust (km/h)"),
            ("wind", "Wind (km/h)"),
            ("pressure", "Pressure (mb)"),
            ("precip. rate", "Precip. Rate (mm/hr)"),
            ("precip. accum", "Precip. Accum. (mm)"),
            ("precip", "Precipitation (mm)"),
            ("rain", "Precipitation (mm)"),
            ("solar", "Solar Radiation"),
            ("uv", "UV"),
        ]
        if is_metric
        else [
            ("time", "Time"),
            ("temperature", "Temperature (°F)"),
            ("temp", "Temperature (°F)"),
            ("dew", "Dew Point (°F)"),
            ("point", "Dew Point (°F)"),
            ("humidity", "Humidity (%)"),
            ("speed", "Wind Speed (mph)"),
            ("direction", "Wind Direction"),
            ("gust", "Wind Gust (mph)"),
            ("wind", "Wind (mph)"),
            ("pressure", "Pressure (inHg)"),
            ("precip. rate", "Precip. Rate (in/hr)"),
            ("precip. accum", "Precip. Accum. (in)"),
            ("precip", "Precipitation (in)"),
            ("rain", "Precipitation (in)"),
            ("solar", "Solar Radiation"),
            ("uv", "UV"),
        ]
    )

    out = []
    for h in headers:
        lower = h.lower()
        label = h
        for key, l in unit_map:
            if key in lower:
                label = l
                break
        out.append(label)
    return out


def rows_to_csv_lines(headers_with_units, rows, strip_cell_units):
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(headers_with_units)
    for row in rows:
        cells = [strip_units(c) for c in row] if strip_cell_units else row
        writer.writerow(cells)
    return buf.getvalue().rstrip("\r\n")


def parse_datetime_from_row(row, time_col_index):
    if time_col_index < 0 or time_col_index >= len(row):
        return None
    v = str(row[time_col_index] or "").strip()
    if not v:
        return None

    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})(?:\s*(AM|PM))?", v, re.IGNORECASE
    )
    m2 = re.match(r"^(\d{1,2}):(\d{2})(?:\s*(AM|PM))?", v, re.IGNORECASE)

    year, month, day_ = 2000, 1, 1
    if m:
        year, month, day_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute, ampm = int(m.group(4)), int(m.group(5)), m.group(6)
    elif m2:
        hour, minute, ampm = int(m2.group(1)), int(m2.group(2)), m2.group(3)
    else:
        return None

    if ampm:
        ampm = ampm.upper()
        if ampm == "PM" and hour < 12:
            hour += 12
        if ampm == "AM" and hour == 12:
            hour = 0

    try:
        return datetime(year, month, day_, hour, minute)
    except ValueError:
        return None


def format_bucket_time(minutes_since_epoch, include_date=True):
    dt = _EPOCH + timedelta(minutes=minutes_since_epoch)
    return dt.strftime("%Y-%m-%d %H:%M") if include_date else dt.strftime("%H:%M")


def degrees_to_cardinal(deg):
    normalized = deg % 360
    idx = round(normalized / 22.5) % 16
    return COMPASS_POINTS[idx]


def circular_mean_cardinal(values):
    sum_x = sum_y = 0.0
    count = 0
    for v in values:
        if not v:
            continue
        s = str(v).strip().upper()
        if not s or s in ("--", "N/A", "CALM", "VAR", "VARIABLE"):
            continue
        deg = COMPASS_DEGREES.get(s)
        if deg is None:
            continue
        sum_x += math.sin(math.radians(deg))
        sum_y += math.cos(math.radians(deg))
        count += 1
    if count == 0:
        return "--"
    mean_x = sum_x / count
    mean_y = sum_y / count
    if math.hypot(mean_x, mean_y) < 0.05:
        return "Variable"
    return degrees_to_cardinal(math.degrees(math.atan2(mean_x, mean_y)))


def refactor_resolution(headers_with_units, rows, resolution_minutes):
    if not rows:
        return headers_with_units, rows

    lower_headers = [(h or "").lower() for h in headers_with_units]
    time_col_index = next(
        (i for i, h in enumerate(lower_headers) if "time" in h or "date" in h), -1
    )
    precip_rate_index = next(
        (i for i, h in enumerate(lower_headers) if "precip. rate" in h or "precip rate" in h), -1
    )
    precip_accum_index = next(
        (i for i, h in enumerate(lower_headers) if "precip. accum" in h or "precip accum" in h), -1
    )
    direction_col_index = next((i for i, h in enumerate(lower_headers) if "direction" in h), -1)

    buckets = {}
    for row in rows:
        dt = parse_datetime_from_row(row, time_col_index)
        if dt is None:
            continue
        minutes = int((dt - _EPOCH).total_seconds() // 60)
        bucket_index = minutes // resolution_minutes
        buckets.setdefault(bucket_index, []).append(row)

    out_rows = []
    for key in sorted(buckets.keys()):
        bucket_rows = buckets[key]
        if not bucket_rows:
            continue
        agg_row = [""] * len(headers_with_units)
        agg_row[time_col_index] = format_bucket_time(key * resolution_minutes, True)

        for col in range(len(headers_with_units)):
            if col == time_col_index:
                continue

            if col == direction_col_index and direction_col_index >= 0:
                agg_row[col] = circular_mean_cardinal([r[col] for r in bucket_rows])
                continue

            if col == precip_accum_index and precip_accum_index >= 0:
                # Running daily total - carry forward the last reading in the bucket.
                last_val = ""
                for r in bucket_rows:
                    n = to_number_or_null(r[col])
                    if n is not None:
                        last_val = str(round2(n))
                agg_row[col] = last_val
                continue

            if col == precip_rate_index:
                total_amount = 0.0
                any_val = False
                for r in bucket_rows:
                    n = to_number_or_null(r[col])
                    if n is not None:
                        # Convert rate (per hour) to amount over 5 minutes, then sum.
                        total_amount += n * (5 / 60)
                        any_val = True
                agg_row[col] = str(round2(total_amount)) if any_val else ""
                continue

            total = 0.0
            count = 0
            for r in bucket_rows:
                n = to_number_or_null(r[col])
                if n is not None:
                    total += n
                    count += 1
            agg_row[col] = str(round2(total / count)) if count else ""

        out_rows.append(agg_row)

    new_headers = list(headers_with_units)
    if precip_rate_index >= 0:
        is_metric = bool(re.search(r"mm/hr|mm/h", headers_with_units[precip_rate_index], re.IGNORECASE))
        new_headers[precip_rate_index] = "Precip. Amount (mm)" if is_metric else "Precip. Amount (in)"

    return new_headers, out_rows


def add_calculated_columns(headers, rows, units):
    # Insert a Wind Speed (m/s) column derived from the raw/resampled wind speed.
    ws_index = next((i for i, h in enumerate(headers) if re.search(r"wind speed", h, re.IGNORECASE)), -1)
    if ws_index < 0:
        return headers, rows

    to_ms = (lambda v: v / 3.6) if units == "metric" else (lambda v: v * 0.44704)

    new_headers = list(headers)
    new_headers.insert(ws_index + 1, "Wind Speed (m/s) (calculated)")

    new_rows = []
    for row in rows:
        r = list(row)
        raw = to_number_or_null(r[ws_index])
        r.insert(ws_index + 1, str(round2(to_ms(raw))) if raw is not None else "")
        new_rows.append(r)

    return new_headers, new_rows


def convert_row_to_metric_if_needed(row, should_convert):
    # Row shape: [Time, Temperature, Dew Point, Humidity, Wind Direction, Wind Speed, Wind Gust, Pressure, Precip Rate, Precip Accum]
    if not should_convert:
        return row
    out = list(row)

    temp_f = to_number_or_null(out[1])
    dew_f = to_number_or_null(out[2])
    wind_mph = to_number_or_null(out[5])
    gust_mph = to_number_or_null(out[6])
    press_inhg = to_number_or_null(out[7])
    pr_inhr = to_number_or_null(out[8])
    pa_in = to_number_or_null(out[9])

    if temp_f is not None:
        out[1] = str(round2((temp_f - 32) * (5 / 9)))
    if dew_f is not None:
        out[2] = str(round2((dew_f - 32) * (5 / 9)))
    if wind_mph is not None:
        out[5] = str(round2(wind_mph * 1.609344))
    if gust_mph is not None:
        out[6] = str(round2(gust_mph * 1.609344))
    if press_inhg is not None:
        out[7] = str(round2(press_inhg * 33.8638866667))  # mb (hPa)
    if pr_inhr is not None:
        out[8] = str(round2(pr_inhr * 25.4))  # mm/hr
    if pa_in is not None:
        out[9] = str(round2(pa_in * 25.4))  # mm

    return out


def should_convert_english_to_metric(sample_temp_value, sample_pressure_value=None):
    # Pressure is the reliable signal: inHg readings sit around 25-31, mb/hPa
    # readings sit around 950-1050 - non-overlapping ranges. Temperature is
    # used only as a fallback since °F and °C genuinely overlap in cold
    # weather (e.g. 47 is a plausible reading in either scale), which can
    # produce a false negative right when this safety net matters most.
    p = to_number_or_null(sample_pressure_value)
    if p is not None:
        return p < 200
    t = to_number_or_null(sample_temp_value)
    if t is None:
        return False
    return t > 55


# --- orchestration -------------------------------------------------------

def run_scrape(
    station_id,
    start_date,
    end_date,
    units="metric",
    resolution="original",
    progress_callback=None,
    should_cancel=None,
):
    """
    Scrapes stationId over [start_date, end_date] (YYYY-MM-DD strings) and
    returns (csv_text, filename). csv_text includes a UTF-8 BOM and a
    diagnostic metadata header, matching the original Node scraper's output.

    progress_callback(current, total, date_str, message) is called after
    each date is processed. should_cancel() is polled between dates; if it
    returns True, ScrapeCancelled is raised.
    """
    start = parse_date(start_date)
    end = parse_date(end_date)
    dates = list(date_range(start, end))
    total = len(dates)

    def report(current, day_str, message):
        if progress_callback:
            progress_callback(current, total, day_str, message)

    all_rows = []
    header_with_units = None
    metric_convert_mode = None
    page_units_detected = None
    mixed_units_detected = False
    cancelled = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context(
                viewport={"width": 1280, "height": 2000},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            preset_unit_preference(page, units)

            for i, d in enumerate(dates):
                if should_cancel and should_cancel():
                    cancelled = True
                    break

                date_str = d.isoformat()
                try:
                    data = scrape_date(page, station_id, date_str)
                    if not data["rows"]:
                        time.sleep(5)
                        data = scrape_date(page, station_id, date_str)

                    if data["headers"] and data["rows"]:
                        if header_with_units is None:
                            header_with_units = build_csv_header_with_units(data["headers"], units)
                        date_col = next(
                            (idx for idx, h in enumerate(data["headers"]) if re.search(r"time|date", h, re.IGNORECASE)),
                            -1,
                        )

                        date_convert_mode = False
                        if units == "metric":
                            date_convert_mode = should_convert_english_to_metric(data["rows"][0][1], data["rows"][0][7])
                            if page_units_detected is None:
                                page_units_detected = "english" if date_convert_mode else "metric"
                                metric_convert_mode = date_convert_mode
                            elif date_convert_mode != metric_convert_mode:
                                mixed_units_detected = True

                        for row in data["rows"]:
                            r = list(row)
                            if date_col >= 0 and r[date_col] and not re.match(r"^\d{4}-\d{2}-\d{2}", r[date_col]):
                                r[date_col] = f"{data['date']} {r[date_col]}"
                            if units == "metric":
                                all_rows.append(convert_row_to_metric_if_needed(r, date_convert_mode))
                            else:
                                all_rows.append(r)
                        report(i + 1, date_str, "ok")
                    else:
                        report(i + 1, date_str, "no data")
                except Exception as e:
                    report(i + 1, date_str, f"error: {e}")

                if i < total - 1:
                    time.sleep(DELAY_S)
        finally:
            browser.close()

    if cancelled:
        raise ScrapeCancelled()

    if header_with_units is None:
        header_with_units = (
            ["Time", "Temperature (°C)", "Dew Point (°C)", "Humidity (%)", "Wind Direction",
             "Wind Speed (km/h)", "Wind Gust (km/h)", "Pressure (mb)", "Precip. Rate (mm/hr)", "Precip. Accum. (mm)"]
            if units == "metric"
            else
            ["Time", "Temperature (°F)", "Dew Point (°F)", "Humidity (%)", "Wind Direction",
             "Wind Speed (mph)", "Wind Gust (mph)", "Pressure (inHg)", "Precip. Rate (in/hr)", "Precip. Accum. (in)"]
        )

    final_headers = header_with_units
    final_rows = all_rows

    if resolution != "original":
        minutes = int(resolution)
        if minutes in (15, 30, 60):
            final_headers, final_rows = refactor_resolution(header_with_units, all_rows, minutes)

    final_headers, final_rows = add_calculated_columns(final_headers, final_rows, units)

    csv_text = rows_to_csv_lines(final_headers, final_rows, True)

    if units == "metric":
        if page_units_detected == "metric" and metric_convert_mode is False:
            unit_method_desc = "Units pre-set via localStorage (wu.Units) before scraping - values scraped directly (°C / km/h / mm/hr / mm)"
        elif page_units_detected == "english" or metric_convert_mode is True:
            unit_method_desc = "WARNING: Page still showed English despite pre-setting wu.Units via localStorage - English values scraped and converted to metric (°F→°C / mph→km/h / in/hr→mm/hr / in→mm)"
        elif page_units_detected is None:
            unit_method_desc = "Unit state unknown - conversion applied based on temperature heuristic"
        else:
            unit_method_desc = f'Page units detected as "{page_units_detected}" - metric conversion {"applied" if metric_convert_mode else "skipped (already metric)"}'
        if mixed_units_detected:
            unit_method_desc += " - WARNING: page units drifted mid-scrape; some dates were detected and converted differently than the first date"
    else:
        unit_method_desc = "English units - values scraped directly (°F / mph / in/hr / in)"

    resolution_desc = "5-min (original)" if resolution == "original" else f"{resolution}-min (resampled from 5-min)"
    scraped = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S") + " UTC"
    meta_lines = "\n".join([
        f"# Station: {station_id}",
        f"# Date range: {start_date} to {end_date}",
        f"# Scraped: {scraped}",
        f"# Units: {units}",
        f"# Unit method: {unit_method_desc}",
        f"# Resolution: {resolution_desc}",
        "#",
    ]) + "\n"

    filename = f"wu_{station_id}_{start_date}_{end_date}.csv"
    return "﻿" + meta_lines + csv_text, filename
