"""
Procore CAC Variance Breakdown, all projects -> password-protected Excel workbook
(a Summary tab plus one tab per property)

For every project in PROJECTS, reads the live budget view from the Procore Analytics Delta
Share, finds the budget snapshot that Procore's "Previous CAC" compares
against, and for every cost code with a CAC Variance explains which of the
seven Current CAC parts moved and which commitments, change events and owner
change orders sit behind the move.

Current CAC = Original Commitment + Adjustment Commitment + Buyout Commitment
            + Pending CO + Internal CO + Owner CO + Estimated COs

All dollar figures come from Procore's own budget view columns. The item
lists (contracts, change events, PCOs) are supporting evidence.

Public-repo safety:
  * the log prints counts and names only, never amounts
  * the workbook is encrypted with the CAC_REPORT_PASSWORD secret, and the
    script refuses to run without it
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import pandas as pd

# ---------------------------------------------------------------- settings

# Procore project ID -> name, from the budget links. Fillmore is found by its
# job number at run time (see EXTRA_BY_NUMBER) until its ID is added here.
PROJECTS = {
    3217143: "Allison Pointe", 2466743: "Aspire on 10th", 2349788: "Banyan High Pointe",
    3363831: "Central Park", 2974146: "County Line Station", 2707116: "Delaware Powers Place",
    2814646: "Emblem Mesa", 3294938: "Everrange", 2974110: "Flying Horse",
    2856585: "Fort Myers Gateway", 2770879: "Forum II", 3217151: "Havana",
    2723338: "Inspire Sonoran Desert", 3168616: "Journey Church",
    3363867: "Legacy at Jackson Landing (Civil)", 3527261: "Legacy at Jackson Landing (Core/Shell)",
    2856701: "Knoxville", 3487743: "LoHi", 2856388: "Longmont II", 3363889: "Maricopa",
    2974140: "Nexton", 2974171: "North Charleston", 2856408: "Parker II",
    2974129: "Pleasant Grove", 2856714: "Powers Place", 2856013: "Uptown Commons",
    2856370: "Vista Highlands",
}
EXTRA_BY_NUMBER = {"23-002": "Fillmore"}
RAW_IDS = [int(x) for x in (os.environ.get("RAW_PROJECT_IDS") or "").replace(" ", "").split(",") if x]
ONLY_IDS = [int(x) for x in (os.environ.get("PROJECT_IDS") or "").replace(" ", "").split(",") if x]
VIEW_ID = int(os.environ.get("VIEW_ID") or "736678")               # from the budget URL
BASELINE_ID = (os.environ.get("BASELINE_SNAPSHOT_ID") or "").strip()
PASSWORD = os.environ.get("CAC_REPORT_PASSWORD") or ""
OUT_DIR = os.environ.get("OUT_DIR") or "report"
SCHEMA = "public"
ET = ZoneInfo("America/New_York")

COMMIT = ["Original Commitment", "Adjustment Commitment", "Buyout Commitment"]
CE_COLS = ["Internal CO", "Pending CO", "Estimated COs"]
OWNER = ["Owner CO"]
COMPONENTS = COMMIT + CE_COLS + OWNER
GROUPS = [("commitment", COMMIT), ("change event", CE_COLS), ("owner", OWNER)]
EXTRA = ["Current CAC", "CAC Variance", "Buyout Savings/Loss", "GC Risk",
         "Revised Commitment", "JTD Cost"]
EPS = 0.5  # ignore sub-dollar rounding


# ---------------------------------------------------------------- helpers

def num(x):
    if x is None:
        return 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        try:
            v = float(str(x).replace(",", "").replace("$", "").strip())
        except (TypeError, ValueError):
            return 0.0
    return 0.0 if v != v else v


def parse_json(v):
    if isinstance(v, (str, bytes)):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v if isinstance(v, dict) else None


def money(v):
    return f"(${abs(v):,.2f})" if v < 0 else f"${v:,.2f}"


def signed(v):
    return f"+${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"


def norm_status(s):
    s = str(s or "").strip().lower().replace("_", " ").replace("-", " ")
    return " ".join(s.split())


def col(df, name, default=None):
    return df[name] if name in df.columns else pd.Series([default] * len(df), index=df.index)


def to_utc(series):
    return pd.to_datetime(series, utc=True, errors="coerce")


def as_int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ data access

class Share:
    def __init__(self):
        import delta_sharing
        self.ds = delta_sharing
        fd, self.profile = tempfile.mkstemp(suffix=".share")
        with os.fdopen(fd, "w") as f:
            json.dump({"shareCredentialsVersion": 1,
                       "endpoint": os.environ["PROCORE_DS_ENDPOINT"],
                       "bearerToken": os.environ["PROCORE_DS_TOKEN"]}, f)
        self.share = os.environ["PROCORE_DS_SHARE"]

    def url(self, table):
        return f"{self.profile}#{self.share}.{SCHEMA}.{table}"

    def all(self, table):
        return self.ds.load_as_pandas(self.url(table))

    def project(self, table, pid):
        hint = json.dumps({"op": "equal", "children": [
            {"op": "column", "name": "project_id", "valueType": "long"},
            {"op": "literal", "value": str(pid), "valueType": "long"}]})
        try:
            df = self.ds.load_as_pandas(self.url(table), jsonPredicateHints=hint)
        except TypeError:
            df = self.all(table)
        if "project_id" in df.columns:
            df = df[pd.to_numeric(df["project_id"], errors="coerce") == pid]
        return df


COMPANY_TABLES = ("budget_view_details", "change_events", "change_event_line_items",
                  "commitments", "commitment_line_items",
                  "prime_contract_potential_change_orders",
                  "prime_contract_change_order_line_items")


class SkipProject(Exception):
    pass


def resolve_projects(projects_df):
    pids = dict(PROJECTS)
    num = projects_df.get("project_number", pd.Series(dtype=str)).astype(str).str.strip()
    for job, name in EXTRA_BY_NUMBER.items():
        hit = projects_df[num == job]
        if len(hit) == 1:
            pid = int(hit["project_id"].iloc[0])
            pids.setdefault(pid, name)
            print(f"   {name}: job {job} -> project ID {pid}")
        else:
            print(f"   {name}: job {job} matched {len(hit)} projects; not added")
    if ONLY_IDS:
        pids = {k: v for k, v in pids.items() if k in ONLY_IDS}
    return pids


def fetch():
    s = Share()
    projects = s.all("projects")
    pids = resolve_projects(projects)
    common = {"budget_views": s.all("budget_views"), "projects": projects}
    company = {}
    for t in COMPANY_TABLES:
        df = s.all(t)
        company[t] = df[pd.to_numeric(df["project_id"], errors="coerce").isin(pids)]
        print(f"   {t:<45} {len(company[t]):>7} rows")
    snaps = {pid: s.project("budget_snapshot_line_item_headers", pid) for pid in pids}
    print(f"   {'budget_snapshot_line_item_headers':<45} {sum(len(v) for v in snaps.values()):>7} rows")
    return pids, common, company, snaps


def project_slice(pid, common, company, snaps):
    d = {t: df[pd.to_numeric(df["project_id"], errors="coerce") == pid] for t, df in company.items()}
    d["budget_snapshot_line_item_headers"] = snaps[pid]
    d["budget_views"] = common["budget_views"]
    pr = common["projects"]
    d["project"] = pr[pd.to_numeric(pr["project_id"], errors="coerce") == pid]
    return d


# ---------------------------------------------------------------- parsing

def line_record(j, wbs_code_id):
    wbs = j.get("wbs_code") if isinstance(j.get("wbs_code"), dict) else {}
    cost_code = str(j.get("cost_code") or "")
    code = wbs.get("flat_code") or cost_code.split(" - ")[0]
    desc = cost_code.split(" - ", 1)[1] if " - " in cost_code else (wbs.get("flat_name") or cost_code)
    rec = {"wbs_code_id": wbs_code_id, "code": str(code), "desc": str(desc),
           "division": str(j.get("root_cost_code") or ""), "category": str(j.get("category") or "")}
    for k in COMPONENTS + EXTRA:
        rec[k] = num(j.get(k))
    oe = j.get("Original Estimate")
    if oe is None:
        for k in j:
            if str(k).startswith("original_budget_amount"):
                oe = j[k]
                break
    rec["Original Estimate"] = num(oe)
    return rec


def parse_live(bvd):
    recs = []
    for _, r in bvd.iterrows():
        j = parse_json(r.get("infoJson"))
        wid = as_int(r.get("wbs_code_id"))
        if j is None or wid is None:
            continue
        recs.append(line_record(j, wid))
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    labels = df.groupby("wbs_code_id")[["code", "desc", "division", "category"]].first()
    nums = df.groupby("wbs_code_id")[COMPONENTS + EXTRA + ["Original Estimate"]].sum()
    return labels.join(nums).reset_index()


def parse_snapshots(hdr):
    recs = []
    for _, r in hdr.iterrows():
        j = parse_json(r.get("line_items"))
        wid = as_int(r.get("wbs_code_id"))
        if j is None or wid is None or "Current CAC" not in j:
            continue
        rec = line_record(j, wid)
        rec.update({"snapshot_id": as_int(r.get("budget_snapshot_id")),
                    "snapshot_name": str(r.get("budget_snapshot_name") or ""),
                    "snapshot_view": str(r.get("budget_template_name") or ""),
                    "snapshot_created": r.get("created_at")})
        recs.append(rec)
    df = pd.DataFrame(recs)
    if not df.empty:
        df["snapshot_created"] = to_utc(df["snapshot_created"])
    return df


def pick_view(bvd, views):
    """Use the view from the budget link if the share has it, else the best CAC view."""
    names = {}
    if len(views) and "id" in views.columns:
        names = dict(zip(pd.to_numeric(views["id"], errors="coerce"), views["name"].astype(str)))
    tmpl = pd.to_numeric(bvd["budget_template_id"], errors="coerce")
    if (tmpl == VIEW_ID).any():
        return VIEW_ID, names.get(VIEW_ID, str(VIEW_ID)), None
    best, best_n = None, 0
    for t in tmpl.dropna().unique():
        sub = bvd[tmpl == t]
        n = sum(1 for v in sub["infoJson"].head(50) if "Current CAC" in (parse_json(v) or {}))
        if n > best_n:
            best, best_n = int(t), n
    if best is None:
        return None, None, None
    note = (f"Procore view {VIEW_ID} ({names.get(VIEW_ID, 'from your budget link')}) is not "
            f"published to the Analytics share for this project, so this report reads view "
            f"{best} ({names.get(best, best)}), which carries the same CAC columns. "
            f"Spot-check two or three figures against the budget page.")
    return best, names.get(best, str(best)), note


# ---------------------------------------------------------------- evidence

def commitment_bucket(ctype, status):
    s, po = norm_status(status), "purchase" in str(ctype).lower()
    if s == "approved":
        return "Original Commitment"
    if po and s in ("submitted", "received", "partially received"):
        return "Buyout Commitment"
    if not po and s in ("out for signature", "complete"):
        return "Buyout Commitment"
    if (po and s == "processing") or (not po and s == "out for bid"):
        return "Adjustment Commitment"
    return "not in CAC"


def ce_bucket(scope, status):
    sc, st = norm_status(scope), norm_status(status)
    if sc == "in scope" and st in ("open", "pending"):
        return "Internal CO"
    if sc == "out of scope" and st == "open":
        return "Pending CO"
    if sc == "tbd" and st in ("open", "pending"):
        return "Estimated COs"
    return "not in CAC"


def owner_bucket(status):
    return "Owner CO" if norm_status(status) in ("approved", "pending pricing") else "not in CAC"


def _lookup(df, key):
    if not len(df) or key not in df.columns:
        return {}
    df = df.copy()
    df["_k"] = pd.to_numeric(df[key], errors="coerce")
    return {k: r for k, r in df.drop_duplicates("_k").set_index("_k").iterrows()}


def _latest(*vals):
    return to_utc(pd.Series(list(vals), dtype=object)).max()


def gather_evidence(d, since_by_wbs):
    """Commitments, change events and owner PCOs on each cost code changed since its comparison date."""
    ev = {w: [] for w in since_by_wbs}

    def keep(w, upd):
        s0 = since_by_wbs.get(w)
        return w in ev and pd.notna(upd) and (s0 is None or pd.isna(s0) or upd >= s0)

    com = _lookup(d["commitments"], "commitment_id")
    for _, r in d["commitment_line_items"].iterrows():
        w = as_int(r.get("wbs_code_id"))
        c = com.get(num(r.get("commitment_id")), {})
        upd = _latest(r.get("updated_at"), c.get("updated_at") if len(c) else None)
        if not keep(w, upd):
            continue
        ctype = c.get("type") if len(c) else r.get("contract_type")
        status = (c.get("status") if len(c) else None) or r.get("contract_status")
        ref = str((c.get("contract_number") if len(c) else "") or "")
        ev[w].append({"ref": ref or ("PO" if "purchase" in str(ctype).lower() else "Subcontract"),
                      "title": str((c.get("title") if len(c) else "") or r.get("description") or ""),
                      "status": norm_status(status).title(), "bucket": commitment_bucket(ctype, status),
                      "amount": num(r.get("extended_amount")) or num(r.get("total_amount"))})

    ces = _lookup(d["change_events"], "change_event_id")
    for _, r in d["change_event_line_items"].iterrows():
        w = as_int(r.get("wbs_code_id"))
        e = ces.get(num(r.get("change_event_id")), {})
        upd = _latest(r.get("updated_at"), e.get("updated_at") if len(e) else None)
        if not keep(w, upd):
            continue
        scope = e.get("scope") if len(e) else None
        status = (e.get("status") if len(e) else None) or r.get("change_event_status")
        number = (e.get("change_event_number") if len(e) else None) or as_int(r.get("change_event_id"))
        ev[w].append({"ref": f"CE-{number}",
                      "title": str((e.get("title") if len(e) else "") or r.get("change_event_title") or ""),
                      "status": f"{norm_status(status).title()}, {norm_status(scope) or 'scope ?'}",
                      "bucket": ce_bucket(scope, status),
                      "amount": num(r.get("cost_rom")) or num(r.get("estimated_cost_amount"))})

    pcos = _lookup(d["prime_contract_potential_change_orders"], "prime_contract_potential_change_order_id")
    for _, r in d["prime_contract_change_order_line_items"].iterrows():
        w = as_int(r.get("wbs_code_id"))
        p = pcos.get(num(r.get("prime_contract_potential_change_order_id")), {})
        upd = _latest(r.get("updated_at"), p.get("updated_at") if len(p) else None)
        if not keep(w, upd):
            continue
        status = (p.get("status") if len(p) else None) or r.get("status")
        ev[w].append({"ref": f"PCO-{(p.get('potential_change_order_number') if len(p) else '') or '?'}",
                      "title": str((p.get("title") if len(p) else "") or r.get("description") or ""),
                      "status": norm_status(status).title(), "bucket": owner_bucket(status),
                      "amount": num(r.get("extended_amount"))})

    for w in ev:
        ev[w].sort(key=lambda i: -abs(i["amount"]))
    return ev


# ------------------------------------------------------------ explanation

SHORT = {"Original Commitment": "Commitment", "Adjustment Commitment": "Adjustment",
         "Buyout Commitment": "Buyout", "Estimated COs": "Estimated CO"}


def explain(line):
    """Returns (driver label, list of sentences)."""
    out, drivers = [], {}
    d = line["deltas"]

    if d is None:
        out.append(f"Procore's comparison point for this line is not in its Analytics data, so the "
                   f"part-by-part change can't be shown. Items on this code changed since {line['since_label']}:")
        cac_items = [i for i in line["items"] if i["bucket"] != "not in CAC"]
        label = cac_items[0]["bucket"] if cac_items else "Not traced"
        return SHORT.get(label, label), out

    moved = {k: v for k, v in d.items() if abs(v) >= EPS}
    for gname, comps in GROUPS:
        gm = {k: moved[k] for k in comps if k in moved}
        if not gm:
            continue
        if len(gm) > 1 and abs(sum(gm.values())) < EPS:
            frm = " and ".join(k for k, v in gm.items() if v < 0)
            to = " and ".join(k for k, v in gm.items() if v > 0)
            out.append(f"{money(sum(v for v in gm.values() if v > 0))} moved from {frm} to {to} "
                       f"(status change, no net cost).")
            continue
        for k, v in sorted(gm.items(), key=lambda kv: -abs(kv[1])):
            drivers[k] = v
            out.append(f"{k} {signed(v)}.")

    di, dr = d.get("Internal CO", 0.0), line["revised_commitment_delta"]
    if di <= -EPS and abs(dr + di) <= max(1.0, 0.02 * abs(di)) and not any(k in moved for k in COMMIT):
        out.append(f"Revised Commitment rose by the same amount, so a change event likely became a "
                   f"commitment CO. Approved commitment COs are not part of Current CAC, so CAC dropped "
                   f"while the cost remains.")
    if any(k in drivers for k in COMMIT):
        bsl = line["Buyout Savings/Loss"]
        out.append(f"Commitments now {money(abs(bsl))} {'over' if bsl > 0 else 'under'} the "
                   f"{money(line['Original Estimate'])} estimate.")
    gap = line["variance"] - sum(d.values())
    if abs(gap) >= 1.0:
        out.append(f"{signed(gap)} not explained by the seven CAC parts; review.")
    if line["new_code"]:
        out.insert(0, "New cost code since the comparison point.")

    if drivers:
        top = max(drivers, key=lambda k: abs(drivers[k]))
        return SHORT.get(top, top), out
    return ("Reclassification" if moved else "Not traced"), out


def build_report(d, pid, name):
    bvd = d["budget_view_details"]
    if len(bvd) == 0:
        raise SkipProject("no live budget rows in the Analytics share")
    view_id, view_name, view_note = pick_view(bvd, d["budget_views"])
    if view_id is None:
        raise SkipProject("no budget view with CAC columns in the share")
    live_raw = bvd[pd.to_numeric(bvd["budget_template_id"], errors="coerce") == view_id]
    live = parse_live(live_raw)
    refreshed = to_utc(col(live_raw, "refreshed_timestamp")).max()

    snaps = parse_snapshots(d["budget_snapshot_line_item_headers"])
    meta = (snaps.drop_duplicates("snapshot_id").sort_values("snapshot_created", ascending=False)
            if len(snaps) else pd.DataFrame(columns=["snapshot_id", "snapshot_name", "snapshot_created"]))
    newest = meta.iloc[0] if len(meta) else None
    by_wbs = {w: g.sort_values("snapshot_created", ascending=False) for w, g in snaps.groupby("wbs_code_id")} \
        if len(snaps) else {}

    # Variance source: Procore's own CAC Variance column, unless it is blank for the whole project
    forced = as_int(BASELINE_ID) if BASELINE_ID else None
    forced = forced if forced in set(meta["snapshot_id"]) else None
    use_procore = forced is None and (live["CAC Variance"].abs() >= EPS).any()
    mode = "procore" if use_procore else ("forced" if forced else "newest")
    fixed = forced if forced else (int(newest["snapshot_id"]) if newest is not None else None)

    lines = []
    for _, r in live.iterrows():
        w = r["wbs_code_id"]
        hist = by_wbs.get(w, pd.DataFrame())
        match = None
        if use_procore:
            variance = r["CAC Variance"]
            if abs(variance) < EPS:
                continue
            prev = r["Current CAC"] - variance
            for _, s in hist.iterrows():
                if abs(s["Current CAC"] - prev) < 1.0 or (abs(prev) < 0.005 and s["Current CAC"] < 0):
                    match = s
                    break
        else:
            if fixed is None:
                continue
            row = hist[hist["snapshot_id"] == fixed] if len(hist) else hist
            match = row.iloc[0] if len(row) else None
            variance = r["Current CAC"] - (match["Current CAC"] if match is not None else 0.0)
            if abs(variance) < EPS:
                continue

        new_code = match is None and len(hist) == 0 and abs(r["Current CAC"] - variance) < 0.005
        traced = match is not None or new_code
        before = ({k: float(match[k]) for k in COMPONENTS + ["Revised Commitment"]} if match is not None
                  else {k: 0.0 for k in COMPONENTS + ["Revised Commitment"]})
        since = match["snapshot_created"] if match is not None else (
            newest["snapshot_created"] if newest is not None else pd.NaT)
        lines.append({
            "wbs_code_id": w, "code": r["code"], "desc": r["desc"], "variance": variance,
            "current": r["Current CAC"], "previous": r["Current CAC"] - variance,
            "deltas": {k: r[k] - before[k] for k in COMPONENTS} if traced else None,
            "revised_commitment_delta": r["Revised Commitment"] - before["Revised Commitment"],
            "Buyout Savings/Loss": r["Buyout Savings/Loss"], "Original Estimate": r["Original Estimate"],
            "new_code": new_code, "traced": traced, "since": since,
            "since_label": pd.Timestamp(since).tz_convert(ET).strftime("%b %d") if pd.notna(since) else "recently",
            "matched_snapshot": str(match["snapshot_name"]) if match is not None else None,
            "items": []})

    ev = gather_evidence(d, {l["wbs_code_id"]: l["since"] for l in lines})
    for l in lines:
        l["items"] = ev.get(l["wbs_code_id"], [])
        l["driver"], l["explanation"] = explain(l)
    lines.sort(key=lambda l: -abs(l["variance"]))

    raw = None
    if pid in RAW_IDS:
        cols = {}
        for _, m_ in meta.head(3).iterrows():
            ser = snaps[snaps["snapshot_id"] == m_["snapshot_id"]].set_index("wbs_code_id")["Current CAC"]
            when = pd.Timestamp(m_["snapshot_created"]).tz_convert(ET).strftime("%b %d")
            cols[f"{m_['snapshot_name']} ({when})"] = ser
        raw = {"live": live, "snaps": cols}

    proj = d["project"].iloc[0] if len(d["project"]) else {}
    matched = pd.Series([l["matched_snapshot"] for l in lines if l["matched_snapshot"]])
    return {
        "pid": pid, "project_name": name,
        "project_number": str(proj.get("project_number", "") if len(proj) else ""),
        "view_id": view_id, "view_name": view_name, "view_note": view_note,
        "refreshed": refreshed, "mode": mode, "lines": lines, "line_count": len(live),
        "newest_snapshot": newest, "compared_to": matched.mode().iloc[0] if len(matched) else None,
        "generated": datetime.now(timezone.utc), "raw": raw,
    }


# ------------------------------------------------------------------ Excel

PART_SHORT = {"Original Commitment": "Orig Commit", "Adjustment Commitment": "Adj Commit",
              "Buyout Commitment": "Buyout Commit", "Pending CO": "Pending CO", "Internal CO": "Internal CO",
              "Owner CO": "Owner CO", "Estimated COs": "Est COs"}


def sheet_names(reps):
    names, used = {}, set()
    for r in reps:
        n = r["project_name"].replace("Legacy at ", "").replace("/", "-")
        n = "".join(ch for ch in n if ch not in '[]:*?\\')[:31].strip()
        base, k = n, 2
        while n.lower() in used or n.lower() == "summary":
            n = f"{base[:28]} {k}"
            k += 1
        used.add(n.lower())
        names[r["pid"]] = n
    return names


def render_xlsx(reps, skipped, path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    F = "Arial"
    NAVY, GREY, ZEBRA = "1F3A5F", "667085", "F9FAFB"
    MONEY = '$#,##0.00;[Red]($#,##0.00);"-"'
    title_f = Font(name=F, size=14, bold=True, color=NAVY)
    sub_f = Font(name=F, size=9, color=GREY)
    head_f = Font(name=F, size=9, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=NAVY)
    body_f, bold_f = Font(name=F, size=9), Font(name=F, size=9, bold=True)
    link_f = Font(name=F, size=9, bold=True, color="1F3A5F", underline="single")
    xlink_f = Font(name=F, size=9)
    note_f = Font(name=F, size=8, italic=True, color=GREY)
    thin = Side(style="thin", color="D0D5DD")
    top_rule = Border(top=Side(style="medium", color=NAVY))
    wrap = Alignment(wrap_text=True, vertical="top")
    top = Alignment(vertical="top")

    def et(ts, fmt="%b %d, %Y %I:%M %p ET"):
        return pd.Timestamp(ts).tz_convert(ET).strftime(fmt) if pd.notna(ts) else "n/a"

    def header(ws, row, labels, widths=None):
        for c, lab in enumerate(labels, 1):
            cell = ws.cell(row=row, column=c, value=lab)
            cell.font, cell.fill = head_f, head_fill
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[row].height = 28
        if widths:
            for c, w in enumerate(widths, 1):
                ws.column_dimensions[get_column_letter(c)].width = w

    names = sheet_names(reps)
    ordered = sorted(reps, key=lambda r: r["project_name"])
    refreshed = max((r["refreshed"] for r in reps if pd.notna(r["refreshed"])), default=pd.NaT)
    generated = reps[0]["generated"] if reps else datetime.now(timezone.utc)
    wb = Workbook()

    # ---------------- Summary tab
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "CAC Variance Breakdown, All Projects"
    ws["A1"].font = title_f
    ws["A2"] = f"Procore data as of {et(refreshed)}  |  Generated {et(generated)}"
    ws["A2"].font = sub_f
    cols = ["Project", "Job #", "Lines with variance", "Traced", "Increases", "Savings",
            "Net CAC variance", "Latest snapshot in Procore data"]
    header(ws, 4, cols, [34, 10, 11, 9, 16, 16, 17, 34])
    row = 5
    for r in ordered:
        tab = names[r["pid"]]
        n = max(len(r["lines"]), 1)
        rng = f"'{tab}'!$E$6:$E${5 + n}"
        ns = r["newest_snapshot"]
        c = ws.cell(row=row, column=1, value=r["project_name"])
        c.hyperlink, c.font = f"#'{tab}'!A1", link_f
        ws.cell(row=row, column=2, value=r["project_number"] or "").font = body_f
        ws.cell(row=row, column=3, value=f"=COUNTIF({rng},\"<>0\")-COUNTBLANK({rng})").font = xlink_f
        ws.cell(row=row, column=4, value=f"=COUNTIF('{tab}'!$G$6:$G${5 + n},\"Yes\")").font = xlink_f
        ws.cell(row=row, column=5, value=f"=SUMIF({rng},\">0\")").font = xlink_f
        ws.cell(row=row, column=6, value=f"=SUMIF({rng},\"<0\")").font = xlink_f
        ws.cell(row=row, column=7, value=f"=SUM({rng})").font = bold_f
        ws.cell(row=row, column=8, value=(f"{ns['snapshot_name']}, {et(ns['snapshot_created'], '%b %d')}"
                                         if ns is not None else "none")).font = body_f
        for col in (5, 6, 7):
            ws.cell(row=row, column=col).number_format = MONEY
        if (row - 5) % 2 == 1:
            for col in range(1, 9):
                ws.cell(row=row, column=col).fill = PatternFill("solid", fgColor=ZEBRA)
        row += 1
    for name, reason in skipped:
        ws.cell(row=row, column=1, value=name).font = body_f
        ws.cell(row=row, column=8, value=f"Not included: {reason}").font = note_f
        row += 1
    last = row - 1
    ws.cell(row=row, column=1, value="Total").font = bold_f
    ws.cell(row=row, column=3, value=f"=SUM(C5:C{last})").font = bold_f
    ws.cell(row=row, column=4, value=f"=SUM(D5:D{last})").font = bold_f
    for col in (5, 6, 7):
        cl = get_column_letter(col)
        cell = ws.cell(row=row, column=col, value=f"=SUM({cl}5:{cl}{last})")
        cell.font, cell.number_format = bold_f, MONEY
    for col in range(1, 9):
        ws.cell(row=row, column=col).border = top_rule
    notes = [
        "Net CAC variance is Procore's own CAC Variance column (Current CAC minus Previous CAC), read from the live budget.",
        f"Budget view {VIEW_ID} is not in Procore's Analytics data, so figures come from view 299759 "
        f"('Eric's Report with Buyout - Change Events'), which has the same CAC columns." if any(r["view_note"] for r in reps) else "",
        "Traced = lines whose comparison point matches a saved snapshot, so the change in each CAC part can be shown. "
        "Untraced lines show Procore's variance and the items that changed recently, but not the part-by-part change.",
        "Click a project name to open its tab. Totals on this tab are formulas that read each project tab.",
    ]
    rr = row + 2
    for t in notes:
        if t:
            ws.cell(row=rr, column=1, value=t).font = note_f
            rr += 1
    ws.freeze_panes = "A5"

    # ---------------- one tab per project
    widths = [12, 26, 15, 15, 15, 13, 8] + [13] * 7 + [60, 70]
    labels = (["Cost code", "Description", "Current CAC", "Previous CAC", "CAC variance", "Driver", "Traced"]
              + [f"{PART_SHORT[k]} change" for k in COMPONENTS] + ["Why", "Items behind it"])
    for r in ordered:
        ws = wb.create_sheet(names[r["pid"]])
        ws["A1"] = r["project_name"] + (f"  ({r['project_number']})" if r["project_number"] else "")
        ws["A1"].font = title_f
        ns = r["newest_snapshot"]
        ws["A2"] = (f"Procore data as of {et(r['refreshed'])}  |  Budget view {r['view_id']} ({r['view_name']})  |  "
                    f"Latest snapshot in Procore data: "
                    + (f"{ns['snapshot_name']}, {et(ns['snapshot_created'], '%b %d')}" if ns is not None else "none"))
        ws["A2"].font = sub_f
        ws["A3"] = "← Summary"
        ws["A3"].hyperlink, ws["A3"].font = "#'Summary'!A1", Font(name=F, size=8, color=NAVY, underline="single")
        header(ws, 5, labels, widths)
        lines = r["lines"]
        if not lines:
            ws.cell(row=6, column=1, value="No cost code has a CAC variance.").font = note_f
        for i, l in enumerate(lines):
            rw = 6 + i
            vals = [l["code"], l["desc"], l["current"], None, l["variance"], l["driver"],
                    "Yes" if l["traced"] else "No"]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(row=rw, column=c, value=v)
                cell.font, cell.alignment = body_f, top
            ws.cell(row=rw, column=1).font = bold_f
            ws.cell(row=rw, column=4, value=f"=C{rw}-E{rw}")           # Previous CAC = Current - Variance
            ws.cell(row=rw, column=5).font = bold_f
            for c in (3, 4, 5):
                ws.cell(row=rw, column=c).number_format = MONEY
            for k, comp in enumerate(COMPONENTS):
                cell = ws.cell(row=rw, column=8 + k)
                if l["deltas"] is not None and abs(l["deltas"][comp]) >= EPS:
                    cell.value = round(l["deltas"][comp], 2)
                cell.number_format, cell.font, cell.alignment = MONEY, body_f, top
            ws.cell(row=rw, column=15, value=" ".join(l["explanation"])).alignment = wrap
            items = "\n".join(f"{i_['ref']}  {i_['title'][:70]}  |  {i_['status']}  |  "
                              f"{i_['bucket'] if i_['bucket'] != 'not in CAC' else 'not in CAC'}  |  {money(i_['amount'])}"
                              for i_ in l["items"][:8])
            if len(l["items"]) > 8:
                items += f"\n+{len(l['items']) - 8} more"
            ws.cell(row=rw, column=16, value=items or "No items on this code changed recently.").alignment = wrap
            for c in (15, 16):
                ws.cell(row=rw, column=c).font = body_f
            if i % 2 == 1:
                for c in range(1, 17):
                    ws.cell(row=rw, column=c).fill = PatternFill("solid", fgColor=ZEBRA)
        n = max(len(lines), 1)
        tr = 6 + n
        ws.cell(row=tr, column=1, value="Total").font = bold_f
        for c in [3, 4, 5] + list(range(8, 15)):
            cl = get_column_letter(c)
            cell = ws.cell(row=tr, column=c, value=f"=SUM({cl}6:{cl}{5 + n})")
            cell.font, cell.number_format = bold_f, MONEY
        for c in range(1, 17):
            ws.cell(row=tr, column=c).border = top_rule
        ws.cell(row=tr + 2, column=1,
                value="Previous CAC = Current CAC minus CAC Variance. Part changes are shown only for traced lines; "
                      "for those, the seven part changes add up to the CAC variance.").font = note_f
        ws.freeze_panes = "C6"
        if lines:
            ws.auto_filter.ref = f"A5:P{5 + n}"

        # optional raw data block for this project
        if r.get("raw"):
            live, snapcols = r["raw"]["live"], r["raw"]["snaps"]
            start = tr + 5
            ws.cell(row=start - 1, column=1, value="Raw data read for this project (every cost code, live view "
                    "values plus Current CAC in the 3 newest snapshots)").font = Font(name=F, size=11, bold=True, color=NAVY)
            snames = list(snapcols)
            rlabels = (["Cost code", "Description"] + [PART_SHORT[k] for k in COMPONENTS]
                       + ["Current CAC", "Previous CAC", "CAC variance", "GC Risk"] + snames)
            header(ws, start, rlabels)
            rw = start + 1
            for _, x in live.sort_values("code").iterrows():
                vals = [x[k] for k in COMPONENTS] + [x["Current CAC"], None, x["CAC Variance"], x["GC Risk"]]
                sv = [snapcols[s_].get(x["wbs_code_id"]) for s_ in snames]
                if all(v is None or abs(v) < EPS for v in vals) and all(v is None or abs(v) < EPS for v in sv):
                    continue
                ws.cell(row=rw, column=1, value=x["code"]).font = bold_f
                ws.cell(row=rw, column=2, value=x["desc"]).font = body_f
                for c, v in enumerate(vals, 3):
                    cell = ws.cell(row=rw, column=c, value=None if v is None else round(v, 2))
                    cell.font, cell.number_format = body_f, MONEY
                ws.cell(row=rw, column=11, value=f"=J{rw}-L{rw}")    # Previous = Current - Variance
                for c, v in enumerate(sv, 14):
                    cell = ws.cell(row=rw, column=c, value=round(v, 2) if v is not None else "not in snapshot")
                    cell.font, cell.number_format = body_f, MONEY
                rw += 1

    for sh in wb.worksheets:                      # print-ready: landscape, one page wide
        sh.page_setup.orientation = "landscape"
        sh.sheet_properties.pageSetUpPr.fitToPage = True
        sh.page_setup.fitToWidth, sh.page_setup.fitToHeight = 1, 0
    wb.save(path)


def encrypt(src, dst, password):
    import msoffcrypto
    with open(src, "rb") as f:
        office = msoffcrypto.OfficeFile(f)
        with open(dst, "wb") as out:
            office.encrypt(password, out)


# ------------------------------------------------------------------- main

def main():
    if len(PASSWORD) < 12:
        sys.exit("CAC_REPORT_PASSWORD secret is missing or shorter than 12 characters. "
                 "Refusing to produce an unencrypted report in a public repo.")
    print(f"Preferred budget view {VIEW_ID}")
    print("Loading tables:")
    pids, common, company, snaps = fetch()

    reps, skipped = [], []
    print(f"\nBuilding {len(pids)} projects:")
    for pid, name in sorted(pids.items(), key=lambda kv: kv[1]):
        try:
            rep = build_report(project_slice(pid, common, company, snaps), pid, name)
        except SkipProject as e:
            skipped.append((name, str(e)))
            print(f"   {name:<40} skipped: {e}")
            continue
        reps.append(rep)
        traced = sum(1 for l in rep["lines"] if l["traced"])
        print(f"   {name:<40} view {rep['view_id']}, {rep['mode']} variance, "
              f"{len(rep['lines'])} lines ({traced} traced), "
              f"{sum(len(l['items']) for l in rep['lines'])} items")
    if not reps:
        sys.exit("No project produced a report.")

    os.makedirs(OUT_DIR, exist_ok=True)
    final = os.path.join(OUT_DIR, f"CAC_Variance_All_Projects_{datetime.now(ET):%Y-%m-%d}.xlsx")
    tmp = os.path.join(tempfile.gettempdir(), "cac_plain.xlsx")
    render_xlsx(reps, skipped, tmp)
    encrypt(tmp, final, PASSWORD)
    os.remove(tmp)
    print(f"\n{len(reps)} projects in report, {len(skipped)} skipped. Wrote {final}")


if __name__ == "__main__":
    main()
