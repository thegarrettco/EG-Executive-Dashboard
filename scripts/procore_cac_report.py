"""
Procore CAC Variance Breakdown, all projects -> password-protected PDF

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
  * the PDF is AES-256 encrypted with the CAC_REPORT_PASSWORD secret, and the
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


def choose_baseline(live, snaps):
    """Find the snapshot Procore's Previous CAC compares against."""
    if snaps.empty:
        return None, 0.0, "No snapshots with CAC columns were found."
    order = (snaps.drop_duplicates("snapshot_id")
             .sort_values("snapshot_created", ascending=False)["snapshot_id"].tolist())
    if BASELINE_ID:
        sid = as_int(BASELINE_ID)
        if sid in order:
            return sid, None, "Baseline snapshot chosen manually for this run."
    prev = live.set_index("wbs_code_id")["Current CAC"] - live.set_index("wbs_code_id")["CAC Variance"]
    any_var = (live["CAC Variance"].abs() >= EPS).any()
    best_sid, best_rate = order[0], -1.0
    for sid in order:
        s = snaps[snaps["snapshot_id"] == sid].set_index("wbs_code_id")["Current CAC"]
        both = prev.index.intersection(s.index)
        if len(both) == 0:
            continue
        p, q = prev.loc[both], s.loc[both]
        ok = ((p - q).abs() < 1.0) | ((p.abs() < 0.005) & (q < 0))  # "if negative, show 0"
        rate = float(ok.mean())
        if rate > best_rate + 1e-9:
            best_sid, best_rate = sid, rate
    if not any_var:
        return order[0], None, ("Procore's CAC Variance is zero on every line in the share, so "
                                "this compares against the newest snapshot.")
    return best_sid, best_rate, None


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
    return f"Not in CAC ({s or 'no status'})"


def ce_bucket(scope, status):
    sc, st = norm_status(scope), norm_status(status)
    if sc == "in scope" and st in ("open", "pending"):
        return "Internal CO"
    if sc == "out of scope" and st == "open":
        return "Pending CO"
    if sc == "tbd" and st in ("open", "pending"):
        return "Estimated COs"
    return f"Not in CAC ({sc or '?'}, {st or '?'})"


def owner_bucket(status):
    s = norm_status(status)
    if s in ("approved", "pending pricing"):
        return "Owner CO"
    return f"Not in CAC ({s or 'no status'})"


def changed_since(*series_list, since):
    out = None
    for s in series_list:
        m = to_utc(s) >= since
        out = m if out is None else (out | m)
    return out.fillna(False)


def gather_evidence(d, wbs_ids, since):
    ev = {w: [] for w in wbs_ids}

    # commitments (subcontracts and POs)
    cli, com = d["commitment_line_items"], d["commitments"]
    if len(cli):
        cli = cli[pd.to_numeric(cli["wbs_code_id"], errors="coerce").isin(wbs_ids)]
        cm = com.set_index(pd.to_numeric(com["commitment_id"], errors="coerce")) if len(com) else pd.DataFrame()
        for _, r in cli.iterrows():
            cid = num(r.get("commitment_id"))
            c = cm.loc[cid] if cid in getattr(cm, "index", []) else {}
            if isinstance(c, pd.DataFrame):
                c = c.iloc[0]
            upd = to_utc(pd.Series([r.get("updated_at"), c.get("updated_at") if len(c) else None])).max()
            if pd.isna(upd) or upd < since:
                continue
            ctype = c.get("type") if len(c) else r.get("contract_type")
            status = (c.get("status") if len(c) else None) or r.get("contract_status")
            amt = num(r.get("extended_amount")) or num(r.get("total_amount"))
            ev[as_int(r["wbs_code_id"])].append({
                "kind": "PO" if "purchase" in str(ctype).lower() else "Subcontract",
                "ref": str((c.get("contract_number") if len(c) else "") or ""),
                "title": str((c.get("title") if len(c) else "") or r.get("description") or ""),
                "status": norm_status(status).title(), "bucket": commitment_bucket(ctype, status),
                "amount": amt, "updated": upd})

    # change events
    celi, ce = d["change_event_line_items"], d["change_events"]
    if len(celi):
        celi = celi[pd.to_numeric(celi["wbs_code_id"], errors="coerce").isin(wbs_ids)]
        cem = ce.set_index(pd.to_numeric(ce["change_event_id"], errors="coerce")) if len(ce) else pd.DataFrame()
        for _, r in celi.iterrows():
            eid = num(r.get("change_event_id"))
            e = cem.loc[eid] if eid in getattr(cem, "index", []) else {}
            if isinstance(e, pd.DataFrame):
                e = e.iloc[0]
            times = [r.get("updated_at"), e.get("updated_at") if len(e) else None]
            upd = to_utc(pd.Series(times)).max()
            if pd.isna(upd) or upd < since:
                continue
            scope = e.get("scope") if len(e) else None
            status = (e.get("status") if len(e) else None) or r.get("change_event_status")
            amt = num(r.get("cost_rom")) or num(r.get("estimated_cost_amount"))
            ev[as_int(r["wbs_code_id"])].append({
                "kind": "Change Event",
                "ref": "CE-" + str((e.get("change_event_number") if len(e) else "") or as_int(eid) or ""),
                "title": str((e.get("title") if len(e) else "") or r.get("change_event_title") or r.get("description") or ""),
                "status": f"{norm_status(status).title()} / {norm_status(scope).title() or '?'}",
                "bucket": ce_bucket(scope, status), "amount": amt, "updated": upd})

    # owner change orders (prime PCOs)
    pl, pco = d["prime_contract_change_order_line_items"], d["prime_contract_potential_change_orders"]
    if len(pl):
        pl = pl[pd.to_numeric(pl["wbs_code_id"], errors="coerce").isin(wbs_ids)]
        pm = pco.set_index(pd.to_numeric(pco["prime_contract_potential_change_order_id"], errors="coerce")) if len(pco) else pd.DataFrame()
        for _, r in pl.iterrows():
            pid = num(r.get("prime_contract_potential_change_order_id"))
            p = pm.loc[pid] if pid in getattr(pm, "index", []) else {}
            if isinstance(p, pd.DataFrame):
                p = p.iloc[0]
            upd = to_utc(pd.Series([r.get("updated_at"), p.get("updated_at") if len(p) else None])).max()
            if pd.isna(upd) or upd < since:
                continue
            status = (p.get("status") if len(p) else None) or r.get("status")
            ev[as_int(r["wbs_code_id"])].append({
                "kind": "Owner PCO",
                "ref": "PCO-" + str((p.get("potential_change_order_number") if len(p) else "") or ""),
                "title": str((p.get("title") if len(p) else "") or r.get("description") or ""),
                "status": norm_status(status).title(), "bucket": owner_bucket(status),
                "amount": num(r.get("extended_amount")), "updated": upd})

    for w in ev:
        ev[w].sort(key=lambda i: -abs(i["amount"]))
    return ev


# ------------------------------------------------------------ explanation

def explain(line):
    d = line["deltas"]
    moved = {k: v for k, v in d.items() if abs(v) >= EPS}
    sentences, drivers = [], {}

    for gname, comps in GROUPS:
        gm = {k: moved[k] for k in comps if k in moved}
        if not gm:
            continue
        net = sum(gm.values())
        if len(gm) > 1 and abs(net) < EPS:
            frm = [k for k, v in gm.items() if v < 0]
            to = [k for k, v in gm.items() if v > 0]
            amt = sum(v for v in gm.values() if v > 0)
            sentences.append(f"{money(amt)} moved from {' and '.join(frm)} to {' and '.join(to)}: "
                             f"a {gname} {'status or scope' if gname == 'change event' else 'status'} change with no net cost.")
            continue
        for k, v in sorted(gm.items(), key=lambda kv: -abs(kv[1])):
            drivers[k] = v
            sentences.append(f"{k} {signed(v)}.")

    di = d.get("Internal CO", 0.0)
    dr = line["revised_commitment_delta"]
    if di <= -EPS and abs(dr + di) <= max(1.0, 0.02 * abs(di)) and not any(k in moved for k in COMMIT):
        sentences.append(f"Revised Commitment rose by about the same amount ({signed(dr)}), so this looks "
                         f"like a change event converted to a commitment change order. Approved "
                         f"commitment COs count only in Revised Commitment, not Current CAC, so CAC "
                         f"dropped even though the cost did not go away.")

    if any(k in drivers for k in COMMIT):
        bsl = line["Buyout Savings/Loss"]
        side = "over" if bsl > 0 else "under"
        sentences.append(f"Commitments on this code are now {money(abs(bsl))} {side} the original "
                         f"estimate of {money(line['Original Estimate'])}.")

    gap = line["variance"] - sum(d.values())
    if abs(gap) >= 1.0:
        sentences.append(f"The seven CAC parts account for {signed(sum(d.values()))} of the "
                         f"{signed(line['variance'])} variance; {signed(gap)} is unexplained. Review.")
    if line["new_code"]:
        sentences.insert(0, "Cost code did not exist in the comparison snapshot.")
    if not line["items"] and drivers:
        sentences.append("No Procore item on this code changed since the snapshot date. Check for "
                         "back-dated edits.")

    if drivers:
        top = max(drivers, key=lambda k: abs(drivers[k]))
        label = {"Buyout Commitment": "Buyout", "Adjustment Commitment": "Adjustment",
                 "Original Commitment": "Commitment", "Estimated COs": "Estimated CO"}.get(top, top)
    elif moved:
        label = "Reclassification"
    else:
        label = "Unexplained"
    return label, sentences


def build_report(d, pid, name):
    bvd = d["budget_view_details"]
    view_id, view_name, view_note = pick_view(bvd, d["budget_views"])
    if len(bvd) == 0:
        raise SkipProject("no live budget rows in the Analytics share")
    if view_id is None:
        raise SkipProject("no budget view with CAC columns in the share")
    live_raw = bvd[pd.to_numeric(bvd["budget_template_id"], errors="coerce") == view_id]
    live = parse_live(live_raw)
    refreshed = to_utc(col(live_raw, "refreshed_timestamp")).max()

    snaps = parse_snapshots(d["budget_snapshot_line_item_headers"])
    base_id, match_rate, base_note = choose_baseline(live, snaps)
    if base_id is None:
        raise SkipProject("no budget snapshots with CAC columns to compare against")
    base = snaps[snaps["snapshot_id"] == base_id]
    base_meta = base.iloc[0]
    base_idx = base.set_index("wbs_code_id")
    since = base_meta["snapshot_created"]

    lines = []
    for _, r in live.iterrows():
        w = r["wbs_code_id"]
        new_code = w not in base_idx.index
        b = base_idx.loc[w] if not new_code else None
        if isinstance(b, pd.DataFrame):
            b = b.iloc[0]
        before = {k: (0.0 if new_code else float(b[k])) for k in COMPONENTS + ["Current CAC", "Revised Commitment"]}
        variance = r["Current CAC"] - before["Current CAC"]
        if abs(variance) < EPS:
            continue
        lines.append({
            "wbs_code_id": w, "code": r["code"], "desc": r["desc"], "division": r["division"],
            "variance": variance, "procore_variance": r["CAC Variance"],
            "before": before, "after": {k: r[k] for k in COMPONENTS + ["Current CAC"]},
            "deltas": {k: r[k] - before[k] for k in COMPONENTS},
            "revised_commitment_delta": r["Revised Commitment"] - before["Revised Commitment"],
            "Buyout Savings/Loss": r["Buyout Savings/Loss"], "Original Estimate": r["Original Estimate"],
            "new_code": new_code, "items": []})

    ev = gather_evidence(d, [l["wbs_code_id"] for l in lines], since)
    for l in lines:
        l["items"] = ev.get(l["wbs_code_id"], [])
        l["driver"], l["explanation"] = explain(l)
        l["mismatch"] = abs(l["variance"] - l["procore_variance"]) >= 1.0
    lines.sort(key=lambda l: -abs(l["variance"]))

    proj = d["project"].iloc[0] if len(d["project"]) else {}
    snap_list = (snaps.drop_duplicates("snapshot_id")
                 .sort_values("snapshot_created", ascending=False)
                 [["snapshot_id", "snapshot_name", "snapshot_view", "snapshot_created"]])
    return {
        "pid": pid, "project_name": name,
        "project_number": str(proj.get("project_number", "") if len(proj) else ""),
        "view_id": view_id, "view_name": view_name, "view_note": view_note,
        "refreshed": refreshed, "baseline": base_meta, "match_rate": match_rate,
        "base_note": base_note, "lines": lines, "line_count": len(live),
        "snapshots": snap_list, "generated": datetime.now(timezone.utc),
    }


# ------------------------------------------------------------------- PDF

def render_pdf(reps, skipped, path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Flowable, KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    NAVY, GREY, LIGHT = colors.HexColor("#1F3A5F"), colors.HexColor("#5B6573"), colors.HexColor("#F2F4F7")
    UP, DOWN = colors.HexColor("#B42318"), colors.HexColor("#067647")
    ss = getSampleStyleSheet()
    H1 = ParagraphStyle("H1", parent=ss["Title"], fontSize=20, leading=24, alignment=0, textColor=NAVY, spaceAfter=2)
    SUB = ParagraphStyle("SUB", parent=ss["Normal"], fontSize=9.5, textColor=GREY, spaceAfter=8)
    H2 = ParagraphStyle("H2", parent=ss["Heading2"], fontSize=13, textColor=NAVY, spaceBefore=10, spaceAfter=6)
    H3 = ParagraphStyle("H3", parent=ss["Heading3"], fontSize=10.5, textColor=NAVY, spaceBefore=4, spaceAfter=3)
    BODY = ParagraphStyle("BODY", parent=ss["Normal"], fontSize=9, leading=12)
    CELL = ParagraphStyle("CELL", parent=ss["Normal"], fontSize=8, leading=10)
    CELLB = ParagraphStyle("CELLB", parent=CELL, fontName="Helvetica-Bold")
    NUMC = ParagraphStyle("NUMC", parent=CELL, alignment=TA_RIGHT)
    NOTE = ParagraphStyle("NOTE", parent=BODY, backColor=colors.HexColor("#FFF7E6"),
                          borderColor=colors.HexColor("#F5C26B"), borderWidth=0.6, borderPadding=6,
                          spaceBefore=6, spaceAfter=10)
    RIGHT_HDR = {"At snapshot", "Now", "Change", "Amount", "CAC variance", "Net variance",
                 "Lines", "Increases", "Savings", "Age"}

    class Bookmark(Flowable):
        """Sidebar bookmark plus a link target for the cover table."""
        def __init__(self, key, title):
            super().__init__()
            self.key, self.title = key, title

        def wrap(self, *args):
            return (0, 0)

        def draw(self):
            self.canv.bookmarkPage(self.key)
            self.canv.addOutlineEntry(self.title, self.key, level=0)

    def P(t, s=CELL):
        return Paragraph(escape(str(t)), s)

    def VP(v, bold=False, left=False):
        st = ParagraphStyle("v", parent=CELL if left else NUMC, textColor=UP if v > 0 else DOWN,
                            fontName="Helvetica-Bold" if bold else "Helvetica")
        return Paragraph(escape(money(v)), st)

    def grid(data, widths, header=True, zebra=True):
        t = Table(data, colWidths=widths, repeatRows=1 if header else 0, hAlign="LEFT")
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#D0D5DD")),
                 ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), NAVY)]
        if zebra:
            for i in range(1 if header else 0, len(data)):
                if i % 2 == 0:
                    style.append(("BACKGROUND", (0, i), (-1, i), LIGHT))
        t.setStyle(TableStyle(style))
        return t

    def hdr(*names):
        return [Paragraph(f"<font color='white'><b>{escape(n)}</b></font>",
                          NUMC if n in RIGHT_HDR else CELL) for n in names]

    def et(ts, fmt="%b %d, %Y %I:%M %p ET"):
        return pd.Timestamp(ts).tz_convert(ET).strftime(fmt) if pd.notna(ts) else "unknown"

    def totals(rep):
        v = [l["variance"] for l in rep["lines"]]
        return sum(v), sum(x for x in v if x > 0), sum(x for x in v if x < 0)

    def age_days(rep):
        c = rep["baseline"]["snapshot_created"]
        return (rep["generated"] - c).days if pd.notna(c) else None

    def label(rep):
        return rep["project_name"] + (f" ({rep['project_number']})" if rep["project_number"] else "")

    def item_label(it):
        ref = it["ref"]
        return ref if ref[:2].isalpha() else f"{it['kind']} {ref}".strip()

    story = []
    generated = datetime.now(timezone.utc)

    # ---------------- cover: portfolio summary
    all_lines = [l for r in reps for l in r["lines"]]
    net = sum(l["variance"] for l in all_lines)
    ups = sum(l["variance"] for l in all_lines if l["variance"] > 0)
    downs = sum(l["variance"] for l in all_lines if l["variance"] < 0)
    with_var = [r for r in reps if r["lines"]]
    story += [Paragraph("CAC Variance Breakdown, All Projects", H1),
              Paragraph(f"Generated {et(generated)}. Positive amounts raise cost; negative amounts are "
                        f"savings. Click a project name to jump to its section.", SUB)]
    kpi = [[P("Net CAC variance", CELLB), P("Increases", CELLB), P("Savings", CELLB),
            P("Projects with variance", CELLB), P("Lines with variance", CELLB)],
           [VP(net, True, True), VP(ups, True, True), VP(downs, True, True),
            P(f"{len(with_var)} of {len(reps)}", CELLB), P(str(len(all_lines)), CELLB)]]
    story.append(grid(kpi, [2 * inch] * 5, header=False, zebra=False))

    fallback = [r for r in reps if r["view_note"]]
    stale = [r for r in reps if (age_days(r) or 0) > 9]
    if fallback:
        story.append(Paragraph(escape(
            f"Budget view {VIEW_ID} is not published to the Analytics share for {len(fallback)} of "
            f"{len(reps)} projects. Those projects use the closest view that carries the same CAC "
            f"columns, named in each project's section. Spot-check a few figures against Procore."), NOTE))
    if stale:
        story.append(Paragraph(escape(
            f"{len(stale)} projects are compared against a budget snapshot more than 9 days old. "
            f"Procore's CAC Variance measures change since that snapshot, so those lines can include "
            f"items already reported in earlier weeks. Taking a snapshot after each weekly report "
            f"resets the baseline."), NOTE))

    story.append(Paragraph("Projects", H2))
    rows = [hdr("Project", "Compared against snapshot", "Age", "Lines", "Increases", "Savings", "Net variance")]
    for r in sorted(reps, key=lambda r: -abs(totals(r)[0])):
        n, u, dn = totals(r)
        a = age_days(r)
        rows.append([Paragraph(f'<a href="#p{r["pid"]}" color="#1F3A5F"><b>{escape(label(r))}</b></a>', CELL),
                     P(f'{r["baseline"]["snapshot_name"]}, {et(r["baseline"]["snapshot_created"], "%b %d")}'),
                     P(f"{a} days" if a is not None else "", NUMC), P(str(len(r["lines"])), NUMC),
                     VP(u) if u else P("-", NUMC), VP(dn) if dn else P("-", NUMC),
                     VP(n, True) if r["lines"] else P("-", NUMC)])
    for name, reason in skipped:
        rows.append([P(name, CELLB), P(f"Not included: {reason}"), P(""), P(""), P(""), P(""), P("")])
    story.append(grid(rows, [2.6 * inch, 2.6 * inch, 0.7 * inch, 0.6 * inch, 1.15 * inch, 1.15 * inch, 1.2 * inch]))

    drv = {}
    for l in all_lines:
        g = drv.setdefault(l["driver"], [0, 0.0])
        g[0] += 1
        g[1] += l["variance"]
    if drv:
        story.append(Paragraph("Portfolio by driver", H2))
        rows = [hdr("Driver", "Lines", "Net variance")]
        for k, (n, v) in sorted(drv.items(), key=lambda kv: -abs(kv[1][1])):
            rows.append([P(k), P(str(n), NUMC), VP(v)])
        story.append(grid(rows, [4 * inch, 1.5 * inch, 2 * inch]))

    # ---------------- one section per project
    for r in sorted(reps, key=lambda r: r["project_name"]):
        lines, b = r["lines"], r["baseline"]
        n, u, dn = totals(r)
        story += [PageBreak(), Bookmark(f"p{r['pid']}", label(r)),
                  Paragraph(f'<a name="p{r["pid"]}"/>{escape(label(r))}', H1)]
        match = (f"{r['match_rate']:.0%} of lines match Procore's Previous CAC"
                 if r["match_rate"] is not None else (r["base_note"] or "newest snapshot"))
        meta = [[P("Procore data as of", CELLB), P(et(r["refreshed"]))],
                [P("Budget view", CELLB), P(f"{r['view_name']} ({r['view_id']})")],
                [P("Compared against snapshot", CELLB),
                 P(f"{b['snapshot_name']}, taken {et(b['snapshot_created'])} ({match})")],
                [P("Cost codes reviewed", CELLB), P(f"{r['line_count']} lines, {len(lines)} with a CAC variance")]]
        story.append(grid(meta, [2.2 * inch, 7.8 * inch], header=False, zebra=False))
        a = age_days(r)
        if a is not None and a > 9:
            story.append(Paragraph(f"Comparison snapshot is {a} days old; lines may include items "
                                   f"reported in earlier weeks.", NOTE))
        story.append(Spacer(1, 6))
        story.append(grid([[P("Net CAC variance", CELLB), P("Increases", CELLB), P("Savings", CELLB)],
                           [VP(n, True, True), VP(u, True, True), VP(dn, True, True)]],
                          [2.5 * inch] * 3, header=False, zebra=False))
        if not lines:
            story.append(Paragraph("No cost code has a CAC variance against the comparison snapshot.", BODY))
            continue

        story.append(Paragraph("Lines with a CAC variance", H2))
        rows = [hdr("Cost code", "Description", "CAC variance", "Driver", "Why")]
        for l in lines:
            rows.append([P(l["code"] + (" *" if l["mismatch"] else ""), CELLB), P(l["desc"]),
                         VP(l["variance"], True), P(l["driver"]), P(" ".join(l["explanation"][:2]))])
        rows.append([P("Total", CELLB), P(""), VP(n, True), P(""), P("")])
        story.append(grid(rows, [0.95 * inch, 1.75 * inch, 1.05 * inch, 1.0 * inch, 5.25 * inch]))
        if any(l["mismatch"] for l in lines):
            story.append(Paragraph("* Differs from Procore's CAC Variance column by $1 or more.", SUB))

        for i, l in enumerate(lines):
            block = [Paragraph("Line detail", H2)] if i == 0 else []
            block += [Paragraph(f"{escape(l['code'])}  {escape(l['desc'])} &nbsp;&nbsp; "
                               f"<font color='{'#B42318' if l['variance'] > 0 else '#067647'}'>"
                               f"{escape(signed(l['variance']))}</font>", H3)]
            rws = [hdr("Current CAC part", "At snapshot", "Now", "Change")]
            for k in COMPONENTS:
                dv = l["deltas"][k]
                if abs(dv) >= EPS:
                    rws.append([P(k), P(money(l["before"][k]), NUMC), P(money(l["after"][k]), NUMC), VP(dv)])
            rws.append([P("Current CAC", CELLB), P(money(l["before"]["Current CAC"]), NUMC),
                        P(money(l["after"]["Current CAC"]), NUMC), VP(l["variance"], True)])
            block += [grid(rws, [2.6 * inch, 1.5 * inch, 1.5 * inch, 1.5 * inch]), Spacer(1, 4),
                      Paragraph(escape(" ".join(l["explanation"])), BODY)]
            if l["items"]:
                irows = [hdr("Item", "Title", "Status", "Counts in", "Amount", "Updated")]
                for it in l["items"][:12]:
                    irows.append([P(item_label(it)), P(it["title"]), P(it["status"]), P(it["bucket"]),
                                  P(money(it["amount"]), NUMC), P(et(it["updated"], "%b %d"))])
                block += [Spacer(1, 4), grid(irows, [1.4 * inch, 3.1 * inch, 1.5 * inch, 1.7 * inch,
                                                     1.2 * inch, 0.8 * inch])]
                if len(l["items"]) > 12:
                    block.append(Paragraph(f"{len(l['items']) - 12} more items not shown.", SUB))
            else:
                block.append(Paragraph("No commitments, change events or owner PCOs on this code changed "
                                       "since the snapshot.", SUB))
            block.append(Spacer(1, 10))
            story.append(KeepTogether(block))

    # ---------------- method
    story += [PageBreak(), Bookmark("method", "Method"), Paragraph("Method", H2)]
    for t in [
        "Current CAC = Original Commitment + Adjustment Commitment + Buyout Commitment + Pending CO + "
        "Internal CO + Owner CO + Estimated COs. A CAC variance is fully explained by changes in these "
        "seven parts, so each line lists the parts that moved.",
        "Figures come from Procore's budget view columns in the Analytics share, not from recalculation. "
        "For each project, the comparison snapshot is the one whose Current CAC matches Procore's "
        "Previous CAC on the most lines (Previous CAC = Current CAC minus CAC Variance).",
        "Items listed under a line are the commitments, change events and owner PCOs on that cost code "
        "updated since the snapshot was taken. 'Counts in' shows which CAC part each item falls in today "
        "based on its status and scope, following the budget view's column definitions.",
        "Moves between parts with no net change (for example a subcontract going from Out For Bid to "
        "Out For Signature) are called out as status changes rather than cost changes.",
    ]:
        story += [Paragraph(escape(t), BODY), Spacer(1, 4)]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(GREY)
        canvas.drawString(0.5 * inch, 0.35 * inch, "CAC Variance Breakdown, All Projects. Confidential.")
        canvas.drawRightString(10.5 * inch, 0.35 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=landscape(letter), leftMargin=0.5 * inch,
                            rightMargin=0.5 * inch, topMargin=0.5 * inch, bottomMargin=0.6 * inch,
                            title="CAC Variance Breakdown, All Projects", author="EG Executive Dashboard")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def encrypt(src, dst, password):
    from pypdf import PdfReader, PdfWriter
    writer = PdfWriter(clone_from=PdfReader(src))
    writer.encrypt(user_password=password, owner_password=None, algorithm="AES-256")
    with open(dst, "wb") as f:
        writer.write(f)


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
        b = rep["baseline"]
        reps.append(rep)
        print(f"   {name:<40} view {rep['view_id']}{' (fallback)' if rep['view_note'] else ''}, "
              f"snapshot '{b['snapshot_name']}' {b['snapshot_created']:%Y-%m-%d}, "
              f"{len(rep['lines'])} lines, {sum(len(l['items']) for l in rep['lines'])} items")
    if not reps:
        sys.exit("No project produced a report.")

    os.makedirs(OUT_DIR, exist_ok=True)
    final = os.path.join(OUT_DIR, f"CAC_Variance_All_Projects_{datetime.now(ET):%Y-%m-%d}.pdf")
    tmp = os.path.join(tempfile.gettempdir(), "cac_plain.pdf")
    render_pdf(reps, skipped, tmp)
    encrypt(tmp, final, PASSWORD)
    os.remove(tmp)
    print(f"\n{len(reps)} projects in report, {len(skipped)} skipped. Wrote {final}")


if __name__ == "__main__":
    main()
