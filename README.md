# Cedar & Sage — Reception Shift Schedule

A staff scheduling app for Cedar & Sage Physiotherapy. It runs entirely in the
browser (Shiny for Python compiled to WebAssembly via shinylive), is hosted for
free on GitHub Pages, and keeps the schedule in a Google Sheet — one tab per
table, shared with staff the ordinary way.

The interface follows Mantine's design language with Tabler icons via Iconify.
Dash Mantine Components itself is a Dash library and cannot run inside Shiny,
so the tokens, radii, shadows and spacing are reproduced in CSS.

There is no server of our own and no database. The page is static; the schedule
lives in the clinic's own Google Sheet, under the clinic's own Google account,
reached directly from the browser.

## Running it

**Live:** https://klimstram.github.io/schedule/ (once Pages is switched on)

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
3. **A Google Sheet**, via the link box and **Connect Sheet**. Works in every
   browser and on phones. Needs a one-time Google Cloud setup — see below.

**Save** writes to whichever of 2 or 3 is connected. If neither is, it says so
and keeps everything in the browser.

Five CSVs make up the schedule:

| File | Columns | What it holds |
|---|---|---|
| `staff.csv` | `name` | Reception staff, max 15. Order sets each person's colour. |
| `template.csv` | `weekday,shift,staff` | The regular weekly pattern. |
| `shifts.csv` | `date,shift,staff,start,end,manual` | The actual schedule — the source of truth. |
| `timeoff.csv` | `staff,start,end,type,note` | Vacation, sick, personal, other. |
| `holidays.csv` | `date,name` | BC and national statutory holidays. |

Dates are `YYYY-MM-DD`, times are 24-hour `HH:MM`. `OPEN` means an unfilled
shift; `CLOSED` means the clinic is shut that half-day.

`manual` is `yes` when that row is a deliberate one-day override, and empty when
it simply follows the weekly pattern. **Rebuild** regenerates every empty row and
leaves the `yes` rows alone; the week view marks them "· one-day".

That flag has to be stored, not inferred. Deciding "is this a hand-edit?" by
comparing a row to the template looks reasonable and is subtly broken: change the
template and suddenly every row differs from it, so the whole schedule reads as
hand-edited and **Rebuild** does nothing at all. `test_logic.py` pins this.

The same five CSVs also live in [`app/data/`](app/data). Those are the ones
bundled into the deployed site, so the app opens with something in it rather
than an empty grid.

> **Keep real staff data out of `app/data/`.** Those files are packed into
> `docs/app.json`, which is served publicly and committed to a public repo.
> Names, sick days and vacation dates put there are readable by anyone with the
> URL — personal information the clinic is responsible for under BC PIPA.
>
> Treat `app/data/` as the demo skeleton: the roster shape, the statutory
> holidays, placeholder names. The real schedule lives in the Google Sheet,
> loaded at runtime, and never touches git.
>
> If you do want real data to load by default, the repo has to be private and
> Pages needs a paid GitHub plan, since Pages from a private repo isn't on the
> free tier.

`starter-csvs/` holds an untouched copy to fall back on.

### Using a Google Sheet (recommended)

One spreadsheet holds everything, one tab per table: `staff`, `template`,
`shifts`, `timeoff`, `holidays`. Reading and writing both go through the Sheets
API, so the sheet's own sharing settings decide who can edit, and every change is
attributed in its version history.

Two ways to get started, both in the header:

- **New sheet** — the app creates the spreadsheet for you, with all five tabs,
  and writes the current schedule into it. Nothing to paste. Afterwards the
  status line links straight to it, so you can open it in Google Sheets and
  share it with staff the normal way.
- **Connect** — for a spreadsheet that already exists: paste its link in the box
  first. Missing tabs are created on the first save, so an empty spreadsheet is
  a fine starting point.

Either way the link is remembered in browser storage, so it is a one-time step
per device.

**Sign-in is per device and per browser session.** The sheet link persists, but
the Google access token does not — it lasts about an hour and is never written to
disk. So on a phone, or after leaving the app for a while, tap **Connect** once
before saving. The app says so plainly: the status line reads "Sheet remembered —
tap Connect to sign in on this device", and pressing Save without a token warns
instead of silently keeping the change in the browser.

**One-time Google Cloud setup, about ten minutes:**

1. [console.cloud.google.com](https://console.cloud.google.com) → create a project.
2. **APIs & Services → Library →** enable **Google Sheets API**.
3. **OAuth consent screen →** External. Add the app name and your email. While
   it stays in *Testing* you list each staff member under **Test users** — up to
   100, and no Google verification is needed.
4. **Credentials → Create credentials → OAuth client ID →** Web application.
   Under **Authorised JavaScript origins** add the origin exactly — host only,
   no `/schedule`, no trailing slash: `https://klimstram.github.io`. Add
   `http://localhost:8000` as well for local development.
5. Paste the client ID into `GOOGLE_CLIENT_ID` in `app/app.py`, re-export, push.
   It is already filled in. The client ID is public by design, so keeping it in
   this repo is fine — there is no client *secret* in this flow.

The scope is `spreadsheets`. Share the sheet with staff the normal way — anyone
with edit access on the sheet can save from the app, anyone with view access can
read it in Google Sheets directly without the app at all.

**Why not store the data in this repo?** Writing a CSV or SQLite file back to
GitHub needs an API token, and a static page cannot hide one. A token with write
access to this repo would let any visitor push code that then deploys to your
Pages URL. On top of that a public repo makes the records public. GitHub hosts
the app; it must not hold the data.

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
   pattern. *Keep one-day changes* protects rows flagged `manual`; untick it to
   reset everything, custom hours included.
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
  exist in Safari or Firefox. Those browsers should connect a Google Sheet,
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
