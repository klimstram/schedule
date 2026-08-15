# Cedar & Sage — Reception Shift Schedule

A staff scheduling app for Cedar & Sage Physiotherapy. It runs entirely in the
browser (Shiny for Python compiled to WebAssembly via shinylive), is hosted for
free on GitHub Pages, and keeps the schedule in plain CSV files — in browser
storage, in a folder on disk, or in Google Drive.

The interface follows Mantine's design language with Tabler icons via Iconify.
Dash Mantine Components itself is a Dash library and cannot run inside Shiny,
so the tokens, radii, shadows and spacing are reproduced in CSS.

There is no server and no database. Nothing about the schedule ever leaves the
clinic's own machines: the page is static, and the staff data lives only in the
CSVs the user opens.

## Running it

**Live:** https://YOUR-USERNAME.github.io/schedule/ (once Pages is switched on)

The built site is committed to [`docs/`](docs) and served straight from `main` —
no CI required. `docs/app.json` is the bundle that holds `app.py` and the CSVs;
it has to sit beside `docs/index.html`, which it does.

**Locally, for development:**

```bash
pip install shiny pandas
shiny run --reload app/app.py
```

**Rebuilding the site after a change:**

```bash
pip install shinylive
shinylive export app docs
touch docs/.nojekyll
python -m http.server --directory docs 8008   # check it locally
git add docs && git commit -m "rebuild site" && git push
```

Anything under `app/` — `app.py` and every CSV in `app/data/` — gets packed into
`docs/app.json` by that export. Editing a source file without re-running the
export changes nothing on the live site.

## Where your changes go

Three storage layers, in the order the app tries them:

1. **Browser storage, always.** Every change is written to `localStorage`
   immediately. Close the tab, reload, come back tomorrow — your work is still
   there. This needs no setup and works in every browser.
2. **A folder on disk**, via **Open folder**. Chrome and Edge only, since it
   uses the File System Access API. Point it at a folder synced by Google Drive
   for Desktop and sharing takes care of itself.
3. **Google Drive**, via **Google Drive** sign-in. Works in every browser and on
   phones. Needs a one-time Google Cloud setup — see below.

**Save** writes to whichever of 2 or 3 is connected. If neither is, it says so
and keeps everything in the browser.

Five CSVs make up the schedule:

| File | Columns | What it holds |
|---|---|---|
| `staff.csv` | `name` | Reception staff, max 15. Order sets each person's colour. |
| `template.csv` | `weekday,shift,staff` | The regular weekly pattern. |
| `shifts.csv` | `date,shift,staff,start,end` | The actual schedule — the source of truth. |
| `timeoff.csv` | `staff,start,end,type,note` | Vacation, sick, personal, other. |
| `holidays.csv` | `date,name` | BC and national statutory holidays. |

Dates are `YYYY-MM-DD`, times are 24-hour `HH:MM`. `OPEN` means an unfilled
shift; `CLOSED` means the clinic is shut that half-day.

The same five CSVs also live in [`app/data/`](app/data). Those are the ones
bundled into the deployed site, so the app opens with something in it rather
than an empty grid.

> **Keep real staff data out of `app/data/`.** Those files are packed into
> `docs/app.json`, which is served publicly and committed to a public repo.
> Names, sick days and vacation dates put there are readable by anyone with the
> URL — personal information the clinic is responsible for under BC PIPA.
>
> Treat `app/data/` as the demo skeleton: the roster shape, the statutory
> holidays, placeholder names. The real schedule lives in the Drive folder that
> staff open with **Open schedule folder** at runtime, and never touches git.
>
> If you do want real data to load by default, the repo has to be private and
> Pages needs a paid GitHub plan, since Pages from a private repo isn't on the
> free tier.

`starter-csvs/` holds an untouched copy to fall back on.

### Switching on Google Drive

The app signs in with your own Google Cloud project, so the clinic's data never
passes through anyone else's. Roughly ten minutes, once:

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create
   a project.
2. **APIs & Services → Library →** enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen →** External. Fill in the app name
   and your email. While it stays in *Testing* you must list each staff member
   under **Test users** — up to 100, and no Google verification is needed.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →**
   Web application. Under **Authorised JavaScript origins** add your Pages
   origin exactly, with no trailing slash:
   `https://YOUR-USERNAME.github.io`. Add `http://localhost:8000` too if you
   want it working while developing.
5. Copy the client ID and paste it into `DRIVE_CLIENT_ID` near the bottom of
   `app/app.py`, then re-export and push.

The requested scope is `drive.file`, the narrowest one available: the app can
only see the folder it creates (`Cedar & Sage Schedule`) and nothing else in
anyone's Drive. Change `DRIVE_FOLDER_NAME` if you'd rather it were called
something else.

Because each person signs in as themselves, `drive.file` gives them access to
their *own* copy of that folder. To have several people work on one shared
schedule, have the owner share the folder in Drive and keep one person as the
one who saves — or move to a Shared Drive.

### Putting the folder on Google Drive

Install Google Drive for Desktop and put the folder inside your synced Drive.
It then behaves like any local folder, and Drive handles getting the files to
everyone else. No API keys, no OAuth, no sharing links.

## Four ways to edit

Nothing in the app requires typing a name or a time — every value is chosen
from a dropdown, so a typo can't quietly break the hours report.

1. **Week tab** — click any shift on a day card. A dialog opens with three
   dropdowns (who, start, end) and a choice between *just this day* and
   *every Tuesday*. Picking the weekly option also updates the pattern and
   every later matching shift.
2. **Shifts tab** — select a row and press **Edit** for the same dialog, or
   **Set to OPEN** to clear it. Column filters help you find things.
3. **Template tab** — the weekly pattern is a grid of dropdowns, one per
   weekday and shift. Change them, then **Rebuild** a date range from the
   pattern. *Keep one-day changes* preserves anything that already differs.
4. **Time off tab** — booking time off automatically opens up that person's
   shifts across the range.

## Hours and overtime

The Hours report tab reproduces the bookkeeping output from the old spreadsheet:
hours per person per day, subtotalled by pay period (1st–15th and 16th–month
end), with BC overtime flagged.

- **Daily OT** — anything over 8 hours in a day.
- **Weekly OT** — anything over 40 hours in a Mon–Sun week, counting only the
  first 8 hours of each day, credited to the pay period the week *ends* in.
- **Stat hours** — hours worked on a statutory holiday, reported separately.

These are review flags, not payroll. Confirm against BC Employment Standards
before paying overtime.

The logic lives in `overtime()` in `app/app.py` and is covered by
`test_logic.py`, which checks it against hand-computed expectations including
the awkward cases: nine-hour days that create daily but not weekly OT, and a
week that straddles the Aug/Sep pay-period boundary.

```bash
python test_logic.py
```

## Deploying

One-time setup: **Settings → Pages → Source → Deploy from a branch → `main` →
`/docs`**. Push, wait a minute, and the site is live. Every later deploy is just
a rebuild of `docs/` and a push.

`docs/.nojekyll` matters — without it GitHub Pages runs Jekyll, which strips
directories beginning with an underscore and breaks the Pyodide assets.

The build is about 43 MB of Pyodide runtime and Python wheels, and it is
committed. That is the trade for not needing CI, but it does mean each rebuild
writes new binaries into git history. If the repo gets uncomfortably large,
switch to the GitHub Actions route instead: export to `site/` in CI, publish the
artifact, and stop committing `docs/`. There's a ready-made workflow in
`.github/workflows/deploy.yml` if you'd rather start there.

## Known limitations

- **First load is slow.** The browser downloads roughly 40 MB of Python runtime
  the first time. It's cached afterwards, but the first visit on a new device
  takes a while — and it's a poor experience over cellular.
- **Opening a folder needs Chrome or Edge.** The File System Access API doesn't
  exist in Safari or Firefox. Those browsers should use Google Drive sign-in,
  which works everywhere. Either way, browser storage keeps your work safe in
  the meantime.
- **Icons and the typeface come from a CDN.** Iconify (`cdn.jsdelivr.net`) and
  Google Fonts. If either is blocked the app still works — buttons keep their
  text labels and the type falls back to the system font.
- **Concurrent edits are last-write-wins.** Two people saving the same file
  within a few minutes of each other will have Drive create a conflicted copy
  rather than merge them. Fine when one person owns the schedule; worth knowing
  if that changes.
- **Anything in `app/data/` is public.** GitHub Pages sites are public, and so
  is this repo, so `docs/app.json` — and every CSV baked into it — can be read
  by anyone who finds the URL. See the warning below.

## Where this came from

Replaces an Excel workbook plus a Google Apps Script (`build_schedule.py` and
`schedule-sync.gs` in the older project folder). The overtime rules, the pay
period split, the statutory holiday list and the pastel colour palette were
carried over from `build_schedule.py` so the numbers match the old workbook.
