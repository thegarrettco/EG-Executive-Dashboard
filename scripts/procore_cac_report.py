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

    proj = d["project"].iloc[0] if len(d["project"]) else {}
    matched = pd.Series([l["matched_snapshot"] for l in lines if l["matched_snapshot"]])
    return {
        "pid": pid, "project_name": name,
        "project_number": str(proj.get("project_number", "") if len(proj) else ""),
        "view_id": view_id, "view_name": view_name, "view_note": view_note,
        "refreshed": refreshed, "mode": mode, "lines": lines, "line_count": len(live),
        "newest_snapshot": newest, "compared_to": matched.mode().iloc[0] if len(matched) else None,
        "generated": datetime.now(timezone.utc),
    }


# ------------------------------------------------------------------- PDF

def render_pdf(reps, skipped, path):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Flowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    NAVY = colors.HexColor("#1F3A5F")
    INK, GREY, RULE = colors.HexColor("#1D2939"), colors.HexColor("#667085"), colors.HexColor("#E4E7EC")
    ZEBRA, HEAD = colors.HexColor("#F9FAFB"), colors.HexColor("#EEF2F6")
    UP, DOWN = "#B42318", "#067647"
    ss = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=ss["Normal"], fontName="Helvetica", fontSize=8, leading=10, textColor=INK)
    TITLE = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=NAVY)
    SUBT = ParagraphStyle("s", parent=base, fontSize=9, leading=12, textColor=GREY)
    SEC = ParagraphStyle("sec", parent=base, fontName="Helvetica-Bold", fontSize=11, leading=14,
                         textColor=NAVY, spaceBefore=4, spaceAfter=6)
    B = ParagraphStyle("b", parent=base, fontName="Helvetica-Bold")
    R = ParagraphStyle("r", parent=base, alignment=TA_RIGHT)
    RB = ParagraphStyle("rb", parent=R, fontName="Helvetica-Bold")
    SM = ParagraphStyle("sm", parent=base, fontSize=7.5, leading=9.5, textColor=GREY)
    KL = ParagraphStyle("kl", parent=base, fontSize=7.5, textColor=GREY)
    KV = ParagraphStyle("kv", parent=base, fontName="Helvetica-Bold", fontSize=13, leading=16)
    PH = ParagraphStyle("ph", parent=base, fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=colors.white)
    PHR = ParagraphStyle("phr", parent=PH, alignment=TA_RIGHT, fontName="Helvetica", fontSize=8.5)
    W = 10 * inch

    class Bookmark(Flowable):
        def __init__(self, key, title):
            super().__init__()
            self.key, self.title = key, title

        def wrap(self, *a):
            return (0, 0)

        def draw(self):
            self.canv.bookmarkPage(self.key)
            self.canv.addOutlineEntry(self.title, self.key, level=0)

    class ProjectTable(Table):
        """Project table whose repeated title row says 'continued' on later pages."""
        _cont_title = None

        def split(self, availWidth, availHeight):
            parts = super().split(availWidth, availHeight)
            # never leave the title and column headers stranded with fewer than 2 rows under them
            if parts and parts[0]._nrows - self.repeatRows < 2:
                return []
            for part in parts[1:]:
                part._cont_title = self._cont_title
                if self._cont_title is not None:
                    # replace the row (it is shared with the first part), don't edit it in place
                    part._cellvalues[0] = [self._cont_title] + list(part._cellvalues[0][1:])
            return parts

    def p(text, style=base):
        return Paragraph(escape(str(text)), style)

    def amt(v, style=R, bold=False):
        c = UP if v > 0 else DOWN
        return Paragraph(f"<font color='{c}'>{'<b>' if bold else ''}{escape(money(v))}{'</b>' if bold else ''}</font>", style)

    def h(text, right=False):
        return Paragraph(f"<b>{escape(text)}</b>", ParagraphStyle("h", parent=SM, textColor=GREY,
                                                                   alignment=TA_RIGHT if right else 0))

    def et(ts, fmt="%b %d, %Y %I:%M %p ET"):
        return pd.Timestamp(ts).tz_convert(ET).strftime(fmt) if pd.notna(ts) else "n/a"

    def tot(r):
        v = [l["variance"] for l in r["lines"]]
        return sum(v), sum(x for x in v if x > 0), sum(x for x in v if x < 0)

    def label(r):
        return r["project_name"] + (f"  {r['project_number']}" if r["project_number"] else "")

    def base_style(n, header_rows=1):
        st = [("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
              ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
              ("LINEBELOW", (0, header_rows - 1), (-1, -1), 0.4, RULE)]
        for i in range(header_rows, n):
            if (i - header_rows) % 2 == 1:
                st.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
        return st

    story = []
    all_lines = [l for r in reps for l in r["lines"]]
    net = sum(l["variance"] for l in all_lines)
    refreshed = max((r["refreshed"] for r in reps if pd.notna(r["refreshed"])), default=pd.NaT)

    # ---------------- page 1: summary
    story += [p("CAC Variance Breakdown", TITLE),
              p(f"All projects  |  Procore data as of {et(refreshed)}  |  Generated {et(reps[0]['generated'])}", SUBT),
              Spacer(1, 10)]
    kpis = [("Net CAC variance", amt(net, KV, True)),
            ("Increases", amt(sum(l["variance"] for l in all_lines if l["variance"] > 0), KV, True)),
            ("Savings", amt(sum(l["variance"] for l in all_lines if l["variance"] < 0), KV, True)),
            ("Projects with a variance", p(f"{sum(1 for r in reps if r['lines'])} of {len(reps) + len(skipped)}", KV)),
            ("Lines with a variance", p(str(len(all_lines)), KV))]
    k = Table([[p(a, KL) for a, _ in kpis], [b for _, b in kpis]], colWidths=[W / 5] * 5, hAlign="LEFT")
    k.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 1.2, NAVY), ("TOPPADDING", (0, 0), (-1, -1), 4),
                           ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [k, Spacer(1, 14)]

    rows = [[h("Project"), h("Lines", True), h("Traced", True), h("Increases", True), h("Savings", True),
             h("Net CAC variance", True), h("Latest snapshot in Procore data")]]
    for r in sorted(reps, key=lambda r: r["project_name"]):
        n, u, dn = tot(r)
        ns = r["newest_snapshot"]
        snap = f"{ns['snapshot_name']}, {et(ns['snapshot_created'], '%b %d')}" if ns is not None else "none"
        if r["lines"]:
            tr = sum(1 for l in r["lines"] if l["traced"])
            rows.append([Paragraph(f'<a href="#p{r["pid"]}" color="#1F3A5F"><b>{escape(r["project_name"])}</b></a>', base),
                         p(len(r["lines"]), R), p(f"{tr}/{len(r['lines'])}", R),
                         amt(u) if u else p("-", R), amt(dn) if dn else p("-", R), amt(n, R, True), p(snap, SM)])
        else:
            rows.append([p(r["project_name"]), p("0", R), p("", R), p("-", R), p("-", R),
                         p("No variance", ParagraphStyle("nv", parent=R, textColor=GREY)), p(snap, SM)])
    for name, reason in skipped:
        rows.append([p(name), p("", R), p("", R), p("", R), p("", R), p("", R), p(f"Not included: {reason}", SM)])
    t = Table(rows, colWidths=[2.3 * inch, 0.55 * inch, 0.65 * inch, 1.2 * inch, 1.2 * inch, 1.3 * inch, 2.8 * inch],
              repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle(base_style(len(rows)) + [("LINEBELOW", (0, 0), (-1, 0), 0.8, NAVY)]))
    story.append(t)

    notes = ["Net CAC variance is Procore's own CAC Variance column (Current CAC minus Previous CAC), "
             "so totals should match the budget page."]
    if any(r["view_note"] for r in reps):
        notes.append(f"Budget view {VIEW_ID} is not published to Procore's Analytics data, so figures come from "
                     f"view 299759 ('Eric's Report with Buyout - Change Events'), which has the same CAC columns.")
    notes.append("Traced = lines whose comparison point was found in Procore's saved snapshots, so the report can "
                 "show which CAC parts moved. Untraced lines still show the correct variance and list the items "
                 "that changed recently, but not the part-by-part change.")
    if any(r["mode"] != "procore" for r in reps):
        notes.append("Projects where Procore's CAC Variance column was blank are compared against their latest snapshot.")
    story.append(Spacer(1, 8))
    for n_ in notes:
        story += [p(n_, SM), Spacer(1, 2)]

    # ---------------- detail: one compact table per project, continuous flow
    story.append(PageBreak())
    first = True
    for r in sorted(reps, key=lambda r: r["project_name"]):
        if not r["lines"]:
            continue
        n, _, _ = tot(r)
        traced = sum(1 for l in r["lines"] if l["traced"])
        title_row = [Paragraph(f'<a name="p{r["pid"]}"/>{escape(label(r))}', PH), "", "",
                     Paragraph(f"{len(r['lines'])} lines ({traced} traced)  |  net {escape(money(n))}", PHR), ""]
        cont = Paragraph(f"{escape(label(r))} <font size='8'>(continued)</font>", PH)
        rows = [title_row, [h("Cost code"), h("Description"), h("CAC variance", True), h("Driver"), h("Why")]]
        for l in r["lines"]:
            why = " ".join(l["explanation"])
            items = l["items"][:3]
            if items:
                parts = [f"<b>{escape(i['ref'])}</b> {escape(i['title'][:60])} ({escape(i['status'])}"
                         f"{', ' + escape(i['bucket']) if i['bucket'] != 'not in CAC' else ', not in CAC'}, "
                         f"{escape(money(i['amount']))})" for i in items]
                more = f"; +{len(l['items']) - 3} more" if len(l["items"]) > 3 else ""
                why_html = escape(why) + "<br/><font color='#667085'>" + "; ".join(parts) + more + "</font>"
            else:
                why_html = escape(why)
            rows.append([p(l["code"] + ("" if l["traced"] else " \u2020"), B), p(l["desc"]),
                         amt(l["variance"], R, True), p(l["driver"]), Paragraph(why_html, base)])
        rows.append([p("Total", B), "", amt(n, R, True), "", ""])
        t = ProjectTable(rows, colWidths=[0.85 * inch, 1.55 * inch, 0.95 * inch, 0.85 * inch, 5.8 * inch],
                         repeatRows=2, hAlign="LEFT")
        t._cont_title = cont
        st = base_style(len(rows), header_rows=2) + [
            ("SPAN", (0, 0), (2, 0)), ("SPAN", (3, 0), (4, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), NAVY), ("BACKGROUND", (0, 1), (-1, 1), HEAD),
            ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, NAVY), ("BACKGROUND", (0, -1), (-1, -1), colors.white)]
        t.setStyle(TableStyle(st))
        head = [p("Detail by project", SEC)] if first else []
        first = False
        block = head + [Bookmark(f"p{r['pid']}", r["project_name"]), t]
        # short tables move whole to the next page; long ones split with a "(continued)" header
        story += ([KeepTogether(block)] if len(r["lines"]) <= 6 else block) + [Spacer(1, 14)]

    story.append(p("\u2020 Untraced line: the variance is Procore's, but its comparison point is not among the "
                   "saved snapshots in Procore's Analytics data. Items listed are those changed since the latest "
                   "snapshot available.", SM))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GREY)
        canvas.drawString(0.5 * inch, 0.32 * inch, "CAC Variance Breakdown  |  Confidential")
        canvas.drawRightString(10.5 * inch, 0.32 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=landscape(letter), leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.45 * inch, bottomMargin=0.55 * inch,
                            title="CAC Variance Breakdown", author="EG Executive Dashboard")
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
        reps.append(rep)
        traced = sum(1 for l in rep["lines"] if l["traced"])
        print(f"   {name:<40} view {rep['view_id']}, {rep['mode']} variance, "
              f"{len(rep['lines'])} lines ({traced} traced), "
              f"{sum(len(l['items']) for l in rep['lines'])} items")
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
