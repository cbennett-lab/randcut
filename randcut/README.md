# RandCut — Random Video Clip Stitcher

Hit one button → picks 3 random videos from your Google Drive folder → grabs the first 4 seconds of each → stitches into one 12-second MP4.

---

## Before You Deploy — Two Setup Steps

### 1. Set Your Google Drive Folder
Open `main.py` and point this at your main folder:
```python
MAIN_DRIVE_FOLDER_ID = "1wsEs_t4F3SqdKtGLiYLtIUrfvIll0Ldr"
```
Make sure the folder is set to **"Anyone with the link can view"** — the app reads Drive
with an API key, not OAuth, so private folders are invisible to it.

Categories and players are discovered from the folder structure at startup, nothing is
hardcoded: each top-level folder is a category needing `Gameplay/` and `Music/`
subfolders, `IRL/<Player Name>/` gives the players, `_Character Images/<Player Name>/`
gives their avatar, and a `recipe.json` in a category makes it a combo built from other
categories. Folders starting with `_` are skipped.

### 2. Get a Google API Key (free)
The app uses Google's Drive API to list your videos. You need a free API key:

1. Go to https://console.cloud.google.com
2. Create a new project (call it anything)
3. Go to **APIs & Services → Library**
4. Search for "Google Drive API" → Enable it
5. Go to **APIs & Services → Credentials**
6. Click **Create Credentials → API Key**
7. Copy the key — you'll paste it into Railway as an environment variable

---

## Deploy to Railway

1. Push to GitHub, then Railway → New Project → Deploy from GitHub.

2. **Add a volume.** The signed-in Google credential is stored on disk, and
   Railway's filesystem is otherwise wiped on every redeploy. Open the Command
   Palette (⌘K) or right-click the project canvas → **Volume**, attach it to the
   service, and set the mount path to:
   ```
   /app/state
   ```
   The Dockerfile's workdir is `/app` and the store defaults to `./state`, so that
   path needs no extra configuration. (Or mount anywhere and point
   `RANDCUT_STATE_DIR` at it.)

3. **Set Variables** (Project → Variables):
   ```
   GOOGLE_CLIENT_ID      = <from Google Cloud>
   GOOGLE_CLIENT_SECRET  = <from Google Cloud>
   OAUTH_REDIRECT_URI    = https://<your-app>.up.railway.app/auth/callback
   ALLOWED_DOMAIN        = gymclassvr.com
   SECRET_KEY            = <fresh 48-byte token, not the local one>
   ```
   Do **not** set `DEV_NO_AUTH` — that disables the login gate. `GOOGLE_API_KEY`
   is not needed; Drive comes from the signed-in account.

4. **Register the production redirect URI** on the same OAuth client in Google
   Cloud, alongside the localhost one. It must match `OAUTH_REDIRECT_URI` exactly.

5. Open the URL → sign in → check **Connections**. The first sign-in is what
   connects Drive.

Keep the service at **one replica**. Volumes are incompatible with replicas, and
the render queue is in-process with a single ffmpeg worker by design.

---

## Run Locally

```bash
# Install FFmpeg
brew install ffmpeg        # Mac
sudo apt install ffmpeg    # Ubuntu/Linux

# Put your Drive API key in randcut/.env  (copy the template, then fill it in)
cp randcut/.env.example randcut/.env

# Launch — creates .venv, installs deps, serves with autoreload
./run.sh                   # from the repo root; PORT=8001 ./run.sh to change port
```

Then open http://localhost:8000.

The template ships with `DEV_NO_AUTH=1`, which skips the login gate and reads Drive
with `GOOGLE_API_KEY` — so local development needs no OAuth setup. `run.sh` generates
a `SECRET_KEY` on first run. **Never set `DEV_NO_AUTH` on the deployed app**; it makes
the URL public.

`.env` is gitignored, so your keys never get committed. `main.py` loads it at import
time and never overrides variables already in the environment — which is why the same
code picks up Railway's Variables in production with no changes.

To exercise the real sign-in flow locally, fill in the OAuth variables and remove
`DEV_NO_AUTH`.

---

## Sign-in (deployed app)

The Railway URL is public, so the app requires a Google sign-in restricted to one
Workspace domain. The same consent grants Drive access, so **production needs no Drive
API key**.

1. Google Cloud → **APIs & Services → OAuth consent screen**. Set User Type to
   **Internal** — only your Workspace can sign in, no verification review is needed,
   and refresh tokens don't expire.
2. **Credentials → Create Credentials → OAuth client ID → Web application.** Add an
   authorised redirect URI of `https://<your-app>.up.railway.app/auth/callback`.
3. In Railway → **Variables**, set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
   `OAUTH_REDIRECT_URI` (matching step 2 exactly), `ALLOWED_DOMAIN`, and a
   `SECRET_KEY` from `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.
4. Add a **volume** mounted where `RANDCUT_STATE_DIR` points. Saved credentials live
   there; without it a redeploy means reconnecting Google once.
5. Open the app, sign in, and check the **Connections** tab.

Buffer has no sign-in link — Buffer closed new OAuth app registration in 2019 and its
current API is personal-key only, so you paste a personal access token into the
Connections panel. It's stored encrypted. Nothing posts to Buffer yet.

---

## How to Use

1. Open your deployed URL
2. Pick an **influencer**, then a **video type** — only the types that influencer has clips for are listed
3. Hit **ADD TO QUEUE** for one render, or **STANDARD WEEK** for 7 spread across all of their video types
4. **Advanced** holds the first-clip seed and the gameplay top/bottom layout
5. Renders run one at a time. Change the settings and add more any time — they stack up in the same queue
6. Grab each video with **SAVE** as it finishes, or **DOWNLOAD ALL** for a zip named `YYYY-MM-DD_influencer.zip`
7. **STOP RENDER** kills the render in progress and drops everything still waiting; **CLEAR FINISHED** empties the list and deletes those files from the server

The queue lives on the server, so a page refresh picks it back up where it left off.

---

## Config Options (in main.py)

| Variable | Default | What it does |
|---|---|---|
| `MAIN_DRIVE_FOLDER_ID` | (your folder) | Which Google Drive folder to pull from |
| `NUM_PAIRS` | `3` | How many gameplay/IRL pairs go into one highlight |
| `CROSSFADE_SEC` | `2.0` | Audio crossfade between segments |
| `MAX_BATCH` | `20` | Ceiling on renders enqueued by one request |

## Generate missing IRL clips

In **Connections**, save a Kie.ai API key, then choose **Enable Drive uploads**
(or reconnect Google). Sign-in now requests `https://www.googleapis.com/auth/drive`
because approvals save files inside the existing, automatically discovered IRL
folders. Add that scope to the Internal OAuth app's Google Cloud consent settings;
existing read-only connections keep working for reads until they reconnect. The
connected account must have permission to add files to the destination folders.
An API key alone cannot upload to Google Drive.

In **File Management**, choose an influencer and expand a video type:

- **Generate** queues that missing clip; **Generate All Missing** queues the eligible
  missing clips in the expanded type, including a combo's source groups. Existing
  matches, queued jobs and pending previews are skipped. The button's count shows
  the unique eligible clips it will generate, without double-counting combo sources.
- Each batch shuffles the JPG/PNG images in that influencer's `_Character Images`
  folder and uses each once before shuffling for another cycle. For 9 clips and 6
  images, the first 6 use distinct images and the next 3 use 3 distinct images again.
  Skipped or duplicate clips do not consume a turn. A single Generate still picks
  a random image. Each job saves its image choice when queued and sends it with
  the same-numbered motion video from the source category's
  `IRL/Carrington` folder to Kie. For example, `layup_009.mp4` uses
  `My Best Layups/IRL/Carrington/Carrington_layup_009.mp4`, while the approved
  output for Elara remains `IRL/Elara/elara_layup_009.mp4`. Missing Carrington
  references block submission; first-person Gameplay is never a fallback.
  The row and review dialog show the selected motion filename. The request uses
  `kling-2.6/motion-control`, `mode: 720p` (standard), `character_orientation: video`,
  and the fixed prompt in `motion_control.py`.
- Carrington motion clips below 3 seconds are extended to 3.1 seconds with forward/slow-reverse
  playback, looping again for very short sources. Sources are never modified.
  The motion reference extension is silent. Clips over 30 seconds, videos over
  100MB, and images over 10MB are rejected with a row-level explanation.
- Yellow **Pending · Review** opens the generated video. **Discard** removes the
  local preview and returns the row to Missing. **Skip** closes an individual
  preview and keeps it Pending. When multiple clips are pending in the expanded
  video type, **Review All** walks through them once, showing your position and
  advancing after approve, discard, or skip. Skipped clips stay Pending for later;
  clips finishing generation during a review wait for the next pass.
  **Approve & Save to Drive** trims
  an extended clip to the Carrington reference's original duration minus 0.1 seconds;
  other clips are capped at the reference's original duration. It saves, for example,
  `elara_layup_019.mp4` to the source category's `IRL/Elara/` folder.

The generation queue and review files live in `RANDCUT_STATE_DIR/motion` on the
same Railway volume as credentials. They survive restarts; previews require login.
Kie's upload service hosts the reference copies, so no additional public media
route is needed. Keep one replica. Local encoding shares a lock with highlight
rendering to avoid concurrent ffmpeg passes.

Kie jobs run serially. Status checks back off from 5 to 30 seconds and stop after
15 minutes. **Check status** resumes the same task without creating another paid
generation. A submission timeout with no task ID is **Unconfirmed**: use
**Recover task** with the matching ID from Kie Logs. The references are verified
before accepting a recovered task. Never blindly regenerate an unconfirmed task.
Approval retries reuse a persisted, preallocated Drive file ID to prevent duplicate
uploads. Finished previews remain until approval/discard; monitor volume space.

Kie API references: [Motion Control](https://docs.kie.ai/market/kling/motion-control),
[file upload](https://docs.kie.ai/file-upload-api/upload-file-stream),
[task status](https://docs.kie.ai/market/common/get-task-detail).

Run the mocked integration tests and synthetic ffmpeg checks without API spend:

```bash
cd randcut
../.venv/bin/python -m unittest discover -s tests -v
```
