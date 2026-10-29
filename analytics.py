"""Server-side Mixpanel events: things only the backend knows (tokens, latency, errors).

Visits, clicks and page views are tracked by the Lovable frontend with Mixpanel's
browser library. The frontend sends its Mixpanel distinct_id in the
X-Distinct-Id header, so backend events join the same user in Mixpanel.

If MIXPANEL_TOKEN is not set, every call here does nothing. Analytics can never
break or slow down an explanation: events are sent on a background thread and
all errors are swallowed.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("explainittomelikeiamyourmom.analytics")

_TOKEN = os.environ.get("MIXPANEL_TOKEN", "").strip()
# api.mixpanel.com (US, default), api-eu.mixpanel.com (EU) or api-in.mixpanel.com (India),
# depending on your Mixpanel project's data residency.
_API_HOST = os.environ.get("MIXPANEL_API_HOST", "api.mixpanel.com").strip()

_mixpanel = None
if _TOKEN:
    try:
        from mixpanel import Consumer, Mixpanel

        _mixpanel = Mixpanel(_TOKEN, consumer=Consumer(api_host=_API_HOST, request_timeout=5))
    except Exception:  # library missing or misconfigured: run without analytics
        log.exception("Mixpanel disabled")

_sender = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mixpanel")

ENABLED = _mixpanel is not None


def track(distinct_id: str | None, event: str, properties: dict | None = None) -> None:
    if not _mixpanel:
        return
    props = {"source": "backend", **(properties or {})}

    def _send():
        try:
            _mixpanel.track(distinct_id or "anonymous-server", event, props)
        except Exception:
            log.warning("Mixpanel event %s not sent", event, exc_info=True)

    _sender.submit(_send)
