"""Tavily wrapper: web search + extract for the Scout agent."""
from __future__ import annotations

from .. import settings
from .util import log, retry


@retry(times=3)
def _live_search(query: str, max_results: int) -> list[dict]:
    from tavily import TavilyClient

    client = TavilyClient(api_key=settings.TAVILY_API_KEY)
    resp = client.search(query=query, max_results=max_results, search_depth="advanced")
    return resp.get("results", [])


def search(query: str, max_results: int = 5) -> list[dict]:
    """Returns a list of {title, url, content} dicts."""
    if settings.mock_mode():
        log("tavily", f"(mock) search: {query!r}")
        return [
            {"title": "Tide myths across coastal cultures",
             "url": "https://example.org/folklore/tide-myths",
             "content": "Many coastal traditions personify the tide as a keeper of the dead..."},
            {"title": "Underwater archaeology of sunken cities",
             "url": "https://example.org/marine-archaeology",
             "content": "Salt crystal formations preserve organic patterns for centuries..."},
            {"title": "Memory and forgetting in oral storytelling",
             "url": "https://example.org/memory-oral",
             "content": "Cultures without writing treat memory as a finite, shared resource..."},
        ][:max_results]
    log("tavily", f"search: {query!r}")
    return _live_search(query, max_results)
