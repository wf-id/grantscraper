#!/usr/bin/env python3
"""
build_report.py
───────────────
Reads search_config.yaml, runs grants_search functions for each keyword group,
merges results, and writes docs/index.html — a self-contained searchable page
suitable for GitHub Pages.

Run directly:
    python scripts/build_report.py \
        [--simpler-key KEY] [--sam-key KEY] [--config search_config.yaml]

Called by the GitHub Action automatically via environment variables:
    SIMPLER_API_KEY, SAM_API_KEY
"""

import argparse
import html
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

# Allow importing from sibling scripts/ dir or repo root
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from grants_search import (
    fetch_legacy, fetch_simpler, fetch_sam, keyword_filter
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_date(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def days_until(s: str) -> int | None:
    d = parse_date(s)
    if d is None:
        return None
    return (d - date.today()).days


def deadline_badge(close_str: str, warn: int, crit: int) -> str:
    n = days_until(close_str)
    if n is None:
        return '<span class="badge badge-none">No deadline</span>'
    if n < 0:
        return f'<span class="badge badge-closed">Closed {-n}d ago</span>'
    if n <= crit:
        return f'<span class="badge badge-critical">Due in {n}d</span>'
    if n <= warn:
        return f'<span class="badge badge-warn">Due in {n}d</span>'
    return f'<span class="badge badge-ok">Due in {n}d</span>'


def source_badge(src: str) -> str:
    labels = {
        "legacy":  ("Grants.gov", "#2563eb"),
        "simpler": ("Simpler.Grants", "#7c3aed"),
        "sam":     ("SAM.gov", "#b45309"),
    }
    label, color = labels.get(src, (src, "#6b7280"))
    return (f'<span class="source-badge" '
            f'style="background:{color}">{label}</span>')


def status_badge(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "forecasted":
        return ('<span class="status-badge status-forecast">'
                'Forecasted</span>')
    if s == "posted":
        return ('<span class="status-badge status-posted">'
                'Posted</span>')
    return (f'<span class="status-badge status-other">'
            f'{html.escape(status or "Unknown")}</span>')


def esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


# ── Data collection ───────────────────────────────────────────────────────────

def collect(cfg: dict, simpler_key: str | None, sam_key: str | None) -> pd.DataFrame:
    api_cfg   = cfg.get("api", {})
    use_leg   = api_cfg.get("use_legacy",  True)
    use_sim   = api_cfg.get("use_simpler", True) and bool(simpler_key)
    use_sam   = api_cfg.get("use_sam",     True) and bool(sam_key)
    max_res   = int(api_cfg.get("max_results", 200))
    status    = api_cfg.get("grants_status", "posted")
    ptype     = api_cfg.get("sam_ptype", "o,k")
    pfrom     = api_cfg.get("sam_posted_from", "01/01/2023")
    sam_kws   = api_cfg.get("sam_keywords")

    all_records: list[dict] = []
    seen_ids: set = set()

    # If SAM uses its own keyword list, run it once up front
    sam_records: list[dict] = []
    if use_sam and sam_kws:
        print("\n── SAM.gov (dedicated keywords) ──")
        sam_records = fetch_sam(
            sam_kws, sam_key, ptype=ptype,
            posted_from=pfrom, max_results=max_res,
        )
        print(f"  [sam]     {len(sam_records)} records")
        filtered_sam = keyword_filter(
            sam_records, sam_kws, "OR",
        )
        print(
            f"  → {len(filtered_sam)} after "
            f"client-side OR filter"
        )
        for r in filtered_sam:
            r["keyword_group"] = "SAM.gov"
            key = (
                r.get("opportunity_number")
                or r["opportunity_title"]
                + "|" + r.get("agency_name", "")
            )
            if key not in seen_ids:
                seen_ids.add(key)
                all_records.append(r)

    for group in cfg.get("keyword_groups", []):
        label    = group["label"]
        keywords = group["keywords"]
        logic    = group.get("logic", "OR")

        print(f"\n── Group: {label} ({logic}) ──")
        group_records: list[dict] = []

        if use_leg:
            recs = fetch_legacy(keywords, status, max_res)
            print(f"  [legacy]  {len(recs)} records")
            group_records.extend(recs)

        if use_sim:
            recs = fetch_simpler(
                keywords, simpler_key, status, max_res,
            )
            print(f"  [simpler] {len(recs)} records")
            group_records.extend(recs)

        # Only use per-group SAM when no dedicated list
        if use_sam and not sam_kws:
            recs = fetch_sam(
                keywords, sam_key, ptype=ptype,
                posted_from=pfrom, max_results=max_res,
            )
            print(f"  [sam]     {len(recs)} records")
            group_records.extend(recs)

        # Client-side filter within group
        filtered = keyword_filter(group_records, keywords, logic)
        print(f"  → {len(filtered)} after client-side {logic} filter")

        for r in filtered:
            r["keyword_group"] = label
            # dedup key across all groups
            key = (r.get("opportunity_number") or
                   r["opportunity_title"] + "|" + r.get("agency_name", ""))
            if key not in seen_ids:
                seen_ids.add(key)
                all_records.append(r)

    if not all_records:
        return pd.DataFrame()

    df = pd.DataFrame(all_records)
    # Sort: soonest deadline first, then by group
    df["_days"] = df["close_date"].apply(days_until)
    df = df.sort_values(["_days", "keyword_group"],
                        na_position="last").drop(columns=["_days"])
    return df


# ── HTML generation ───────────────────────────────────────────────────────────

def build_html(df: pd.DataFrame, cfg: dict) -> str:
    pg      = cfg.get("page", {})
    title   = pg.get("title", "Open Funding Opportunities")
    sub     = pg.get("subtitle", "")
    warn    = int(pg.get("deadline_warn_days", 30))
    crit    = int(pg.get("deadline_critical_days", 14))
    updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    n_total = len(df)

    # Collect filter options
    groups   = sorted(df["keyword_group"].unique().tolist()) if not df.empty else []
    sources  = sorted(df["source"].unique().tolist())       if not df.empty else []
    statuses = sorted(
        df["opportunity_status"].str.strip().str.lower()
        .unique().tolist()
    ) if not df.empty else []

    # Serialise rows to JSON for client-side search
    rows_json = json.dumps(df.fillna("").to_dict(orient="records"),
                           default=str)

    cards_html = ""
    if df.empty:
        cards_html = '<p class="empty">No opportunities matched the search criteria.</p>'
    else:
        for _, row in df.iterrows():
            kw_tags = " ".join(
                f'<span class="kw-tag">{esc(k)}</span>'
                for k in str(row.get("matched_keywords", "")).split("; ")
                if k.strip()
            )
            desc = str(row.get("summary_description", "") or "")
            desc_short = esc(desc[:300] + ("…" if len(desc) > 300 else ""))

            naics = row.get("naics_code", "")
            naics_html = (f'<span class="meta-item">NAICS {esc(naics)}</span>'
                          if naics else "")
            setaside = row.get("set_aside", "")
            setaside_html = (f'<span class="meta-item">{esc(setaside)}</span>'
                             if setaside else "")
            award_ceil = row.get("award_ceiling", "")
            award_html = ""
            if award_ceil:
                try:
                    award_html = (f'<span class="meta-item">'
                                  f'Up to ${float(award_ceil):,.0f}</span>')
                except (ValueError, TypeError):
                    award_html = f'<span class="meta-item">{esc(award_ceil)}</span>'

            cards_html += f"""
      <div class="card"
           data-group="{esc(row.get('keyword_group',''))}"
           data-source="{esc(row.get('source',''))}"
           data-status="{esc(str(row.get('opportunity_status','')).strip().lower())}"
           data-title="{esc(row.get('opportunity_title',''))}"
           data-agency="{esc(row.get('agency_name',''))}"
           data-keywords="{esc(row.get('matched_keywords',''))}">
        <div class="card-header">
          {source_badge(str(row.get('source','')))}
          {status_badge(str(row.get('opportunity_status','')))}
          {deadline_badge(str(row.get('close_date','')), warn, crit)}
          <span class="group-label">{esc(row.get('keyword_group',''))}</span>
        </div>
        <h3 class="card-title">
          <a href="{esc(row.get('url','#'))}" target="_blank" rel="noopener">
            {esc(row.get('opportunity_title','(untitled)'))}
          </a>
        </h3>
        <div class="card-agency">{esc(row.get('agency_name',''))}</div>
        <div class="card-meta">
          <span class="meta-item">Posted: {esc(row.get('post_date',''))}</span>
          <span class="meta-item">Closes: {esc(row.get('close_date','—'))}</span>
          {naics_html}{setaside_html}{award_html}
        </div>
        <p class="card-desc">{desc_short}</p>
        <div class="kw-tags">{kw_tags}</div>
      </div>"""

    group_opts = "\n".join(
        f'<option value="{esc(g)}">{esc(g)}</option>' for g in groups)
    source_opts = "\n".join(
        f'<option value="{esc(s)}">{esc(s).capitalize()}</option>'
        for s in sources)
    status_opts = "\n".join(
        f'<option value="{esc(s)}">{esc(s).capitalize()}</option>'
        for s in statuses)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #f8fafc; color: #1e293b; line-height: 1.5;
    }}
    header {{
      background: #0f172a; color: #f1f5f9; padding: 1.5rem 2rem;
      border-bottom: 3px solid #2563eb;
    }}
    header h1 {{ font-size: 1.5rem; font-weight: 700; }}
    header p  {{ font-size: 0.9rem; color: #94a3b8; margin-top: .25rem; }}
    .meta-bar {{
      background: #1e293b; color: #94a3b8; padding: .5rem 2rem;
      font-size: .8rem; display: flex; gap: 1.5rem; align-items: center;
    }}
    .meta-bar strong {{ color: #f1f5f9; }}

    /* Controls */
    .controls {{
      display: flex; flex-wrap: wrap; gap: .75rem;
      padding: 1rem 2rem; background: #fff;
      border-bottom: 1px solid #e2e8f0; position: sticky; top: 0; z-index: 10;
      box-shadow: 0 1px 4px rgba(0,0,0,.06);
    }}
    .controls input, .controls select {{
      padding: .45rem .75rem; border: 1px solid #cbd5e1;
      border-radius: 6px; font-size: .875rem; background: #f8fafc;
      color: #1e293b;
    }}
    .controls input {{ flex: 1; min-width: 220px; }}
    #result-count {{ font-size: .85rem; color: #64748b; align-self: center; }}

    /* Cards */
    #card-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(380px, 1fr));
      gap: 1rem; padding: 1.25rem 2rem;
    }}
    .card {{
      background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
      padding: 1rem 1.1rem; display: flex; flex-direction: column; gap: .5rem;
      transition: box-shadow .15s;
    }}
    .card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,.09); }}
    .card-header {{ display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; }}
    .card-title {{ font-size: .95rem; font-weight: 600; }}
    .card-title a {{ color: #1d4ed8; text-decoration: none; }}
    .card-title a:hover {{ text-decoration: underline; }}
    .card-agency {{ font-size: .8rem; color: #64748b; }}
    .card-meta {{
      display: flex; flex-wrap: wrap; gap: .4rem;
      font-size: .75rem; color: #475569;
    }}
    .meta-item {{
      background: #f1f5f9; border-radius: 4px; padding: .15rem .45rem;
    }}
    .card-desc {{ font-size: .8rem; color: #475569; }}
    .kw-tags {{ display: flex; flex-wrap: wrap; gap: .3rem; margin-top: .2rem; }}
    .kw-tag {{
      background: #eff6ff; color: #1d4ed8; border: 1px solid #bfdbfe;
      border-radius: 4px; padding: .1rem .4rem; font-size: .72rem;
    }}

    /* Badges */
    .source-badge, .badge, .group-label {{
      display: inline-block; border-radius: 4px;
      padding: .15rem .5rem; font-size: .72rem; font-weight: 600;
      color: #fff;
    }}
    .badge-critical {{ background: #dc2626; }}
    .badge-warn     {{ background: #d97706; }}
    .badge-ok       {{ background: #16a34a; }}
    .badge-closed   {{ background: #6b7280; }}
    .badge-none     {{ background: #94a3b8; }}
    .group-label    {{ background: #64748b; font-weight: 500; }}
    .status-badge   {{ display: inline-block; border-radius: 4px; padding: .15rem .5rem; font-size: .72rem; font-weight: 600; color: #fff; }}
    .status-forecast {{ background: #8b5cf6; }}
    .status-posted   {{ background: #059669; }}
    .status-other    {{ background: #6b7280; }}

    .empty {{ padding: 3rem 2rem; color: #94a3b8; text-align: center; }}
    footer {{
      text-align: center; padding: 1.5rem; font-size: .78rem;
      color: #94a3b8; border-top: 1px solid #e2e8f0;
    }}
  </style>
</head>
<body>
<header>
  <h1>{esc(title)}</h1>
  <p>{esc(sub)}</p>
</header>
<div class="meta-bar">
  <span>Last updated: <strong>{updated}</strong></span>
  <span>Total opportunities: <strong>{n_total}</strong></span>
  <span>Sources: grants.gov (legacy &amp; simpler) · SAM.gov</span>
</div>

<div class="controls">
  <input type="search" id="search-box" placeholder="Search title, agency, keyword…" autocomplete="off">
  <select id="filter-group">
    <option value="">All groups</option>
    {group_opts}
  </select>
  <select id="filter-source">
    <option value="">All sources</option>
    {source_opts}
  </select>
  <select id="filter-status">
    <option value="">All statuses</option>
    {status_opts}
  </select>
  <select id="sort-by">
    <option value="deadline">Sort: soonest deadline</option>
    <option value="posted">Sort: most recent</option>
    <option value="title">Sort: title A–Z</option>
  </select>
  <span id="result-count"></span>
</div>

<div id="card-grid">
{cards_html}
</div>

<footer>
  Generated automatically by
  <a href="https://github.com" target="_blank">GitHub Actions</a> ·
  Data from grants.gov and SAM.gov (public APIs) ·
  Not affiliated with any government agency.
</footer>

<script>
const RAW = {rows_json};

// Parse and store deadline days for sorting
const data = RAW.map(r => ({{
  ...r,
  _days: (() => {{
    const s = r.close_date || "";
    if (!s) return 99999;
    const d = new Date(s);
    if (isNaN(d)) return 99999;
    return Math.round((d - Date.now()) / 86400000);
  }})()
}}));

const grid    = document.getElementById("card-grid");
const cards   = Array.from(grid.querySelectorAll(".card"));
const counter = document.getElementById("result-count");

function normalize(s) {{
  return (s || "").toLowerCase();
}}

function render() {{
  const q      = normalize(document.getElementById("search-box").value);
  const group  = document.getElementById("filter-group").value;
  const source = document.getElementById("filter-source").value;
  const status = document.getElementById("filter-status").value;
  const sortBy = document.getElementById("sort-by").value;

  // Filter
  let visible = cards.filter(card => {{
    if (group  && card.dataset.group  !== group)  return false;
    if (source && card.dataset.source !== source) return false;
    if (status && card.dataset.status !== status) return false;
    if (q) {{
      const hay = [card.dataset.title, card.dataset.agency,
                   card.dataset.keywords].map(normalize).join(" ");
      if (!hay.includes(q)) return false;
    }}
    return true;
  }});

  // Sort
  const idxMap = new Map(data.map((r, i) => [
    (r.opportunity_title || "") + "|" + (r.agency_name || ""), i
  ]));

  visible.sort((a, b) => {{
    const ai = idxMap.get(a.dataset.title + "|" + a.dataset.agency) ?? 0;
    const bi = idxMap.get(b.dataset.title + "|" + b.dataset.agency) ?? 0;
    const ad = data[ai], bd = data[bi];
    if (sortBy === "deadline") return (ad._days ?? 99999) - (bd._days ?? 99999);
    if (sortBy === "posted")   return (bd.post_date || "").localeCompare(ad.post_date || "");
    if (sortBy === "title")    return (ad.opportunity_title || "").localeCompare(bd.opportunity_title || "");
    return 0;
  }});

  // Show/hide
  const hiddenSet = new Set(cards);
  visible.forEach(c => hiddenSet.delete(c));
  hiddenSet.forEach(c => {{ c.style.display = "none"; }});
  visible.forEach(c  => {{ c.style.display = "";      grid.appendChild(c); }});

  counter.textContent = `${{visible.length}} of ${{cards.length}} shown`;
}}

["search-box","filter-group","filter-source","filter-status","sort-by"].forEach(id => {{
  document.getElementById(id).addEventListener("input", render);
}});

render();
</script>
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config",      default="search_config.yaml")
    p.add_argument("--simpler-key", default=os.environ.get("SIMPLER_API_KEY"))
    p.add_argument("--sam-key",     default=os.environ.get("SAM_API_KEY"))
    p.add_argument("--out-dir",     default="_report")
    return p.parse_args()


def main():
    args = parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    api_cfg = cfg.get("api", {})
    if api_cfg.get("use_simpler") and not args.simpler_key:
        print("Warning: use_simpler=true but no SIMPLER_API_KEY; skipping.",
              file=sys.stderr)
    if api_cfg.get("use_sam") and not args.sam_key:
        print("Warning: use_sam=true but no SAM_API_KEY; skipping.",
              file=sys.stderr)

    df = collect(cfg, args.simpler_key, args.sam_key)
    print(f"\nTotal unique opportunities: {len(df)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write HTML
    html_out = out_dir / "index.html"
    html_out.write_text(build_html(df, cfg), encoding="utf-8")
    print(f"Report written → {html_out}")

    # Also write CSV for download / audit
    if not df.empty:
        csv_out = out_dir / "opportunities.csv"
        df.to_csv(csv_out, index=False)
        print(f"CSV written    → {csv_out}")


if __name__ == "__main__":
    main()
