import os, re, json, tempfile
import requests
import delta_sharing
import pandas as pd

DS_ENDPOINT = os.environ["PROCORE_DS_ENDPOINT"]
DS_TOKEN    = os.environ["PROCORE_DS_TOKEN"]
DS_SHARE    = os.environ["PROCORE_DS_SHARE"]
WORKER_URL  = os.environ["WORKER_URL"]
SMOKE       = os.environ.get("SMOKE_TEST", "").lower() == "true"

# Delta Sharing reads credentials from a profile file (JSON)
profile = {
    "shareCredentialsVersion": 1,
    "endpoint": DS_ENDPOINT,
    "bearerToken": DS_TOKEN,
}
with tempfile.NamedTemporaryFile("w", suffix=".share", delete=False) as f:
    json.dump(profile, f)
    profile_path = f.name

# --- Diagnostic: prove auth works and show the real table names ---
print("=== Tables visible in this share ===")
client = delta_sharing.SharingClient(profile_path)
for t in client.list_all_tables():
    print(f"  {t.share}.{t.schema}.{t.name}")
print("=== end table list ===\n")

table_url = f"{profile_path}#{DS_SHARE}.public.projects"

if SMOKE:
    print("SMOKE TEST — loading 10 rows only, will not POST\n")
    df = delta_sharing.load_as_pandas(table_url, limit=10)
    print(f"Loaded {len(df)} rows, {len(df.columns)} columns")
    print("projects columns:", list(df.columns))

    # --- Custom field discovery (smoke runs only) ---
    print("\n=== project_custom_field_configs ===")
    cfg = delta_sharing.load_as_pandas(
        f"{profile_path}#{DS_SHARE}.public.project_custom_field_configs", limit=300)
    print("columns:", list(cfg.columns))
    if "custom_field_name" in cfg.columns:
        names = sorted(set(cfg["custom_field_name"].dropna().astype(str)))
        print("field names:", names)

    print("\n=== project_custom_fields ===")
    pcf = delta_sharing.load_as_pandas(
        f"{profile_path}#{DS_SHARE}.public.project_custom_fields", limit=5)
    print("columns:", list(pcf.columns))
    print()
else:
    df = delta_sharing.load_as_pandas(table_url)
    print(f"Loaded {len(df)} rows, {len(df.columns)} columns")


def clean(v):
    """NaN/NaT/None -> empty string; everything else -> trimmed str."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def as_date(v):
    """YYYY-MM-DD — the format psFmtDate on the dashboard understands."""
    s = clean(v)
    return s[:10] if s else ""


def pid_key(v):
    """Normalize project_id to a string key. Pandas turns int columns
    containing nulls into floats, so 12345 can arrive as '12345.0'."""
    s = clean(v)
    return s[:-2] if s.endswith(".0") else s


# Projects that belong in this table despite not matching the -9 rule.
# Keep in sync with TP_EXTRA_NUMBERS in index.html.
EXTRA_PROJECT_NUMBERS = {"23-011", "22-023"}


def is_third_party(number):
    n = clean(number)
    if n in EXTRA_PROJECT_NUMBERS:
        return True
    m = re.search(r"-\s*(\d)", n)
    return bool(m) and m.group(1) == "9"


def col(row, name):
    return row[name] if name in row.index else None


# --- Custom field lookups (Developer, Unit Count) ---
# Names are matched without regard to case or extra spaces.
# If Procore labels the field differently, add the name here.
DEVELOPER_FIELD_NAMES = {"developer"}
UNITS_FIELD_NAMES     = {"unit count", "units", "# of units", "number of units", "total units"}

# Fallback: a standard column on the projects table, if one exists
UNITS_COLUMN_NAMES = ["unit_count", "units", "number_of_units", "total_units"]


def field_key(v):
    return re.sub(r"\s+", " ", clean(v).lower())


def as_count(v):
    """'250', '250.0', '1,250' -> '250' / '1250'. Anything else is kept as text."""
    s = clean(v).replace(",", "")
    if not s:
        return ""
    try:
        n = float(s)
        return str(int(n)) if n.is_integer() else str(n)
    except ValueError:
        return clean(v)


print("Loading project custom field values ...")
cf_df = delta_sharing.load_as_pandas(
    f"{profile_path}#{DS_SHARE}.public.project_custom_fields")

dev_map, units_map = {}, {}
seen_field_names = set()
for _, cf in cf_df.iterrows():
    fname = field_key(cf.get("custom_field_name"))
    if fname:
        seen_field_names.add(fname)
    pid = pid_key(cf.get("project_id"))
    val = clean(cf.get("custom_field_value"))
    if not pid or not val:
        continue
    if fname in DEVELOPER_FIELD_NAMES:
        dev_map[pid] = val
    elif fname in UNITS_FIELD_NAMES:
        units_map[pid] = as_count(val)

print(f"Found Developer values for {len(dev_map)} projects")
print(f"Found Unit Count custom field values for {len(units_map)} projects")

units_column = next((c for c in UNITS_COLUMN_NAMES if c in df.columns), None)
print(f"Unit Count column on projects table: {units_column or 'none'}")

if not units_map and not units_column:
    hints = sorted(n for n in seen_field_names if "unit" in n)
    print("WARNING: no Unit Count source found. "
          f"Custom field names containing 'unit': {hints or 'none'}")


rows = []
for _, r in df.iterrows():
    number = clean(col(r, "project_number"))
    if not is_third_party(number):
        continue
    if col(r, "is_demo") is True:
        continue
    if col(r, "is_active") is False:
        continue

    name = clean(col(r, "name"))
    if not name:
        continue

    start = as_date(col(r, "actual_start_date")) or as_date(col(r, "estimated_start_date"))
    finish = (
        as_date(col(r, "actual_completion_date"))
        or as_date(col(r, "projected_finish_date"))
        or as_date(col(r, "estimated_completion_date"))
    )

    pid = pid_key(col(r, "project_id"))
    units = units_map.get(pid, "")
    if not units and units_column:
        units = as_count(col(r, units_column))

    rows.append({
        "projectName":    name,
        "projectNumber":  number,
        "city":           clean(col(r, "city")),
        "state":          clean(col(r, "state_name")),
        "startDate":      start,
        "completionDate": finish,
        "projectType":    clean(col(r, "project_type")),
        "region":         clean(col(r, "region")),
        "program":        clean(col(r, "program_name")),
        "squareFeet":     clean(col(r, "square_feet")),
        "units":          units,
        "totalValue":     clean(col(r, "total_value")),
        "developer":      dev_map.get(pid_key(col(r, "project_id")), ""),
    })

rows.sort(key=lambda x: x["projectName"].lower())
print(f"\nMatched {len(rows)} third-party projects")

if SMOKE:
    print("\nSmoke test complete — nothing sent to Worker.")
else:
    resp = requests.post(WORKER_URL, json={"rows": rows}, timeout=60)
    print(f"\nWorker responded {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
