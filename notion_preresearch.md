# Notion Zotero Knowledge Base Reader

This folder contains a small Python CLI for reading the shared Notion `Zotero`
knowledge base through the public Notion API.

## Verified configuration

- Connection token: provide through `NOTION_TOKEN`; do not hard-code it.
- Required capability: read content.
- Required sharing: the parent database must be shared with the connection.
- Notion API version used by the script: `2026-03-11`.
- Data source found: `Zotero`
- Data source id: `1a958b0f-ad1f-4f78-a082-cbdc66a3cd23`

## Usage

```bash
export NOTION_TOKEN="ntn_..."
```

Search shared pages/data sources by title:

```bash
./notion_kb_reader.py search zotero --type data_source --limit 5
./notion_kb_reader.py search "chain of thought" --type page --limit 10
```

Query the Zotero data source and locally filter page properties:

```bash
./notion_kb_reader.py query \
  --data-source-id 1a958b0f-ad1f-4f78-a082-cbdc66a3cd23 \
  "reasoning" \
  --limit 10
```

Read a page into Markdown-like text:

```bash
./notion_kb_reader.py read 12fee1d7-d69e-813b-81c8-f150b3d324af --output sample_page.md
```

Find text inside one page after fetching properties and block content:

```bash
./notion_kb_reader.py find 12fee1d7-d69e-813b-81c8-f150b3d324af reasoning
```

Search or query, select the first matched page, then read it:

```bash
./notion_kb_reader.py demo \
  --data-source-id 1a958b0f-ad1f-4f78-a082-cbdc66a3cd23 \
  "reasoning" \
  --output first_match.md
```

```python
#!/usr/bin/env python3
"""Search and read a Notion knowledge base via the public API.

Set NOTION_TOKEN before running:
  export NOTION_TOKEN="ntn_..."
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2026-03-11")


class NotionAPIError(RuntimeError):
    pass


class NotionClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        retries: int = 4,
    ) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None}
            )
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
            "User-Agent": "notion-kb-reader/1.0",
        }

        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                retry_after = exc.headers.get("Retry-After")
                if exc.code in {429, 500, 502, 503, 504} and attempt < retries:
                    sleep_s = float(retry_after or min(2**attempt, 8))
                    time.sleep(sleep_s)
                    continue
                raise NotionAPIError(f"HTTP {exc.code} {exc.reason}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < retries:
                    time.sleep(min(2**attempt, 8))
                    continue
                raise NotionAPIError(str(exc)) from exc

        raise NotionAPIError("request failed after retries")


def require_token() -> str:
    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if not token:
        raise SystemExit(
            "Missing token. Run: export NOTION_TOKEN='ntn_...'\n"
            "Tip: do not hard-code the token in this script."
        )
    return token


def paginate(
    client: NotionClient,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        request_body = dict(body or {})
        request_query = dict(query or {})
```