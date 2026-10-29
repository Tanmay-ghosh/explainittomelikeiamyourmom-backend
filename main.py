"""explainittomelikeiamyourmom.com backend API.

The Lovable frontend talks to this server. Flow:
  1. POST /explain       -> starts a job, returns job_id immediately
  2. GET  /jobs/{job_id} -> poll every 2-3 s; shows the stage, then the explanation
  3. POST /explain-again -> same material at another class level or language
                            (reuses the expert notes, so it's fast and cheap)
  4. GET  /stories       -> this week's ready-made stories
  5. GET  /stories/{id}  -> one story's explanation at a class level

Jobs and caches live in memory, so run exactly ONE server process.
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import anthropic
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import analytics
from modes import DEFAULT_LEVEL, LANGUAGES, LEVEL_BLURBS, LEVELS
from pipeline import (
    EXPERT_MODEL,
    WRITER_MODEL,
    ExplainError,
    normalize_explanation,
    not_suitable_explanation,
    prepare_article,
    run_expert,
    run_writer,
    text_from_image,
    text_from_pdf,
    text_from_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("explainittomelikeiamyourmom")


# ---------- settings (environment variables; see .env.example) ----------

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str) -> float | None:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return None


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
ALLOWED_ORIGIN_REGEX = os.environ.get(
    "ALLOWED_ORIGIN_REGEX",
    r"https://([a-z0-9-]+\.)*(lovable\.app|lovableproject\.com|lovable\.dev)|http://localhost(:\d+)?",
)
DAILY_LIMIT = _env_int("DAILY_EXPLANATION_LIMIT", 50)     # fresh (non-cached) explanations per UTC day
PER_IP_HOURLY_LIMIT = _env_int("PER_IP_HOURLY_LIMIT", 10)  # fresh explanations per visitor per hour
MAX_FILES = _env_int("MAX_FILES", 5)
MAX_FILE_MB = _env_int("MAX_FILE_MB", 10)
WORKERS = _env_int("WORKERS", 4)                           # explanations processed at the same time
INPUT_PRICE = _env_float("INPUT_PRICE_PER_MTOK")           # optional, US$ per million input tokens
OUTPUT_PRICE = _env_float("OUTPUT_PRICE_PER_MTOK")         # optional, US$ per million output tokens

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
JOB_TTL_SECONDS = 2 * 60 * 60

STAGE_MESSAGES = {
    "queued": "Getting ready…",
    "reading": "Reading what you shared…",
    "expert": "Our expert is thinking…",
    "writing": "Making it simple for you…",
    "done": "Done!",
    "error": "Something went wrong",
}


# ---------- small in-memory stores ----------

class LRU:
    """A dict that forgets its oldest entries beyond max_items."""

    def __init__(self, max_items: int):
        self.max_items = max_items
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)


articles = LRU(200)        # article_id -> {"text", "trimmed", "input_type"}
notes_cache = LRU(200)     # article_id -> (expert_notes, usage)
results_cache = LRU(600)   # (article_id, level, language) -> (explanation, usage)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="job")

_daily = {"day": None, "count": 0}
_ip_hits: dict[str, deque] = {}
limits_lock = threading.Lock()

_client = None


def get_client():
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise ExplainError("The server has no Claude API key configured yet.")
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=3)
    return _client


# ---------- helpers ----------

def article_id_for(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def check_limits(ip: str, distinct_id: str | None):
    """Reserve one fresh explanation, or raise 429 with a friendly message."""
    now = time.time()
    today = datetime.now(timezone.utc).date().isoformat()
    with limits_lock:
        if _daily["day"] != today:
            _daily["day"], _daily["count"] = today, 0
        if _daily["count"] >= DAILY_LIMIT:
            analytics.track(distinct_id, "Limit Reached", {"limit": "daily", "limit_value": DAILY_LIMIT})
            raise HTTPException(429, "We've explained all we can for today. Please come back tomorrow!")

        hits = _ip_hits.setdefault(ip, deque())
        while hits and now - hits[0] > 3600:
            hits.popleft()
        if len(hits) >= PER_IP_HOURLY_LIMIT:
            analytics.track(distinct_id, "Limit Reached", {"limit": "per_visitor_hourly", "limit_value": PER_IP_HOURLY_LIMIT})
            raise HTTPException(429, "You've explained a lot this hour. Take a chai break and try again soon!")

        hits.append(now)
        _daily["count"] += 1


def estimate_cost(*usages) -> float | None:
    """Rough US$ cost from token counts, if prices are configured.

    Cache writes are billed at 1.25x and cache reads at 0.1x the input price.
    """
    if INPUT_PRICE is None or OUTPUT_PRICE is None:
        return None
    total = 0.0
    for u in usages:
        if not u:
            continue
        total += u.get("input_tokens", 0) * INPUT_PRICE
        total += u.get("cache_write_tokens", 0) * INPUT_PRICE * 1.25
        total += u.get("cache_read_tokens", 0) * INPUT_PRICE * 0.1
        total += u.get("output_tokens", 0) * OUTPUT_PRICE
    return round(total / 1_000_000, 5)


def new_job(level: str, language: str, input_type: str, distinct_id: str | None) -> dict:
    job = {
        "job_id": uuid.uuid4().hex,
        "status": "queued",
        "error": None,
        "level": level,
        "language": language,
        "input_type": input_type,
        "article_id": None,
        "explanation": None,
        "meta": {},
        "usage": {},
        "distinct_id": distinct_id,
        "created_at": time.time(),
        "finished_at": None,
    }
    with jobs_lock:
        # forget old jobs so memory stays small
        cutoff = time.time() - JOB_TTL_SECONDS
        for old_id in [jid for jid, j in jobs.items() if j["created_at"] < cutoff]:
            del jobs[old_id]
        jobs[job["job_id"]] = job
    return job


def set_status(job: dict, status: str):
    job["status"] = status


def public_view(job: dict, include_notes: bool = False) -> dict:
    view = {
        "job_id": job["job_id"],
        "status": job["status"],
        "message": job["error"] if job["status"] == "error" else STAGE_MESSAGES[job["status"]],
        "error": job["error"],
        "article_id": job["article_id"],
        "level": job["level"],
        "language": job["language"],
        "explanation": job["explanation"],
        "meta": job["meta"],
        "usage": job["usage"],
    }
    if include_notes and job["article_id"]:
        cached = notes_cache.get(job["article_id"])
        view["expert_notes"] = cached[0] if cached else None
    return view


# ---------- the work, done on a background thread ----------

def explain(job: dict, article_id: str):
    """Run Layer 1 (if not cached) and Layer 2 (if not cached) for this job."""
    article = articles.get(article_id)
    if article is None:
        raise ExplainError("This article has expired from memory. Please submit it again.")
    started = time.time()
    job["article_id"] = article_id

    cached_notes = notes_cache.get(article_id)
    expert_cached = cached_notes is not None
    if not expert_cached:
        set_status(job, "expert")
        cached_notes = run_expert(get_client(), article["text"], article["input_type"])
        notes_cache.set(article_id, cached_notes)
    notes, expert_usage = cached_notes

    key = (article_id, job["level"], job["language"])
    cached_result = results_cache.get(key)
    writer_cached = cached_result is not None
    if not writer_cached:
        if notes.get("safety") == "not_suitable":
            # Never explain unsuitable material: skip the Writer entirely.
            cached_result = (not_suitable_explanation(), {"input_tokens": 0, "output_tokens": 0})
        else:
            set_status(job, "writing")
            cached_result = run_writer(get_client(), article["text"], notes, LEVELS[job["level"]], job["language"])
        results_cache.set(key, cached_result)
    result, writer_usage = cached_result

    read_usage = job["usage"].get("reading")
    fresh_expert = None if expert_cached else expert_usage
    fresh_writer = None if writer_cached else writer_usage
    job["usage"].update({
        "expert": {**expert_usage, "cached": expert_cached},
        "writer": {**writer_usage, "cached": writer_cached},
        "estimated_cost_usd": estimate_cost(read_usage, fresh_expert, fresh_writer),
        "models": {"expert": expert_usage.get("model", EXPERT_MODEL), "writer": writer_usage.get("model", WRITER_MODEL)},
    })
    job["meta"] = {
        "subject": notes.get("subject"),
        "topic": notes.get("topic"),
        "trimmed": article["trimmed"],
        "input_type": article["input_type"],
        "article_chars": len(article["text"]),
    }
    job["explanation"] = result
    set_status(job, "done")
    job["finished_at"] = time.time()

    analytics.track(job["distinct_id"], "Explanation Completed", {
        "level": job["level"],
        "language": job["language"],
        "input_type": article["input_type"],
        "subject": notes.get("subject"),
        "safety": notes.get("safety"),
        "article_chars": len(article["text"]),
        "trimmed": article["trimmed"],
        "expert_cached": expert_cached,
        "writer_cached": writer_cached,
        "expert_model": expert_usage.get("model"),
        "writer_model": writer_usage.get("model"),
        "is_rewrite": job["input_type"] == "rewrite",
        "seconds_total": round(job["finished_at"] - job["created_at"], 1),
        "seconds_ai": round(job["finished_at"] - started, 1),
        "expert_seconds": None if expert_cached else expert_usage.get("seconds"),
        "writer_seconds": None if writer_cached else writer_usage.get("seconds"),
        "input_tokens": sum(u.get("input_tokens", 0) for u in (read_usage, fresh_expert, fresh_writer) if u),
        "output_tokens": sum(u.get("output_tokens", 0) for u in (read_usage, fresh_expert, fresh_writer) if u),
        "cache_read_tokens": sum(u.get("cache_read_tokens", 0) for u in (fresh_expert, fresh_writer) if u),
        "estimated_cost_usd": job["usage"]["estimated_cost_usd"],
        "json_retries": sum(u.get("attempts", 1) - 1 for u in (fresh_expert, fresh_writer) if u),
    })


def process_new_article(job: dict, text: str, url: str, files: list[tuple[str, bytes]]):
    try:
        set_status(job, "reading")
        if files:
            parts, reading = [], {"input_tokens": 0, "output_tokens": 0, "seconds": 0.0, "images": 0}
            for name, data in files:
                if name.lower().endswith(IMAGE_EXTENSIONS):
                    part, usage = text_from_image(get_client(), data)
                    for k in ("input_tokens", "output_tokens", "seconds"):
                        reading[k] += usage[k]
                    reading["images"] += 1
                else:
                    part = text_from_pdf(data)
                parts.append(part)
            raw = "\n\n".join(parts)
            if reading["images"]:
                job["usage"]["reading"] = reading
        elif url:
            raw = text_from_url(url)
        else:
            raw = text

        article_text, trimmed = prepare_article(raw)
        article_id = article_id_for(article_text)
        if articles.get(article_id) is None:
            articles.set(article_id, {"text": article_text, "trimmed": trimmed, "input_type": job["input_type"]})
        explain(job, article_id)
    except ExplainError as err:
        fail(job, str(err))
    except Exception:
        log.exception("Job %s crashed", job["job_id"])
        fail(job, "Something went wrong on our side. Please try again.")


def process_rewrite(job: dict, article_id: str):
    try:
        explain(job, article_id)
    except ExplainError as err:
        fail(job, str(err))
    except Exception:
        log.exception("Job %s crashed", job["job_id"])
        fail(job, "Something went wrong on our side. Please try again.")


def fail(job: dict, message: str):
    failed_stage = job["status"]
    job["error"] = message
    set_status(job, "error")
    job["finished_at"] = time.time()
    analytics.track(job["distinct_id"], "Explanation Failed", {
        "stage": failed_stage,
        "input_type": job["input_type"],
        "level": job["level"],
        "language": job["language"],
        "error_message": message,
    })


# ---------- the API ----------

app = FastAPI(title="explainittomelikeiamyourmom.com API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=ALLOWED_ORIGIN_REGEX or None,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _openapi_with_file_picker():
    """Describe /explain's `files` field as plain file uploads in the docs.

    The endpoint also tolerates empty strings (see start_explanation), but the
    docs should still show a file picker, not a text box.
    """
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    body = schema.get("components", {}).get("schemas", {}).get("Body_start_explanation_explain_post")
    if body and "files" in body.get("properties", {}):
        body["properties"]["files"]["items"] = {"type": "string", "format": "binary"}
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_file_picker


def validate_level_language(level: str, language: str):
    if level not in LEVELS:
        raise HTTPException(400, f"Unknown class level. Choose one of: {', '.join(LEVELS)}")
    if language not in LANGUAGES:
        raise HTTPException(400, f"Unknown language. Choose one of: {', '.join(LANGUAGES)}")


@app.get("/")
def root():
    return {"name": "explainittomelikeiamyourmom.com API", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health():
    """Also used by the frontend to wake the server up early."""
    return {
        "status": "ok",
        "claude_key_configured": bool(ANTHROPIC_API_KEY),
        "analytics_enabled": analytics.ENABLED,
    }


@app.get("/config")
def config():
    """Everything the frontend needs to draw its class and language pickers."""
    return {
        "levels": [{"id": lv, "blurb": LEVEL_BLURBS[lv]} for lv in LEVELS],
        "default_level": DEFAULT_LEVEL,
        "languages": LANGUAGES,
        "default_language": "English",
        "upload": {
            "accepted": [".pdf", *IMAGE_EXTENSIONS],
            "max_files": MAX_FILES,
            "max_file_mb": MAX_FILE_MB,
        },
    }


@app.post("/explain", status_code=202)
async def start_explanation(
    request: Request,
    text: str = Form(""),
    url: str = Form(""),
    level: str = Form(DEFAULT_LEVEL),
    language: str = Form("English"),
    # Accept str too: some clients (including the /docs page) send an empty
    # string when no file is chosen. Those are ignored below.
    files: list[UploadFile | str] = File(default=[]),
    x_distinct_id: str | None = Header(default=None),
):
    """Start explaining study material or an article. Send ONE of: files, url, or text."""
    validate_level_language(level, language)
    text, url = text.strip(), url.strip()
    files = [f for f in files if not isinstance(f, str) and f.filename]

    if files:
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"Upload at most {MAX_FILES} files at once.")
        payload = []
        for f in files:
            if not f.filename.lower().endswith((".pdf", *IMAGE_EXTENSIONS)):
                raise HTTPException(400, f"{f.filename}: only PDF, JPG and PNG files are supported.")
            data = await f.read()
            if len(data) > MAX_FILE_MB * 1024 * 1024:
                raise HTTPException(400, f"{f.filename} is larger than {MAX_FILE_MB} MB.")
            payload.append((f.filename, data))
        has_image = any(name.lower().endswith(IMAGE_EXTENSIONS) for name, _ in payload)
        input_type = "image" if has_image else "pdf"
    elif url:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "That doesn't look like a web link. It should start with https://")
        payload, input_type = [], "link"
    elif text:
        payload, input_type = [], "text"
    else:
        raise HTTPException(400, "Paste some text, upload a file, or add a link first.")

    # Same pasted article already explained at this class level and language: answer instantly, free.
    if input_type == "text":
        try:
            prepared, _ = prepare_article(text)
        except ExplainError as err:
            raise HTTPException(400, str(err))
        aid = article_id_for(prepared)
        if results_cache.get((aid, level, language)) and articles.get(aid):
            job = new_job(level, language, input_type, x_distinct_id)
            process_rewrite(job, aid)
            return public_view(job)

    check_limits(client_ip(request), x_distinct_id)
    job = new_job(level, language, input_type, x_distinct_id)
    executor.submit(process_new_article, job, text, url, payload)
    return public_view(job)


class ExplainAgainRequest(BaseModel):
    article_id: str
    level: str
    language: str


@app.post("/explain-again", status_code=202)
def explain_again(
    body: ExplainAgainRequest,
    request: Request,
    x_distinct_id: str | None = Header(default=None),
):
    """Same material, different class level or language. Reuses the expert's notes."""
    validate_level_language(body.level, body.language)
    if articles.get(body.article_id) is None:
        raise HTTPException(404, "This took too long and expired. Please share it again.")

    job = new_job(body.level, body.language, "rewrite", x_distinct_id)
    if results_cache.get((body.article_id, body.level, body.language)):
        process_rewrite(job, body.article_id)  # already made: instant and free
        return public_view(job)

    check_limits(client_ip(request), x_distinct_id)
    executor.submit(process_rewrite, job, body.article_id)
    return public_view(job)


@app.get("/jobs/{job_id}")
def get_job(job_id: str, include_notes: bool = False):
    """Poll this every 2-3 seconds until status is 'done' or 'error'."""
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found. It may have expired; please submit the article again.")
    return public_view(job, include_notes=include_notes)


# ---------- this week's stories (ready-made, served from a file) ----------

STORIES_FILE = Path(__file__).parent / "stories" / "this_week.json"
STORY_FIELDS = ("id", "title", "teaser", "bucket", "topic", "safety", "source")


def _load_stories() -> dict:
    """Load and normalise this week's stories once, at startup."""
    try:
        raw = json.loads(STORIES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("No stories file at %s", STORIES_FILE)
        return {"week_label": "", "stories": []}
    stories = []
    for item in raw.get("stories", []):
        safety = item.get("safety") if item.get("safety") in ("fine", "heavy") else "fine"
        story = {f: item.get(f) for f in STORY_FIELDS}
        story["safety"] = safety
        explanations = {
            level: normalize_explanation(exp, safety)
            for level, exp in (item.get("explanations") or {}).items()
            if level in LEVELS and isinstance(exp, dict)
        }
        stories.append({"story": story, "explanations": explanations})
    log.info("Loaded %d weekly stories", len(stories))
    return {"week_label": raw.get("week_label", ""), "stories": stories}


WEEKLY = _load_stories()


@app.get("/stories")
def list_stories():
    """This week's stories, in ranked order (English for now)."""
    return {"week_label": WEEKLY["week_label"], "stories": [s["story"] for s in WEEKLY["stories"]]}


@app.get("/stories/{story_id}")
def get_story(story_id: str, level: str = DEFAULT_LEVEL):
    """One story at a class level. explanation is null while a story is still being prepared."""
    if level not in LEVELS:
        raise HTTPException(400, f"Unknown class level. Choose one of: {', '.join(LEVELS)}")
    entry = next((s for s in WEEKLY["stories"] if s["story"]["id"] == story_id), None)
    if entry is None:
        raise HTTPException(404, "We couldn't find that story.")
    explanations = entry["explanations"]
    # Prefer the requested level; otherwise fall back to the closest one that exists.
    order = list(LEVELS)
    by_distance = sorted(order, key=lambda lv: abs(order.index(lv) - order.index(level)))
    chosen = next((lv for lv in by_distance if lv in explanations), None)
    return {
        "story": entry["story"],
        "level": chosen or level,
        "explanation": explanations.get(chosen) if chosen else None,
    }
