"""
Cedar & Sage Physiotherapy — Reception Shift Schedule
=====================================================

A Shiny for Python app that runs entirely in the browser (shinylive/WASM) and
reads & writes plain CSV files on disk — either a local folder or a folder
synced by Google Drive for Desktop.

Data files (all in one folder):
    staff.csv     name
    template.csv  weekday,shift,staff        the regular weekly pattern
    shifts.csv    date,shift,staff,start,end the actual schedule (source of truth)
    timeoff.csv   staff,start,end,type,note
    holidays.csv  date,name                  BC + national stat holidays

Four ways to edit, all writing to the same shifts.csv:
    1. the week view quick-assign
    2. direct cell editing in the Shifts grid
    3. regenerating a date range from the weekly template
    4. applying time off, which opens up the affected shifts

Overtime follows BC rules, ported from build_schedule.py:
    daily OT   = hours over 8 in a day
    weekly OT  = hours over 40 in a Mon-Sun week, counting only the first 8/day,
                 credited to the pay period the week ends in
    stat hours = hours worked on a statutory holiday, reported separately
Flags are for review only, not payroll.
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import json
import pathlib
from typing import Any

import pandas as pd
from htmltools import Tag
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

# --------------------------------------------------------------------------
# brand + constants (kept in step with build_schedule.py)
# --------------------------------------------------------------------------

NAVY = "#2F4259"
CREAM = "#FAF7F1"
SAGE = "#7C9473"
LINE = "#E4DED2"
OPEN_RED = "#E57373"
GOLD = "#F3E2BE"
MUTED = "#8C8778"

PASTELS = [
    "#F8D7DA", "#D6E6F2", "#FBF0C9", "#D9EDDF", "#E8DAF0",
    "#FDE2CE", "#D2ECE6", "#F9D9EC", "#DEE6ED", "#E6F0D2",
    "#EADAE4", "#F0E6D2", "#DCE0F5", "#FADCD5", "#E4EADB",
]

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]
SHIFTS = ["AM", "PM"]
OPEN = "OPEN"
CLOSED = "CLOSED"

DEFAULT_TIMES = {"AM": ("08:00", "13:00"), "PM": ("13:00", "18:00")}

DAILY_OT_AFTER = 8.0
WEEKLY_OT_AFTER = 40.0

FILES = ["staff.csv", "template.csv", "shifts.csv", "timeoff.csv", "holidays.csv"]

SCHEMAS: dict[str, list[str]] = {
    "staff.csv": ["name"],
    "template.csv": ["weekday", "shift", "staff"],
    "shifts.csv": ["date", "shift", "staff", "start", "end"],
    "timeoff.csv": ["staff", "start", "end", "type", "note"],
    "holidays.csv": ["date", "name"],
}


# --------------------------------------------------------------------------
# pure helpers — no Shiny in here, so they can be unit tested
# --------------------------------------------------------------------------

def parse_time(s: Any) -> float | None:
    """'08:30' or '8:30 AM' -> hours as a float (8.5). None if unparseable."""
    if s is None:
        return None
    text = str(s).strip().upper()
    if not text:
        return None
    ampm = None
    for suffix in ("AM", "PM"):
        if text.endswith(suffix):
            ampm = suffix
            text = text[: -len(suffix)].strip()
            break
    if ":" not in text:
        text = text + ":00"
    try:
        hh_s, mm_s = text.split(":")[:2]
        hh, mm = int(hh_s), int(mm_s)
    except (ValueError, TypeError):
        return None
    if ampm == "PM" and hh != 12:
        hh += 12
    if ampm == "AM" and hh == 12:
        hh = 0
    if not (0 <= hh <= 24 and 0 <= mm < 60):
        return None
    return hh + mm / 60.0


def fmt_time(hours: float | None) -> str:
    if hours is None:
        return ""
    hh = int(hours) % 24
    mm = int(round((hours - int(hours)) * 60))
    return f"{hh:02d}:{mm:02d}"


def shift_hours(start: Any, end: Any) -> float:
    """Length of a shift in hours. Zero if either end is missing or reversed."""
    a, b = parse_time(start), parse_time(end)
    if a is None or b is None:
        return 0.0
    if b <= a:
        return 0.0
    return round(b - a, 4)


def parse_date(s: Any) -> dt.date | None:
    if isinstance(s, dt.datetime):
        return s.date()
    if isinstance(s, dt.date):
        return s
    text = str(s).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


# Day numbers are formatted by hand below: the no-padding strftime directive is
# a glibc extension and is NOT available in the Emscripten/WASM build of Python.
def fmt_day(d: dt.date) -> str:
    return f"{d:%a}, {d:%b} {d.day}"


def fmt_short(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


def fmt_long(d: dt.date) -> str:
    return f"{d:%B} {d.day}, {d.year}"


def empty(name: str) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in SCHEMAS[name]})


def coerce(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Force a loaded CSV into the expected columns, dropping blank rows."""
    cols = SCHEMAS[name]
    out = pd.DataFrame()
    for c in cols:
        out[c] = df[c].astype("object") if c in df.columns else pd.Series(dtype="object")
    out = out.fillna("")
    for c in cols:
        out[c] = out[c].map(lambda v: str(v).strip() if v != "" else "")
    key = cols[0]
    out = out[out[key] != ""].reset_index(drop=True)
    return out


def is_off(staff: str, day: dt.date, timeoff: pd.DataFrame) -> bool:
    """True if this person has approved time off covering that date."""
    if not staff or staff in (OPEN, CLOSED) or timeoff.empty:
        return False
    for _, row in timeoff.iterrows():
        if str(row["staff"]).strip() != staff:
            continue
        a, b = parse_date(row["start"]), parse_date(row["end"])
        if a and b and a <= day <= b:
            return True
    return False


def build_shifts(
    start: dt.date,
    end: dt.date,
    template: pd.DataFrame,
    timeoff: pd.DataFrame,
    existing: pd.DataFrame | None = None,
    keep_manual: bool = True,
) -> pd.DataFrame:
    """
    Generate weekday shift rows across a date range from the weekly template,
    blanking anyone who has time off. Rows already present in `existing` that
    differ from the template are preserved when keep_manual is True.
    """
    tmpl: dict[tuple[str, str], str] = {}
    for _, r in template.iterrows():
        wd, sh = str(r["weekday"]).strip()[:3].title(), str(r["shift"]).strip().upper()
        if wd in WEEKDAYS and sh in SHIFTS:
            tmpl[(wd, sh)] = str(r["staff"]).strip()

    manual: dict[tuple[str, str], dict[str, str]] = {}
    if existing is not None and not existing.empty and keep_manual:
        for _, r in existing.iterrows():
            d = parse_date(r["date"])
            if d is None:
                continue
            manual[(d.isoformat(), str(r["shift"]).strip().upper())] = {
                "staff": str(r["staff"]).strip(),
                "start": str(r["start"]).strip(),
                "end": str(r["end"]).strip(),
            }

    rows: list[dict[str, str]] = []
    day = start
    while day <= end:
        if day.weekday() < 5:
            wd = WEEKDAYS[day.weekday()]
            for sh in SHIFTS:
                key = (day.isoformat(), sh)
                who = tmpl.get((wd, sh), "")
                if not who or is_off(who, day, timeoff):
                    who = OPEN
                d_start, d_end = DEFAULT_TIMES[sh]
                if key in manual:
                    prev = manual[key]
                    prev_auto = tmpl.get((wd, sh), "")
                    changed = prev["staff"] not in ("", prev_auto)
                    if changed and not is_off(prev["staff"], day, timeoff):
                        who = prev["staff"]
                    d_start = prev["start"] or d_start
                    d_end = prev["end"] or d_end
                rows.append({
                    "date": day.isoformat(), "shift": sh, "staff": who,
                    "start": d_start, "end": d_end,
                })
        day += dt.timedelta(days=1)
    return pd.DataFrame(rows, columns=SCHEMAS["shifts.csv"])


def apply_timeoff(shifts: pd.DataFrame, timeoff: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Open up any shift where the assigned person now has time off."""
    if shifts.empty:
        return shifts, 0
    out = shifts.copy()
    hits = 0
    for i, row in out.iterrows():
        d = parse_date(row["date"])
        who = str(row["staff"]).strip()
        if d and is_off(who, d, timeoff):
            out.at[i, "staff"] = OPEN
            hits += 1
    return out, hits


def hours_table(shifts: pd.DataFrame, staff: list[str]) -> pd.DataFrame:
    """One row per date, one column per person, values = hours worked."""
    if shifts.empty or not staff:
        return pd.DataFrame(columns=["date"] + staff)
    acc: dict[dt.date, dict[str, float]] = {}
    for _, r in shifts.iterrows():
        d = parse_date(r["date"])
        who = str(r["staff"]).strip()
        if d is None or who in ("", OPEN, CLOSED):
            continue
        acc.setdefault(d, {})
        acc[d][who] = acc[d].get(who, 0.0) + shift_hours(r["start"], r["end"])
    rows = []
    for d in sorted(acc):
        row: dict[str, Any] = {"date": d.isoformat()}
        for s in staff:
            row[s] = round(acc[d].get(s, 0.0), 2)
        rows.append(row)
    return pd.DataFrame(rows, columns=["date"] + staff)


def overtime(
    shifts: pd.DataFrame, staff: list[str], holidays: set[dt.date]
) -> pd.DataFrame:
    """
    Per person: total hours, daily OT, weekly OT and stat hours, split by
    pay period (1st-15th, 16th-month end). Weekly OT is credited to the pay
    period containing the last day of that Mon-Sun week.
    """
    ht = hours_table(shifts, staff)
    cols = ["staff", "period", "hours", "daily_ot", "weekly_ot", "stat_hours"]
    if ht.empty:
        return pd.DataFrame(columns=cols)

    def period_of(d: dt.date) -> str:
        last = calendar.monthrange(d.year, d.month)[1]
        return (f"{d:%Y-%m} 1-15" if d.day <= 15 else f"{d:%Y-%m} 16-{last}")

    totals: dict[tuple[str, str], dict[str, float]] = {}

    def bucket(who: str, per: str) -> dict[str, float]:
        return totals.setdefault(
            (who, per),
            {"hours": 0.0, "daily_ot": 0.0, "weekly_ot": 0.0, "stat_hours": 0.0},
        )

    # daily pass
    weekly: dict[tuple[str, dt.date], float] = {}
    for _, row in ht.iterrows():
        d = parse_date(row["date"])
        if d is None:
            continue
        per = period_of(d)
        for who in staff:
            hrs = float(row[who] or 0.0)
            if hrs <= 0:
                continue
            b = bucket(who, per)
            b["hours"] += hrs
            b["daily_ot"] += max(0.0, hrs - DAILY_OT_AFTER)
            if d in holidays:
                b["stat_hours"] += hrs
            capped = min(hrs, DAILY_OT_AFTER)
            weekly[(who, monday_of(d))] = weekly.get((who, monday_of(d)), 0.0) + capped

    # weekly pass — credit to the period holding the week's final day
    for (who, wk_start), capped_total in weekly.items():
        extra = max(0.0, capped_total - WEEKLY_OT_AFTER)
        if extra <= 0:
            continue
        bucket(who, period_of(wk_start + dt.timedelta(days=6)))["weekly_ot"] += extra

    out = [
        {"staff": who, "period": per,
         "hours": round(v["hours"], 2), "daily_ot": round(v["daily_ot"], 2),
         "weekly_ot": round(v["weekly_ot"], 2), "stat_hours": round(v["stat_hours"], 2)}
        for (who, per), v in totals.items()
    ]
    df = pd.DataFrame(out, columns=cols)
    return df.sort_values(["period", "staff"]).reset_index(drop=True) if not df.empty else df


def colour_for(name: str, staff: list[str]) -> str:
    if name in (OPEN, "", CLOSED):
        return "#EFEFEF"
    try:
        return PASTELS[staff.index(name) % len(PASTELS)]
    except ValueError:
        return "#EFEFEF"


# --------------------------------------------------------------------------
# seed data — bundled so the app opens with something to look at
# --------------------------------------------------------------------------

SEED: dict[str, pd.DataFrame] = {
    "staff.csv": pd.DataFrame({"name": ["Sarah M.", "Jordan T."]}),
    "template.csv": pd.DataFrame([
        {"weekday": wd, "shift": sh, "staff": "Sarah M." if sh == "AM" else "Jordan T."}
        for wd in WEEKDAYS for sh in SHIFTS
    ]),
    "timeoff.csv": pd.DataFrame([
        {"staff": "Sarah M.", "start": "2026-08-10", "end": "2026-08-14",
         "type": "Vacation", "note": "Example — replace"},
    ]),
    "holidays.csv": pd.DataFrame([
        {"date": "2026-01-01", "name": "New Year's Day"},
        {"date": "2026-02-16", "name": "Family Day (BC)"},
        {"date": "2026-04-03", "name": "Good Friday"},
        {"date": "2026-05-18", "name": "Victoria Day"},
        {"date": "2026-07-01", "name": "Canada Day"},
        {"date": "2026-08-03", "name": "BC Day"},
        {"date": "2026-09-07", "name": "Labour Day"},
        {"date": "2026-09-30", "name": "National Day for Truth and Reconciliation"},
        {"date": "2026-10-12", "name": "Thanksgiving"},
        {"date": "2026-11-11", "name": "Remembrance Day"},
        {"date": "2026-12-25", "name": "Christmas Day"},
    ]),
}
SEED["shifts.csv"] = build_shifts(
    dt.date(2026, 8, 1), dt.date(2026, 9, 30),
    SEED["template.csv"], SEED["timeoff.csv"],
)


# ==========================================================================
# storage: browser localStorage, a local folder, or Google Drive
# ==========================================================================

DATA_DIR = pathlib.Path(__file__).parent / "data"


def load_bundled() -> tuple[dict[str, pd.DataFrame], str]:
    """
    Read the CSVs shipped alongside the app. `shinylive export` packs anything
    under the app directory into app.json, so these travel with the deployed
    site. Falls back to SEED for anything missing or unreadable.
    """
    out: dict[str, pd.DataFrame] = {}
    found = 0
    for name in FILES:
        try:
            out[name] = coerce(
                pd.read_csv(DATA_DIR / name, dtype=str, keep_default_na=False), name)
            found += 1
        except Exception:
            out[name] = SEED[name].copy()
    return out, (f"bundled ({found}/{len(FILES)})" if found else "built-in example")


class Store:
    def __init__(self) -> None:
        self.data, self.folder = load_bundled()
        self.source = "bundled"
        self.dirty = False

    def staff_names(self) -> list[str]:
        return [s for s in self.data["staff.csv"]["name"].tolist() if str(s).strip()]

    def holiday_set(self) -> set[dt.date]:
        out = set()
        for _, r in self.data["holidays.csv"].iterrows():
            d = parse_date(r["date"])
            if d:
                out.add(d)
        return out

    def to_csv_map(self) -> dict[str, str]:
        return {name: df.to_csv(index=False) for name, df in self.data.items()}

    def load_csv_map(self, blob: dict[str, str]) -> list[str]:
        loaded = []
        for name in FILES:
            text = blob.get(name)
            if not text:
                continue
            try:
                df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
            except Exception:
                continue
            self.data[name] = coerce(df, name)
            loaded.append(name)
        return loaded


STORE = Store()

TIME_CHOICES = [fmt_time(6 + i * 0.25) for i in range(int((22 - 6) / 0.25) + 1)]


# ==========================================================================
# browser glue: capability probe, localStorage, File System Access, Drive
# ==========================================================================
#
# Everything here is defensive on purpose. The previous version registered a
# Shiny message handler at parse time, before Shiny existed, which threw and
# silently took the save path down with it. Now nothing touches Shiny until
# it is actually ready, queued messages are flushed once it is, and failures
# are written into a banner in the DOM so they are visible even if the Shiny
# connection is the thing that broke.

BROWSER_JS = r"""
(function () {
  var LS_KEY = "cedarsage.schedule.v1";
  var dirHandle = null;
  var queue = [];
  var SHEET_KEY = "cedarsage.sheeturl";
  var sheetToken = null;
  var sheetId = null;

  // ---------------------------------------------------------------- utils
  function send(name, value) {
    if (window.Shiny && window.Shiny.setInputValue) {
      try { window.Shiny.setInputValue(name, value, { priority: "event" }); return; }
      catch (e) { /* fall through to queue */ }
    }
    queue.push([name, value]);
  }

  function flush() {
    while (queue.length && window.Shiny && window.Shiny.setInputValue) {
      var m = queue.shift();
      try { window.Shiny.setInputValue(m[0], m[1], { priority: "event" }); }
      catch (e) { break; }
    }
  }

  function toast(msg, kind) {
    var host = document.getElementById("cs-toast");
    if (!host) { console.log("[schedule] " + msg); return; }
    host.textContent = msg;
    host.className = "cs-toast show " + (kind || "info");
    clearTimeout(host._t);
    host._t = setTimeout(function () { host.className = "cs-toast"; }, 4200);
  }

  function capabilities() {
    var storage = false;
    try { localStorage.setItem("_cs_t", "1"); localStorage.removeItem("_cs_t"); storage = true; }
    catch (e) { storage = false; }
    return {
      fsa: typeof window.showDirectoryPicker === "function",
      secure: !!window.isSecureContext,
      storage: storage,
      framed: window.top !== window.self,
      origin: location.origin
    };
  }

  // ------------------------------------------------------- local storage
  function saveLocal(files) {
    try { localStorage.setItem(LS_KEY, JSON.stringify({ t: Date.now(), files: files })); }
    catch (e) { toast("Browser storage is full — changes are not being kept locally.", "warn"); }
  }

  function readLocal() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) { return null; }
  }

  // -------------------------------------------------- File System Access
  async function openFolder() {
    var c = capabilities();
    if (!c.secure) {
      toast("This page is not a secure context, so folders can't be opened. Use https or localhost.", "warn");
      return;
    }
    if (!c.fsa) {
      toast("This browser has no folder access. Use Chrome or Edge, or sign in to Google Drive.", "warn");
      return;
    }
    try {
      var dir = await window.showDirectoryPicker({ mode: "readwrite", id: "cedarsage" });
      if (dir.requestPermission) {
        var perm = await dir.requestPermission({ mode: "readwrite" });
        if (perm !== "granted") { toast("Write permission was declined.", "warn"); return; }
      }
      dirHandle = dir;
      var files = {};
      for await (var entry of dir.values()) {
        if (entry.kind === "file" && entry.name.toLowerCase().endsWith(".csv")) {
          files[entry.name] = await (await entry.getFile()).text();
        }
      }
      send("fs_load", JSON.stringify({ folder: dir.name, files: files, source: "folder" }));
    } catch (e) {
      if (e && e.name === "AbortError") return;
      toast("Could not open that folder: " + (e && e.message ? e.message : e), "error");
    }
  }

  async function writeFolder(files) {
    if (!dirHandle) return false;
    for (var name in files) {
      var h = await dirHandle.getFileHandle(name, { create: true });
      var w = await h.createWritable();
      await w.write(files[name]);
      await w.close();
    }
    return true;
  }

  // ------------------------------------------------------- Google Sheets
  // One spreadsheet, one tab per table. Reading and writing both go through
  // the Sheets API with a token from Google Identity Services, so the sheet's
  // own sharing settings decide who may edit and every change is attributed
  // in its version history.
  var TABS = ["staff", "template", "shifts", "timeoff", "holidays"];

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[src="' + src + '"]')) return resolve();
      var s = document.createElement("script");
      s.src = src; s.async = true; s.defer = true;
      s.onload = resolve; s.onerror = function () { reject(new Error("blocked: " + src)); };
      document.head.appendChild(s);
    });
  }

  function sheetIdFrom(url) {
    var t = String(url || "").trim();
    var m = t.match(/\/spreadsheets\/d\/([a-zA-Z0-9\-_]+)/);
    if (m) return m[1];
    return /^[a-zA-Z0-9\-_]{20,}$/.test(t) ? t : null;
  }

  // --- minimal CSV <-> grid, quoting to Sheets' expectations -------------
  function toCsv(rows) {
    return (rows || []).map(function (r) {
      return (r || []).map(function (c) {
        c = c === null || c === undefined ? "" : String(c);
        return /[",\n]/.test(c) ? '"' + c.replace(/"/g, '""') + '"' : c;
      }).join(",");
    }).join("\n");
  }

  function fromCsv(text) {
    var rows = [], row = [], cur = "", q = false;
    text = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    for (var i = 0; i < text.length; i++) {
      var ch = text[i];
      if (q) {
        if (ch === '"' && text[i + 1] === '"') { cur += '"'; i++; }
        else if (ch === '"') { q = false; }
        else { cur += ch; }
      } else if (ch === '"') { q = true; }
      else if (ch === ",") { row.push(cur); cur = ""; }
      else if (ch === "\n") { row.push(cur); rows.push(row); row = []; cur = ""; }
      else { cur += ch; }
    }
    if (cur !== "" || row.length) { row.push(cur); rows.push(row); }
    return rows;
  }

  function gfetch(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign(
      { Authorization: "Bearer " + sheetToken, "Content-Type": "application/json" },
      opts.headers || {});
    return fetch(url, opts).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          throw new Error("Sheets API " + r.status + " — " +
                          (t || "").slice(0, 160));
        });
      }
      return r;
    });
  }

  var API = "https://sheets.googleapis.com/v4/spreadsheets/";

  function signIn(cfg) {
    return loadScript("https://accounts.google.com/gsi/client").then(function () {
      return new Promise(function (resolve, reject) {
        var tc = google.accounts.oauth2.initTokenClient({
          client_id: cfg.client_id,
          scope: "https://www.googleapis.com/auth/spreadsheets",
          callback: function (r) {
            if (r.error) reject(new Error(r.error));
            else resolve(r.access_token);
          }
        });
        tc.requestAccessToken({ prompt: sheetToken ? "" : "consent" });
      });
    });
  }

  async function tabTitles() {
    var meta = await (await gfetch(API + sheetId + "?fields=sheets.properties.title")).json();
    return (meta.sheets || []).map(function (s) { return s.properties.title; });
  }

  async function sheetLoad() {
    var have = await tabTitles();
    var want = TABS.filter(function (t) { return have.indexOf(t) >= 0; });
    if (!want.length) {
      toast("That sheet has no matching tabs yet — press Save to set it up.", "warn");
      send("sheet_ready", JSON.stringify({ id: sheetId }));
      return;
    }
    var qs = want.map(function (t) { return "ranges=" + encodeURIComponent(t); }).join("&");
    var got = await (await gfetch(API + sheetId + "/values:batchGet?" + qs)).json();
    var files = {};
    (got.valueRanges || []).forEach(function (vr, i) {
      files[want[i] + ".csv"] = toCsv(vr.values || []);
    });
    send("fs_load", JSON.stringify({
      folder: "Google Sheet", files: files, source: "sheet"
    }));
  }

  async function sheetWrite(files) {
    var have = await tabTitles();
    var missing = TABS.filter(function (t) { return have.indexOf(t) < 0; });
    if (missing.length) {
      await gfetch(API + sheetId + ":batchUpdate", {
        method: "POST",
        body: JSON.stringify({
          requests: missing.map(function (t) {
            return { addSheet: { properties: { title: t } } };
          })
        })
      });
    }
    // clear first, so removing a row actually removes it
    await gfetch(API + sheetId + "/values:batchClear", {
      method: "POST", body: JSON.stringify({ ranges: TABS })
    });
    await gfetch(API + sheetId + "/values:batchUpdate", {
      method: "POST",
      body: JSON.stringify({
        valueInputOption: "RAW",
        data: TABS.map(function (t) {
          return { range: t, values: fromCsv(files[t + ".csv"] || "") };
        })
      })
    });
  }

  async function sheetConnect(cfg) {
    var input = document.getElementById("sheet_url");
    var url = input ? input.value : "";
    if (!url) { url = localStorage.getItem(SHEET_KEY) || ""; }
    var id = sheetIdFrom(url);
    if (!id) { toast("Paste the full Google Sheet link first.", "warn"); return; }
    if (!cfg.client_id) {
      toast("No Google client ID configured yet — see the README.", "warn");
      return;
    }
    try {
      sheetToken = await signIn(cfg);
      sheetId = id;
      try { localStorage.setItem(SHEET_KEY, url); } catch (e) {}
      await sheetLoad();
      toast("Connected to the sheet.", "ok");
    } catch (e) {
      toast("Sheet: " + (e && e.message ? e.message : e), "error");
    }
  }

  // ------------------------------------------------------------- actions
  document.addEventListener("click", function (ev) {
    var el = ev.target.closest("[data-cs]");
    if (!el) return;
    var action = el.getAttribute("data-cs");

    if (action === "open-folder") { openFolder(); return; }

    if (action === "sheet-connect") {
      var cfg = {};
      try { cfg = JSON.parse(el.getAttribute("data-cfg") || "{}"); } catch (e) {}
      sheetConnect(cfg);
      return;
    }

    if (action === "shift") {
      send("cell_click", JSON.stringify({
        date: el.getAttribute("data-date"),
        shift: el.getAttribute("data-shift"),
        n: Math.random()
      }));
      return;
    }
  });

  // ------------------------------------------------- messages from Shiny
  function onSave(msg) {
    var files = msg.files;
    saveLocal(files);
    (async function () {
      try {
        if (sheetToken && sheetId) {
          await sheetWrite(files);
          toast("Saved to the Google Sheet.", "ok");
        } else if (dirHandle) {
          await writeFolder(files);
          toast("Saved to " + dirHandle.name + ".", "ok");
        } else {
          toast("Kept in this browser. Connect a Google Sheet to save it there.", "info");
        }
        send("save_done", Date.now());
      } catch (e) {
        toast("Save failed: " + (e && e.message ? e.message : e), "error");
      }
    })();
  }

  function onAutosave(msg) { saveLocal(msg.files); }

  // Registering the message handlers only needs the Shiny object to exist.
  function wire() {
    if (!window.Shiny || !window.Shiny.addCustomMessageHandler) {
      return setTimeout(wire, 60);
    }
    window.Shiny.addCustomMessageHandler("cs_save", onSave);
    window.Shiny.addCustomMessageHandler("cs_autosave", onAutosave);
  }
  wire();

  // Sending inputs is different: setInputValue before the session connects is
  // dropped on the floor, which is why the restore-from-browser silently did
  // nothing. Wait for shiny:connected, and guard against having missed it.
  var announced = false;
  function afterConnect() {
    if (announced) return;
    announced = true;
    flush();
    send("cs_caps", JSON.stringify(capabilities()));
    try {
      var savedUrl = localStorage.getItem(SHEET_KEY);
      var box = document.getElementById("sheet_url");
      if (savedUrl && box) box.value = savedUrl;
    } catch (e) {}
    var cached = readLocal();
    if (cached && cached.files && Object.keys(cached.files).length) {
      send("fs_load", JSON.stringify({
        folder: "this browser", files: cached.files, source: "browser"
      }));
    }
  }
  document.addEventListener("shiny:connected", afterConnect);
  (function poll(n) {
    if (announced) return;
    if (window.Shiny && window.Shiny.shinyapp && window.Shiny.shinyapp.$socket) {
      return afterConnect();
    }
    if (n < 200) setTimeout(function () { poll(n + 1); }, 100);
  })(0);
})();
"""

ICONIFY = "https://cdn.jsdelivr.net/npm/iconify-icon@2/dist/iconify-icon.min.js"
FONT = "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap"

# Mantine's design language, rebuilt as plain CSS: its 10-step gray ramp,
# radius and shadow scales, and Inter as the type face. Dash Mantine
# Components itself is Dash-only and cannot run inside Shiny.
MANTINE_CSS = f"""
:root {{
  --m-white:#fff;
  --m-gray-0:#f8f9fa; --m-gray-1:#f1f3f5; --m-gray-2:#e9ecef; --m-gray-3:#dee2e6;
  --m-gray-4:#ced4da; --m-gray-5:#adb5bd; --m-gray-6:#868e96; --m-gray-7:#495057;
  --m-gray-8:#343a40; --m-gray-9:#212529;
  --m-primary:{NAVY}; --m-primary-hover:#25344a; --m-primary-light:#eef1f5;
  --m-sage:{SAGE}; --m-red:{OPEN_RED}; --m-gold:{GOLD};
  --m-radius-sm:4px; --m-radius:8px; --m-radius-lg:12px; --m-radius-xl:16px;
  --m-shadow-xs:0 1px 3px rgba(0,0,0,.05);
  --m-shadow-sm:0 1px 3px rgba(0,0,0,.05),0 10px 15px -5px rgba(0,0,0,.05);
  --m-shadow-md:0 1px 3px rgba(0,0,0,.05),0 12px 20px -8px rgba(0,0,0,.08);
}}
*{{box-sizing:border-box;}}
body{{
  font-family:Inter,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;
  font-size:14px; line-height:1.55; color:var(--m-gray-9);
  background:var(--m-gray-0); -webkit-font-smoothing:antialiased;
}}
iconify-icon{{vertical-align:-.18em;}}

/* ---------- shell ---------- */
.cs-header{{
  background:var(--m-white); border-bottom:1px solid var(--m-gray-2);
  padding:14px 20px; display:flex; align-items:center; gap:14px;
  position:sticky; top:0; z-index:30;
}}
.cs-brand{{font-size:16px; font-weight:600; color:var(--m-primary); letter-spacing:-.01em;}}
.cs-brand small{{display:block; font-size:12px; font-weight:400; color:var(--m-gray-6);
  letter-spacing:0; margin-top:-2px;}}
.cs-spacer{{margin-left:auto;}}
.cs-shell{{max-width:1140px; margin:0 auto; padding:20px;}}

/* ---------- buttons ---------- */
.m-btn{{
  display:inline-flex; align-items:center; gap:7px; height:36px; padding:0 16px;
  border-radius:var(--m-radius); border:1px solid transparent; cursor:pointer;
  font-family:inherit; font-size:14px; font-weight:500; white-space:nowrap;
  background:var(--m-primary); color:#fff; transition:background .12s,border-color .12s;
}}
.m-btn:hover{{background:var(--m-primary-hover);}}
.m-btn:active{{transform:translateY(1px);}}
.m-btn.light{{background:var(--m-primary-light); color:var(--m-primary);}}
.m-btn.light:hover{{background:#e3e8ef;}}
.m-btn.default{{background:var(--m-white); color:var(--m-gray-9); border-color:var(--m-gray-3);}}
.m-btn.default:hover{{background:var(--m-gray-0);}}
.m-btn.subtle{{background:transparent; color:var(--m-gray-7);}}
.m-btn.subtle:hover{{background:var(--m-gray-1);}}
.m-btn.sm{{height:30px; padding:0 12px; font-size:13px;}}
.m-btn.danger{{background:var(--m-red);}}
.m-btn.block{{width:100%; justify-content:center;}}

/* ---------- cards ---------- */
.m-card{{
  background:var(--m-white); border:1px solid var(--m-gray-2);
  border-radius:var(--m-radius-lg); box-shadow:var(--m-shadow-xs); margin-bottom:16px;
}}
.m-card-head{{
  padding:14px 18px; border-bottom:1px solid var(--m-gray-2);
  display:flex; align-items:center; gap:9px; font-weight:600; color:var(--m-gray-9);
}}
.m-card-head .sub{{font-weight:400; color:var(--m-gray-6); font-size:13px; margin-left:auto;}}
.m-card-body{{padding:18px;}}
.m-card-body.tight{{padding:10px 12px;}}

/* ---------- day cards ---------- */
.cs-days{{display:grid; grid-template-columns:repeat(5,1fr); gap:14px;}}
@media (max-width:1100px){{.cs-days{{grid-template-columns:repeat(2,1fr);}}}}
.cs-day{{
  background:var(--m-white); border:1px solid var(--m-gray-2);
  border-radius:var(--m-radius-lg); overflow:hidden; box-shadow:var(--m-shadow-xs);
}}
.cs-day.today{{border-color:var(--m-primary); box-shadow:0 0 0 3px var(--m-primary-light);}}
.cs-day.stat{{border-color:#e8d6a8;}}
.cs-day-head{{padding:11px 14px; border-bottom:1px solid var(--m-gray-2); background:var(--m-gray-0);}}
.cs-day.stat .cs-day-head{{background:var(--m-gold);}}
.cs-dow{{font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--m-gray-6);}}
.cs-dnum{{font-size:17px; font-weight:600; color:var(--m-gray-9); line-height:1.2;}}
.cs-stat-name{{font-size:11px; color:#8A6A2F; margin-top:2px;}}
.cs-slot{{
  display:flex; align-items:center; gap:9px; padding:11px 14px; cursor:pointer;
  border-top:1px solid var(--m-gray-1); transition:background .1s;
}}
.cs-slot:hover{{background:var(--m-gray-0);}}
.cs-slot:first-of-type{{border-top:0;}}
.cs-slot .lab{{
  font-size:11px; font-weight:600; color:var(--m-gray-5); width:22px; flex:0 0 22px;
}}
.cs-slot .edit{{margin-left:auto; color:var(--m-gray-4); font-size:15px;}}
.cs-slot:hover .edit{{color:var(--m-primary);}}
.cs-times{{font-size:11px; color:var(--m-gray-6); margin-top:2px;}}
.m-badge{{
  display:inline-block; padding:3px 11px; border-radius:14px; font-size:13px;
  font-weight:600; color:var(--m-primary); background:var(--m-gray-2);
  max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}
.m-badge.open{{background:#ffe3e3; color:#c92a2a;}}
.m-badge.closed{{background:var(--m-gray-2); color:var(--m-gray-6);}}

/* ---------- misc ---------- */
.m-alert{{
  display:flex; gap:10px; padding:12px 14px; border-radius:var(--m-radius);
  background:var(--m-primary-light); color:var(--m-gray-8); font-size:13px;
  margin-bottom:14px; align-items:flex-start;
}}
.m-alert.warn{{background:#fff4e6; color:#8a5a1f;}}
.m-alert.ok{{background:#ebfbee; color:#2b6a35;}}
.m-alert iconify-icon{{font-size:17px; flex:0 0 auto; margin-top:1px;}}
.cs-status{{display:flex; align-items:center; gap:7px; font-size:13px; color:var(--m-gray-6);}}
.cs-dot{{width:8px; height:8px; border-radius:4px; background:var(--m-gray-4);}}
.cs-dot.on{{background:var(--m-sage);}}
.cs-dot.warn{{background:#f59f00;}}
.cs-toast{{
  position:fixed; left:50%; bottom:26px; transform:translateX(-50%) translateY(120%);
  background:var(--m-gray-9); color:#fff; padding:11px 18px; border-radius:var(--m-radius);
  font-size:13px; z-index:100; box-shadow:var(--m-shadow-md); opacity:0;
  transition:transform .18s,opacity .18s; max-width:88vw; text-align:center;
}}
.cs-toast.show{{transform:translateX(-50%) translateY(0); opacity:1;}}
.cs-toast.ok{{background:#2b8a3e;}} .cs-toast.error{{background:#c92a2a;}}
.cs-toast.warn{{background:#e8590c;}}

/* ---------- form controls ---------- */
.form-control,.form-select,.selectize-input{{
  border:1px solid var(--m-gray-3)!important; border-radius:var(--m-radius)!important;
  font-size:14px!important; min-height:36px!important; box-shadow:none!important;
  color:var(--m-gray-9)!important;
}}
.form-control:focus,.form-select:focus,.selectize-input.focus{{
  border-color:var(--m-primary)!important; box-shadow:0 0 0 3px var(--m-primary-light)!important;
}}
.control-label,label{{font-size:13px!important; font-weight:500; color:var(--m-gray-7); margin-bottom:5px;}}

/* ---------- tabs ---------- */
.nav-tabs{{border-bottom:1px solid var(--m-gray-2); gap:2px; margin-bottom:18px;}}
.nav-tabs .nav-link{{
  border:0!important; border-bottom:2px solid transparent!important;
  color:var(--m-gray-6); font-weight:500; font-size:14px; padding:9px 15px;
  display:flex; align-items:center; gap:6px; background:transparent!important;
}}
.nav-tabs .nav-link:hover{{color:var(--m-gray-9); background:var(--m-gray-1)!important;
  border-radius:var(--m-radius) var(--m-radius) 0 0;}}
.nav-tabs .nav-link.active{{color:var(--m-primary)!important;
  border-bottom-color:var(--m-primary)!important;}}

/* ---------- modal ---------- */
.modal-content{{border:0; border-radius:var(--m-radius-lg); box-shadow:var(--m-shadow-md);}}
.modal-header{{border-bottom:1px solid var(--m-gray-2); padding:16px 20px;}}
.modal-title{{font-size:16px; font-weight:600; color:var(--m-gray-9);}}
.modal-body{{padding:20px;}}
.modal-footer{{border-top:1px solid var(--m-gray-2); padding:12px 20px;}}

/* ---------- data grid ---------- */
.shiny-data-frame table{{font-size:13px;}}
.shiny-data-frame thead th{{
  background:var(--m-gray-0)!important; font-weight:600!important;
  color:var(--m-gray-7)!important; border-bottom:1px solid var(--m-gray-2)!important;
}}
.shiny-data-frame td{{border-color:var(--m-gray-1)!important;}}

@media (max-width:640px){{
  .cs-shell{{padding:12px;}} .cs-days{{grid-template-columns:1fr;}}
}}
"""


def icon(name: str, size: str = "16px", colour: str | None = None):
    """An Iconify web component. Tabler icons, the set Mantine itself uses."""
    style = f"font-size:{size};" + (f"color:{colour};" if colour else "")
    return Tag("iconify-icon", icon=name, style=style)


def card(title: str, icon_name: str, *body, sub: str = "", tight: bool = False):
    head = [icon(title and icon_name or icon_name, "17px", NAVY), title]
    if sub:
        head.append(ui.tags.span(sub, class_="sub"))
    return ui.tags.div(
        ui.tags.div(*head, class_="m-card-head"),
        ui.tags.div(*body, class_=f"m-card-body{' tight' if tight else ''}"),
        class_="m-card",
    )


def btn(label: str, icon_name: str, variant: str = "", **kw):
    """
    A Mantine-styled button. When given an id it also carries Bootstrap's
    `action-button` class, which is what Shiny binds to — so these behave as
    ordinary action buttons on the server despite the custom markup.
    """
    cls = f"m-btn {variant}".strip()
    if "id" in kw:
        cls += " action-button"
    return ui.tags.button(icon(icon_name), label, class_=cls, type="button", **kw)


def alert(text: str, kind: str = "", icon_name: str = "tabler:info-circle"):
    return ui.tags.div(icon(icon_name), ui.tags.div(text), class_=f"m-alert {kind}".strip())


# ==========================================================================
# UI
# ==========================================================================

def tab_label(name: str, icon_name: str):
    return ui.tags.span(icon(icon_name), name)


app_ui = ui.page_fluid(
    ui.head_content(
    ui.tags.meta(name="viewport", content="width=device-width, initial-scale=1"),
    ui.tags.link(rel="stylesheet", href=FONT),
    ui.tags.script(src=ICONIFY),
    ui.tags.style(MANTINE_CSS),
    ui.tags.script(BROWSER_JS),
    ),

    ui.tags.div(
        icon("tabler:calendar-heart", "26px", NAVY),
        ui.tags.div(
            "Cedar & Sage",
            ui.tags.small("Reception schedule"),
            class_="cs-brand",
        ),
        ui.tags.div(
        ui.tags.input(
            id="sheet_url", type="text", class_="form-control",
            placeholder="Paste your Google Sheet link once",
            style="min-width:280px;height:36px;font-size:13px;",
        ),
        style="margin-left:auto;",
    ),
    ui.tags.div(ui.output_ui("conn_status")),
        btn("Open folder", "tabler:folder-open", "default", **{"data-cs": "open-folder"}),
        ui.output_ui("drive_button"),
        btn("Save", "tabler:device-floppy", "", id="btn_save"),
        class_="cs-header",
    ),
    ui.tags.div(
        ui.output_ui("storage_note"),
        ui.navset_tab(
            ui.nav_panel(
                tab_label("Week", "tabler:calendar-week"),
                ui.tags.div(
                    btn("\u2039", "tabler:chevron-left", "default", id="wk_prev"),
                    ui.tags.span(ui.output_text("week_header"),
                                 style="font-weight:600;margin:0 14px;"),
                    btn("\u203a", "tabler:chevron-right", "default", id="wk_next"),
                    btn("This week", "tabler:calendar-due", "subtle", id="wk_today"),
                    style="display:flex;align-items:center;margin-bottom:16px;",
                ),
                ui.output_ui("week_view"),
                ui.tags.p(
                    icon("tabler:hand-click"),
                    " Click any shift to change who's on it.",
                    style="color:#868e96;font-size:13px;margin-top:14px;",
                ),
            ),
            ui.nav_panel(
                tab_label("Shifts", "tabler:list-details"),
                card(
                    "Every shift", "tabler:list-details",
                    alert("Select a row, then press Edit. Everything is chosen from a "
                          "dropdown — nothing here needs typing.", "",
                          "tabler:pointer"),
                    ui.tags.div(
                        btn("Edit selected", "tabler:edit", "", id="grid_edit"),
                        btn("Set to OPEN", "tabler:user-off", "default", id="grid_open"),
                        style="display:flex;gap:8px;margin-bottom:14px;",
                    ),
                    ui.output_data_frame("grid_shifts"),
                    sub="filter with the column boxes",
                ),
            ),
            ui.nav_panel(
                tab_label("Template", "tabler:template"),
                ui.tags.div(
                    card("Staff", "tabler:users",
                         ui.output_ui("staff_list"),
                         ui.tags.div(
                             ui.input_text("new_staff", None, placeholder="Add someone"),
                             btn("Add", "tabler:plus", "light", id="add_staff"),
                             style="display:flex;gap:8px;margin-top:12px;align-items:center;",
                         )),
                    card("Weekly pattern", "tabler:repeat",
                         ui.output_ui("template_grid")),
                    style="display:grid;grid-template-columns:1fr;gap:0;",
                ),
                card(
                    "Rebuild from the pattern", "tabler:refresh",
                    ui.tags.div(
                        ui.input_date("gen_from", "From", value=dt.date(2026, 8, 1), weekstart=1),
                        ui.input_date("gen_to", "To", value=dt.date(2026, 9, 30), weekstart=1),
                        ui.input_checkbox("keep_manual", "Keep one-day changes", value=True),
                        style="display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;",
                    ),
                    ui.tags.div(btn("Rebuild", "tabler:refresh", "", id="gen_go"),
                                style="margin-top:14px;"),
                ),
            ),
            ui.nav_panel(
                tab_label("Time off", "tabler:beach"),
                card(
                    "Book time off", "tabler:beach",
                    ui.tags.div(
                        ui.input_select("to_staff", "Who", choices=[]),
                        ui.input_date("to_from", "From", weekstart=1),
                        ui.input_date("to_to", "To", weekstart=1),
                        ui.input_select("to_type", "Type",
                                        choices=["Vacation", "Sick", "Personal", "Other"]),
                        style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;",
                    ),
                    ui.tags.div(btn("Book it", "tabler:calendar-plus", "", id="to_add"),
                                style="margin-top:14px;"),
                ),
                card("Booked", "tabler:list", ui.output_data_frame("grid_timeoff")),
                card("Days used", "tabler:sum", ui.output_data_frame("grid_counts")),
            ),
            ui.nav_panel(
                tab_label("Hours", "tabler:clock-hour-4"),
                card(
                    "Hours and potential overtime", "tabler:clock-hour-4",
                    alert("Over 8 hrs/day, plus over 40 hrs/week counting only the first 8 "
                          "of each day, credited to the pay period the week ends in. Stat "
                          "hours are separate. Review only — confirm against BC Employment "
                          "Standards before paying overtime.", "warn",
                          "tabler:alert-triangle"),
                    ui.output_data_frame("grid_hours"),
                    ui.tags.div(ui.download_button("dl_hours", "Download report"),
                                style="margin-top:14px;"),
                ),
                card("Per person per day", "tabler:table", ui.output_data_frame("grid_daily")),
            ),
            id="tabs",
        ),
        class_="cs-shell",
    ),
    ui.tags.div(id="cs-toast", class_="cs-toast"),
)

# ==========================================================================
# Google Drive configuration
# ==========================================================================
# Paste the OAuth client ID from your Google Cloud project here. Until it is
# filled in, the Drive button explains what is missing instead of failing.
# Setup steps are in the README.

GOOGLE_CLIENT_ID = ""   # OAuth client ID from your Google Cloud project


def server(input: Inputs, output: Outputs, session: Session) -> None:
    ver = reactive.value(0)      # bump to re-render grids (load, rebuild)
    edits = reactive.value(0)    # bump on edits, so reports recompute
    caps = reactive.value({})
    monday = reactive.value(monday_of(dt.date(2026, 8, 3)))
    target = reactive.value(None)

    def touch_all() -> None:
        ver.set(ver() + 1)

    def touch_edit() -> None:
        edits.set(edits() + 1)

    def changed() -> int:
        return ver() + edits()

    async def persist() -> None:
        """Autosave into browser storage after every change."""
        await session.send_custom_message("cs_autosave", {"files": STORE.to_csv_map()})

    # ------------------------------------------------------------ capabilities

    @reactive.effect
    @reactive.event(input.cs_caps)
    def _caps() -> None:
        try:
            caps.set(json.loads(input.cs_caps()))
        except Exception:
            caps.set({})

    @render.ui
    def conn_status():
        changed()
        c = caps()
        src = STORE.source
        if src == "sheet":
            dot, label = "on", "Google Sheet — saving there"
        elif src == "folder":
            dot, label = "on", f"Folder · {STORE.folder}"
        elif src == "browser":
            dot, label = "warn", "Restored from this browser — not saved to a file yet"
        else:
            dot, label = "", "Example data — open a folder or sign in"
        return ui.tags.div(
            ui.tags.span(class_=f"cs-dot {dot}"), label, class_="cs-status",
        )

    @render.ui
    def drive_button():
        cfg = json.dumps({"client_id": GOOGLE_CLIENT_ID})
        return btn("Connect Sheet", "tabler:table-share", "default",
                   **{"data-cs": "sheet-connect", "data-cfg": cfg})

    @render.ui
    def storage_note():
        c = caps()
        if not c:
            return None
        bits = []
        if not c.get("storage"):
            bits.append(alert(
                "This browser is blocking local storage, so changes will not survive a "
                "reload. Save to a folder or Drive as you go.", "warn",
                "tabler:alert-triangle"))
        if not c.get("fsa") and not GOOGLE_CLIENT_ID:
            bits.append(alert(
                "This browser cannot open folders directly (that needs Chrome or Edge), "
                "and the Google Sheet connection isn't configured yet. Changes are kept in "
                "this browser only. See the README to switch on Sheets.", "warn",
                "tabler:folder-off"))
        return ui.tags.div(*bits) if bits else None

    # ------------------------------------------------------------ load & save

    @reactive.effect
    @reactive.event(input.fs_load)
    async def _load() -> None:
        payload = json.loads(input.fs_load())
        names = STORE.load_csv_map(payload.get("files", {}))
        STORE.folder = payload.get("folder", "?")
        STORE.source = payload.get("source", "folder")
        touch_all()
        if STORE.source != "browser":
            await persist()

    @reactive.effect
    @reactive.event(input.sheet_ready)
    def _sheet_ready() -> None:
        STORE.source = "sheet"
        touch_all()

    @reactive.effect
    @reactive.event(input.btn_save)
    async def _save() -> None:
        await session.send_custom_message("cs_save", {"files": STORE.to_csv_map()})

    # ------------------------------------------------------------ week nav

    @reactive.effect
    @reactive.event(input.wk_prev)
    def _prev() -> None:
        monday.set(monday() - dt.timedelta(days=7))

    @reactive.effect
    @reactive.event(input.wk_next)
    def _next() -> None:
        monday.set(monday() + dt.timedelta(days=7))

    @reactive.effect
    @reactive.event(input.wk_today)
    def _today() -> None:
        monday.set(monday_of(dt.date.today()))

    @reactive.calc
    def week_days() -> list[dt.date]:
        return [monday() + dt.timedelta(days=i) for i in range(5)]

    @render.text
    def week_header() -> str:
        d = week_days()
        return f"{fmt_short(d[0])} – {fmt_long(d[4])}"

    def shift_index() -> dict[tuple[str, str], Any]:
        out = {}
        for _, r in STORE.data["shifts.csv"].iterrows():
            out[(str(r["date"]).strip(), str(r["shift"]).strip().upper())] = r
        return out

    @render.ui
    def week_view():
        changed()
        monday()
        staff = STORE.staff_names()
        holidays = STORE.holiday_set()
        hol_names = {parse_date(r["date"]): r["name"]
                     for _, r in STORE.data["holidays.csv"].iterrows()
                     if parse_date(r["date"])}
        idx = shift_index()
        today = dt.date.today()

        cards = []
        for d in week_days():
            stat = d in holidays
            slots = []
            for sh in SHIFTS:
                rec = idx.get((d.isoformat(), sh))
                who = str(rec["staff"]).strip() if rec is not None else OPEN
                who = who or OPEN
                times = (f"{rec['start']} – {rec['end']}"
                         if rec is not None and str(rec["start"]).strip() else "")
                if who == OPEN:
                    badge = ui.tags.span("Open", class_="m-badge open")
                elif who == CLOSED:
                    badge = ui.tags.span("Closed", class_="m-badge closed")
                else:
                    badge = ui.tags.span(
                        who, class_="m-badge",
                        style=f"background:{colour_for(who, staff)}")
                slots.append(ui.tags.div(
                    ui.tags.span(sh, class_="lab"),
                    ui.tags.div(badge, ui.tags.div(times, class_="cs-times")),
                    ui.tags.span(icon("tabler:pencil"), class_="edit"),
                    class_="cs-slot",
                    **{"data-cs": "shift", "data-date": d.isoformat(), "data-shift": sh},
                ))
            klass = "cs-day"
            if d == today:
                klass += " today"
            if stat:
                klass += " stat"
            cards.append(ui.tags.div(
                ui.tags.div(
                    ui.tags.div(f"{d:%A}", class_="cs-dow"),
                    ui.tags.div(fmt_short(d), class_="cs-dnum"),
                    ui.tags.div(hol_names.get(d, ""), class_="cs-stat-name") if stat else None,
                    class_="cs-day-head",
                ),
                *slots,
                class_=klass,
            ))
        return ui.tags.div(*cards, class_="cs-days")

    # ------------------------------------------------------- the shift editor

    def open_editor(date_iso: str, shift: str) -> None:
        staff = STORE.staff_names()
        idx = shift_index()
        rec = idx.get((date_iso, shift))
        cur = str(rec["staff"]).strip() if rec is not None else OPEN
        d = parse_date(date_iso)
        start = (str(rec["start"]).strip() if rec is not None and str(rec["start"]).strip()
                 else DEFAULT_TIMES[shift][0])
        end = (str(rec["end"]).strip() if rec is not None and str(rec["end"]).strip()
               else DEFAULT_TIMES[shift][1])
        target.set({"date": date_iso, "shift": shift})
        weekday = WEEKDAYS[d.weekday()] if d and d.weekday() < 5 else None

        scope = {"once": "Just this day"}
        if weekday:
            scope["weekly"] = f"Every {d:%A}"

        ui.modal_show(ui.modal(
            ui.input_select("m_staff", "Who's working",
                            choices=[OPEN, CLOSED] + staff,
                            selected=cur if cur in ([OPEN, CLOSED] + staff) else OPEN),
            ui.tags.div(
                ui.input_select("m_start", "Start", choices=TIME_CHOICES, selected=start),
                ui.input_select("m_end", "End", choices=TIME_CHOICES, selected=end),
                style="display:grid;grid-template-columns:1fr 1fr;gap:14px;",
            ),
            ui.input_radio_buttons("m_scope", "Apply to", choices=scope, selected="once"),
            title=f"{shift} shift · {fmt_day(d) if d else date_iso}",
            footer=ui.tags.div(
                ui.modal_button("Cancel", class_="m-btn subtle"),
                btn("Save", "tabler:check", "", id="m_save"),
                style="display:flex;gap:8px;justify-content:flex-end;",
            ),
            easy_close=True,
        ))

    @reactive.effect
    @reactive.event(input.cell_click)
    def _cell_click() -> None:
        payload = json.loads(input.cell_click())
        open_editor(payload["date"], payload["shift"])

    @reactive.effect
    @reactive.event(input.grid_edit)
    def _grid_edit() -> None:
        rows = list(grid_shifts.data_view_rows() or [])
        sel = grid_shifts.input_cell_selection()
        picked = list(sel.get("rows", [])) if sel else []
        if not picked:
            ui.notification_show("Select a row first.", type="warning")
            return
        df = STORE.data["shifts.csv"]
        src = rows[picked[0]] if picked[0] < len(rows) else picked[0]
        row = df.iloc[src]
        open_editor(str(row["date"]).strip(), str(row["shift"]).strip().upper())

    def write_shift(date_iso: str, shift: str, who: str,
                    start: str, end: str) -> None:
        df = STORE.data["shifts.csv"]
        mask = (df["date"] == date_iso) & (df["shift"].str.upper() == shift)
        if mask.any():
            df.loc[mask, ["staff", "start", "end"]] = [who, start, end]
        else:
            STORE.data["shifts.csv"] = pd.concat(
                [df, pd.DataFrame([{"date": date_iso, "shift": shift, "staff": who,
                                    "start": start, "end": end}])],
                ignore_index=True,
            ).sort_values(["date", "shift"]).reset_index(drop=True)

    @reactive.effect
    @reactive.event(input.m_save)
    async def _modal_save() -> None:
        t = target()
        if not t:
            return
        who = input.m_staff()
        start, end = input.m_start(), input.m_end()
        write_shift(t["date"], t["shift"], who, start, end)

        if input.m_scope() == "weekly" and who not in (OPEN, CLOSED):
            d = parse_date(t["date"])
            wd = WEEKDAYS[d.weekday()]
            tmpl = STORE.data["template.csv"]
            m = (tmpl["weekday"] == wd) & (tmpl["shift"] == t["shift"])
            if m.any():
                tmpl.loc[m, "staff"] = who
            else:
                STORE.data["template.csv"] = pd.concat(
                    [tmpl, pd.DataFrame([{"weekday": wd, "shift": t["shift"], "staff": who}])],
                    ignore_index=True)
            # push it across every future date that still follows the pattern
            for i, r in STORE.data["shifts.csv"].iterrows():
                rd = parse_date(r["date"])
                if rd and rd >= d and rd.weekday() == d.weekday() \
                        and str(r["shift"]).upper() == t["shift"]:
                    STORE.data["shifts.csv"].at[i, "staff"] = who

        ui.modal_remove()
        touch_all()
        await persist()

    @reactive.effect
    @reactive.event(input.grid_open)
    async def _grid_open() -> None:
        sel = grid_shifts.input_cell_selection()
        picked = list(sel.get("rows", [])) if sel else []
        if not picked:
            ui.notification_show("Select a row first.", type="warning")
            return
        rows = list(grid_shifts.data_view_rows() or [])
        df = STORE.data["shifts.csv"]
        for p in picked:
            src = rows[p] if p < len(rows) else p
            df.iat[src, df.columns.get_loc("staff")] = OPEN
        touch_all()
        await persist()

    # ------------------------------------------------------------ grids

    @render.data_frame
    def grid_shifts():
        ver()
        return render.DataGrid(STORE.data["shifts.csv"], filters=True,
                               height="520px", selection_mode="rows")

    @render.data_frame
    def grid_timeoff():
        changed()
        return render.DataGrid(STORE.data["timeoff.csv"], height="240px")

    # ------------------------------------------------------- staff & template

    @render.ui
    def staff_list():
        changed()
        names = STORE.staff_names()
        if not names:
            return ui.tags.p("Nobody yet.", style="color:#868e96;font-size:13px;")
        pills = [ui.tags.span(n, class_="m-badge",
                              style=f"background:{colour_for(n, names)};margin:0 6px 6px 0;")
                 for n in names]
        return ui.tags.div(
            ui.tags.div(*pills, style="display:flex;flex-wrap:wrap;"),
            ui.tags.div(
                ui.input_select("rm_staff", None, choices=[""] + names),
                btn("Remove", "tabler:trash", "default", id="del_staff"),
                style="display:flex;gap:8px;margin-top:10px;align-items:center;",
            ),
        )

    @render.ui
    def template_grid():
        changed()
        names = STORE.staff_names()
        tmpl = {}
        for _, r in STORE.data["template.csv"].iterrows():
            tmpl[(str(r["weekday"]).strip(), str(r["shift"]).strip().upper())] = \
                str(r["staff"]).strip()
        cols = []
        for wd in WEEKDAYS:
            fields = []
            for sh in SHIFTS:
                cur = tmpl.get((wd, sh), "")
                fields.append(ui.input_select(
                    f"tmpl_{wd}_{sh}", sh, choices=[OPEN] + names,
                    selected=cur if cur in names else OPEN))
            cols.append(ui.tags.div(
                ui.tags.div(wd, style="font-weight:600;font-size:13px;margin-bottom:6px;"),
                *fields,
            ))
        return ui.tags.div(
            *cols,
            style="display:grid;grid-template-columns:repeat(5,minmax(140px,1fr));gap:12px;overflow-x:auto;",
        )

    @reactive.effect
    async def _sync_template() -> None:
        names = STORE.staff_names()
        rows = []
        for wd in WEEKDAYS:
            for sh in SHIFTS:
                try:
                    val = input[f"tmpl_{wd}_{sh}"]()
                except Exception:
                    return
                rows.append({"weekday": wd, "shift": sh,
                             "staff": "" if val in (OPEN, None) else str(val)})
        new = pd.DataFrame(rows, columns=SCHEMAS["template.csv"])
        with reactive.isolate():
            if not new.equals(STORE.data["template.csv"]):
                STORE.data["template.csv"] = new
                touch_edit()
                await persist()

    @reactive.effect
    @reactive.event(input.add_staff)
    async def _add_staff() -> None:
        name = (input.new_staff() or "").strip()
        if not name:
            return
        df = STORE.data["staff.csv"]
        if name in df["name"].tolist():
            ui.notification_show(f"{name} is already listed.", type="warning")
            return
        if len(df) >= 15:
            ui.notification_show("That's 15 people — the colour palette stops there.",
                                 type="warning")
            return
        STORE.data["staff.csv"] = pd.concat([df, pd.DataFrame([{"name": name}])],
                                            ignore_index=True)
        ui.update_text("new_staff", value="")
        touch_all()
        await persist()

    @reactive.effect
    @reactive.event(input.del_staff)
    async def _del_staff() -> None:
        name = input.rm_staff()
        if not name:
            return
        df = STORE.data["staff.csv"]
        STORE.data["staff.csv"] = df[df["name"] != name].reset_index(drop=True)
        touch_all()
        await persist()

    @reactive.effect
    def _staff_choices() -> None:
        changed()
        ui.update_select("to_staff", choices=STORE.staff_names())

    # ------------------------------------------------------------ rebuild

    @reactive.effect
    @reactive.event(input.gen_go)
    async def _rebuild() -> None:
        a, b = input.gen_from(), input.gen_to()
        if not a or not b or b < a:
            ui.notification_show("The end date is before the start date.", type="warning")
            return
        STORE.data["shifts.csv"] = build_shifts(
            a, b, STORE.data["template.csv"], STORE.data["timeoff.csv"],
            existing=STORE.data["shifts.csv"], keep_manual=bool(input.keep_manual()),
        )
        ui.notification_show(f"Rebuilt {fmt_short(a)} to {fmt_short(b)}.", type="message")
        touch_all()
        await persist()

    # ------------------------------------------------------------ time off

    @reactive.effect
    @reactive.event(input.to_add)
    async def _add_timeoff() -> None:
        who, a, b = input.to_staff(), input.to_from(), input.to_to()
        if not who or not a or not b:
            ui.notification_show("Pick a person and both dates.", type="warning")
            return
        if b < a:
            ui.notification_show("The end date is before the start date.", type="warning")
            return
        STORE.data["timeoff.csv"] = pd.concat(
            [STORE.data["timeoff.csv"],
             pd.DataFrame([{"staff": who, "start": a.isoformat(), "end": b.isoformat(),
                            "type": input.to_type(), "note": ""}])],
            ignore_index=True)
        STORE.data["shifts.csv"], hits = apply_timeoff(
            STORE.data["shifts.csv"], STORE.data["timeoff.csv"])
        ui.notification_show(
            f"{who} off {fmt_short(a)}–{fmt_short(b)}. {hits} shift(s) opened up.",
            type="message")
        touch_all()
        await persist()

    @render.data_frame
    def grid_counts():
        changed()
        rows = []
        for who in STORE.staff_names():
            tally: dict[str, float] = {}
            for _, r in STORE.data["timeoff.csv"].iterrows():
                if str(r["staff"]).strip() != who:
                    continue
                a, b = parse_date(r["start"]), parse_date(r["end"])
                if not a or not b or b < a:
                    continue
                days = sum(1 for i in range((b - a).days + 1)
                           if (a + dt.timedelta(days=i)).weekday() < 5)
                kind = str(r["type"]).strip() or "Other"
                tally[kind] = tally.get(kind, 0) + days
            rows.append({"staff": who, "vacation": tally.get("Vacation", 0),
                         "sick": tally.get("Sick", 0), "personal": tally.get("Personal", 0),
                         "other": tally.get("Other", 0)})
        return render.DataGrid(pd.DataFrame(rows), height="200px")

    # ------------------------------------------------------------ hours

    @reactive.calc
    def hours_df() -> pd.DataFrame:
        changed()
        return overtime(STORE.data["shifts.csv"], STORE.staff_names(), STORE.holiday_set())

    @render.data_frame
    def grid_hours():
        return render.DataGrid(hours_df(), height="380px", filters=True)

    @render.data_frame
    def grid_daily():
        changed()
        return render.DataGrid(
            hours_table(STORE.data["shifts.csv"], STORE.staff_names()),
            height="380px", filters=True)

    @render.download_button(filename="hours-report.csv")
    def dl_hours():
        yield hours_df().to_csv(index=False)


app = App(app_ui, server)
