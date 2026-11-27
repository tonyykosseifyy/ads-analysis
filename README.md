# Ad Creative Analysis

Watches every ad that ran in the WavyTalk Lebanon Meta ad account (`act_1136851577945373`) and writes down,
for each video or image, exactly what is said and shown. The results sit next to each ad's performance
numbers so the team can see what separates winners from losers.

For each asset Gemini records:
- **Transcript.** Word for word, timestamped and split by speaker. Lebanese Arabic is written in Arabic
  script, English and French in Latin letters, keeping the switches mid-sentence. An English translation
  comes with it.
- **Language mix.** Rough shares of Arabic, English and French, the Arabic dialect, and the language of
  the on-screen text.
- **Hook.** The first frame, plus what is seen, said and written in the first 3 seconds, and the hook type.
- **Scene by scene.** Shot type, what is in frame, and what happens in each shot.
- **On-screen text.** Every overlay, caption and end card, word for word, with timestamps.
- **Format and story.** Format, angle, claims, pain points, objections handled and social proof.
- **Product timing.** When the product first appears, when it is first used on hair, and when the result
  is shown.
- **Offer and call to action.** Offer, price, code, urgency and the CTA.
- **Talent and setting.** Who is on screen, the setting, and the hair before and after.
- **Audio and edit.** Audio type, music, captions, production style, number of cuts and pacing.

Gemini never sees the performance numbers. It only describes the ad, so its description isn't biased by
knowing how the ad did. The comparison against results happens afterwards.

## How it works

```
Meta API ── lifetime metrics for every ad that ran (incl. paused/archived/deleted)
   │        └─ each ad's creative → which videos/images it shows
   ▼
download video/image ──► Gemini (watches video + audio) ──► data/analyses/<asset>.json
   │                                                         │
   └─ same file used by several ads? watched once ───────────┘
                                                             ▼
                                   Google Sheet (Ads, Creatives, AssetBreakdown, Pilot, Runs, Failures)
                                   + data/*.csv in this repo
```

- **Runs daily on GitHub Actions.** Each run refreshes metrics and only watches new assets.
- **Every asset is watched once.** Copies of the same video are detected even when they were uploaded
  under different IDs.
- **Placement versions count once.** Ads with a Feed version and a Reels/Stories version of the same
  video only have it watched once. Ads that rotate several *different* videos have each one watched,
  and the `AssetBreakdown` tab splits their results per video.
- **Each ad is labelled by market** from its landing page (wavytalk-lb.com = LB, wavytalk-jo.com = JO),
  because this account also runs some Jordan ads. Filter on `market = LB` for the Lebanon-only study.
- **Failed assets are retried.** An asset that can't be downloaded or analysed is retried on the next
  two runs, then listed in the `Failures` tab with the reason.

## Sheet tabs

| Tab | One row per | Use it for |
|---|---|---|
| **Ads** | ad | Ads Manager metrics (spend, CTR, hook rate, hold rate, ATC, IC, purchases, cost per IC, CPA, ROAS) plus the analysis of the ad's video |
| **Creatives** | unique video/image | The same analysis with results added up across every ad that used the asset. This is the tab to compare creatives on |
| **AssetBreakdown** | video inside a rotating ad | Meta's own per-video split for ads that rotate several videos |
| **Pilot** | asset | Model comparison, with each model's transcript side by side |
| **Runs / Failures** | run / asset | Run history, and what couldn't be watched and why |

Full detail (every transcript segment and scene) is in `data/analyses/*.json`.

## One-time setup

### 1. Meta access token (system user)
1. Go to Meta Business Settings for **Wavytalk BM**, then **Users → System users → Add**. Name it
   `ad-analysis` and give it the *Employee* role.
2. Click **Assign assets** and add:
   - the **Wavy talk ad account**, with *View performance*
   - both Facebook Pages the ads run from, the WavyTalk Lebanon page and the Jordan page, with
     *Content* access. This lets the token download the videos.
   - the linked **Instagram accounts**. These are the fallback for ads made from Instagram posts.
3. Click **Generate new token**. Choose your app (if you have none, create a *Business* app at
   developers.facebook.com and connect it to Wavytalk BM), set the expiry to **Never**, and tick
   `ads_read`, `business_management`, `pages_show_list`, `pages_read_engagement` and `instagram_basic`.

### 2. Gemini API key
Create a key at aistudio.google.com and **turn on billing** for its project. The paid tier keeps your
ads out of Google's training data and has limits high enough for hundreds of videos.

### 3. Google Sheet (optional, but recommended)
1. In console.cloud.google.com, create a project, enable the **Google Sheets API**, then go to
   **IAM → Service accounts → Create**. Open the new account, choose **Keys → Add key → JSON**, and
   download the file.
2. Create an empty Google Sheet and **share it with the service account's email** as *Editor*.
3. The Sheet ID is the long part of its URL, between `/d/` and `/edit`.

### 4. GitHub
In this repo, go to **Settings → Secrets and variables → Actions**.
- **Secrets:** `META_ACCESS_TOKEN`, `GEMINI_API_KEY`, and `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the
  whole JSON file).
- **Variables:** `SHEET_ID`. Optionally also `GEMINI_MODEL` (default `gemini-3.8-flash`) and
  `CONCURRENCY` (default 6).

## Running it

Go to **Actions → Analyze ads → Run workflow**.

1. **`dry-run`**: checks the Meta token and lists everything it would watch. Costs nothing.
2. **`pilot`**: watches the 10 pre-filled ads with both models and fills the **Pilot** tab. Check the
   Arabic transcripts, then set `GEMINI_MODEL` to the model that got them right.
3. **`run`**: works through the backlog, biggest spend first. After that it runs every day by itself.
   One run stops starting new videos after 5 hours and saves what it has, so if the backlog is bigger
   than that, run it again or let tomorrow's run continue.

Local run: `pip install -r requirements.txt`, export the same variables, then `python -m aca.main --help`.

## Cost

Gemini bills about 280 tokens per second of video. On top of that, each asset gets a few thousand
tokens of written output. A backlog of around 400 assets costs roughly $10 on a Flash model, or $30–60
on the flagship. After that, a new ad costs a few cents.
