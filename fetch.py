#!/usr/bin/env python3
"""Fetch econ job ads from public sources and write a slim ads.json index.

Only listing metadata (institution, title, location, fields, deadline, link) is
kept — never full ad texts. Each source fails independently; its status is
recorded so downstream consumers can warn instead of silently missing ads.
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html import unescape

UA = {"User-Agent": "econ-job-ads-mirror/1.0 (personal job-search index; github.com/nelsonsunuwar/econ-job-ads-mirror)"}
OUT = "ads.json"


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


PAGES = "https://nelsonsunuwar.github.io/econ-job-ads-mirror"
FIRST_SEEN = "first_seen.json"
EJM_TEXT = {}  # ejm id -> raw feed ad HTML, only consulted for ads whose EJM page isn't live


def fetch_ejm():
    data = json.loads(get("https://backend.econjobmarket.org/data/zz_public/json/Ads"))
    ads = []
    for a in data:
        url = a.get("url") or ""
        m = re.search(r"/positions/(\d+)", url)
        loc = (a.get("locations") or [{}])[0]
        city, country = loc.get("city"), loc.get("country_code") or loc.get("country")
        ad_id = "ejm:" + (m.group(1) if m else url)
        EJM_TEXT[ad_id] = a.get("adtext") or ""
        ads.append({
            "id": ad_id,
            "source": "ejm",
            "institution": a.get("name"),
            "title": a.get("adtitle"),
            "location": ", ".join(x for x in [city, country] if x),
            "fields": [c.get("name") for c in a.get("categories") or []],
            "position_types": [p.get("name") for p in a.get("position_types") or []],
            "section": None,
            "deadline": a.get("deadline_date"),
            "posted": a.get("startdate"),
            "url": url,
        })
    return ads


def html_to_text(h):
    """Feed ad HTML -> plain text (rendered with textContent on the Pages ad
    view, so no markup from the feed ever reaches the page)."""
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", h)
    h = re.sub(r"(?i)<li[^>]*>", "• ", h)
    t = unescape(re.sub(r"<[^>]+>", "", h))
    t = re.sub(r"[ \t\xa0]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n\n", t).strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def ejm_page_status(pid):
    """Probe one EJM position page without following redirects:
    'ok' = live ad page, 'not_live' = redirects to login/home (seen even when
    logged in), 'dead' = 404/410."""
    req = urllib.request.Request(f"https://econjobmarket.org/positions/{pid}", headers=BROWSER_UA)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as r:
            html = r.read().decode("utf-8", errors="replace")
            return "ok" if "application/ld+json" in html else "not_live"
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return "not_live"
        if e.code in (404, 410):
            return "dead"
        raise


def check_ejm_links(ads, site_ids):
    """LINK CHECK (2026-10-09): EJM's feed carries some ads whose position page
    is not live — it bounces to the login page, and stays empty even when
    logged in (ISEG Lisbon 12703, Sabancı 12770, ...; recruiter hasn't
    published it yet, or pulled it). Ads on the public /positions listing are
    live; every other feed ad gets probed. Not-live ads keep a link that shows
    something real: our Pages ad view with the feed's own ad text (EJM's
    zz_public feed is the recruiter-approved redistribution channel). Re-probed
    every run, so the link flips back to EJM as soon as the page goes live."""
    for ad in ads:
        if ad["source"] != "ejm":
            continue
        pid = ad["id"].split(":", 1)[1]
        status = "ok" if pid in site_ids else None
        if status is None:
            try:
                status = ejm_page_status(pid)
            except Exception:  # noqa: BLE001 — a failed probe leaves the link as is
                status = "ok"
        ad["link_status"] = status
        if status == "not_live":
            raw = EJM_TEXT.get(ad["id"], "")
            ad["source_url"] = ad["url"]
            ad["url"] = f"{PAGES}/ad.html#{ad['id']}"
            ad["text"] = html_to_text(raw)[:8000]
            ad["links"] = list(dict.fromkeys(
                u for u in re.findall(r'href="(https?://[^"]+)"', raw) if "econjobmarket.org" not in u))[:6]


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def get_browser(url, timeout=30):
    req = urllib.request.Request(url, headers=BROWSER_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def ejm_site_ids():
    """Ids on EJM's public /positions listing (paginated) — the live ads."""
    site_ids = []
    for page in range(1, 9):
        html = get_browser(f"https://econjobmarket.org/positions?page={page}")
        ids = list(dict.fromkeys(re.findall(r"/positions/(\d+)", html)))
        new = [i for i in ids if i not in site_ids]
        if not new:
            break
        site_ids.extend(new)
    return site_ids


def supplement_ejm_from_site(have_ids, site_ids):
    """FAILSAFE: EJM's public JSON feed omits some live ads (recruiters opt in;
    e.g. Stanford's 2026-09-17 TT ad never appeared). For listing ids the feed
    missed, build records from each position page's schema.org JobPosting
    JSON-LD."""
    missing = [i for i in site_ids if i not in have_ids][:25]  # politeness cap
    records = []
    for pid in missing:
        try:
            html = get_browser(f"https://econjobmarket.org/positions/{pid}")
            m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
            if not m:
                continue
            j = json.loads(m.group(1))
            loc = (j.get("jobLocation") or [{}])[0].get("address", {})
            city = (loc.get("addressLocality") or "").title()
            records.append({
                "id": f"ejm:{pid}",
                "source": "ejm",
                "institution": (j.get("hiringOrganization") or {}).get("name"),
                "title": j.get("title"),
                "location": ", ".join(x for x in [city, loc.get("addressCountry")] if x),
                "fields": [],
                "position_types": [],
                "section": None,
                "deadline": (j.get("validThrough") or "")[:10] or None,
                "posted": (j.get("datePosted") or "")[:10] or None,
                "url": f"https://econjobmarket.org/positions/{pid}",
            })
        except Exception:  # noqa: BLE001 — one bad page must not kill the sweep
            continue
    return records


def fetch_joe():
    xml = get("https://www.aeaweb.org/joe/resultset_output.php?mode=full_xml")
    root = ET.fromstring(xml)
    # Canonical listing URLs need the cycle prefix (e.g. 2026-02_<jp_id>);
    # bare numeric JOE_IDs work but bounce through a 302, which trips Gmail's
    # redirect interstitial.
    yr = root.find("year")
    iss = yr.find("issue") if yr is not None else None
    prefix = ""
    if yr is not None and iss is not None and yr.get("joe_year_ID") and iss.get("joe_issue_ID"):
        prefix = f"{yr.get('joe_year_ID')}-{int(iss.get('joe_issue_ID')):02d}_"
    ads = []
    for p in root.iter("position"):
        jp_id = p.get("jp_id")
        txt = lambda tag: (p.findtext(tag) or "").strip()
        locs = []
        for loc in p.iter("location"):
            city = (loc.findtext("city") or "").strip()
            country = (loc.findtext("country") or "").strip().title()
            locs.append(", ".join(x for x in [city, country] if x))
        deadline = txt("jp_application_deadline").split(" ")[0] or None
        ads.append({
            "id": f"joe:{jp_id}",
            "source": "joe",
            "institution": txt("jp_institution"),
            "title": txt("jp_title"),
            "location": "; ".join(x for x in locs if x),
            "fields": sorted({(j.findtext("jc_code") or "").strip() + " " + (j.findtext("jc_description") or "").strip()
                              for j in p.iter("jel_class")}),
            "position_types": [],
            "section": txt("jp_section"),
            "deadline": deadline,
            "posted": None,
            "url": f"https://www.aeaweb.org/joe/listing.php?JOE_ID={prefix}{jp_id}",
        })
    return ads




def fetch_econjobs():
    # The listings page is JS-rendered, but WordPress exposes a job-listing sitemap.
    xml = get("https://econ-jobs.com/job_listing-sitemap.xml")
    root = ET.fromstring(xml)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    ads = []
    for u in root.findall("sm:url", ns):
        loc = (u.findtext("sm:loc", default="", namespaces=ns) or "").strip()
        if "/job-offer/" not in loc:
            continue
        slug = loc.rstrip("/").rsplit("/", 1)[-1]
        lastmod = (u.findtext("sm:lastmod", default="", namespaces=ns) or "").strip()
        ads.append({
            "id": "ej:" + slug,
            "source": "econjobs",
            "institution": None,
            "title": slug.replace("-", " ").capitalize(),
            "location": None,
            "fields": [],
            "position_types": [],
            "section": None,
            "deadline": None,
            "posted": lastmod.split("T")[0] or None,
            "url": loc,
        })
    return ads


def is_predoc(ad):
    """Pre-doc / RA-level ads (Nelson is post-PhD; these are filtered downstream).
    (jobs.ac.uk source dropped 2026-09-13; its exemption below is now moot but
    kept harmless)."""
    title = ad["title"] or ""
    pts = ad["position_types"] or []
    if re.search(r"pre-?doc", title, re.I):
        return True
    # Not actual openings for a new PhD: internships and standing
    # "unsolicited application" ads (e.g. IPP's EJM 12319/12320).
    if re.search(r"\bintern(ship)?s?\b|\bstagiaire\b|unsolicited|candidature spontan", title, re.I):
        return True
    if any("Pre-Doc" in p for p in pts):
        return True
    # PhD-student positions (not jobs FOR PhD holders like "PhD Economist" or
    # "Consultant (New PhD)", and not post-docs).
    if any(re.search(r"\bdoctoral student\b", p, re.I) for p in pts):
        return True
    if not re.search(r"post-?doc", title, re.I) and re.search(
            r"(ph\.?d|doctoral)[\s-]+(position|student|studentship|candidate|scholarship|researcher|programme|program)|\bstudentship\b",
            title, re.I):
        return True
    if ad["source"] != "jobsacuk" and re.search(r"\bresearch (assistant|associate|professional)\b", title, re.I):
        if re.search(r"post-?doc|professor|fellow", title, re.I):
            return False
        if any(re.search(r"postdoc|professor", p, re.I) for p in pts):
            return False
        return True
    return False


JUNIOR_RE = re.compile(r"assistant|\basst\b|junior|open.?rank|any.?rank|all.?ranks|postdoc|pre-?doc|\bw1\b|\blecturer\b", re.I)
SENIOR_RE = re.compile(r"\b(associate|full|tenured)\s+professor|professor\s*\(full\)|\bprofessorship|\bw[23]\b|\bchair\b|senior lecturer|\breadership\b|\breader in\b|full or advanced|\bdirector\b", re.I)


def is_senior(ad):
    """Exclusively associate-and-above ads (Nelson is a junior candidate).
    Open-rank ads that include assistant level are NOT senior."""
    title = ad["title"] or ""
    pts = ad["position_types"] or []
    if any(re.search(r"assistant|postdoc|lecturer|instructor|visiting", p, re.I) for p in pts):
        return False
    if JUNIOR_RE.search(title):
        return False
    if any(re.search(r"associate professor|full professor|tenured", p, re.I) for p in pts):
        return True
    return bool(SENIOR_RE.search(title))


def main():
    # NABE dropped 2026-09-09: econjobs.nabe.com 403s GitHub runners permanently.
    fetchers = {
        "ejm": fetch_ejm,
        "joe": fetch_joe,
        "econjobs": fetch_econjobs,
    }
    all_ads, status = [], {}
    for name, fn in fetchers.items():
        try:
            ads = fn()
            status[name] = {"status": "ok", "count": len(ads)}
            all_ads.extend(ads)
        except Exception as e:  # noqa: BLE001 — a broken source must not kill the rest
            status[name] = {"status": f"error: {type(e).__name__}: {e}", "count": 0}
    # FAILSAFE 1: catch EJM ads the public feed omits, via the site listing;
    # then make sure every EJM link opens a live ad (see check_ejm_links).
    if status.get("ejm", {}).get("status") == "ok":
        try:
            site_ids = ejm_site_ids()
            have = {a["id"].split(":")[1] for a in all_ads if a["source"] == "ejm"}
            extra = supplement_ejm_from_site(have, site_ids)
            all_ads.extend(extra)
            status["ejm"]["count"] += len(extra)
            status["ejm"]["site_listing"] = len(site_ids)
            status["ejm"]["site_extra"] = len(extra)
            check_ejm_links(all_ads, set(site_ids))
            status["ejm"]["not_live"] = sum(a.get("link_status") == "not_live" for a in all_ads)
            status["ejm"]["dead"] = sum(a.get("link_status") == "dead" for a in all_ads)
        except Exception as e:  # noqa: BLE001
            status["ejm"]["status"] = f"warning: site supplement failed: {type(e).__name__}: {e}"

    # FAILSAFE 2: JOE coverage assertion — one light fetch of the site's first
    # listings page; any id there that the XML export lacks means the export
    # has developed an EJM-style gap. Warn, don't crawl further (JOE ToS).
    if status.get("joe", {}).get("status") == "ok":
        try:
            html = get_browser("https://www.aeaweb.org/joe/listings")
            page_ids = set(re.findall(r"JOE_ID=(?:\d{4}-\d\d_)?(\d+)", html))
            joe_ids = {a["id"].split(":")[1] for a in all_ads if a["source"] == "joe"}
            gap = page_ids - joe_ids
            if page_ids and gap:
                status["joe"]["status"] = f"warning: {len(gap)} ads on the JOE site are missing from the XML export"
            # Our listing links carry a cycle prefix (2026-02_…); if the site's
            # own links use another one (cycle rollover), ours lead nowhere.
            site_prefixes = set(re.findall(r"JOE_ID=(\d{4}-\d\d)_\d+", html))
            ours = {m.group(1) for a in all_ads if a["source"] == "joe"
                    for m in [re.search(r"JOE_ID=(\d{4}-\d\d)_", a["url"])] if m}
            if site_prefixes and ours - site_prefixes:
                status["joe"]["status"] = (f"warning: JOE links use cycle {', '.join(sorted(ours))} but the site uses "
                                           f"{', '.join(sorted(site_prefixes))} — listing links may be broken")
        except Exception:  # noqa: BLE001 — the assertion itself must never break the fetch
            pass

    # FAILSAFE 3: a source whose count collapses versus the last snapshot
    # probably means silent parser/format breakage — surface it in the digest.
    try:
        prev = json.load(open(OUT))["sources"]
        for name, s in status.items():
            old = prev.get(name, {}).get("count", 0)
            if s["status"] == "ok" and old >= 20 and s["count"] < old * 0.5:
                s["status"] = f"warning: count dropped {old}->{s['count']} (possible parser breakage)"
    except Exception:  # noqa: BLE001 — no previous snapshot is fine
        pass

    # first_seen: the UTC date an id first appeared in any snapshot. Kept in a
    # monotonic side file so an ad that drops out and returns keeps its date.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        first_seen = json.load(open(FIRST_SEEN))
    except Exception:  # noqa: BLE001
        first_seen = {}
    for ad in all_ads:
        ad["predoc"] = is_predoc(ad)
        ad["senior"] = is_senior(ad)
        ad.setdefault("link_status", "ok" if ad["source"] in ("ejm", "joe") else "unchecked")
        ad["first_seen"] = first_seen.setdefault(ad["id"], today)
    with open(FIRST_SEEN, "w") as f:
        json.dump(dict(sorted(first_seen.items())), f, indent=0)
    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": status,
        "ads": all_ads,
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    print(json.dumps(status, indent=2))
    ok = sum(1 for s in status.values() if s["status"] == "ok")
    print(f"{len(all_ads)} ads from {ok}/{len(fetchers)} sources -> {OUT}")
    # Exit nonzero only if the two primary sources both failed
    if status["ejm"]["status"] != "ok" and status["joe"]["status"] != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
