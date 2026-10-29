# explainittomelikeiamyourmom.com backend API

The Python server behind explainittomelikeiamyourmom.com. The Lovable frontend sends it an article; it runs the two Claude layers (Expert, then Writer) and returns the plain-language explanation.

```
Lovable frontend ──POST /explain──▶ this API ──▶ Claude (read image → Expert → Writer)
        ▲                               │
        └────GET /jobs/{id} (poll)──────┘          Both sides send events to Mixpanel
```

## Files

| File | What it does |
|---|---|
| `main.py` | The API: endpoints, jobs, caching, limits, CORS |
| `pipeline.py` | Text extraction (PDF, image, link) and the two Claude calls, with JSON checks, retries and prompt caching |
| `prompts.py` | The Expert and Writer prompts |
| `modes.py` | Mom mode, Teen mode, Quick brief, and the language list |
| `analytics.py` | Server-side Mixpanel events (off until `MIXPANEL_TOKEN` is set) |
| `Dockerfile`, `.dockerignore` | How Cloud Run builds and starts the server |
| `.env.example` | Every setting, with explanations |

---

## Deploy on Google Cloud Run

### Why two special settings matter
Cloud Run normally gives a server CPU **only while it is answering a request**. The API answers `POST /explain` immediately and keeps working in the background, so with default settings that background work would crawl and jobs would hang at "The expert is studying…". It also starts extra copies of the server under load, but our jobs live in one server's memory. So we deploy with:
- `--no-cpu-throttling`: CPU stays on while the server is running, so background jobs finish.
- `--max-instances 1`: exactly one copy, so polling always finds the job.
- `--min-instances 0`: the server shuts down when idle, so you don't pay for idle time. The first visit after a quiet spell waits a few seconds for it to start.

Billing note: with CPU always on, Cloud Run bills for the whole time the server is up, including a short idle period before it shuts down, rather than per request. The free monthly allowance should cover testing, but set a budget alert (step 6).

### 1. One-time setup
1. Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install
2. In a terminal:
   ```
   gcloud auth login
   gcloud config set project YOUR-PROJECT-ID
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
   ```
   Your project ID is shown on the Google Cloud console home page.

### 2. Store your Claude API key as a secret
Keep the key out of code and out of plain settings:
1. In the Google Cloud console, open **Security → Secret Manager → Create secret**.
2. Name it `anthropic-api-key`, paste your key as the secret value, and click **Create**.
3. Let Cloud Run read it (run in your terminal):
   ```
   PROJECT_NUMBER=$(gcloud projects describe $(gcloud config get-value project) --format='value(projectNumber)')
   gcloud secrets add-iam-policy-binding anthropic-api-key \
     --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
     --role="roles/secretmanager.secretAccessor"
   ```

### 3. Deploy
From a terminal inside this folder:
```
gcloud run deploy explainittomelikeiamyourmom-backend \
  --source . \
  --region asia-south1 \
  --allow-unauthenticated \
  --max-instances 1 \
  --min-instances 0 \
  --no-cpu-throttling \
  --cpu 1 \
  --memory 512Mi \
  --timeout 300 \
  --set-secrets ANTHROPIC_API_KEY=anthropic-api-key:latest \
  --set-env-vars DAILY_EXPLANATION_LIMIT=50,PER_IP_HOURLY_LIMIT=10
```
- `asia-south1` is Mumbai, closest to your users. If it asks to create an Artifact Registry repository, answer **Y**.
- `--allow-unauthenticated` lets your Lovable site call it. Abuse is limited by the daily and per-visitor caps plus CORS.
- The build uses the `Dockerfile` and takes a few minutes the first time. Cloud Build and image storage have free allowances, so expect at most a few cents.

When it finishes, it prints a **Service URL** like `https://explainittomelikeiamyourmom-backend-xxxxx.asia-south1.run.app`. That's what Lovable calls.

### 4. Check it works
- Open `https://YOUR-URL/health`. You should see `"status": "ok"` and `"claude_key_configured": true`.
- Open `https://YOUR-URL/docs` to try every endpoint in the browser, including pasting a real article into `/explain`.
- Logs: Google Cloud console → **Cloud Run → explainittomelikeiamyourmom-backend → Logs**.

### 5. Change settings later (no rebuild needed)
```
gcloud run services update explainittomelikeiamyourmom-backend --region asia-south1 \
  --update-env-vars MIXPANEL_TOKEN=your-token,MIXPANEL_API_HOST=api-in.mixpanel.com
```
Use the same command for any variable in `.env.example`, such as `DAILY_EXPLANATION_LIMIT`, `INPUT_PRICE_PER_MTOK` and `OUTPUT_PRICE_PER_MTOK`, or `ALLOWED_ORIGINS` once you connect your own domain. To ship code changes, run the step 3 deploy command again.

### 6. Protect your wallet
- Google Cloud console → **Billing → Budgets & alerts → Create budget**, for example ₹500 a month with email alerts. Budgets alert you; they don't stop spending.
- Set a spend limit in the Claude Console too.

### Keep in mind
- **Every deploy and every idle shutdown clears memory**, including jobs in progress and cached explanations. That's fine at this stage. A database for saved results is a later upgrade.
- **Don't raise `--max-instances`** until jobs are moved out of memory.

---

## Run it on your computer (optional)

```
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then put your real key in .env
export $(grep -v '^#' .env | xargs)
uvicorn main:app --reload --port 8000
```
Then open http://localhost:8000/docs.

---

## API guide (for building the Lovable frontend)

Base URL: your Cloud Run service URL. All responses are JSON.

### `GET /health`
Call this as soon as the page loads, so the server is awake by the time the user submits.

### `GET /config`
Returns the modes (with short blurbs), languages, and upload limits, so the frontend can draw its pickers without hard-coding them.

### `POST /explain` — start an explanation
Send as **multipart form data**, with exactly ONE input:

| Field | Type | Notes |
|---|---|---|
| `text` | string | Pasted article text |
| `url` | string | Article link (some sites block this; suggest pasting if it fails) |
| `files` | file(s) | PDF, JPG or PNG. Repeat the field for several photos, in reading order |
| `mode` | string | `Mom mode`, `Teen mode` or `Quick brief` (default `Mom mode`) |
| `language` | string | `English`, `Hindi` or `Bengali` (default `English`) |

Optional header: `X-Distinct-Id` set to the Mixpanel browser distinct ID, so backend events join the same user in Mixpanel.

Returns `202` with a job (see below). If the same pasted article was already explained in that mode and language, the job comes back already `done`.

### `GET /jobs/{job_id}` — check progress
Poll every 2–3 seconds until `status` is `done` or `error`.

```json
{
  "job_id": "…",
  "status": "queued | reading | expert | writing | done | error",
  "message": "The expert is studying the article…",
  "error": null,
  "article_id": "a1b2c3…",
  "mode": "Mom mode",
  "language": "English",
  "explanation": {
    "headline": "…",
    "simple_version": "…",
    "why_explainers": [{"question": "…", "answer": "…", "source": "article|background"}],
    "backstory": [{"point": "…", "source": "article|background"}],
    "side_stories": [{"point": "…", "source": "article|background"}],
    "fun_facts": [{"point": "…", "source": "article|background"}],
    "key_people": [{"name": "…", "role": "…", "country": "…", "why_they_matter": "…", "source": "article|background"}]
  },
  "meta": {"genre": "…", "topic": "…", "expert_role": "…", "trimmed": false, "input_type": "text"},
  "usage": {"expert": {…}, "writer": {…}, "estimated_cost_usd": 0.02}
}
```
Show `message` to the user while waiting. `explanation` is filled in once `status` is `done`. On `error`, show `error`; it's always written in plain language. Add `?include_notes=true` to also get the Expert's raw notes (useful for a builder or debug view).

### `POST /explain-again` — switch mode or language
JSON body: `{"article_id": "…", "mode": "Teen mode", "language": "Hindi"}`. Returns a new job to poll. It reuses the Expert's notes, so only the Writer runs: faster and cheaper. If that combination was made before, the job comes back already `done`.

### Errors
- `400`: bad input (wrong file type, too short, unknown mode). `detail` explains it in plain language.
- `404`: job or article expired (kept for about 2 hours). Ask the user to submit again.
- `429`: the daily limit or the per-visitor hourly limit was reached. Show `detail` as is.

### Source badges
Each point carries `source`. Show `article` items as "From the article" and `background` items as "Background, worth checking".

---

## Mixpanel events sent by the backend

| Event | When | Key properties |
|---|---|---|
| `Explanation Completed` | An explanation is ready | mode, language, input_type, genre, seconds_total, expert_cached, writer_cached, is_rewrite, input/output/cache tokens, estimated_cost_usd, json_retries |
| `Explanation Failed` | Any job fails | stage, input_type, mode, language, error_message |
| `Limit Reached` | Daily or per-visitor limit hit | limit, limit_value |

Visits, page views, button clicks and feedback are tracked in the frontend with Mixpanel's browser library. Article text is never sent to Mixpanel.

## Settings you can change
Set with `gcloud run services update` (see step 5 and `.env.example`): `DAILY_EXPLANATION_LIMIT`, `PER_IP_HOURLY_LIMIT`, `MAX_FILES`, `MAX_FILE_MB`, `WORKERS`, `ALLOWED_ORIGINS` (add your own domain once you connect one), `ALLOWED_ORIGIN_REGEX`. Models are set at the top of `pipeline.py` (`EXPERT_MODEL`, `WRITER_MODEL`, `TRANSCRIBE_MODEL`).
