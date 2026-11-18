"""
Procore CAC Variance - DISCOVERY RUN (read-only, never POSTs anywhere)

Answers the go/no-go questions before we build the real weekly sync:
  1. Which budget / change event / commitment tables are in the share?
  2. Do the budget-view columns (Current CAC, Previous CAC, GC Risk, ...)
     come through, and under exactly what names?
  3. What does a Nexton budget line look like structurally, and how many
     budget snapshots exist for it?

Safe for a public repo log: prints table names, column names, JSON keys,
row counts, snapshot dates and config labels. It never prints dollar amounts.
"""

import json
import os
import tempfile

import delta_sharing
import pandas as pd

ENDPOINT = os.environ["PROCORE_DS_ENDPOINT"]
TOKEN = os.environ["PROCORE_DS_TOKEN"]
SHARE = os.environ["PROCORE_DS_SHARE"]
PROJECT_ID = int(os.environ.get("PROJECT_ID") or "736678")  # Nexton
SCHEMA = "public"

# The 15 columns on the Nexton budget view, as named in the Procore UI
TARGET_COLUMNS = [
    "Original Estimate", "Original Commitment", "Buyout Commitment",
    "Adjustment Commitment", "Buyout Savings/Loss", "Owner CO", "Pending CO",
    "Internal CO", "Estimated COs", "Revised Commitment", "JTD Cost",
    "Current CAC", "Previous CAC", "CAC Variance", "GC Risk",
]

# Tables worth inspecting at all
KEYWORDS = (
    "budget", "change_event", "change_order", "commitment", "contract",
    "cost_code", "snapshot", "forecast", "direct_cost", "wbs", "line_item_type",
)

# Tables worth counting Nexton rows in (full loads, so keep this tight)
PRIORITY = (
    "budget_snapshot", "budget_line", "budget_change", "budget_view",
    "change_event", "commitment_change_order",
)

SNAPSHOT_TABLE = "budget_snapshot_line_item_headers"
SNAPSHOT_JSON_COL = "line_items"

# ---------------------------------------------------------------- helpers

fd, profile_path = tempfile.mkstemp(suffix=".share")
with os.fdopen(fd, "w") as f:
    json.dump({"shareCredentialsVersion": 1, "endpoint": ENDPOINT,
               "bearerToken": TOKEN}, f)


def url(table):
    return f"{profile_path}#{SHARE}.{SCHEMA}.{table}"


def section(title):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def project_hint():
    return json.dumps({"op": "equal", "children": [
        {"op": "column", "name": "project_id", "valueType": "long"},
        {"op": "literal", "value": str(PROJECT_ID), "valueType": "long"},
    ]})


_project_cache = {}


def load_project(table):
    """All rows for PROJECT_ID. Predicate hint is best-effort; we filter after."""
    if table in _project_cache:
        return _project_cache[table]
    try:
        df = delta_sharing.load_as_pandas(url(table), jsonPredicateHints=project_hint())
    except TypeError:  # older library without predicate hints
        df = delta_sharing.load_as_pandas(url(table))
    if "project_id" in df.columns:
        df = df[pd.to_numeric(df["project_id"], errors="coerce") == PROJECT_ID]
    _project_cache[table] = df
    return df


def match_targets(names, label):
    lookup = {norm(n): n for n in names}
    print(f"\nTarget column check against {label}:")
    exact = 0
    for t in TARGET_COLUMNS:
        hit = lookup.get(norm(t))
        if hit:
            exact += 1
            print(f"  FOUND    {t:<22} -> {hit}")
            continue
        loose = [n for k, n in lookup.items() if norm(t) in k or k in norm(t)]
        if loose:
            print(f"  ~maybe   {t:<22} -> {', '.join(loose[:3])}")
        else:
            print(f"  missing  {t}")
    print(f"  {exact}/{len(TARGET_COLUMNS)} exact matches")
    return exact


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
    """Replace every value with its type so structure prints but amounts don't."""
    if isinstance(o, dict):
        return {k: mask(v) for k, v in o.items()}
    if isinstance(o, list):
        return ([mask(o[0])] + (["..."] if len(o) > 1 else [])) if o else []
    return type(o).__name__


# ------------------------------------------------------------------- run

section("0. Environment")
print(f"delta-sharing {getattr(delta_sharing, '__version__', '?')}, pandas {pd.__version__}")
print(f"Project ID: {PROJECT_ID}")

section("1. Relevant tables in the share")
client = delta_sharing.SharingClient(profile_path)
names = sorted({t.name for t in client.list_all_tables() if t.schema == SCHEMA})
relevant = [n for n in names if any(k in n for k in KEYWORDS)]
print(f"{len(names)} tables in schema '{SCHEMA}', {len(relevant)} match budget keywords:")
for n in relevant:
    print(f"   {n}")

section("2. Column lists (1-row sample, values not printed)")
columns_by_table = {}
for t in relevant:
    try:
        df = delta_sharing.load_as_pandas(url(t), limit=1)
        columns_by_table[t] = list(df.columns)
        print(f"\n--- {t} ({len(df.columns)} cols)")
        for c, d in df.dtypes.items():
            print(f"    {c:<48} {d}")
    except Exception as e:
        print(f"\n--- {t}: ERROR {type(e).__name__}: {str(e)[:200]}")

section(f"3. Row counts for project {PROJECT_ID}")
for t in relevant:
    cols = columns_by_table.get(t, [])
    if "project_id" not in cols or not any(p in t for p in PRIORITY):
        continue
    try:
        print(f"   {t:<48} {len(load_project(t)):>7} rows")
    except Exception as e:
        print(f"   {t:<48} ERROR {type(e).__name__}: {str(e)[:150]}")

section(f"4. Budget snapshots ({SNAPSHOT_TABLE})")
if SNAPSHOT_TABLE not in columns_by_table:
    print("   Table not in share.")
else:
    hdr = load_project(SNAPSHOT_TABLE)
    print(f"   {len(hdr)} rows for this project")
    meta_cols = [c for c in hdr.columns
                 if c != SNAPSHOT_JSON_COL
                 and any(k in c for k in ("snapshot", "view", "created", "date"))]
    for c in meta_cols:
        vals = hdr[c].dropna().astype(str).value_counts().sort_index(ascending=False)
        print(f"\n   {c}: {len(vals)} distinct (newest 15, with line counts)")
        for v, n in vals.head(15).items():
            print(f"      {v:<40} {n}")
    if SNAPSHOT_JSON_COL in hdr.columns and len(hdr):
        keys = json_keys(hdr[SNAPSHOT_JSON_COL])
        print(f"\n   Keys inside '{SNAPSHOT_JSON_COL}' JSON ({len(keys)}):")
        for k in keys:
            print(f"      {k}")
        match_targets(keys, f"{SNAPSHOT_TABLE}.{SNAPSHOT_JSON_COL}")
        sample = parse_json(hdr[SNAPSHOT_JSON_COL].dropna().iloc[0])
        print("\n   Structure of one value (types only):")
        print("   " + json.dumps(mask(sample), indent=2)[:3000].replace("\n", "\n   "))

section("5. Live budget table columns vs. target list")
for t in ("budget_line_items", "budget_lines"):
    if t in columns_by_table:
        match_targets(columns_by_table[t], t)

section("6. Budget view / column configuration tables")
config_tables = [t for t in relevant if "budget" in t and "snapshot" not in t
                 and any(k in t for k in ("view", "column", "formula"))]
if not config_tables:
    print("   None found. We'll need the column formulas from the Procore UI.")
for t in config_tables:
    try:
        df = delta_sharing.load_as_pandas(url(t))
        text_cols = [c for c in df.columns if df[c].dtype == object]
        print(f"\n--- {t}: {len(df)} rows; text columns shown")
        with pd.option_context("display.max_colwidth", 80, "display.width", 250):
            print(df[text_cols].head(80).to_string(index=False))
    except Exception as e:
        print(f"\n--- {t}: ERROR {type(e).__name__}: {str(e)[:200]}")

section("Done. Paste this whole log back to Claude.")
