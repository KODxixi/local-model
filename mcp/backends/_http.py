"""Shared HTTP JSON POST helper for MCP backends.

Extracted from rag_client.py (P0-1 fix) so that both the embedder
(lmstudio.py) and the reranker (reranker.py) share the same exponential
backoff retry policy. Without this, the first MCP embed/rerank call after
llama-swap TTL unload fails immediately because cold load takes ~12s.

Retry policy:
  - 5 attempts, initial_delay=2.0s, backoff=2.0, max_delay=30.0s
  - jitter: random.uniform(0, delay * 0.3) to avoid thundering herd
  - retry on ConnectionReset / URLError / TimeoutError / OSError / 5xx / 429
  - do NOT retry on other 4xx
"""

from __future__ import annotations

import json
import random
import sys
import time
import urllib.error
import urllib.request
from typing import Any

RETRY_MAX_ATTEMPTS = 5
RETRY_INITIAL_DELAY = 2.0
RETRY_MAX_DELAY = 30.0
RETRY_BACKOFF = 2.0


def request_json(
    base_url: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 180,
    *,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    initial_delay: float = RETRY_INITIAL_DELAY,
) -> Any:
    """POST (or GET when payload is None) JSON with exponential backoff retry.

    Preserves the historical signature `_request_json(base_url, path, payload, timeout)`
    used by lmstudio.py and reranker.py.
    """
    url = f"{base_url.rstrip('/')}{path}"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    method = "POST" if payload is not None else "GET"

    last_exc: Exception | None = None
    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                method=method,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last_exc = e
            else:
                raise
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
        except Exception:
            raise

        if attempt < max_attempts:
            # jitter: add up to 30% of current delay as random noise
            sleep_for = min(delay + random.uniform(0, delay * 0.3), RETRY_MAX_DELAY)
            print(
                f"[mcp._http] {url} attempt {attempt}/{max_attempts} failed: "
                f"{type(last_exc).__name__}, retry in {sleep_for:.1f}s",
                file=sys.stderr,
            )
            time.sleep(sleep_for)
            delay = min(delay * RETRY_BACKOFF, RETRY_MAX_DELAY)

    raise RuntimeError(
        f"请求失败（已重试 {max_attempts} 次）: {url} — "
        f"{type(last_exc).__name__}: {last_exc}"
    ) from last_exc
