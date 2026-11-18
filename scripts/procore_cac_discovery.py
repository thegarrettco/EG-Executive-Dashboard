"""
Procore CAC Variance - DISCOVERY RUN v2 (read-only, never POSTs anywhere)

v1 found every table we need but returned 0 rows for project 736678 in all
of them. v2:
  A. Looks Nexton up in the `projects` table to get the ID the share uses.
  B. Tests each ID with and without the server-side filter, so we know
     whether the ID or the filter was the problem.
  C-F. For the right ID: budget view names, the column names on those views,
     and the JSON keys inside the live budget view and the snapshots.

Log safety: values are printed ONLY from explicit allowlists of ID, name,
date and status columns. Amount columns are never printed. JSON is shown
as keys and value types only.
"""

import json
import os
import tempfile

import delta_sharing
import pandas as pd

ENDPOINT = os.environ["PROCORE_DS_ENDPOINT"]
TOKEN = os.environ["PROCORE_DS_TOKEN"]
SHARE = os.environ["PROCORE_DS_SHARE"]
GIVEN_ID = int(os.environ.get("PROJECT_ID") or "736678")
SEARCH = (os.environ.get("PROJECT_SEARCH") or "nexton").lower()
SCHEMA = "public"

TARGET_COLUMNS = [
    "Original Estimate", "Original Commitment", "Buyout Commitment",
    "Adjustment Commitment", "Buyout Savings/Loss", "Owner CO", "Pending CO",
    "Internal CO", "Estimated COs", "Revised Commitment", "JTD Cost",
    "Current CAC", "Previous CAC", "CAC Variance", "GC Risk",
]

fd, profile_path = tempfile.mkstemp(suffix=".share")
with os.fdopen(fd, "w") as f:
    json.dump({"shareCredentialsVersion": 1, "endpoint": ENDPOINT,
               "bearerToken": TOKEN}, f)


# ---------------------------------------------------------------- helpers

def url(table):
    return f"{profile_path}#{SHARE}.{SCHEMA}.{table}"


def section(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def hint(pid):
    return json.dumps({"op": "equal", "children": [
        {"op": "column", "name": "project_id", "valueType": "long"},
        {"op": "literal", "value": str(pid), "valueType": "long"},
    ]})


def match_pid(df, pid):
    if "project_id" not in df.columns:
        return df
    return df[pd.to_numeric(df["project_id"], errors="coerce") == pid]


def load_all(table):
    return delta_sharing.load_as_pandas(url(table))


def load_hinted(table, pid):
    try:
        df = delta_sharing.load_as_pandas(url(table), jsonPredicateHints=hint(pid))
    except TypeError:
        df = load_all(table)
    return match_pid(df, pid)


def load_project(table, pid):
    """Server filter first; if that returns nothing, load everything and match."""
    df = load_hinted(table, pid)
    if len(df) == 0:
        df = match_pid(load_all(table), pid)
    return df


def show(df, cols, limit=40):
    """Print ONLY the allowlisted columns given."""
    cols = [c for c in cols if c in df.columns]
    if df is None or df.empty or not cols:
        print("   (nothing to show)")
        return
    with pd.option_context("display.max_colwidth", 60, "display.width", 220):
        print(df[cols].head(limit).to_string(index=False))


def parse_json(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def json_keys(series, max_rows=500):
    keys = set()
    for v in series.dropna().head(max_rows):
        obj = parse_json(v)
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if isinstance(it, dict):
                keys.update(it.keys())
    return sorted(keys)


def mask(o):
    if isinstance(o, dict):
        return {k: mask(v) for k, v in o.items()}
    if isinstance(o, list):
        return ([mask(o[0])] + (["..."] if len(o) > 1 else [])) if o else []
    return type(o).__name__


def match_targets(names, label):
    lookup = {norm(n): n for n in names}
    print(f"\nTarget column check against {label}:")
    exact = 0
    for t in TARGET_COLUMNS:
        hit = lookup.get(norm(t))
        if hit:
            exact += 1
            print(f"  FOUND    {t:<22} -> {hit}")
        else:
            print(f"  missing  {t}")
    print(f"  {exact}/{len(TARGET_COLUMNS)} exact matches")


def show_json(series, label):
    if series is None or series.dropna().empty:
        print(f"   No {label} values.")
        return
    keys = json_keys(series)
    print(f"\n   Keys inside {label} ({len(keys)}):")
    for k in keys:
        print(f"      {k}")
    match_targets(keys, label)
    print(f"\n   Structure of one {label} value (types only):")
    sample = parse_json(series.dropna().iloc[0])
    print("   " + json.dumps(mask(sample), indent=2)[:3000].replace("\n", "\n   "))


# ------------------------------------------------------------------- run

section("A. Find the project in `projects`")
projects = load_all("projects")
id_col = "project_id" if "project_id" in projects.columns else (
    "id" if "id" in projects.columns else None)
SKIP = ("by", "user", "contact", "owner", "manager", "address", "email")
label_cols = [c for c in projects.columns
              if any(k in c.lower() for k in ("name", "number"))
              and not any(s in c.lower() for s in SKIP)
              and pd.api.types.is_string_dtype(projects[c])]
status_cols = [c for c in ("active", "status", "stage", "project_stage")
               if c in projects.columns]
print(f"id column: {id_col}")
print(f"label columns searched: {label_cols}")

hit_mask = pd.Series(False, index=projects.index)
for c in label_cols:
    hit_mask |= projects[c].astype(str).str.lower().str.contains(SEARCH, na=False)
hits = projects[hit_mask]
print(f"\n{len(hits)} project(s) matching '{SEARCH}':")
show(hits, ([id_col] if id_col else []) + label_cols + status_cols)

ids = pd.to_numeric(projects[id_col], errors="coerce") if id_col else pd.Series(dtype=float)
given_present = bool((ids == GIVEN_ID).any())
print(f"\nID {GIVEN_ID} present in projects table: {given_present}")

candidates = []
if id_col and len(hits):
    candidates = [int(x) for x in pd.to_numeric(hits[id_col], errors="coerce").dropna().unique()]
if given_present:
    PID = GIVEN_ID
elif candidates:
    PID = candidates[0]
else:
    PID = GIVEN_ID
print(f"Using project ID {PID} for sections C-F")

section("B. Server filter vs. no filter")
for table in ("budget_views", "change_events", "budget_view_details"):
    full = load_all(table)
    tids = pd.to_numeric(full["project_id"], errors="coerce")
    print(f"\n{table}: {len(full)} rows company-wide across {tids.nunique()} projects")
    for pid in sorted(set([GIVEN_ID] + candidates)):
        n_match = int((tids == pid).sum())
        n_hint = len(load_hinted(table, pid))
        print(f"   project {pid}: {n_match} rows by matching, {n_hint} rows via server filter")

section(f"C. Budget views for project {PID}")
views = load_project("budget_views", PID)
print(f"{len(views)} rows")
show(views, ["id", "name", "role", "description", "deleted_at", "updated_at"])

section(f"D. Live budget view rows (budget_view_details) for {PID}")
bvd = load_project("budget_view_details", PID)
print(f"{len(bvd)} rows")
tmpl_ids = set()
if len(bvd):
    tmpl_ids = set(pd.to_numeric(bvd["budget_template_id"], errors="coerce").dropna().astype(int))
    print("\nRows per budget_template_id:")
    print(bvd["budget_template_id"].value_counts().to_string())
    print("\nSample cost codes (labels only):")
    show(bvd, ["budget_template_id", "root_cost_code", "cost_code", "category"], 10)
    if "infoJson" in bvd.columns:
        show_json(bvd["infoJson"], "budget_view_details.infoJson")

section("E. Column definitions (budget_columns)")
bcols = load_all("budget_columns")
if "id" in views.columns:
    tmpl_ids |= set(pd.to_numeric(views["id"], errors="coerce").dropna().astype(int))
tcol = pd.to_numeric(bcols["budget_template_id"], errors="coerce")
sub = bcols[tcol.isin(tmpl_ids)] if tmpl_ids else bcols.iloc[0:0]
print(f"{len(sub)} column definitions on this project's templates {sorted(tmpl_ids)}")
show(sub.sort_values(["budget_template_id", "position"]),
     ["budget_template_id", "position", "name", "format", "calculable", "aggregatable"], 200)
if len(sub):
    match_targets(sub["name"].dropna().astype(str).unique(), "budget_columns.name")

targets_norm = {norm(t) for t in TARGET_COLUMNS}
company_hits = bcols[bcols["name"].astype(str).map(norm).isin(targets_norm)]
print(f"\nCompany-wide: templates that have any of the 15 column names "
      f"({company_hits['budget_template_id'].nunique()} templates):")
print(company_hits.groupby("budget_template_id")["name"].apply(
    lambda s: ", ".join(sorted(set(s)))).to_string())

section(f"F. Budget snapshots for {PID}")
hdr = load_project("budget_snapshot_line_item_headers", PID)
print(f"{len(hdr)} line rows")
if len(hdr):
    snaps = hdr.drop_duplicates("budget_snapshot_id").copy()
    snaps["weekday"] = pd.to_datetime(snaps["created_at"], errors="coerce").dt.day_name()
    snaps = snaps.sort_values("created_at", ascending=False)
    print(f"\n{len(snaps)} snapshots (newest 30):")
    show(snaps, ["budget_snapshot_id", "budget_snapshot_name", "budget_template_name",
                 "snapshot_type", "approval_status", "created_at", "weekday"], 30)
    show_json(hdr["line_items"] if "line_items" in hdr.columns else None,
              "budget_snapshot_line_item_headers.line_items")

section("Done. Paste this whole log back to Claude.")
