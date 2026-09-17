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


def fetch_ejm():
    data = json.loads(get("https://backend.econjobmarket.org/data/zz_public/json/Ads"))
    ads = []
    for a in data:
        url = a.get("url") or ""
        m = re.search(r"/positions/(\d+)", url)
        loc = (a.get("locations") or [{}])[0]
        city, country = loc.get("city"), loc.get("country_code") or loc.get("country")
        ads.append({
            "id": "ejm:" + (m.group(1) if m else url),
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


BROWSER_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


def get_browser(url, timeout=30):
    req = urllib.request.Request(url, headers=BROWSER_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def supplement_ejm_from_site(have_ids):
    """FAILSAFE: EJM's public JSON feed omits some live ads (recruiters opt in;
    e.g. Stanford's 2026-09-17 TT ad never appeared). Crawl the site's own
    /positions listing (paginated), and for ids the feed missed, build records
    from each position page's schema.org JobPosting JSON-LD.
    Returns (records, site_id_count)."""
    site_ids = []
    for page in range(1, 9):
        html = get_browser(f"https://econjobmarket.org/positions?page={page}")
        ids = list(dict.fromkeys(re.findall(r"/positions/(\d+)", html)))
        new = [i for i in ids if i not in site_ids]
        if not new:
            break
        site_ids.extend(new)
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
    return records, len(site_ids)


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
    # FAILSAFE 1: catch EJM ads the public feed omits, via the site listing.
    if status.get("ejm", {}).get("status") == "ok":
        try:
            have = {a["id"].split(":")[1] for a in all_ads if a["source"] == "ejm"}
            extra, site_n = supplement_ejm_from_site(have)
            all_ads.extend(extra)
            status["ejm"]["count"] += len(extra)
            status["ejm"]["site_listing"] = site_n
            status["ejm"]["site_extra"] = len(extra)
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

    for ad in all_ads:
        ad["predoc"] = is_predoc(ad)
        ad["senior"] = is_senior(ad)
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
