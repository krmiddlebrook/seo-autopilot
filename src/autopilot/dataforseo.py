"""Paid Google organic results and keyword metrics from the DataForSEO v3 Live APIs."""

from __future__ import annotations

import os
from typing import Any

import httpx

from .config import secret


def _items(client: httpx.Client, endpoint: str, params: dict, language: str, country: str) -> list[dict]:
    location = os.environ.get("DATAFORSEO_LOCATION_CODE") or ("2840" if country.lower() == "us" else "")
    if not location.isascii() or not location.isdecimal() or int(location) <= 0:
        raise ValueError("Set DATAFORSEO_LOCATION_CODE to the numeric DataForSEO location for your market")
    task = {**params, "language_code": language, "location_code": int(location)}
    response = client.post(
        f"https://api.dataforseo.com/v3/{endpoint}", json=[task],
        auth=(secret("DATAFORSEO_LOGIN"), secret("DATAFORSEO_PASSWORD")), follow_redirects=False,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status_code") != 20000:
        raise RuntimeError(f"DataForSEO rejected the request (status {data.get('status_code')})")
    tasks = data.get("tasks") or []
    if len(tasks) != 1:
        raise RuntimeError("DataForSEO returned an unexpected number of tasks")
    result = tasks[0]
    code = result.get("status_code")
    if code == 40102:  # No Search Results
        return []
    if code != 20000:
        raise RuntimeError(f"DataForSEO {endpoint} task failed (status {code}). Check API access and balance.")
    return [item for group in result.get("result") or [] for item in group.get("items") or []]


def organic_search(client: httpx.Client, query: str, language: str, country: str) -> dict[str, Any]:
    items = _items(client, "serp/google/organic/live/advanced", {"keyword": query, "depth": 10}, language, country)
    results, questions, related = [], [], []
    for item in items:
        kind = item.get("type")
        if kind == "organic" and item.get("url"):
            results.append({"url": item["url"], "title": item.get("title") or "", "snippet": item.get("description") or ""})
        elif kind == "people_also_ask":
            questions.extend(q["title"] for q in item.get("items") or [] if q.get("title"))
        elif kind == "related_searches":
            related.extend(q for q in item.get("items") or [] if isinstance(q, str))
    return {"results": results, "questions": questions, "related": related}


def keyword_metrics(client: httpx.Client, keywords: list[str], language: str, country: str) -> dict[str, dict]:
    batch = list(dict.fromkeys(kw.lower() for kw in keywords))[:50]
    if not batch:
        return {}
    items = _items(client, "dataforseo_labs/google/keyword_overview/live", {"keywords": batch}, language, country)
    metrics = {}
    for item in items:
        volume = (item.get("keyword_info") or {}).get("search_volume")
        difficulty = (item.get("keyword_properties") or {}).get("keyword_difficulty")
        metrics[item["keyword"].lower()] = {key: value for key, value in (("volume", volume), ("kd", difficulty))
                                            if value is not None}
    return {kw: metrics[kw.lower()] for kw in keywords if metrics.get(kw.lower())}
