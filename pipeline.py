"""Backend: text extraction, the two AI layers, and output validation."""

import io
import json
import re
import time

import anthropic
from pypdf import PdfReader

from prompts import EXPERT_SYSTEM, EXPERT_USER, WRITER_SYSTEM, WRITER_USER

EXPERT_MODEL = "claude-sonnet-5-5"
WRITER_MODEL = "claude-sonnet-5-5"
MAX_ARTICLE_CHARS = 60_000  # keeps very long PDFs within a sensible budget
MIN_ARTICLE_CHARS = 200


class ExplainError(Exception):
    """An error with a message that is safe to show the user."""


# ---------- Step 1: get clean text ----------

def text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ExplainError("Couldn't read that PDF. Is it password-protected or damaged?") from exc
    text = "\n".join(pages).strip()
    if len(text) < MIN_ARTICLE_CHARS:
        raise ExplainError(
            "This PDF has almost no readable text, so it's probably scanned. "
            "Take screenshots of the pages and upload them as JPG or PNG instead."
        )
    return text


TRANSCRIBE_MODEL = "claude-sonnet-5-5"
MAX_IMAGE_SIDE = 2000          # pixels; larger photos are scaled down first
MAX_IMAGE_BYTES = 4_500_000    # stay under the API's per-image size limit

TRANSCRIBE_PROMPT = """This image is a photo or screenshot of a news article or document.
Transcribe the article text exactly as written, in reading order, including the
headline and any subheadings. Join words split across lines or columns.
Leave out adverts, menus, captions of unrelated photos, and page furniture
such as page numbers. Do not summarise, translate or add anything.
If the image contains no readable article text, reply with exactly: NO_TEXT"""


def _prepare_image(file_bytes: bytes) -> tuple[str, str]:
    """Shrink and re-encode an image if needed. Returns (base64_data, media_type)."""
    import base64

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(file_bytes)))
    except UnidentifiedImageError as exc:
        raise ExplainError("That file isn't a readable JPG or PNG image.") from exc

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    if max(image.size) > MAX_IMAGE_SIDE:
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))

    for quality in (90, 80, 70, 60):
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        if buffer.tell() <= MAX_IMAGE_BYTES:
            return base64.b64encode(buffer.getvalue()).decode(), "image/jpeg"
    raise ExplainError("That image is too large to read. Try a smaller photo or a screenshot.")


def text_from_image(client, file_bytes: bytes) -> tuple[str, dict]:
    """Read article text from a JPG/PNG using Claude's vision. Returns (text, usage)."""
    data, media_type = _prepare_image(file_bytes)
    started = time.time()
    try:
        response = client.messages.create(
            model=TRANSCRIBE_MODEL,
            max_tokens=8000,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                ],
            }],
        )
    except anthropic.AuthenticationError as exc:
        raise ExplainError("The Claude API key is missing or wrong. Check your secrets file.") from exc
    except anthropic.APIError as exc:
        raise ExplainError(f"Claude API error while reading the image: {exc}") from exc

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text or text == "NO_TEXT":
        raise ExplainError("Couldn't find any article text in that image. Try a clearer, closer photo.")
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "seconds": round(time.time() - started, 1),
    }
    return text, usage


def text_from_url(url: str) -> str:
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        raise ExplainError("Couldn't open that link. The site may block automated reading; try pasting the text.")
    text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) or ""
    if len(text) < MIN_ARTICLE_CHARS:
        raise ExplainError(
            "Couldn't find the article text on that page (it may be behind a paywall). Try pasting the text."
        )
    return text


def prepare_article(text: str) -> tuple[str, bool]:
    """Clean whitespace and trim very long documents. Returns (text, was_trimmed)."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_ARTICLE_CHARS:
        raise ExplainError("That's too short to explain. Paste the full article.")
    if len(text) > MAX_ARTICLE_CHARS:
        return text[:MAX_ARTICLE_CHARS], True
    return text, False


# ---------- Step 2: call Claude and get JSON back ----------

def _parse_json(raw: str) -> dict:
    """Pull a JSON object out of the model's reply, tolerating ``` fences."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(cleaned[start : end + 1])


def _call_json(client, model, system, user, max_tokens, required_keys):
    """Call Claude, parse JSON, check required keys. Retries once on bad output."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    messages = [{"role": "user", "content": user}]
    # The system prompts are long and identical across calls, so mark them for
    # prompt caching: repeat calls read them from cache at a much lower price.
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    started = time.time()
    last_error = None

    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model, max_tokens=max_tokens, system=system_blocks, messages=messages
            )
        except anthropic.AuthenticationError as exc:
            raise ExplainError("The Claude API key is missing or wrong. Check your secrets file.") from exc
        except anthropic.RateLimitError as exc:
            raise ExplainError("Too many requests right now. Wait a minute and try again.") from exc
        except anthropic.APIError as exc:
            raise ExplainError(f"Claude API error: {exc}") from exc

        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["cache_read_tokens"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
        usage["cache_write_tokens"] += getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        raw = "".join(block.text for block in response.content if block.type == "text")

        try:
            data = _parse_json(raw)
            missing = [key for key in required_keys if key not in data]
            if missing:
                raise ValueError(f"missing keys: {', '.join(missing)}")
            usage["seconds"] = round(time.time() - started, 1)
            usage["attempts"] = attempt + 1
            return data, usage
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            # Ask the model to fix its own output once.
            messages = messages + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": f"Your reply was not valid JSON in the required shape ({exc}). "
                    "Reply again with ONLY the corrected JSON object, nothing else.",
                },
            ]

    raise ExplainError(f"The AI returned a reply in the wrong format twice ({last_error}). Please try again.")


EXPERT_KEYS = [
    "genre", "topic", "expert_role", "core_facts", "why_it_happened", "mechanisms",
    "previous_stories", "key_people", "key_dates", "reader_impact",
    "explanation_plan", "tricky_terms", "fun_fact_candidates",
]
WRITER_KEYS = [
    "headline", "simple_version", "why_explainers", "backstory",
    "side_stories", "fun_facts", "key_people",
]


def run_expert(client, article_text: str):
    """Layer 1: analyse the article once. Mode- and language-independent."""
    return _call_json(
        client,
        EXPERT_MODEL,
        EXPERT_SYSTEM,
        EXPERT_USER.format(article_text=article_text),
        max_tokens=6000,
        required_keys=EXPERT_KEYS,
    )


def run_writer(client, article_text: str, expert_notes: dict, mode_instructions: str, language: str):
    """Layer 2: turn expert notes into the reader-facing explanation."""
    return _call_json(
        client,
        WRITER_MODEL,
        WRITER_SYSTEM.format(language=language, mode_instructions=mode_instructions),
        WRITER_USER.format(
            article_text=article_text,
            expert_notes=json.dumps(expert_notes, ensure_ascii=False, indent=2),
        ),
        max_tokens=5000,
        required_keys=WRITER_KEYS,
    )
