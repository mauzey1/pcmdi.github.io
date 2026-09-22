#!/usr/bin/env python3
"""Find verifiable publication links from DOI, title, and author metadata.

The program performs lookups only.  It deliberately leaves page edits to the caller
because a plausible bibliographic match is not sufficient evidence to rewrite a page.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import ssl
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


CROSSREF_WORKS_URL = "https://api.crossref.org/works"
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
USER_AGENT = "pcmdi-publication-link-finder/1.0 (publication-link-maintenance)"


def normalize_doi(value: str | None) -> str | None:
    """Extract a bare, lower-case DOI from a DOI string or URL."""
    if not value:
        return None
    match = DOI_PATTERN.search(html.unescape(value))
    return match.group(0).rstrip(".,;)").lower() if match else None


def doi_url(doi: str) -> str:
    return f"https://doi.org/{quote(doi, safe='/')}"


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def title_similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


def author_surnames(value: str | list[str] | None) -> set[str]:
    """Return surname-like tokens from a legacy author string or a list of names."""
    if value is None:
        return set()
    text = "; ".join(value) if isinstance(value, list) else value
    normalized = normalize_text(text)
    # Crossref surnames are compared as whole tokens.  This also works for both
    # "Smith, J." and "J. Smith" forms without pretending to fully parse citations.
    return {token for token in normalized.split() if len(token) > 1 and token not in {"and", "et", "al"}}


def title_from_work(work: dict[str, Any]) -> str:
    title = work.get("title")
    if isinstance(title, list) and title:
        return str(title[0])
    return str(title) if isinstance(title, str) else ""


def work_author_surnames(work: dict[str, Any]) -> set[str]:
    authors = work.get("author")
    if not isinstance(authors, list):
        return set()
    return {
        normalize_text(family)
        for author in authors
        if isinstance(author, dict)
        and isinstance((family := author.get("family")), str)
        and normalize_text(family)
    }


def publisher_url_from_work(work: dict[str, Any]) -> str | None:
    resource = work.get("resource")
    if isinstance(resource, dict):
        primary = resource.get("primary")
        if isinstance(primary, dict) and isinstance(primary.get("URL"), str):
            return primary["URL"]
    links = work.get("link")
    if isinstance(links, list):
        for link in links:
            if isinstance(link, dict) and isinstance(link.get("URL"), str):
                return link["URL"]
    return None


def request_json(url: str, timeout: float, context: ssl.SSLContext | None) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout, context=context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def crossref_work(doi: str, timeout: float, context: ssl.SSLContext | None) -> dict[str, Any] | None:
    try:
        payload = request_json(f"{CROSSREF_WORKS_URL}/{quote(doi, safe='')}", timeout, context)
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    message = payload.get("message")
    return message if isinstance(message, dict) else None


def crossref_search(query: str, rows: int, timeout: float, context: ssl.SSLContext | None) -> list[dict[str, Any]]:
    params = urlencode({"query.bibliographic": query, "rows": rows})
    payload = request_json(f"{CROSSREF_WORKS_URL}?{params}", timeout, context)
    message = payload.get("message")
    items = message.get("items") if isinstance(message, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def resolve_doi(doi: str, timeout: float, context: ssl.SSLContext | None) -> str | None:
    request = Request(doi_url(doi), headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout, context=context) as response:
            return response.url
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError):
        return None


def candidate(work: dict[str, Any], title: str, authors: str | list[str] | None, source: str) -> dict[str, Any]:
    doi = normalize_doi(str(work.get("DOI", "")))
    candidate_title = title_from_work(work)
    score = title_similarity(title, candidate_title) if title and candidate_title else None
    expected_authors = author_surnames(authors)
    matched_authors = sorted(expected_authors & work_author_surnames(work))
    return {
        "source": source,
        "doi": doi,
        "canonical_doi_url": doi_url(doi) if doi else None,
        "publisher_url": publisher_url_from_work(work),
        "title": candidate_title or None,
        "container_title": (work.get("container-title") or [None])[0] if isinstance(work.get("container-title"), list) else work.get("container-title"),
        "published_year": (work.get("published-print") or work.get("published-online") or {}).get("date-parts", [[None]])[0][0],
        "title_similarity": round(score, 4) if score is not None else None,
        "matched_author_surnames": matched_authors,
    }


def confidence(item: dict[str, Any], expected_authors: bool) -> str:
    if item["source"] == "doi":
        return "high"
    score = item["title_similarity"] or 0
    has_author_match = bool(item["matched_author_surnames"])
    if score >= 0.95 and (not expected_authors or has_author_match):
        return "high"
    if score >= 0.85 and (not expected_authors or has_author_match):
        return "medium"
    return "low"


def find_record(record: dict[str, Any], args: argparse.Namespace, context: ssl.SSLContext | None) -> dict[str, Any]:
    doi = normalize_doi(str(record.get("doi", record.get("doi_id", ""))))
    title = str(record.get("title", "")).strip()
    authors = record.get("authors", record.get("author"))
    if not doi and not title:
        return {"input": record, "error": "Provide at least a DOI or title.", "candidates": []}

    works: list[tuple[dict[str, Any], str]] = []
    errors: list[str] = []
    if doi:
        work = crossref_work(doi, args.timeout, context)
        if work:
            works.append((work, "doi"))
        else:
            errors.append("Crossref could not verify the supplied DOI.")
    if not works and title:
        query = f"{title} {authors}" if authors else title
        try:
            works = [(work, "title-author-search") for work in crossref_search(query, args.rows, args.timeout, context)]
        except (HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"Crossref search failed: {exc}")

    candidates = [candidate(work, title, authors, source) for work, source in works]
    expected_authors = bool(author_surnames(authors))
    for item in candidates:
        item["confidence"] = confidence(item, expected_authors)
    candidates.sort(key=lambda item: (item["confidence"] == "high", item["title_similarity"] or 0), reverse=True)
    best = candidates[0] if candidates else None
    resolved_url = resolve_doi(best["doi"], args.timeout, context) if best and best["doi"] else None
    if best:
        best["resolved_doi_url"] = resolved_url
    recommended_url = None
    if best and best["confidence"] == "high":
        recommended_url = best["publisher_url"] if args.prefer_publisher and best["publisher_url"] else best["canonical_doi_url"]
    return {"input": {"doi": doi, "title": title or None, "authors": authors}, "recommended_url": recommended_url, "best_match": best, "candidates": candidates, "errors": errors}


def ssl_context(args: argparse.Namespace) -> ssl.SSLContext | None:
    if args.no_verify_tls and args.ca_bundle:
        raise ValueError("--no-verify-tls cannot be used with --ca-bundle")
    if args.no_verify_tls:
        return ssl._create_unverified_context()
    if args.ca_bundle:
        if not args.ca_bundle.is_file():
            raise ValueError(f"CA bundle does not exist: {args.ca_bundle}")
        return ssl.create_default_context(cafile=str(args.ca_bundle))
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--doi",
        help="DOI to verify; accepts a bare DOI, DOI URL, or string containing a DOI",
    )
    parser.add_argument(
        "--title",
        help="publication title, used to search and rank Crossref candidates",
    )
    parser.add_argument(
        "--authors",
        help="publication author list, used to confirm title-search candidates",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        help="JSON file containing one object or a list of objects with doi, title, and authors fields",
    )
    parser.add_argument(
        "--prefer-publisher",
        action="store_true",
        help="recommend Crossref's publisher URL instead of the default canonical DOI URL",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=5,
        help="maximum Crossref title/author search candidates to retrieve (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="maximum seconds to wait for each DOI or Crossref request (default: 20)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="seconds to wait between records supplied with --metadata (default: 0)",
    )
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        help="PEM CA bundle to use for TLS verification",
    )
    parser.add_argument(
        "--no-verify-tls",
        action="store_true",
        help="disable TLS certificate verification; do not use with --ca-bundle",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.rows < 1:
        raise SystemExit("--rows must be at least 1")
    if args.metadata and any((args.doi, args.title, args.authors)):
        raise SystemExit("Use either --metadata or individual metadata options, not both.")
    if args.metadata:
        payload = json.loads(args.metadata.read_text(encoding="utf-8"))
        records = payload if isinstance(payload, list) else [payload]
        if not all(isinstance(record, dict) for record in records):
            raise SystemExit("--metadata must contain a JSON object or a list of objects.")
    else:
        records = [{"doi": args.doi, "title": args.title, "authors": args.authors}]
    try:
        context = ssl_context(args)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    results = []
    for number, record in enumerate(records):
        results.append(find_record(record, args, context))
        if args.delay and number < len(records) - 1:
            time.sleep(args.delay)
    print(json.dumps(results[0] if len(results) == 1 else results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
