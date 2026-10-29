"""Backend: text extraction, the two AI layers, output validation and normalisation."""

import io
import json
import logging
import os
import re
import time

import anthropic
from pypdf import PdfReader

from prompts import EXPERT_SYSTEM, EXPERT_USER, WRITER_SYSTEM, WRITER_USER

# Models are settings, so they can be changed on Cloud Run without a code change.
# The Expert does the hard thinking (facts, causes, safety), so it gets the
# bigger model. The Writer only rephrases the Expert's notes, so a smaller,
# cheaper model does that job well.
EXPERT_MODEL = os.environ.get("EXPERT_MODEL", "claude-sonnet-5-5")
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-haiku-5-5")
# Speed: short, simple text (a paragraph or two of notes) goes to the faster,
# cheaper model for the Expert step too. Set SHORT_INPUT_CHARS=0 to turn off.
EXPERT_MODEL_FAST = os.environ.get("EXPERT_MODEL_FAST", "claude-haiku-5-5")
SHORT_INPUT_CHARS = int(os.environ.get("SHORT_INPUT_CHARS", "2500") or 0)
# The Writer re-reads the original text only when it is short; for long
# documents it works from the Expert's notes alone (faster, cheaper).
WRITER_ARTICLE_CHARS = int(os.environ.get("WRITER_ARTICLE_CHARS", "6000") or 0)
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


TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "claude-sonnet-5-5")  # reading photos needs good vision
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
        raise ExplainError("The server's AI key is missing or wrong. Please tell the site owner.") from exc
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
        raise ExplainError("That's too short to explain. Paste a bit more: at least a few sentences.")
    if len(text) > MAX_ARTICLE_CHARS:
        return text[:MAX_ARTICLE_CHARS], True
    return text, False


# ---------- Step 2: call Claude and get JSON back ----------

log = logging.getLogger("explainittomelikeiamyourmom.pipeline")

# Claude returns its answer through a forced "tool call". The API hands the
# arguments back as an already-parsed object, so stray quote marks or other
# JSON typing slips in long text can't break parsing.
SUBMIT_TOOL = {
    "name": "submit_result",
    "description": "Submit your complete answer as a single JSON object in exactly the shape described in the instructions.",
    "input_schema": {"type": "object", "additionalProperties": True},
}


TOOL_INSTRUCTION = """

HOW TO SUBMIT: When tools are available, give your answer by calling the
submit_result tool, passing the complete JSON object as its input. Do not
write the answer as plain text. If no tool is available, reply with ONLY the
JSON object, with no other text."""

# Models that rejected tool use; we stop offering them tools for this process.
_NO_TOOL_MODELS: set[str] = set()


def _parse_json(raw: str) -> dict:
    """Fallback for plain-text replies: pull out a JSON object, repairing small slips."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        import json_repair

        repaired = json_repair.loads(candidate)
        if not isinstance(repaired, dict):
            raise ValueError("could not repair the JSON reply")
        return repaired


def _extract(response) -> dict:
    """Get the structured answer out of a Claude response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == SUBMIT_TOOL["name"]:
            data = block.input
            # Some replies wrap the object in a single key; unwrap it.
            if isinstance(data, dict) and len(data) == 1:
                only = next(iter(data.values()))
                if isinstance(only, dict):
                    data = only
                elif isinstance(only, str) and only.strip().startswith("{"):
                    data = _parse_json(only)
            if isinstance(data, dict):
                return data
    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_json(text)


def _call_json(client, model, system, user, max_tokens, required_keys):
    """Call Claude, get a structured answer, check required keys. Tries twice."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    # The system prompts are long and identical across calls, so mark them for
    # prompt caching: repeat calls read them from cache at a much lower price.
    system_blocks = [{"type": "text", "text": system + TOOL_INSTRUCTION, "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": user}]
    started = time.time()
    last_error = None

    for attempt in range(2):
        use_tool = model not in _NO_TOOL_MODELS
        request = dict(model=model, max_tokens=max_tokens, system=system_blocks, messages=messages)
        if use_tool:
            # "auto" lets the model decide to call the tool. Some models reject a
            # forced tool_choice ("tool" / "any"), so we never force it.
            request.update(tools=[SUBMIT_TOOL], tool_choice={"type": "auto"})
        try:
            response = client.messages.create(**request)
        except anthropic.BadRequestError as exc:
            if use_tool and "tool" in str(exc).lower():
                # This model won't take tools at all: remember that and fall back
                # to a plain JSON reply, which _parse_json can repair if needed.
                log.warning("Model %s rejected tools (%s); falling back to plain JSON", model, exc)
                _NO_TOOL_MODELS.add(model)
                request.pop("tools", None)
                request.pop("tool_choice", None)
                try:
                    response = client.messages.create(**request)
                except anthropic.APIError as exc2:
                    log.warning("Claude API error: %s", exc2)
                    raise ExplainError("Our AI helper is having trouble right now. Please try again in a minute.") from exc2
            else:
                log.warning("Claude API error: %s", exc)
                raise ExplainError("Our AI helper is having trouble right now. Please try again in a minute.") from exc
        except anthropic.AuthenticationError as exc:
            raise ExplainError("The server's AI key is missing or wrong. Please tell the site owner.") from exc
        except anthropic.RateLimitError as exc:
            raise ExplainError("Lots of students are using this right now. Please wait a minute and try again.") from exc
        except anthropic.APIError as exc:
            log.warning("Claude API error: %s", exc)
            raise ExplainError("Our AI helper is having trouble right now. Please try again in a minute.") from exc

        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["cache_read_tokens"] += getattr(response.usage, "cache_read_input_tokens", 0) or 0
        usage["cache_write_tokens"] += getattr(response.usage, "cache_creation_input_tokens", 0) or 0

        try:
            if getattr(response, "stop_reason", None) == "max_tokens":
                raise ValueError("reply was cut off before it finished")
            data = _extract(response)
            missing = [key for key in required_keys if key not in data]
            if missing:
                raise ValueError(f"missing keys: {', '.join(missing)}")
            usage["seconds"] = round(time.time() - started, 1)
            usage["attempts"] = attempt + 1
            return data, usage
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            log.warning("Unusable reply from %s (attempt %d): %s", model, attempt + 1, exc)
            # Simply ask again from scratch; the second try usually succeeds.

    log.error("Giving up after two unusable replies: %s", last_error)
    raise ExplainError("Our AI helper got a bit muddled explaining this one. Please try again.")


EXPERT_KEYS = [
    "subject", "topic", "safety", "core_facts", "why_it_happened", "mechanisms",
    "previous_stories", "key_people", "student_relevance", "explanation_plan",
    "tricky_terms", "fun_fact_candidates", "quiz_ideas",
]
# The Expert may legitimately skip analysis fields for unsuitable material, so
# only the classification fields are strictly required.
EXPERT_REQUIRED = ["subject", "topic", "safety"]
WRITER_KEYS = ["headline", "in_3_lines", "quiz"]

SAFETY_VALUES = ("fine", "heavy", "not_suitable")
KINDS = ("person", "organisation", "term")


def expert_model_for(article_text: str, input_type: str = "text") -> str:
    """Short typed or pasted text uses the fast model; photos, PDFs, links and long text use the full model."""
    if SHORT_INPUT_CHARS and input_type in ("text", "rewrite") and len(article_text) <= SHORT_INPUT_CHARS:
        return EXPERT_MODEL_FAST
    return EXPERT_MODEL


def run_expert(client, article_text: str, input_type: str = "text"):
    """Layer 1: analyse the material once. Level- and language-independent."""
    model = expert_model_for(article_text, input_type)
    notes, usage = _call_json(
        client,
        model,
        EXPERT_SYSTEM,
        EXPERT_USER.format(article_text=article_text),
        max_tokens=8000,
        required_keys=EXPERT_REQUIRED,
    )
    usage["model"] = model
    if notes.get("safety") not in SAFETY_VALUES:
        notes["safety"] = "fine"
    return notes, usage


def run_writer(client, article_text: str, expert_notes: dict, level_instructions: str, language: str):
    """Layer 2: turn the teacher's notes into the student-facing explanation."""
    result, usage = _call_json(
        client,
        WRITER_MODEL,
        WRITER_SYSTEM.format(language=language, level_instructions=level_instructions),
        WRITER_USER.format(
            material_block=(
                f"Here is the original material:\n\n<material>\n{article_text}\n</material>\n\n"
                if WRITER_ARTICLE_CHARS and len(article_text) <= WRITER_ARTICLE_CHARS
                else ""
            ),
            # Compact JSON: fewer tokens for the Writer to read.
            expert_notes=json.dumps(expert_notes, ensure_ascii=False, separators=(",", ":")),
        ),
        max_tokens=8000,
        required_keys=WRITER_KEYS,
    )
    usage["model"] = WRITER_MODEL
    return normalize_explanation(result, expert_notes.get("safety", "fine")), usage


# ---------- Step 3: make every explanation match the frontend contract ----------

def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _source(value) -> str:
    return value if value in ("article", "background") else "background"


def _items(value, fields, required):
    """Keep only dict items whose required text fields are present."""
    out = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        clean = {f: _text(item.get(f)) for f in fields}
        if all(clean[f] for f in required):
            clean["source"] = _source(item.get("source"))
            out.append(clean)
    return out


def not_suitable_explanation() -> dict:
    """What the frontend receives for material that must not be explained."""
    return {
        "headline": "",
        "safety": "not_suitable",
        "gentle_note": None,
        "in_3_lines": [],
        "why_explainers": [],
        "backstory": [],
        "key_people": [],
        "side_stories": [],
        "fun_facts": [],
        "quiz": [],
    }


def normalize_explanation(raw: dict, safety: str) -> dict:
    """Coerce the Writer's reply into exactly the shape the frontend expects."""
    if safety == "not_suitable":
        return not_suitable_explanation()

    lines = [_text(x) for x in raw.get("in_3_lines", []) if _text(x)] if isinstance(raw.get("in_3_lines"), list) else []
    if not lines and _text(raw.get("in_3_lines")):
        lines = [_text(raw.get("in_3_lines"))]

    key_people = []
    for item in _items(raw.get("key_people"), ["name", "kind", "role", "country", "why_they_matter"], ["name"]):
        if item["kind"] not in KINDS:
            item["kind"] = "term" if not item["country"] else "person"
        key_people.append(item)

    quiz = []
    for q in raw.get("quiz", []) if isinstance(raw.get("quiz"), list) else []:
        if not isinstance(q, dict):
            continue
        options = [_text(o) for o in q.get("options", []) if _text(o)] if isinstance(q.get("options"), list) else []
        answer = q.get("answer_index")
        if (
            _text(q.get("question"))
            and len(options) >= 2
            and isinstance(answer, int)
            and not isinstance(answer, bool)
            and 0 <= answer < min(len(options), 4)
        ):
            quiz.append({
                "question": _text(q.get("question")),
                "options": options[:4],
                "answer_index": answer,
                "why": _text(q.get("why")),
            })

    gentle = _text(raw.get("gentle_note")) if safety == "heavy" else ""
    if safety == "heavy" and not gentle:
        gentle = "This topic is about something serious. It's okay to feel worried. Talking to a parent or teacher can help."

    return {
        "headline": _text(raw.get("headline")) or "Here's what this means",
        "safety": safety,
        "gentle_note": gentle or None,
        "in_3_lines": lines[:3],
        "why_explainers": _items(raw.get("why_explainers"), ["question", "answer"], ["question", "answer"]),
        "backstory": _items(raw.get("backstory"), ["year", "point"], ["point"]),
        "key_people": key_people,
        "side_stories": _items(raw.get("side_stories"), ["point"], ["point"]),
        "fun_facts": _items(raw.get("fun_facts"), ["point"], ["point"]),
        "quiz": quiz[:3],
    }
