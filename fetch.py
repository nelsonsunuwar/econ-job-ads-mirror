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


def fetch_joe():
    xml = get("https://www.aeaweb.org/joe/resultset_output.php?mode=full_xml")
    root = ET.fromstring(xml)
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
            "url": f"https://www.aeaweb.org/joe/listing.php?JOE_ID={jp_id}",
        })
    return ads


def fetch_jobsacuk():
    # The old RSS feeds 404 now; the category/search pages are server-rendered.
    ads, seen = [], set()
    for page in ("https://www.jobs.ac.uk/categories/economics",
                 "https://www.jobs.ac.uk/search/economics"):
        html = get(page)
        for m in re.finditer(r'href="/job/([A-Z0-9]+)/([^"]*)"[^>]*>(.*?)</a>', html, re.S | re.I):
            code, text = m.group(1), unescape(re.sub(r"<[^>]+>", " ", m.group(3))).strip()
            if code in seen:
                continue
            seen.add(code)
            ads.append({
                "id": "jacuk:" + code,
                "source": "jobsacuk",
                "institution": None,
                "title": re.sub(r"\s+", " ", text)[:200] or m.group(2).replace("-", " "),
                "location": None,
                "fields": [],
                "position_types": [],
                "section": None,
                "deadline": None,
                "posted": None,
                "url": f"https://www.jobs.ac.uk/job/{code}/{m.group(2)}",
            })
    return ads


def _scrape_links(page_url, href_re, base=""):
    html = get(page_url)
    ads, seen = [], set()
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        href, text = m.group(1), unescape(re.sub(r"<[^>]+>", " ", m.group(2))).strip()
        if not re.search(href_re, href) or not text or len(text) < 4:
            continue
        url = href if href.startswith("http") else base + href
        if url in seen:
            continue
        seen.add(url)
        ads.append({
            "id": None,  # filled by caller with source prefix
            "source": None,
            "institution": None,
            "title": re.sub(r"\s+", " ", text)[:200],
            "location": None,
            "fields": [],
            "position_types": [],
            "section": None,
            "deadline": None,
            "posted": None,
            "url": url,
        })
    return ads


def fetch_nabe():
    ads = _scrape_links("https://econjobs.nabe.com/jobs/", r"/jobs?/\d|/job/", "https://econjobs.nabe.com")
    for a in ads:
        a["id"], a["source"] = "nabe:" + a["url"], "nabe"
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


def main():
    fetchers = {
        "ejm": fetch_ejm,
        "joe": fetch_joe,
        "jobsacuk": fetch_jobsacuk,
        "nabe": fetch_nabe,
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
