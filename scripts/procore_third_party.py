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


def is_third_party(number):
    m = re.search(r"-\s*(\d)", clean(number))
    return bool(m) and m.group(1) == "9"


def col(row, name):
    return row[name] if name in row.index else None


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
        "totalValue":     clean(col(r, "total_value")),
        "developer":      "",   # custom field — wired separately
    })

rows.sort(key=lambda x: x["projectName"].lower())
print(f"\nMatched {len(rows)} third-party projects:")
for x in rows:
    print(f"  {x['projectNumber']}  {x['projectName']}")

if SMOKE:
    print("\nSmoke test complete — nothing sent to Worker.")
else:
    resp = requests.post(WORKER_URL, json={"rows": rows}, timeout=60)
    print(f"\nWorker responded {resp.status_code}: {resp.text[:300]}")
    resp.raise_for_status()
