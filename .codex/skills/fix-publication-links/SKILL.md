---
name: fix-publication-links
description: Find and safely repair publication links in repository pages from a publication entry's DOI, title, and author list. Use when a research article link is broken, outdated, missing, or needs verification; search DOI metadata and Crossref, assess title/author matches, and update the page only after a high-confidence result is confirmed.
---

# Fix Publication Links

Use `scripts/find_publication_link.py` to obtain structured candidates before changing a page. Keep lookup separate from edits.

## Workflow

1. Inspect the page entry and retain its title, author list, DOI (if present), and existing URL. Do not treat the old URL as evidence that a candidate is correct.
2. Look up the entry. Prefer a DOI when available:

   ```bash
   python .codex/skills/fix-publication-links/scripts/find_publication_link.py \
     --doi '10.1029/2007JD008972' --title 'Performance metrics for model evaluation'
   ```

   For an entry without a DOI, include the full title and authors:

   ```bash
   python .codex/skills/fix-publication-links/scripts/find_publication_link.py \
     --title 'Performance metrics for model evaluation' \
     --authors 'Gleckler, P. J.; Taylor, K. E.; Doutriaux, C.'
   ```

   Use `--metadata entry.json` for a JSON object or list of objects with `doi`, `title`, and `authors` fields. Use `--prefer-publisher` only when the page convention requires a publisher URL; the default recommended link is the durable DOI URL.
3. Accept an automatic recommendation only when `confidence` is `high`. DOI matches are high confidence; title searches require a near-exact title and, where supplied, an overlapping author surname. Review `medium` and `low` candidates manually against the entry's title, authors, venue, and year.
4. Update only the intended publication URL. Preserve the page's existing markup and link text. Use the recommended URL rather than a search-result URL.
5. Re-read the edited entry and run the repository's relevant formatting or link checks. Report entries with no high-confidence match instead of guessing.

## Output and network behavior

The utility writes JSON to standard output. `recommended_url` is null when the candidate is not safe to apply automatically. It reports the canonical DOI URL, Crossref publisher resource, DOI redirect target, title similarity, and matched author surnames for review.

The tool queries `doi.org` and the Crossref API. It does not modify pages, follows no search-engine results, and uses no third-party Python packages. Use `--timeout`, `--delay`, `--ca-bundle`, or `--no-verify-tls` only when the local network environment requires them.
