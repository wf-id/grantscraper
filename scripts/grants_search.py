#!/usr/bin/env python3
"""
grants_search.py
────────────────
Query grants.gov (legacy), simpler.grants.gov (v2), and/or sam.gov for
funding opportunities matching user-defined keywords.

NOTE: SAM.gov covers federal CONTRACT opportunities (STTR, SBIR, solicitations,
awards). Grants (assistance listings) are on grants.gov. Use --api sam or
--api all when targeting DoD STTR/SBIR solicitations.

Usage
-----
python grants_search.py \
    --keywords "wound infection" "multidrug resistant" "biofilm" \
    --logic OR \
    --status posted \
    --max-results 200 \
    --output results.csv \
    --api all \
    --simpler-key YOUR_SIMPLER_KEY \
    --sam-key YOUR_SAM_KEY \
    --sam-ptype o,k          # o=solicitation, k=SBIR/STTR
    --sam-posted-from 01/01/2024

SAM.gov API key: free from sam.gov → profile → "Request API Key"
  - Unregistered: 10 req/day
  - Registered entity: 1,000 req/day

Dependencies: requests, pandas
    pip install requests pandas
"""

import argparse
import re
import sys
import time
from typing import Optional

import pandas as pd
import requests

# ── API endpoints ─────────────────────────────────────────────────────────────
LEGACY_URL  = "https://api.grants.gov/v1/api/search2"
SIMPLER_URL = "https://api.simpler.grants.gov/v1/opportunities/search"
SAM_URL     = "https://api.sam.gov/prod/opportunities/v2/search"

# Fields searched for client-side keyword matching
SEARCH_FIELDS = ["opportunity_title", "agency_name", "summary_description",
                 "opportunity_number", "notice_type", "set_aside"]

# ── Legacy grants.gov (no auth) ───────────────────────────────────────────────

def fetch_legacy(keywords: list[str], status: str = "posted",
                 max_results: int = 500) -> list[dict]:
    """
    Pull from api.grants.gov/v1/api/search2.
    The API accepts a single keyword string and paginates via startRecordNum.
    We issue one query per keyword and deduplicate by opportunityId.
    """
    seen, records = set(), []
    page_size = 25  # API max per call

    for kw in keywords:
        start = 0
        while True:
            payload = {
                "keyword":        kw,
                "oppStatuses":    status,
                "startRecordNum": start,
                "rows":           page_size,
            }
            try:
                r = requests.post(LEGACY_URL, json=payload, timeout=30)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"[legacy] Request error for keyword '{kw}': {e}",
                      file=sys.stderr)
                break

            data  = r.json()
            hits  = data.get("data", {}).get("oppHits", [])
            total = data.get("data", {}).get("hitCount", 0)

            for h in hits:
                oid = h.get("id")
                if oid and oid not in seen:
                    seen.add(oid)
                    records.append({
                        "source":              "legacy",
                        "opportunity_id":      oid,
                        "opportunity_number":  h.get("number", ""),
                        "opportunity_title":   h.get("title", ""),
                        "agency_name":         h.get("agencyName", ""),
                        "post_date":           h.get("openDate", ""),
                        "close_date":          h.get("closeDate", ""),
                        "award_floor":         h.get("awardFloor", ""),
                        "award_ceiling":       h.get("awardCeiling", ""),
                        "summary_description": h.get("synopsis", ""),
                        "opportunity_status":  h.get("oppStatus", ""),
                        "url": f"https://www.grants.gov/search-results-detail/{oid}",
                    })

            start += page_size
            if start >= min(total, max_results):
                break
            time.sleep(0.2)   # polite rate-limit

    return records


# ── Simpler grants.gov (API key required) ────────────────────────────────────

def fetch_simpler(keywords: list[str], api_key: str,
                  status: str = "posted",
                  max_results: int = 500) -> list[dict]:
    """
    Pull from api.simpler.grants.gov/v1/opportunities/search.
    Sends each keyword as a separate query; deduplicates by opportunity_id.
    Rate limit: 60 req/min, 10 000 req/day.
    """
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    seen, records = set(), []
    page_size = min(25, max_results)

    for kw in keywords:
        page = 1
        while True:
            payload = {
                "query": kw,
                "filters": {"opportunity_status": {"one_of": [status]}},
                "pagination": {
                    "page_offset": page,
                    "page_size":   page_size,
                    "sort_order":  [{"order_by": "post_date",
                                     "sort_direction": "descending"}],
                },
            }
            try:
                r = requests.post(SIMPLER_URL, json=payload,
                                  headers=headers, timeout=30)
                r.raise_for_status()
            except requests.RequestException as e:
                print(f"[simpler] Request error for keyword '{kw}': {e}",
                      file=sys.stderr)
                break

            body  = r.json()
            hits  = body.get("data", [])
            total = body.get("pagination_info", {}).get("total_records", 0)

            for h in hits:
                oid = h.get("opportunity_id")
                if oid and oid not in seen:
                    seen.add(oid)
                    summary = (h.get("summary") or {})
                    records.append({
                        "source":              "simpler",
                        "opportunity_id":      oid,
                        "opportunity_number":  h.get("opportunity_number", ""),
                        "opportunity_title":   h.get("opportunity_title", ""),
                        "agency_name":         h.get("agency_name", ""),
                        "post_date":           summary.get("post_date", ""),
                        "close_date":          summary.get("close_date", ""),
                        "award_floor":         summary.get("award_floor", ""),
                        "award_ceiling":       summary.get("award_ceiling", ""),
                        "summary_description": summary.get("summary_description", ""),
                        "opportunity_status":  h.get("opportunity_status", ""),
                        "url": (f"https://simpler.grants.gov/opportunities/{oid}"),
                    })

            fetched = (page - 1) * page_size + len(hits)
            if fetched >= min(total, max_results) or not hits:
                break
            page += 1
            time.sleep(1.5)   # 40 req/min ceiling

    return records


# ── SAM.gov (contract opportunities / STTR / SBIR) ───────────────────────────

# SAM.gov procurement type codes (comma-separate multiples via --sam-ptype)
# p  pre-solicitation     o  solicitation        k  SBIR/STTR
# a  award notice         s  special notice       r  sources sought
SAM_PTYPE_HELP = "p=pre-sol, o=solicitation, k=SBIR/STTR, a=award, s=special, r=sources-sought"

def _sam_get_with_retry(
    params: dict,
    retries: int = 4,
    backoff: float = 2.0,
) -> requests.Response:
    """GET SAM_URL with exponential backoff on 429."""
    delay = backoff
    for attempt in range(retries + 1):
        r = requests.get(SAM_URL, params=params, timeout=30)
        if r.status_code != 429 or attempt == retries:
            r.raise_for_status()
            return r
        print(
            f"[sam] 429 rate-limited; retrying in {delay:.0f}s "
            f"(attempt {attempt + 1}/{retries})",
            file=sys.stderr,
        )
        time.sleep(delay)
        delay *= 2
    return r  # unreachable, but satisfies type checkers


def fetch_sam(keywords: list[str], api_key: str,
              ptype: str = "o,k",
              posted_from: str = "01/01/2023",
              posted_to:   Optional[str] = None,
              max_results: int = 500) -> list[dict]:
    """
    Pull from api.sam.gov/prod/opportunities/v2/search.

    SAM.gov does NOT support free-text keyword search via the API — only title
    search. Strategy: iterate keywords as title queries; client-side filter
    then broadens coverage using description text returned in the payload.

    Rate limits:
      - 10 req/day  (unauthenticated / unregistered)
      - 1,000/day   (registered SAM.gov entity account)
    """
    from datetime import datetime
    if posted_to is None:
        posted_to = datetime.now().strftime("%m/%d/%Y")

    ptypes = [p.strip() for p in ptype.split(",")]
    seen, records = set(), []
    page_size = 25

    for kw in keywords:
        for pt in ptypes:
            offset = 0
            while True:
                params = {
                    "api_key":    api_key,
                    "title":      kw,         # server-side: title only
                    "ptype":      pt,
                    "postedFrom": posted_from,
                    "postedTo":   posted_to,
                    "limit":      page_size,
                    "offset":     offset,
                }
                try:
                    r = _sam_get_with_retry(params)
                except requests.RequestException as e:
                    print(
                        f"[sam] Request error "
                        f"(kw='{kw}', ptype={pt}): {e}",
                        file=sys.stderr,
                    )
                    break

                body  = r.json()
                total = body.get("totalRecords", 0)
                hits  = body.get("opportunitiesData", [])

                for h in hits:
                    nid = h.get("noticeId")
                    if nid and nid not in seen:
                        seen.add(nid)
                        poc   = (h.get("pointOfContact") or [{}])[0]
                        award = h.get("award") or {}
                        records.append({
                            "source":              "sam",
                            "opportunity_id":      nid,
                            "opportunity_number":  h.get("solicitationNumber", ""),
                            "opportunity_title":   h.get("title", ""),
                            "agency_name":         h.get("fullParentPathName", ""),
                            "post_date":           h.get("postedDate", ""),
                            "close_date":          h.get("responseDeadLine", ""),
                            "award_floor":         "",
                            "award_ceiling":       award.get("amount", ""),
                            "summary_description": h.get("description", ""),
                            "opportunity_status":  h.get("active", ""),
                            "naics_code":          h.get("naicsCode", ""),
                            "set_aside":           h.get("typeOfSetAsideDescription", ""),
                            "notice_type":         h.get("type", ""),
                            "contact_email":       poc.get("email", ""),
                            "url": (f"https://sam.gov/opp/{nid}/view"),
                        })

                offset += page_size
                if offset >= min(total, max_results) or not hits:
                    break
                time.sleep(1.5)

    return records


# ── Client-side keyword filter ────────────────────────────────────────────────

def keyword_filter(records: list[dict], keywords: list[str],
                   logic: str = "OR") -> list[dict]:
    """
    Re-filter records locally using AND / OR / PHRASE logic.
    Searches across SEARCH_FIELDS (case-insensitive).

    logic:
      OR     – at least one keyword present
      AND    – all keywords present
      PHRASE – treat the joined keywords as an exact phrase
    """
    patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords]
    if logic == "PHRASE":
        phrase = re.compile(re.escape(" ".join(keywords)), re.IGNORECASE)

    filtered = []
    for rec in records:
        corpus = " ".join(str(rec.get(f, "")) for f in SEARCH_FIELDS)

        if logic == "OR":
            match = any(p.search(corpus) for p in patterns)
        elif logic == "AND":
            match = all(p.search(corpus) for p in patterns)
        elif logic == "PHRASE":
            match = bool(phrase.search(corpus))
        else:
            raise ValueError(f"Unknown logic: {logic}")

        if match:
            # annotate which keywords were found
            found = [kw for kw, p in zip(keywords, patterns)
                     if p.search(corpus)]
            rec["matched_keywords"] = "; ".join(found)
            filtered.append(rec)

    return filtered


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Search grants.gov by keyword and export results.")
    p.add_argument("--keywords", nargs="+", required=True,
                   help="Keywords to search (space-separated, quote phrases).")
    p.add_argument("--logic", choices=["OR", "AND", "PHRASE"], default="OR",
                   help="Boolean logic for client-side filter (default: OR).")
    p.add_argument("--status",
                   choices=["posted", "forecasted", "closed", "archived"],
                   default="posted",
                   help="Opportunity status filter (default: posted).")
    p.add_argument("--max-results", type=int, default=200,
                   help="Maximum records to fetch per keyword per API.")
    p.add_argument("--api",
                   choices=["legacy", "simpler", "sam", "both", "all"],
                   default="legacy",
                   help=("Which API(s) to query. 'both'=legacy+simpler, "
                         "'all'=legacy+simpler+sam. Default: legacy."))
    p.add_argument("--simpler-key", default=None,
                   help="API key for simpler.grants.gov.")
    p.add_argument("--sam-key", default=None,
                   help="API key for sam.gov (profile → Request API Key).")
    p.add_argument("--sam-ptype", default="o,k",
                   help=(f"SAM.gov procurement type codes [{SAM_PTYPE_HELP}]. "
                         "Default: 'o,k' (solicitations + SBIR/STTR)."))
    p.add_argument("--sam-posted-from", default="01/01/2023",
                   metavar="MM/DD/YYYY",
                   help="SAM.gov posted-from date filter (default: 01/01/2023).")
    p.add_argument("--sam-posted-to", default=None,
                   metavar="MM/DD/YYYY",
                   help="SAM.gov posted-to date (default: today).")
    p.add_argument("--output", default="grants_results.csv",
                   help="Output CSV path (default: grants_results.csv).")
    p.add_argument("--no-client-filter", action="store_true",
                   help="Skip client-side re-filtering; export all API hits.")
    return p.parse_args()


def main():
    args = parse_args()

    use_simpler = args.api in ("simpler", "both", "all")
    use_sam     = args.api in ("sam", "all")
    use_legacy  = args.api in ("legacy", "both", "all")

    if use_simpler and not args.simpler_key:
        sys.exit("Error: --simpler-key required when using simpler.grants.gov.")
    if use_sam and not args.sam_key:
        sys.exit("Error: --sam-key required when using sam.gov. "
                 "Get a free key at sam.gov → profile → Request API Key.")

    all_records: list[dict] = []

    if use_legacy:
        print(f"[legacy] Querying {len(args.keywords)} keyword(s)…")
        recs = fetch_legacy(args.keywords, args.status, args.max_results)
        print(f"[legacy] {len(recs)} unique records retrieved.")
        all_records.extend(recs)

    if use_simpler:
        print(f"[simpler] Querying {len(args.keywords)} keyword(s)…")
        recs = fetch_simpler(args.keywords, args.simpler_key,
                             args.status, args.max_results)
        print(f"[simpler] {len(recs)} unique records retrieved.")
        all_records.extend(recs)

    if use_sam:
        print(f"[sam] Querying {len(args.keywords)} keyword(s) "
              f"(ptype={args.sam_ptype})…")
        recs = fetch_sam(
            args.keywords, args.sam_key,
            ptype=args.sam_ptype,
            posted_from=args.sam_posted_from,
            posted_to=args.sam_posted_to,
            max_results=args.max_results,
        )
        print(f"[sam] {len(recs)} unique records retrieved.")
        all_records.extend(recs)

    if not all_records:
        print("No records retrieved. Check connectivity / API keys.")
        return

    # Deduplicate across APIs by opportunity_number (fall back to title+agency)
    before = len(all_records)
    df = pd.DataFrame(all_records)
    df["_dedup_key"] = df["opportunity_number"].where(
        df["opportunity_number"].str.strip() != "",
        df["opportunity_title"].str.strip() + "|" + df["agency_name"].str.strip()
    )
    df = df.drop_duplicates(subset=["_dedup_key"], keep="first").drop(
        columns=["_dedup_key"])
    print(f"After cross-API dedup: {len(df)} records "
          f"(removed {before - len(df)} duplicates).")

    # Client-side keyword filter
    if not args.no_client_filter:
        filtered = keyword_filter(df.to_dict("records"),
                                  args.keywords, args.logic)
        df = pd.DataFrame(filtered)
        print(f"After client-side filter ({args.logic}): {len(df)} records.")
    else:
        df["matched_keywords"] = ""

    if df.empty:
        print("No records match the keyword filter.")
        return

    df.to_csv(args.output, index=False)
    print(f"\nSaved {len(df)} records → {args.output}")
    print(df[["source", "opportunity_title", "agency_name",
              "close_date", "matched_keywords"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()
