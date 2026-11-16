"""Bundesagentur API client, email extraction, cached search."""
import asyncio, base64, html, json, re

import httpx

import db
from config import (BA_BASE, BA_HEADERS, SEARCH_PATHS, DETAIL_PATHS, EMAIL_RE,
                    CACHE_TTL_H, log, step)

# ------------------------------------------------------------------ email filtering
BAD_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply", "datenschutz", "privacy",
             "dataprotection", "dsgvo", "abuse", "postmaster", "webmaster", "mailer-daemon")
GOOD_LOCAL = ("bewerbung", "karriere", "career", "jobs", "job", "recruiting", "personal", "hr")


def is_bad_email(e: str) -> bool:
    e = (e or "").lower().strip()
    if not EMAIL_RE.fullmatch(e):
        return True
    local = e.split("@", 1)[0]
    return any(b in local for b in BAD_LOCAL) or e.endswith((".png", ".jpg", ".gif", ".webp"))


def pick_email(blob: str):
    found = []
    for e in EMAIL_RE.findall(blob):
        e = e.strip(".").lower()
        if not is_bad_email(e) and e not in found:
            found.append(e)
    if not found:
        return None
    found.sort(key=lambda e: 0 if any(g in e.split("@")[0] for g in GOOD_LOCAL) else 1)
    return found[0]


# ------------------------------------------------------------------ BA API
_search_path_cache = None


def _ordered(paths, cached):
    return ([cached] if cached else []) + [p for p in paths if p != cached]


def extract_hits(data) -> list:
    if isinstance(data, list):
        return data
    for key in ("ergebnisliste", "stellenangebote", "jobs", "items", "results"):
        if isinstance(data.get(key), list):
            return data[key]
    if data.get("maxErgebnisse", 0) == 0 or "ergebnisliste" not in data:
        return []  # genuinely empty result
    log.warning("[ba] unknown response shape, top-level keys: %s", list(data.keys()))
    return []


async def ba_search(client: httpx.AsyncClient, what: str, where, size: int = 10) -> list:
    """Search jobs. Tries several endpoint versions; a 403/404 means that route doesn't exist."""
    global _search_path_cache
    params = {"was": what, "size": size, "page": 1, "angebotsart": 1}
    if where:
        params["wo"] = where
        params["umkreis"] = 50
    last = "no attempt"
    for path in _ordered(SEARCH_PATHS, _search_path_cache):
        for attempt in range(1, 4):
            try:
                r = await client.get(BA_BASE + path, params=params, headers=BA_HEADERS)
            except httpx.TransportError as e:
                last = f"{path}: network error {e!r}"
                log.warning("[ba] %s attempt %d: %s", path, attempt, last)
                await asyncio.sleep(attempt * 2)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{path}: HTTP {r.status_code}"
                log.warning("[ba] %s attempt %d: HTTP %d -> retry", path, attempt, r.status_code)
                await asyncio.sleep(attempt * 2)
                continue
            if r.status_code in (400, 403, 404):
                last = f"{path}: HTTP {r.status_code} {r.text[:100]!r}"
                log.warning("[ba] %s -> HTTP %d (%s), trying next endpoint", path, r.status_code, r.text[:100])
                break
            r.raise_for_status()
            data = r.json()
            if _search_path_cache != path:
                log.info("[ba] working search endpoint: %s", path)
                _search_path_cache = path
            hits = extract_hits(data)
            log.info("[ba] '%s' (where=%s) -> %d hits via %s", what, where or "Germany", len(hits), path)
            return hits
    raise RuntimeError(f"Bundesagentur search failed for '{what}': {last}")


async def ba_details(client: httpx.AsyncClient, refnr: str) -> dict:
    enc = base64.b64encode(refnr.encode()).decode()
    for tmpl in DETAIL_PATHS:
        path = tmpl.format(enc)
        try:
            r = await client.get(BA_BASE + path, headers=BA_HEADERS)
            if r.status_code == 200:
                return r.json()
            log.warning("[ba] details %s -> HTTP %d", refnr, r.status_code)
        except Exception as e:
            log.warning("[ba] details %s failed: %r", refnr, e)
    return {}


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def clean_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


async def search_with_fallback(client, title: str, where):
    hits = await ba_search(client, title, where)
    if hits:
        return hits
    for w in re.split(r"[\s/,\-]+", title):
        if len(w) >= 5 and w.lower() != title.lower():
            log.info("[ba] no hits for '%s', retrying with keyword '%s'", title, w)
            hits = await ba_search(client, w, where)
            if hits:
                return hits
    return []


async def find_jobs(titles: list, where, limit: int = 25) -> list:
    titles = titles[:4]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        with step(f"BA search for {len(titles)} titles (where={where or 'Germany'})"):
            results = await asyncio.gather(
                *[search_with_fallback(client, t, where) for t in titles], return_exceptions=True
            )
        hits, errors = [], []
        for t, res in zip(titles, results):
            if isinstance(res, Exception):
                log.error("[ba] search '%s' failed: %s", t, res)
                errors.append(str(res))
            else:
                hits.extend(res)
        if not hits and errors:
            raise RuntimeError(errors[0])

        seen, base = set(), []
        for h in hits:
            ref = _first(h, "refnr", "referenznummer", "hashId")
            if ref and ref not in seen:
                seen.add(ref)
                h["_ref"] = ref
                base.append(h)
        base = base[:limit]
        log.info("[jobs] %d unique jobs, fetching details", len(base))
        with step(f"fetch details for {len(base)} jobs"):
            details = await asyncio.gather(*[ba_details(client, h["_ref"]) for h in base])

    jobs = []
    for h, d in zip(base, details):
        email = pick_email(json.dumps(d, ensure_ascii=False))
        # v6: stellenlokationen[0].adresse.ort   (v4: arbeitsort.ort)
        place = ""
        locs = h.get("stellenlokationen")
        if isinstance(locs, list) and locs:
            place = (locs[0].get("adresse") or {}).get("ort", "")
        elif isinstance(h.get("arbeitsort"), dict):
            place = h["arbeitsort"].get("ort", "")
        ref = h["_ref"]
        jobs.append({
            "refnr": ref,
            "title": _first(h, "stellenangebotsTitel", "titel", "beruf", default="?"),
            "employer": _first(h, "firma", "arbeitgeber", "arbeitgeberName", default="?"),
            "place": place,
            "description": clean_html(_first(d, "stellenangebotsBeschreibung", "stellenbeschreibung", default="")),
            "email": email,
            "url": h.get("externeUrl") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
        })
    log.info("[jobs] %d jobs ready, %d with email", len(jobs), sum(1 for j in jobs if j["email"]))
    return jobs


async def find_jobs_cached(titles: list, where) -> list:
    """Shared 3h cache keyed by location + sorted titles. Also stores jobs for button lookups."""
    key = f"{where or 'DE'}|{'|'.join(sorted(t.lower() for t in titles[:4]))}"
    jobs = await db.arun(db.get_cache, key, CACHE_TTL_H)
    if jobs is not None:
        log.info("[jobs] cache hit for %s (%d jobs)", key, len(jobs))
    else:
        jobs = await find_jobs(titles, where)
        if jobs:
            await db.arun(db.put_cache, key, jobs)
    await db.arun(db.upsert_jobs, jobs)
    return jobs