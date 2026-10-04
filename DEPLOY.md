# Deploying the Audio & Video Downloader to Azure App Service (Free F1)

This app is Flask + yt-dlp + ffmpeg. It runs on **Azure App Service** (Linux,
Python). It does **not** run on Azure Static Web Apps.

## Files that matter for deployment
- `server.py`        – the Flask app (exposes `app` for gunicorn)
- `index.html`       – the web page
- `requirements.txt` – Python packages Azure installs automatically
- `startup.sh`       – installs ffmpeg, then starts gunicorn

`startup.sh` runs gunicorn with `--threads 8`. Keep it: the page polls
`/progress/<job>` while a download is still running, and a single-threaded
worker would queue those polls behind the download so the bar never moves.

## One-time deploy with the Azure CLI

```bash
# 1. Log in
az login

# 2. Create a resource group
az group create --name audio-dl-rg --location southeastasia

# 3. Create a FREE (F1) Linux Python app + plan, and deploy this folder.
#    Run this from inside the project folder.
az webapp up \
  --name my-audio-downloader \        # must be globally unique
  --resource-group audio-dl-rg \
  --runtime "PYTHON:3.12" \
  --sku F1 \
  --os-type Linux

# 4. Set the startup command (installs ffmpeg, runs gunicorn)
az webapp config set \
  --name my-audio-downloader \
  --resource-group audio-dl-rg \
  --startup-file "bash startup.sh"

# 5. Set your access code (CHANGE THIS to your own secret)
az webapp config appsettings set \
  --name my-audio-downloader \
  --resource-group audio-dl-rg \
  --settings ACCESS_CODE="pick-a-strong-code"

# 6. Restart to apply
az webapp restart --name my-audio-downloader --resource-group audio-dl-rg
```

Your site: `https://my-audio-downloader.azurewebsites.net`

## Free-tier limits to know
- **Cold start:** F1 sleeps when idle; the first request after a nap waits
  ~1 min while ffmpeg installs. Subsequent requests are normal.
- **230-second platform timeout:** Azure kills any single request over 230s,
  no matter what gunicorn's own timeout says. Audio is usually fine. **The
  Video tab is the risk:** `/video-download` fetches the full video at the
  chosen resolution and then re-encodes the clip, and for a long video at
  1080p that can exceed 230s and fail. Lower resolutions and shorter
  selections stay well inside it. This limit is the platform's, not the app's,
  so B1 or higher is the only real fix.
- **1 GB disk / 60 CPU-min per day:** files auto-delete after 1 hour, 20 files,
  or 700 MB total — whichever comes first. Video eats all three far faster than
  audio. Trimmed clips also delete the full download once the cut is made.
  Re-encoding video is CPU-heavy, so the 60 CPU-min/day cap is easy to hit —
  upgrade to **B1** (`--sku B1`, ~$13/mo) to remove the cold start and the
  CPU quota.

## YouTube blocks datacenter IPs — you need cookies

A cloud-hosted instance will hit `Sign in to confirm you're not a bot` where
your home machine works fine. YouTube challenges cloud provider IP ranges
(Azure, AWS, GCP) but leaves residential connections alone. There is no way to
pass that challenge to a visitor's browser: the blocked request is the one the
*server* makes, so the trust has to belong to the server's session.

The fix is to give yt-dlp a signed-in YouTube session.

**Use a throwaway Google account, never your personal one.** The server acts as
that account for every download, and Google does suspend accounts for
datacenter-pattern access. Cookies are full account access — treat the file as a
password.

1. Sign into YouTube as the throwaway account in a browser.
2. Export cookies for `youtube.com` in **Netscape format** (a "Get cookies.txt"
   browser extension does this).
3. Put the file's contents in an app setting:

```bash
az webapp config appsettings set -n my-audio-downloader -g audio-dl-rg   --settings YT_COOKIES="$(cat cookies.txt)"
```

   Or paste it into **Settings -> Environment variables -> YT_COOKIES** in the
   portal. Restart the app afterwards.

Locally you can instead drop a `cookies.txt` next to `server.py` — it's
gitignored.

**Cookies expire**, usually within weeks. When downloads start failing again the
app will say so explicitly; re-export and update the setting.

## What it needs at runtime
- **ffmpeg** — required, not optional. It trims every clip, builds the audio
  preview, and merges video with its sound. It ships as the `imageio-ffmpeg`
  pip wheel, so there's nothing to install at boot. (It used to be apt-get
  installed in `startup.sh`; that was slow and a failure took the whole
  container down with it.)
- **yt-dlp** is unpinned in `requirements.txt` on purpose. YouTube changes
  things every few months and breaks older versions (a stale one returns
  `HTTP Error 403: Forbidden` on download while metadata still works), so each
  redeploy should pick up the newest release.

## Change the access code later
```bash
az webapp config appsettings set -n my-audio-downloader -g audio-dl-rg \
  --settings ACCESS_CODE="new-code"
```
