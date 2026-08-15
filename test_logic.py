import sys; sys.path.insert(0, 'app')
"""Checks the scheduling and overtime logic against hand-computed expectations."""

import datetime as dt
import sys

import pandas as pd

from app import (
    OPEN, build_shifts, hours_table, overtime, parse_date, parse_time,
    shift_hours, apply_timeoff, SEED,
)

fails = []


def check(label, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        fails.append(label)


# ---------------------------------------------------------------- time parsing
check("parse 08:00", parse_time("08:00"), 8.0)
check("parse 1:30 PM", parse_time("1:30 PM"), 13.5)
check("parse 12:00 AM", parse_time("12:00 AM"), 0.0)
check("parse 12:30 PM", parse_time("12:30 PM"), 12.5)
check("parse junk", parse_time("banana"), None)
check("AM shift length", shift_hours("08:00", "13:00"), 5.0)
check("PM shift length", shift_hours("13:00", "18:00"), 5.0)
check("reversed shift", shift_hours("18:00", "13:00"), 0.0)
check("missing end", shift_hours("08:00", ""), 0.0)
check("date iso", parse_date("2026-08-03"), dt.date(2026, 8, 3))

# ------------------------------------------------------- template generation
template = pd.DataFrame(
    [{"weekday": wd, "shift": sh, "staff": "Sarah M." if sh == "AM" else "Jordan T."}
     for wd in ["Mon", "Tue", "Wed", "Thu", "Fri"] for sh in ["AM", "PM"]]
)
timeoff = pd.DataFrame([{"staff": "Sarah M.", "start": "2026-08-10", "end": "2026-08-14",
                         "type": "Vacation", "note": ""}])

gen = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), template, timeoff)

# Aug 3-14 2026 is two full Mon-Fri weeks = 10 weekdays x 2 shifts
check("rows generated", len(gen), 20)
check("no weekend rows",
      all(parse_date(d).weekday() < 5 for d in gen["date"]), True)

wk2_am = gen[(gen["shift"] == "AM") & (gen["date"] >= "2026-08-10")]
check("Sarah's vacation week is OPEN", set(wk2_am["staff"]), {OPEN})
wk1_am = gen[(gen["shift"] == "AM") & (gen["date"] < "2026-08-10")]
check("Sarah works week 1", set(wk1_am["staff"]), {"Sarah M."})
check("Jordan unaffected", set(gen[gen["shift"] == "PM"]["staff"]), {"Jordan T."})

# a row flagged manual="yes" survives a rebuild
edited = gen.copy()
idx = edited.index[(edited["date"] == "2026-08-04") & (edited["shift"] == "PM")][0]
edited.at[idx, "staff"] = "Sarah M."
edited.at[idx, "manual"] = "yes"
again = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), template, timeoff,
                     existing=edited, keep_manual=True)
kept = again[(again["date"] == "2026-08-04") & (again["shift"] == "PM")]["staff"].iloc[0]
check("flagged one-day change kept on rebuild", kept, "Sarah M.")

wiped = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), template, timeoff,
                     existing=edited, keep_manual=False)
back = wiped[(wiped["date"] == "2026-08-04") & (wiped["shift"] == "PM")]["staff"].iloc[0]
check("one-day change dropped when asked", back, "Jordan T.")

# THE REGRESSION THIS FIXES: change the template, rebuild, and unflagged rows
# must follow the new pattern. Previously every row was compared against the
# template, so changing it made the whole schedule look hand-edited and nothing
# rebuilt at all.
new_template = template.copy()
new_template.loc[(new_template["weekday"] == "Tue") &
                 (new_template["shift"] == "AM"), "staff"] = "Jordan T."
rebuilt = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), new_template,
                       timeoff, existing=gen, keep_manual=True)
tue_am = rebuilt[(rebuilt["date"] == "2026-08-04") & (rebuilt["shift"] == "AM")]
check("template change reaches the schedule", tue_am["staff"].iloc[0], "Jordan T.")
mon_am = rebuilt[(rebuilt["date"] == "2026-08-03") & (rebuilt["shift"] == "AM")]
check("other days untouched by that change", mon_am["staff"].iloc[0], "Sarah M.")

# ...and a flagged row still resists it
mixed = gen.copy()
j = mixed.index[(mixed["date"] == "2026-08-04") & (mixed["shift"] == "AM")][0]
mixed.at[j, "staff"] = "Sarah M."
mixed.at[j, "manual"] = "yes"
held = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), new_template,
                    timeoff, existing=mixed, keep_manual=True)
check("flagged row resists a template change",
      held[(held["date"] == "2026-08-04") & (held["shift"] == "AM")]["staff"].iloc[0],
      "Sarah M.")

# custom hours survive a rebuild even on template-driven rows
timed = gen.copy()
k = timed.index[(timed["date"] == "2026-08-05") & (timed["shift"] == "AM")][0]
timed.at[k, "start"] = "09:00"
kept_times = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), template,
                          timeoff, existing=timed, keep_manual=True)
check("custom start time survives rebuild",
      kept_times[(kept_times["date"] == "2026-08-05") &
                 (kept_times["shift"] == "AM")]["start"].iloc[0], "09:00")
reset = build_shifts(dt.date(2026, 8, 3), dt.date(2026, 8, 14), template,
                     timeoff, existing=timed, keep_manual=False)
check("full rebuild resets times",
      reset[(reset["date"] == "2026-08-05") &
            (reset["shift"] == "AM")]["start"].iloc[0], "08:00")

# ------------------------------------------------------------- time off apply
more_off = pd.DataFrame([{"staff": "Jordan T.", "start": "2026-08-05",
                          "end": "2026-08-05", "type": "Sick", "note": ""}])
after, hits = apply_timeoff(gen, more_off)
check("one sick day opens one shift", hits, 1)
check("that shift is OPEN",
      after[(after["date"] == "2026-08-05") & (after["shift"] == "PM")]["staff"].iloc[0],
      OPEN)

# ------------------------------------------------------------------ hours
ht = hours_table(gen, ["Sarah M.", "Jordan T."])
check("hours rows = weekdays", len(ht), 10)
check("Sarah 5h on Aug 3",
      float(ht[ht["date"] == "2026-08-03"]["Sarah M."].iloc[0]), 5.0)
check("Sarah 0h while away",
      float(ht[ht["date"] == "2026-08-12"]["Sarah M."].iloc[0]), 0.0)

# --------------------------------------------------------------- overtime
# Sarah: 5h/day x 5 days = 25h/week. No daily OT, no weekly OT.
ot = overtime(gen, ["Sarah M.", "Jordan T."], set())
sarah_p1 = ot[(ot["staff"] == "Sarah M.") & (ot["period"] == "2026-08 1-15")]
check("Sarah hours 1-15", float(sarah_p1["hours"].iloc[0]), 25.0)
check("Sarah no daily OT", float(sarah_p1["daily_ot"].iloc[0]), 0.0)
check("Sarah no weekly OT", float(sarah_p1["weekly_ot"].iloc[0]), 0.0)

# nine-hour days: 1h daily OT each, but capped hours are 8x5=40 so no weekly OT
long_days = pd.DataFrame([
    {"date": d, "shift": "AM", "staff": "Pat", "start": "08:00", "end": "17:00"}
    for d in ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
])
ot2 = overtime(long_days, ["Pat"], set())
check("9h x5 total hours", float(ot2["hours"].iloc[0]), 45.0)
check("9h x5 daily OT", float(ot2["daily_ot"].iloc[0]), 5.0)
check("9h x5 weekly OT stays 0 (first 8 only)", float(ot2["weekly_ot"].iloc[0]), 0.0)

# add a Saturday to push capped hours past 40
with_sat = pd.concat([long_days, pd.DataFrame([
    {"date": "2026-08-08", "shift": "AM", "staff": "Pat", "start": "08:00", "end": "12:00"}
])], ignore_index=True)
ot3 = overtime(with_sat, ["Pat"], set())
check("Sat pushes weekly OT to 4", float(ot3["weekly_ot"].iloc[0]), 4.0)
check("Sat daily OT unchanged", float(ot3["daily_ot"].iloc[0]), 5.0)

# weekly OT lands in the pay period where the week ENDS
# week of Mon Aug 31 runs to Sun Sep 6, so OT belongs to the Sept 1-15 period
cross = pd.DataFrame(
    [{"date": d, "shift": "AM", "staff": "Pat", "start": "08:00", "end": "16:00"}
     for d in ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]]
    + [{"date": "2026-09-05", "shift": "AM", "staff": "Pat",
        "start": "08:00", "end": "11:00"}]
)
ot4 = overtime(cross, ["Pat"], set())
sep = ot4[ot4["period"] == "2026-09 1-15"]
check("weekly OT credited to September", float(sep["weekly_ot"].iloc[0]), 3.0)
aug = ot4[ot4["period"] == "2026-08 16-31"]
check("August period carries only its own hours", float(aug["hours"].iloc[0]), 8.0)
check("August period has no weekly OT", float(aug["weekly_ot"].iloc[0]), 0.0)

# stat holidays counted separately
ot5 = overtime(gen, ["Sarah M.", "Jordan T."], {dt.date(2026, 8, 3)})
s5 = ot5[(ot5["staff"] == "Sarah M.") & (ot5["period"] == "2026-08 1-15")]
check("stat hours flagged", float(s5["stat_hours"].iloc[0]), 5.0)
check("stat hours still counted in total", float(s5["hours"].iloc[0]), 25.0)

# ------------------------------------------------------------- seed sanity
check("seed shifts cover Aug+Sep weekdays", len(SEED["shifts.csv"]),
      sum(1 for i in range((dt.date(2026, 9, 30) - dt.date(2026, 8, 1)).days + 1)
          if (dt.date(2026, 8, 1) + dt.timedelta(days=i)).weekday() < 5) * 2)

print()
if fails:
    print(f"{len(fails)} FAILED: {fails}")
    sys.exit(1)
print("All logic checks passed.")
