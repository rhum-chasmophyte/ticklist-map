# Ticklist map — setup guide

This project turns your Google Sheet ticklist into a live web page. Every night, a robot on
GitHub reads the Sheet, looks up the weather for each send in Google Earth Engine, and saves the
results as a data file the web page loads.

You set it up once. After that you just log climbs in your Apps Script form as usual.

---

## How the pieces fit together

```
 Google Sheet (stays in your Drive, unchanged)
        │   read every night
        ▼
 GitHub repository  ── holds this folder ──►  GitHub Actions  (the nightly robot)
        │                                         │ asks Earth Engine for weather
        │                                         ▼
        │                                  writes docs/data/sends.json
        ▼
 GitHub Pages  (free hosting of the docs/ folder)  ──►  embedded in your Weebly page
```

**Nothing moves into Google Drive.** The Sheet stays where it is. This folder goes to GitHub.
The `docs/` folder is the website folder (GitHub Pages' name for it), not a place for documents.

The robot needs a way to log in to Google without your password. That is what the
**service account** in Part 1 is: a robot Google account with its own key file, which you share
the Sheet with.

You'll do this in four parts:

1. Google Cloud: create the robot account and its key (about 20 minutes)
2. Test on your PC (10 minutes, optional but recommended)
3. GitHub: create the repository and upload this folder (15 minutes)
4. GitHub: add the secrets, turn on the website, run it

---

## Part 1 — Google Cloud

You already have a Google Cloud project, because your Earth Engine project `ee-richiehum` *is* one.
Use the Google account you normally use for Earth Engine.

### 1.1 Open the right project

1. Go to <https://console.cloud.google.com>.
2. At the top left, next to the Google Cloud logo, there's a project picker. Click it and
   choose **ee-richiehum**. Check that it says ee-richiehum before every step below, since it's easy to
   end up in the wrong project.

### 1.2 Turn on the two APIs

An "API" here just means a Google service the robot is allowed to use.

1. Open <https://console.cloud.google.com/apis/library> (project still ee-richiehum).
2. Search **Google Earth Engine API**, open it, and click **Enable**.
   If it says **Manage** instead, it's already on.
3. Go back, search **Google Sheets API**, open it, and click **Enable**.

### 1.3 Create the service account (the robot)

1. Open <https://console.cloud.google.com/iam-admin/serviceaccounts>.
2. Click **+ Create service account**.
3. Name: `ticklist-bot`. The ID fills in automatically. Click **Create and continue**.
4. Under **Grant this service account access to project**, add two roles. Click the
   **Select a role** box, type the name, and pick it. Then click **+ Add another role** for the second.
   - `Earth Engine Resource Viewer`
   - `Service Usage Consumer`
5. Click **Continue**, then **Done**.
6. You'll see the account in the list with an email like
   `ticklist-bot@ee-richiehum.iam.gserviceaccount.com`. Copy that email; you'll need it twice.

### 1.4 Download the key

1. Click the `ticklist-bot` account, then the **Keys** tab.
2. **Add key → Create new key → JSON → Create.** A `.json` file downloads.
3. Move it somewhere **outside Dropbox and outside this project folder**, for example
   `C:\Users\Richard\keys\ticklist-bot-key.json`.

> **Treat this file like a password.** Anyone with it can use your Earth Engine quota and read
> your Sheet. Never email it, never put it in Dropbox, never upload it to GitHub. If it ever
> leaks, delete the key on that same Keys tab and make a new one.

**If key creation is blocked** with a message about an organization policy
(`iam.disableServiceAccountKeyCreation`): Google blocks keys by default for newer Workspace
organizations such as snaplands.com. Either (a) as the Workspace admin, go to
**IAM & Admin → Organization policies**, find *Disable service account key creation*, and set an
exception for the ee-richiehum project; or (b) send me the exact error and I'll adjust the setup.

### 1.5 Share the Sheet with the robot

1. Open your ticklist Sheet.
2. Click **Share**, paste the `ticklist-bot@...` email, set it to **Viewer**, untick
   *Notify people*, and click **Share**.

### 1.6 Note the Sheet ID

In the Sheet's address bar, the ID is the long code between `/d/` and `/edit`:

```
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOp.../edit
                                       └──── Sheet ID ────┘
```

Copy it somewhere handy.

---

## Part 2 — Test on your PC (recommended)

This checks that the key, the APIs, and the Sheet sharing all work before GitHub gets involved,
and it's where errors are easiest to read.

1. Unzip this folder anywhere (Dropbox is fine for the project, just not for the key).
2. Open **PowerShell** (Start menu → type PowerShell).
3. Go to the folder. Adjust the path to wherever you unzipped it:
   ```powershell
   cd "C:\Users\Richard\Dropbox\ticklist-map"
   ```
4. Install the libraries (one time):
   ```powershell
   python -m pip install -r requirements.txt
   ```
5. Point it at your key and Sheet, then run it:
   ```powershell
   $env:GOOGLE_APPLICATION_CREDENTIALS = "C:\Users\Richard\keys\ticklist-bot-key.json"
   $env:SHEET_ID = "paste-your-sheet-id-here"
   python -m pipeline.build
   ```
   (These two `$env:` lines only last until you close PowerShell. Re-run them in a new window.)

The first run lists each crag as it fetches 25 years of weather. Expect a few minutes per
crag. When it finishes, it prints something like:

```
Wrote ...\docs\data\sends.json — 214 sends, 31 crags, climate: {'ok': 210, 'pending': 2, 'no_location': 2}
```

- **`no_location`** sends have a Crag + State pair that doesn't match a row in Crag Locations.
  The warning above that line names them; usually it's a spelling or spacing difference.
- **`pending`** sends are recent international ones; ERA5-Land runs about a week behind.
- Run it a second time: it should finish in seconds, because the weather is now cached.

If it fails, copy the whole error message to me. The most common ones:

| Error mentions | Fix |
|---|---|
| `PERMISSION_DENIED` / `403` on the spreadsheet | The Sheet isn't shared with the robot email (step 1.5) |
| `Google Sheets API has not been used` | Step 1.2, Sheets API not enabled (check the project is ee-richiehum) |
| `Earth Engine` + `not registered` or `permission` | Step 1.3 roles, or step 1.2 Earth Engine API |
| `WorksheetNotFound` | Tab names must be exactly `Ticklist` and `Crag Locations` |
| `KeyError: '8a points'` (or another column) | A header in the Sheet differs from the list below |

---

## Part 3 — GitHub

GitHub stores the project online and runs the nightly robot for free. You'll use
**GitHub Desktop**, an app that uploads changes with buttons instead of commands.

### 3.1 Create an account and install GitHub Desktop

1. Sign up at <https://github.com>. Your username will appear in the website address
   (`username.github.io`), so pick something you're happy to put on a portfolio.
2. Install GitHub Desktop from <https://desktop.github.com> and sign in with that account.

### 3.2 Create the repository from this folder

A repository ("repo") is just a project folder that GitHub keeps in sync.

1. In GitHub Desktop: **File → Add local repository**, and choose this folder.
2. It will say this isn't a Git repository yet. Click **create a repository**, keep the name
   `ticklist-map`, and click **Create repository**.
3. Click **Publish repository** (top bar).
   - **Untick "Keep this code private."** Free GitHub Pages hosting needs a public repo.
     That's safe: the published data has no Notes or Beta, and the `.gitignore` file in this
     folder blocks key files and CSV exports from ever being uploaded.
   - Click **Publish repository**.

**Before publishing, check the list of files in GitHub Desktop's left panel.** There should be
no `.json` key file and no `.csv` file in it. If there is, stop and move it out of the folder.

### 3.3 Making changes later

Whenever you change something in this folder (or I send you updated files): open GitHub Desktop,
type a short summary at the bottom left, click **Commit to main**, then **Push origin**.

The nightly robot also commits to the repo. If GitHub Desktop says your copy is behind,
click **Pull origin** before making your own changes.

---

## Part 4 — Secrets, website, first run

All of this is on github.com, on your repo's page (`github.com/<username>/ticklist-map`).

### 4.1 Add the two secrets

Secrets are encrypted settings that only the robot can read; nobody browsing your repo sees them.

1. Repo page → **Settings** (top tab) → left menu **Secrets and variables → Actions**.
2. **New repository secret**:
   - Name: `GCP_SA_KEY`
   - Value: open your key `.json` file in Notepad, select everything (Ctrl+A), copy, and paste.
   - **Add secret**.
3. **New repository secret** again:
   - Name: `SHEET_ID`
   - Value: the Sheet ID from step 1.6.

### 4.2 Let the robot save its results

**Settings → Actions → General**, scroll to **Workflow permissions**, select
**Read and write permissions**, and click **Save**.

### 4.3 Turn on the website

**Settings → Pages**. Under *Build and deployment*: Source **Deploy from a branch**,
Branch **main**, folder **/docs**, and click **Save**. After a minute or two the page shows
your site address: `https://<username>.github.io/ticklist-map/`.

(Until the map page is built, the site only has the data file. You can check it at
`https://<username>.github.io/ticklist-map/data/sends.json`.)

### 4.4 Run it

1. Repo page → **Actions** tab. If it asks, click to enable workflows.
2. Click **Nightly data refresh** on the left → **Run workflow** → **Run workflow**.
3. Click the run that appears to watch it. A green tick means it worked. A red cross: click
   into the failed step and copy the error to me.

If you completed Part 2, the cache is already filled, so this run is quick. From now on it runs
every night at about 3 am Denver time.

> GitHub pauses scheduled robots on repos with no activity for 60 days and sends you an email.
> If that happens, go to the Actions tab and re-enable the workflow.

---

## Part 5 — The map page and Weebly

The page lives in `docs/` (`index.html`, `app.js`, `style.css`) and loads `docs/data/sends.json`.
Once it's pushed, it's at `https://<username>.github.io/ticklist-map/`. It reads the data fresh on
every visit, so the nightly run updates it automatically, and the 365-day window rolls forward
on its own.

### Embed it in Weebly

1. In the Weebly editor, open the page and drag an **Embed Code** element (under *Build*, near the
   bottom of the element list) to where the map should go.
2. Click the element → **Edit Custom HTML**, and paste:
   ```html
   <iframe src="https://<username>.github.io/ticklist-map/?embed=1&theme=light"
           width="100%" height="800" style="border:0;display:block" loading="lazy"
           title="Bouldering ticklist map"></iframe>
   ```
3. Click outside the element and **Publish**. The map won't show inside the editor preview on some
   plans; check the published page.

`?embed=1` makes the page fill the frame exactly, so it never scrolls inside its own box; change
`height` to make it taller or shorter. `?theme=light` keeps it light to match a light site. Use `?theme=dark` for a dark site, or remove it
to follow each visitor's system setting. Inside an iframe, the map needs two fingers (or Ctrl + scroll)
to pan and zoom, so visitors can still scroll past it.

If your Weebly plan doesn't offer Embed Code, use a **Button** element linking to the page instead.

### Approximate dates

A date with more than 20 sends across all crags (a trip logged on one day), or any January 1
(a year-only entry), is treated as approximate. Those sends count on the map, in the top 10 and in
scores, but are left out of the conditions analysis, because their weather would belong to the
wrong day. Change the rule with `APPROX_DATE_MIN_SENDS` and `APPROX_DATE_JAN1` in
`pipeline/config.py`. Fixing the real dates in the Sheet brings those sends back into the analysis.

---

## Part 6 — The logging form (Apps Script)

The form's code is in `apps-script/`. It doesn't run on GitHub; it lives in your Sheet.

1. Open the Sheet → **Extensions → Apps Script**.
2. Open `Code.gs`, select everything, and paste the contents of `apps-script/Code.gs` over it.
3. Open `Index.html` the same way and paste `apps-script/Index.html` over it.
4. Click **Save** (the disk icon).
5. **Deploy → Manage deployments** → the pencil icon on your existing deployment →
   **Version: New version** → **Deploy**. Your form's web address stays the same. If you skip
   this step, the old form keeps running.
6. **One-time date fix:** the old form saved dates as 23:00 the day before. In the toolbar's
   function list choose `fixEntryDates` and click **Run**. It moves every date in Entries to
   midnight on the intended day and logs how many it changed. Running it again changes nothing.

What the form does now:

- Dropdowns cascade Nation → State → Crag → Zone → Boulder, built from everything in the Ticklist
  and Boulder Locations. Each level has **+ New…**. Zone and Boulder can be left as "not sure".
- Picking a boulder with Zone left blank fills in that boulder's zone.
- For a **new boulder**, or a listed one without coordinates, a map appears. Tap the spot,
  press **Use my location**, or paste "lat, lon" (in Google Maps: long-press the spot and tap
  the coordinates to copy them). Your other boulders at the crag show as green dots.
- On **Log climb**, the climb goes to Entries (each value under its matching header), and the
  boulder is added to Boulder Locations, or gets its coordinates filled in if it was listed
  without them. A new boulder with no location is still added, with x/y blank, as a reminder.
- Stars take halves (tap 3, then +½). Place selections stay put for logging several problems.

If **Use my location** says the browser blocked it, your phone or Google's page settings aren't
sharing location with the form; tapping the map or pasting coordinates always works.

## Reference

### Sheet columns this reads

**Ticklist:** `Date`, `Grade`, `Stars`, `Name`, `Boulder`, `Zone`, `Crag`, `State`, `Nation`,
`NumberSessions`, `Notes`, `Beta`, `Style`. Columns are found by header name, so their order and
any extra columns (like `Location`) don't matter. Notes and Beta are read but never published.

- Rows with no Grade (day logs) are skipped.
- Stars can be halves (3.5). Blank means unrated, which is different from 0.
- "FA" or "First Ascent" anywhere in Style marks a first ascent.
- Points are calculated from Grade and Style (450 + 50 per V-grade; Flash +53, First Ascent +33,
  Second Go +2, Repetition −1). An `8a points` column, if present, is used instead.

**Boulder Locations:** `Nation`, `State`, `Crag`, `Zone`, `Boulder`, `x` (longitude), `y` (latitude).
A row with Boulder blank is a pin for the zone itself. Boulders are matched to the Ticklist on
State + Crag + Zone + Boulder (spelling must match; capitals don't matter). A boulder named in the
Ticklist but missing here, or here without x/y, is drawn at its zone, then its crag, on a dashed
"approximate" marker. The run log lists those, so you know what's left to place.

**Crag Locations** (now optional for crags that have boulder coordinates):

| Column | Required | Meaning |
|---|---|---|
| `Crag`, `State` | yes | Must match the Ticklist exactly; the pair is the join key |
| `Country` | yes | Two-letter code: US, CA, NO, ZA |
| `Lat`, `Lon` | yes | Decimal degrees. Used only for crags with no boulder coordinates |
| `Elev_m` | no | Typical boulder elevation in metres; overrides the DEM |
| `Radius_m` | no | Spread of the area; crag elevation becomes the DEM average within it |

A crag with boulder coordinates is placed at their centre, and its elevation (for the weather
correction) is the DEM average over those boulders. Its downloaded weather is kept as long as
that centre stays in the same weather grid cell; otherwise only that crag is re-downloaded.

### What's in the folder

| Path | What it is |
|---|---|
| `pipeline/` | The Python code. Settings (lapse rates, baseline years, rain threshold) are in `pipeline/config.py` |
| `.github/workflows/nightly.yml` | The nightly robot's instructions |
| `cache/` | Saved daily weather per crag, so each night only fetches new days |
| `docs/` | The website: `index.html`, `app.js` (map, panels, charts), `style.css`. `docs/data/sends.json` is the data it loads |
| `apps-script/` | The logging form (`Code.gs`, `Index.html`), pasted into the Sheet's Apps Script editor |
| `.gitignore` | Stops key files and CSV exports from being uploaded |

### Method notes

- **Climate source:** gridMET for crags in the contiguous US; ERA5-Land everywhere else
  (including Alaska and Hawaii). Each send records which one it used.
- **Lapse rate:** T_crag = T_grid + Γ·(z_grid − z_crag), with Γ = 6.5 °C/km for Tmax and
  4.0 °C/km for Tmin. z_grid is the Copernicus 30 m DEM averaged over the climate grid cell.
  Specific humidity is held fixed; RH, VPD, dewpoint and dewpoint spread are recalculated at the
  crag. Precipitation and wind are not corrected.
- **Baseline:** for each send, that crag's days within ±15 days of the same date, 2001–2025.
  Each variable gets a percentile. The test of whether send days differ from typical days
  (Kolmogorov–Smirnov against an even spread) counts each climbing day (crag + date) once,
  so five sends on one day are one observation. The page recomputes it for whatever is in view.
- **ERA5-Land** is land-only, so coastal crags are moved to the nearest grid cell with data.
- The cache stores uncorrected weather, so changing lapse rates or the baseline in
  `config.py` only needs a rerun, not a re-download.

### Offline test (no Google access needed)

Export both tabs as CSV, put them in this folder, and run:

```powershell
python -m pipeline.build --ticklist-csv ticklist.csv --crags-csv crags.csv --boulders-csv boulders.csv --fake-climate
```

This uses made-up weather to check matching and formatting. Results go to a separate
`test-output/` folder, which is never uploaded, so it can't mix with the real data.
