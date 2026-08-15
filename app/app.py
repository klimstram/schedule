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
from shiny import App, Inputs, Outputs, Session, reactive, render, req, ui

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


# --------------------------------------------------------------------------
# the store — plain (non-reactive) so grid edits don't retrigger the grid
# --------------------------------------------------------------------------

DATA_DIR = pathlib.Path(__file__).parent / "data"


def load_bundled() -> tuple[dict[str, pd.DataFrame], str]:
    """
    Read the CSVs shipped alongside the app. These are bundled into app.json by
    `shinylive export`, so the deployed site opens with the clinic's real data
    instead of the built-in example. Update them by committing new CSVs.
    Falls back to SEED for anything missing or unreadable.
    """
    out: dict[str, pd.DataFrame] = {}
    found = 0
    for name in FILES:
        path = DATA_DIR / name
        try:
            raw = pd.read_csv(path, dtype=str, keep_default_na=False)
            out[name] = coerce(raw, name)
            found += 1
        except Exception:
            out[name] = SEED[name].copy()
    label = (f"bundled data ({found}/{len(FILES)} CSVs)" if found
             else "built-in example data")
    return out, label


class Store:
    def __init__(self) -> None:
        self.data, self.folder = load_bundled()

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


# --------------------------------------------------------------------------
# browser file-system glue
# --------------------------------------------------------------------------
# The picker must be called inside a real user gesture, so it is wired up in
# plain JS on the button's click rather than through a Shiny event (which
# arrives after the gesture has expired).

FS_JS = """
(function () {
  window.__csDir = null;

  function announce(msg, ok) {
    Shiny.setInputValue('fs_status', JSON.stringify({msg: msg, ok: !!ok, t: Date.now()}),
                        {priority: 'event'});
  }

  document.addEventListener('click', async function (ev) {
    var btn = ev.target.closest('#btn_open');
    if (!btn) return;
    if (!('showDirectoryPicker' in window)) {
      announce('This browser cannot open folders directly. Use Chrome or Edge, ' +
               'or use the upload box below.', false);
      return;
    }
    try {
      var dir = await window.showDirectoryPicker({ mode: 'readwrite', id: 'cedarsage' });
      window.__csDir = dir;
      var files = {};
      for await (var entry of dir.values()) {
        if (entry.kind === 'file' && entry.name.toLowerCase().endsWith('.csv')) {
          var f = await entry.getFile();
          files[entry.name] = await f.text();
        }
      }
      Shiny.setInputValue('fs_load',
        JSON.stringify({ folder: dir.name, files: files, t: Date.now() }),
        { priority: 'event' });
    } catch (e) {
      if (e && e.name === 'AbortError') return;
      announce('Could not open that folder: ' + e, false);
    }
  });

  document.addEventListener('shiny:connected', function () {
    Shiny.setInputValue('fs_supported', ('showDirectoryPicker' in window));
  });

  Shiny.addCustomMessageHandler('cs_save', async function (msg) {
    if (!window.__csDir) {
      announce('No folder open yet — click "Open schedule folder" first, ' +
               'or use the download buttons.', false);
      return;
    }
    try {
      for (var name in msg.files) {
        var h = await window.__csDir.getFileHandle(name, { create: true });
        var w = await h.createWritable();
        await w.write(msg.files[name]);
        await w.close();
      }
      announce('Saved to ' + window.__csDir.name + '.', true);
    } catch (e) {
      announce('Save failed: ' + e, false);
    }
  });
})();
"""

CSS = f"""
body {{ background: {CREAM}; }}
.navbar, .bslib-sidebar-layout > .sidebar {{ background: #fff; }}
h1, h2, h3, h4, .card-header {{ font-family: Georgia, 'Times New Roman', serif; color: {NAVY}; }}
.cs-title {{ font-size: 1.15rem; margin: 0; }}
.cs-sub {{ font-size: .78rem; color: {MUTED}; font-family: Arial, sans-serif; }}
.cs-week {{ width: 100%; border-collapse: separate; border-spacing: 0 6px; }}
.cs-week td {{ padding: 8px 10px; background: #fff; border-top: 1px solid {LINE};
              border-bottom: 1px solid {LINE}; vertical-align: middle; }}
.cs-week td:first-child {{ border-left: 1px solid {LINE};
                           border-radius: 10px 0 0 10px; width: 128px; }}
.cs-week td:last-child {{ border-right: 1px solid {LINE}; border-radius: 0 10px 10px 0;
                          text-align: right; color: {MUTED}; font-size: .82rem;
                          white-space: nowrap; }}
.cs-week tr.stat td {{ background: {GOLD}; }}
.cs-day {{ font-weight: bold; color: {NAVY}; font-size: .88rem; }}
.cs-tag {{ color: {MUTED}; font-size: .7rem; letter-spacing: .06em; width: 34px; }}
.cs-pill {{ display: inline-block; padding: 4px 12px; border-radius: 13px;
            font-size: .85rem; font-weight: bold; color: {NAVY}; }}
.cs-pill.open {{ background: {OPEN_RED}; color: #fff; }}
.cs-statname {{ font-size: .7rem; color: #8A6A2F; font-style: italic; }}
.cs-flag {{ font-size: .72rem; color: {OPEN_RED}; }}
.cs-ok {{ font-size: .72rem; color: {SAGE}; }}
"""


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

app_ui = ui.page_sidebar(
    ui.sidebar(
        ui.h4("Cedar & Sage", class_="cs-title"),
        ui.p("Reception schedule", class_="cs-sub"),
        ui.hr(),
        ui.input_action_button("btn_open", "Open schedule folder", class_="btn-primary w-100"),
        ui.output_ui("folder_note"),
        ui.input_action_button("btn_save", "Save all changes", class_="btn-outline-primary w-100 mt-2"),
        ui.output_ui("save_note"),
        ui.hr(),
        ui.input_date("week_of", "Week of", value=dt.date(2026, 8, 3), weekstart=1),
        ui.hr(),
        ui.accordion(
            ui.accordion_panel(
                "If your browser can't open folders",
                ui.p("Safari and Firefox can't write to a folder. Upload the CSVs here, "
                     "then use the download buttons to save them back.",
                     class_="cs-sub"),
                ui.input_file("upload", "Upload CSV files", multiple=True, accept=[".csv"]),
                ui.download_button("dl_shifts", "Download shifts.csv", class_="btn-sm w-100 mb-1"),
                ui.download_button("dl_all", "Download all as one CSV", class_="btn-sm w-100"),
            ),
            open=False,
        ),
        width=320,
    ),
    ui.head_content(ui.tags.style(CSS), ui.tags.script(FS_JS)),
    ui.navset_tab(
        ui.nav_panel(
            "Week",
            ui.card(
                ui.card_header(ui.output_text("week_header")),
                ui.output_ui("week_view"),
            ),
            ui.card(
                ui.card_header("Quick assign"),
                ui.layout_columns(
                    ui.input_select("qa_date", "Day", choices=[]),
                    ui.input_select("qa_shift", "Shift", choices=SHIFTS),
                    ui.input_select("qa_staff", "Who", choices=[]),
                    ui.input_text("qa_start", "Start", placeholder="08:00"),
                    ui.input_text("qa_end", "End", placeholder="13:00"),
                    col_widths=[3, 2, 3, 2, 2],
                ),
                ui.input_action_button("qa_apply", "Assign", class_="btn-primary"),
                ui.output_ui("qa_note"),
            ),
        ),
        ui.nav_panel(
            "Shifts",
            ui.card(
                ui.card_header("Every shift — click a cell to edit"),
                ui.p("Edits save to shifts.csv when you press Save all changes. "
                     "Type OPEN for an unfilled shift, CLOSED for a closed day.",
                     class_="cs-sub"),
                ui.output_data_frame("grid_shifts"),
            ),
        ),
        ui.nav_panel(
            "Template & staff",
            ui.layout_columns(
                ui.card(
                    ui.card_header("Staff"),
                    ui.p("Up to 15. Order sets each person's colour.", class_="cs-sub"),
                    ui.output_data_frame("grid_staff"),
                    ui.input_text("new_staff", "Add someone", placeholder="Name"),
                    ui.input_action_button("add_staff", "Add", class_="btn-sm btn-outline-primary"),
                ),
                ui.card(
                    ui.card_header("Weekly template"),
                    ui.p("The regular pattern. Editing here changes nothing until you "
                         "rebuild a date range below.", class_="cs-sub"),
                    ui.output_data_frame("grid_template"),
                ),
                col_widths=[5, 7],
            ),
            ui.card(
                ui.card_header("Rebuild the schedule from the template"),
                ui.layout_columns(
                    ui.input_date("gen_from", "From", value=dt.date(2026, 8, 1), weekstart=1),
                    ui.input_date("gen_to", "To", value=dt.date(2026, 9, 30), weekstart=1),
                    ui.input_checkbox("keep_manual", "Keep one-day changes", value=True),
                    col_widths=[4, 4, 4],
                ),
                ui.input_action_button("gen_go", "Rebuild", class_="btn-primary"),
                ui.output_ui("gen_note"),
            ),
        ),
        ui.nav_panel(
            "Time off",
            ui.card(
                ui.card_header("Time off"),
                ui.p("Dates as YYYY-MM-DD. Type: Vacation, Sick, Personal or Other.",
                     class_="cs-sub"),
                ui.output_data_frame("grid_timeoff"),
                ui.layout_columns(
                    ui.input_select("to_staff", "Who", choices=[]),
                    ui.input_date("to_from", "From", weekstart=1),
                    ui.input_date("to_to", "To", weekstart=1),
                    ui.input_select("to_type", "Type",
                                    choices=["Vacation", "Sick", "Personal", "Other"]),
                    col_widths=[3, 3, 3, 3],
                ),
                ui.input_action_button("to_add", "Add and open up those shifts",
                                       class_="btn-primary"),
                ui.output_ui("to_note"),
            ),
            ui.card(
                ui.card_header("Days used"),
                ui.output_data_frame("grid_counts"),
            ),
        ),
        ui.nav_panel(
            "Hours report",
            ui.card(
                ui.card_header("Hours and potential overtime by pay period"),
                ui.p("Potential OT = over 8 hrs/day, plus over 40 hrs/week counting only "
                     "the first 8 hrs of each day, credited to the pay period the week ends "
                     "in. Stat hours are listed separately. Review only — confirm against "
                     "BC Employment Standards before paying OT.", class_="cs-sub"),
                ui.output_data_frame("grid_hours"),
                ui.download_button("dl_hours", "Download hours report",
                                   class_="btn-sm btn-outline-primary mt-2"),
            ),
            ui.card(
                ui.card_header("Hours per person per day"),
                ui.output_data_frame("grid_daily"),
            ),
        ),
        ui.nav_panel(
            "Stat holidays",
            ui.card(
                ui.card_header("Statutory holidays"),
                ui.p("BC and national. Used to flag stat hours in the report.",
                     class_="cs-sub"),
                ui.output_data_frame("grid_holidays"),
            ),
        ),
    ),
    title="Reception Schedule",
    fillable=False,
)


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------

def server(input: Inputs, output: Outputs, session: Session) -> None:
    # ver  -> bump to force the grids to re-render (load, rebuild, bulk change)
    # edits-> bump on every cell edit, so downstream reports recompute without
    #         re-rendering (and thus resetting) the grid being typed into
    ver = reactive.value(0)
    edits = reactive.value(0)
    status = reactive.value(("", True))

    def touch_all() -> None:
        ver.set(ver() + 1)

    def touch_edit() -> None:
        edits.set(edits() + 1)

    def any_change() -> int:
        return ver() + edits()

    # ---------------------------------------------------------------- loading

    @reactive.effect
    @reactive.event(input.fs_load)
    def _load_from_folder() -> None:
        payload = json.loads(input.fs_load())
        names = STORE.load_csv_map(payload.get("files", {}))
        STORE.folder = payload.get("folder", "folder")
        if names:
            status.set((f"Loaded {', '.join(names)} from {STORE.folder}.", True))
        else:
            status.set((f"No recognised CSVs in {STORE.folder}. "
                        "Press Save all changes to write a fresh set there.", False))
        touch_all()

    @reactive.effect
    @reactive.event(input.upload)
    def _load_from_upload() -> None:
        blob: dict[str, str] = {}
        for f in input.upload() or []:
            try:
                with open(f["datapath"], "r", encoding="utf-8") as fh:
                    blob[f["name"]] = fh.read()
            except Exception:
                continue
        names = STORE.load_csv_map(blob)
        STORE.folder = "(uploaded files)"
        status.set((f"Loaded {', '.join(names)}." if names
                    else "Those files didn't match any expected name.", bool(names)))
        touch_all()

    @reactive.effect
    @reactive.event(input.fs_status)
    def _fs_status() -> None:
        msg = json.loads(input.fs_status())
        status.set((msg.get("msg", ""), msg.get("ok", False)))

    # ---------------------------------------------------------------- saving

    @reactive.effect
    @reactive.event(input.btn_save)
    async def _save() -> None:
        await session.send_custom_message("cs_save", {"files": STORE.to_csv_map()})

    @render.ui
    def folder_note():
        return ui.p(f"Folder: {STORE.folder}", class_="cs-sub mt-2")

    @render.ui
    def save_note():
        any_change()
        msg, ok = status()
        if not msg:
            return ui.p("Nothing saved yet this session.", class_="cs-sub")
        return ui.p(msg, class_="cs-ok" if ok else "cs-flag")

    # ---------------------------------------------------------------- week view

    @reactive.calc
    def week_days() -> list[dt.date]:
        base = input.week_of() or dt.date(2026, 8, 3)
        start = monday_of(base)
        return [start + dt.timedelta(days=i) for i in range(5)]

    @render.text
    def week_header() -> str:
        days = week_days()
        return f"Week of {fmt_long(days[0])}"

    @render.ui
    def week_view():
        any_change()
        days = week_days()
        staff = STORE.staff_names()
        holidays = STORE.holiday_set()
        hol_names = {parse_date(r["date"]): r["name"]
                     for _, r in STORE.data["holidays.csv"].iterrows()
                     if parse_date(r["date"])}
        shifts = STORE.data["shifts.csv"]

        index: dict[tuple[str, str], dict[str, str]] = {}
        for _, r in shifts.iterrows():
            index[(str(r["date"]).strip(), str(r["shift"]).strip().upper())] = r

        rows = []
        for d in days:
            is_stat = d in holidays
            for sh in SHIFTS:
                rec = index.get((d.isoformat(), sh))
                who = str(rec["staff"]).strip() if rec is not None else ""
                times = (f"{rec['start']} – {rec['end']}"
                         if rec is not None and rec["start"] else "")
                pill_class = "cs-pill open" if who in ("", OPEN) else "cs-pill"
                style = "" if who in ("", OPEN) else f"background:{colour_for(who, staff)}"
                label = who if who else OPEN
                first = sh == "AM"
                rows.append(
                    ui.tags.tr(
                        ui.tags.td(
                            ui.tags.div(fmt_day(d), class_="cs-day") if first else "",
                            ui.tags.div(hol_names.get(d, ""), class_="cs-statname")
                            if first and is_stat else "",
                        ),
                        ui.tags.td(sh, class_="cs-tag"),
                        ui.tags.td(ui.tags.span(label, class_=pill_class, style=style)),
                        ui.tags.td(times),
                        class_="stat" if is_stat else "",
                    )
                )
        return ui.tags.table(ui.tags.tbody(*rows), class_="cs-week")

    # ------------------------------------------------------- select choices

    @reactive.effect
    def _refresh_choices() -> None:
        any_change()
        staff = STORE.staff_names()
        days = week_days()
        ui.update_select(
            "qa_date",
            choices={d.isoformat(): fmt_day(d) for d in days},
        )
        ui.update_select("qa_staff", choices=[OPEN] + staff + [CLOSED])
        ui.update_select("to_staff", choices=staff)

    # ---------------------------------------------------------- quick assign

    @reactive.effect
    @reactive.event(input.qa_apply)
    def _quick_assign() -> None:
        date_s = input.qa_date()
        shift_s = input.qa_shift()
        who = input.qa_staff()
        if not date_s or not shift_s:
            return
        df = STORE.data["shifts.csv"]
        mask = (df["date"] == date_s) & (df["shift"].str.upper() == shift_s)
        start = input.qa_start().strip() or DEFAULT_TIMES[shift_s][0]
        end = input.qa_end().strip() or DEFAULT_TIMES[shift_s][1]
        if mask.any():
            df.loc[mask, ["staff", "start", "end"]] = [who, start, end]
        else:
            STORE.data["shifts.csv"] = pd.concat(
                [df, pd.DataFrame([{"date": date_s, "shift": shift_s, "staff": who,
                                    "start": start, "end": end}])],
                ignore_index=True,
            )
        status.set((f"{who} set for {date_s} {shift_s}. Not saved to disk yet.", True))
        touch_all()

    @render.ui
    def qa_note():
        any_change()
        return ui.p("Changes live in the browser until you press Save all changes.",
                    class_="cs-sub")

    # ---------------------------------------------------------------- grids

    def editable(name: str, height: str = "460px"):
        return render.DataGrid(STORE.data[name], editable=True, filters=True, height=height)

    def bind_edits(grid_obj, name: str) -> None:
        """Write cell edits straight into the store, keeping column order."""
        @grid_obj.set_patches_fn
        def _(*, patches):
            df = STORE.data[name]
            cols = list(df.columns)
            view_rows = grid_obj.data_view_rows()
            for p in patches:
                try:
                    src = view_rows[p["row_index"]]
                except (IndexError, TypeError):
                    src = p["row_index"]
                if 0 <= src < len(df) and 0 <= p["column_index"] < len(cols):
                    df.iat[src, p["column_index"]] = str(p["value"]).strip()
            touch_edit()
            return patches

    @render.data_frame
    def grid_shifts():
        ver()
        return editable("shifts.csv", "560px")

    bind_edits(grid_shifts, "shifts.csv")

    @render.data_frame
    def grid_staff():
        ver()
        return editable("staff.csv", "260px")

    bind_edits(grid_staff, "staff.csv")

    @render.data_frame
    def grid_template():
        ver()
        return editable("template.csv", "260px")

    bind_edits(grid_template, "template.csv")

    @render.data_frame
    def grid_timeoff():
        ver()
        return editable("timeoff.csv", "300px")

    bind_edits(grid_timeoff, "timeoff.csv")

    @render.data_frame
    def grid_holidays():
        ver()
        return editable("holidays.csv", "400px")

    bind_edits(grid_holidays, "holidays.csv")

    # ------------------------------------------------------------ staff add

    @reactive.effect
    @reactive.event(input.add_staff)
    def _add_staff() -> None:
        name = input.new_staff().strip()
        if not name:
            return
        df = STORE.data["staff.csv"]
        if name in df["name"].tolist():
            status.set((f"{name} is already on the list.", False))
            return
        if len(df) >= 15:
            status.set(("That's 15 people — the colour palette stops there.", False))
            return
        STORE.data["staff.csv"] = pd.concat(
            [df, pd.DataFrame([{"name": name}])], ignore_index=True)
        ui.update_text("new_staff", value="")
        status.set((f"Added {name}.", True))
        touch_all()

    # ------------------------------------------------------------- rebuild

    @reactive.effect
    @reactive.event(input.gen_go)
    def _rebuild() -> None:
        a, b = input.gen_from(), input.gen_to()
        if not a or not b or b < a:
            status.set(("Check the date range — the end is before the start.", False))
            return
        STORE.data["shifts.csv"] = build_shifts(
            a, b, STORE.data["template.csv"], STORE.data["timeoff.csv"],
            existing=STORE.data["shifts.csv"], keep_manual=bool(input.keep_manual()),
        )
        status.set((f"Rebuilt {fmt_short(a)} to {fmt_short(b)} from the template. Not saved yet.", True))
        touch_all()

    @render.ui
    def gen_note():
        any_change()
        n = len(STORE.data["shifts.csv"])
        return ui.p(f"{n} shift rows currently loaded.", class_="cs-sub")

    # ------------------------------------------------------------- time off

    @reactive.effect
    @reactive.event(input.to_add)
    def _add_timeoff() -> None:
        who, a, b = input.to_staff(), input.to_from(), input.to_to()
        if not who or not a or not b:
            status.set(("Pick a person and both dates.", False))
            return
        if b < a:
            status.set(("The end date is before the start date.", False))
            return
        STORE.data["timeoff.csv"] = pd.concat(
            [STORE.data["timeoff.csv"],
             pd.DataFrame([{"staff": who, "start": a.isoformat(), "end": b.isoformat(),
                            "type": input.to_type(), "note": ""}])],
            ignore_index=True,
        )
        STORE.data["shifts.csv"], hits = apply_timeoff(
            STORE.data["shifts.csv"], STORE.data["timeoff.csv"])
        status.set((f"{who} off {fmt_short(a)} to {fmt_short(b)}. {hits} shift(s) opened up.", True))
        touch_all()

    @render.ui
    def to_note():
        any_change()
        return ui.p("Adding time off opens up that person's shifts in the range.",
                    class_="cs-sub")

    @render.data_frame
    def grid_counts():
        any_change()
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
            rows.append({
                "staff": who,
                "vacation days": tally.get("Vacation", 0),
                "sick days": tally.get("Sick", 0),
                "personal days": tally.get("Personal", 0),
                "other": tally.get("Other", 0),
            })
        return render.DataGrid(pd.DataFrame(rows), height="220px")

    # ---------------------------------------------------------------- hours

    @reactive.calc
    def hours_df() -> pd.DataFrame:
        any_change()
        return overtime(STORE.data["shifts.csv"], STORE.staff_names(), STORE.holiday_set())

    @render.data_frame
    def grid_hours():
        return render.DataGrid(hours_df(), height="420px", filters=True)

    @render.data_frame
    def grid_daily():
        any_change()
        return render.DataGrid(
            hours_table(STORE.data["shifts.csv"], STORE.staff_names()),
            height="420px", filters=True,
        )

    # ------------------------------------------------------------ downloads

    @render.download_button(filename="shifts.csv")
    def dl_shifts():
        yield STORE.data["shifts.csv"].to_csv(index=False)

    @render.download_button(filename="hours-report.csv")
    def dl_hours():
        yield hours_df().to_csv(index=False)

    @render.download_button(filename="reception-schedule-all.csv")
    def dl_all():
        buf = io.StringIO()
        for name, df in STORE.data.items():
            buf.write(f"# {name}\n")
            buf.write(df.to_csv(index=False))
            buf.write("\n")
        yield buf.getvalue()


app = App(app_ui, server)
