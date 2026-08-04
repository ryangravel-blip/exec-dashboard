"""
Amperity — Real-Time Dashboards (Streamlit port)

Ports the live Sales Performance / Customer Health dashboard (exec-dashboard-repo/index.html)
to Streamlit. Same Snowflake tables, same FYTD/quota/coverage logic, same Customer Health
scoring — just a different rendering layer.

One deliberate difference from the HTML version: the "Weekly pipeline changes" and
"Customer health — weekly summary" narrative blocks are NOT recomputed here. They're pulled
live from the deployed index.html on GitHub (which the exec-dashboard-weekly-refresh scheduled
task already updates every Monday) so there's a single source of truth for that diffing logic
instead of two copies to keep in sync.
"""

import html
import datetime as dt

import numpy as np
import pandas as pd
import requests
import streamlit as st
import snowflake.connector
from cryptography.hazmat.primitives import serialization

st.set_page_config(page_title="Amperity — Real-Time Dashboards", layout="wide")

# ─── CONSTANTS ──────────────────────────────────────────────────────────────
NAME_ALIAS = {
    "Christopher Hemmings": "Chris Hemmings",
    "Robert Pope": "Rob Pope",
    "Jeff Dome": "Jeffrey Dome",
    "David Newman": "Dave Newman",
}
EXCLUDED_REPS = {"Kevin Kovacevich"}
LATE_STAGES = {"Stage 4 – Proposal & Plan", "Stage 4 – Selection", "Stage 5 – Negotiation"}

GITHUB_INDEX_URL = "https://raw.githubusercontent.com/ryangravel-blip/exec-dashboard/main/index.html"
WEEKLY_SUMMARY_START, WEEKLY_SUMMARY_END = "<!-- WEEKLY_SUMMARY_START -->", "<!-- WEEKLY_SUMMARY_END -->"
HEALTH_SUMMARY_START, HEALTH_SUMMARY_END = "<!-- HEALTH_SUMMARY_START -->", "<!-- HEALTH_SUMMARY_END -->"


def alias(n):
    n = n or ""
    return NAME_ALIAS.get(n, n)


def esc(s):
    return html.escape(str(s)) if s is not None else ""


# ─── FY / FYTD WINDOW (Amperity FY starts Feb 1; Jan belongs to the prior FY) ──
# Computed fresh from today's date every run — matches the HTML dashboard's
# computeFYTD(), so closed actuals AND quota YTD auto-extend through the current
# month with no manual edits, and the FY label (e.g. "2027 Qn" prefix) stays
# correct across FY boundaries too (the HTML version hardcodes that prefix).
TODAY = dt.date.today()
FY_START_YEAR = TODAY.year if TODAY.month >= 2 else TODAY.year - 1
CURRENT_FY_LABEL_PREFIX = f"{FY_START_YEAR + 1} "


def compute_fytd():
    month_starts, month_ends = [], []
    cy, cm = FY_START_YEAR, 2  # start at February
    while True:
        month_starts.append(f"{cy}-{cm:02d}-01")
        next_month = dt.date(cy + 1, 1, 1) if cm == 12 else dt.date(cy, cm + 1, 1)
        last_day = (next_month - dt.timedelta(days=1)).day
        month_ends.append(f"{cy}-{cm:02d}-{last_day:02d}")
        if cy == TODAY.year and cm == TODAY.month:
            break
        cm += 1
        if cm > 12:
            cm = 1
            cy += 1
    return month_starts, month_ends


def compute_fy_full_months():
    starts = []
    cy, cm = FY_START_YEAR, 2
    for _ in range(12):
        starts.append(f"{cy}-{cm:02d}-01")
        cm += 1
        if cm > 12:
            cm = 1
            cy += 1
    return starts


FYTD_MONTH_STARTS, FYTD_MONTH_ENDS = compute_fytd()
FULL_YEAR_MONTHS = compute_fy_full_months()


def is_current_fy(fq):
    return bool(fq) and fq.startswith(CURRENT_FY_LABEL_PREFIX)


# ─── FORMATTERS ─────────────────────────────────────────────────────────────
def _isnum(v):
    return v is not None and not (isinstance(v, float) and np.isnan(v))


def js_round(x):
    """Match JS Math.round: round half away from zero (towards +Infinity on ties),
    unlike Python's round() which rounds half-to-even. Only differs from Python's
    round() on exact .5 ties (e.g. an ARR of exactly $500,500 -> 500.5K), but those
    do occur in real data, so match the live HTML dashboard's rounding exactly."""
    import math
    return int(math.floor(x + 0.5))


def N(v):
    if not _isnum(v):
        return "—"
    return f"{js_round(v):,}"


def K(v):
    if not _isnum(v):
        return "—"
    n = v / 1000
    return ("-$" if n < 0 else "$") + f"{js_round(abs(n)):,}K"


def PCT(v, d=0):
    if not _isnum(v):
        return "—"
    return f"{v * 100:.{d}f}%"


def cov_class(cov):
    if cov is None:
        return ""
    if cov >= 3:
        return "cov-good"
    if cov >= 1.5:
        return "cov-warn"
    return "cov-bad"


def parse_date(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, (dt.date, dt.datetime, pd.Timestamp)):
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    s = str(v)
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    try:
        n = int(s)
        return (dt.date(1970, 1, 1) + dt.timedelta(days=n)).strftime("%Y-%m-%d")
    except ValueError:
        return s


def fmt_date_short(dstr):
    if not dstr:
        return '<span style="color:#9ba3af">—</span>'
    try:
        d = dt.date.fromisoformat(dstr[:10])
    except ValueError:
        return esc(dstr)
    return d.strftime("%b %-d, %Y")


def fmt_contract_end(dstr):
    if not dstr:
        return '<span style="color:#9ba3af">—</span>'
    try:
        d = dt.date.fromisoformat(dstr[:10])
    except ValueError:
        return esc(dstr)
    days_out = (d - TODAY).days
    label = d.strftime("%b %-d, %Y")
    if days_out < 90:
        return f'<span class="contract-end-soon">{label}</span>'
    if days_out < 180:
        return f'<span class="contract-end-near">{label}</span>'
    return label


def fmt_score(v):
    return f"{v:.1f}" if _isnum(v) else '<span style="color:#9ba3af">—</span>'


def tag_html(t):
    cls = "tag-sub" if t == "Subscription" else ("tag-con" if t == "Consumption" else "tag-unk")
    return f'<span class="tag {cls}">{esc(t)}</span>'


# ─── SNOWFLAKE ──────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner=False)
def get_connection():
    cfg = st.secrets["snowflake"]
    pem = cfg["private_key"].replace("\\n", "\n").encode()
    key = serialization.load_pem_private_key(pem, password=None)
    pkb = key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return snowflake.connector.connect(
        account=cfg["account"],
        user=cfg["username"],
        private_key=pkb,
        warehouse=cfg["warehouse"],
        database=cfg.get("database", "load"),
        role=cfg["role"],
        client_session_keep_alive=True,
    )


@st.cache_data(ttl=600, show_spinner=False)
def run_query(sql: str) -> pd.DataFrame:
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetch_pandas_all()
    except Exception:
        # Connection may have gone stale (long-idle Community Cloud app) — reconnect once.
        get_connection.clear()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetch_pandas_all()
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ─── QUERIES ────────────────────────────────────────────────────────────────
Q_PIPELINE = """
    SELECT OPPORTUNITY_NAME, ACCOUNT_NAME, OWNER_NAME, RVP_NAME, STAGE_NAME,
           ARR, CLOSE_DT, CLOSE_FISCAL_PERIOD
    FROM PROD.GTM.SALES_PIPELINE
    WHERE STATE = 'Open'
      AND TYPE IN ('Upsell','Full License')
      AND STAGE_NAME IN (
        'Stage 1 - Discovery','Stage 2 - Solution Identification','Stage 3 - Validation',
        'Stage 4 – Proposal & Plan','Stage 4 – Selection','Stage 5 – Negotiation'
      )
    ORDER BY ARR DESC
"""

Q_CUSTOMER_HEALTH = """
    WITH sf_customers AS (
      SELECT
        cam.CUSTOMER_ID,
        cam.CUSTOMER_NAME,
        cam.ACCOUNT_ID,
        SUM(a.ARR) AS ARR,
        TO_CHAR(MAX(a.CONTRACT_END_DT), 'YYYY-MM-DD') AS CONTRACT_END_DT,
        TO_CHAR(MIN(a.CONTRACT_START_DT), 'YYYY-MM-DD') AS CONTRACT_START_DT,
        CASE WHEN MAX(CASE WHEN a.CONSUMPTION_TYPE IS NOT NULL AND a.CONSUMPTION_TYPE != '' THEN 1 ELSE 0 END) = 1
          THEN 'Consumption' ELSE 'Subscription' END AS CONTRACT_TYPE
      FROM PROD.DEALSBASE.CUSTOMER_ACCOUNT_MAP cam
      JOIN PROD.SALESFORCE.ACCOUNTS a ON cam.ACCOUNT_ID = a.ACCOUNT_ID
      WHERE a.TYPE = 'Customer' AND a.ARR > 0
      GROUP BY cam.CUSTOMER_ID, cam.CUSTOMER_NAME, cam.ACCOUNT_ID
    ),
    sf_vertical AS (
      SELECT cam.CUSTOMER_ID, a.VERTICAL, COUNT(*) AS cnt
      FROM PROD.DEALSBASE.CUSTOMER_ACCOUNT_MAP cam
      JOIN PROD.SALESFORCE.ACCOUNTS a ON cam.ACCOUNT_ID = a.ACCOUNT_ID
      WHERE a.VERTICAL IS NOT NULL AND a.VERTICAL != ''
      GROUP BY cam.CUSTOMER_ID, a.VERTICAL
      QUALIFY ROW_NUMBER() OVER (PARTITION BY cam.CUSTOMER_ID ORDER BY cnt DESC) = 1
    ),
    health AS (
      SELECT ACCOUNT_ID, P_SCORE, A_SCORE, M_SCORE, E_SCORE, OVERALL_SCORE, HEALTH_COLOR
      FROM PROD.ACCOUNT360.ACCOUNT_HEALTH
      QUALIFY ROW_NUMBER() OVER (PARTITION BY ACCOUNT_ID ORDER BY CALC_DT DESC) = 1
    )
    SELECT
      c.CUSTOMER_NAME, c.ARR, c.CONTRACT_TYPE, c.CONTRACT_START_DT, c.CONTRACT_END_DT,
      COALESCE(v.VERTICAL, '') AS VERTICAL,
      h.P_SCORE, h.A_SCORE, h.M_SCORE, h.E_SCORE, h.OVERALL_SCORE, h.HEALTH_COLOR
    FROM sf_customers c
    LEFT JOIN sf_vertical v ON c.CUSTOMER_ID = v.CUSTOMER_ID
    LEFT JOIN health h ON c.ACCOUNT_ID = h.ACCOUNT_ID
    ORDER BY c.ARR DESC
"""


def q_closed_fytd():
    return f"""
    SELECT CLOSE_MONTH, OPPORTUNITY_OWNER, COALESCE(ACCOUNT_RVP,'') as ACCOUNT_RVP,
           SUM(CLOSED_ARR) as CLOSED_ARR
    FROM load.finance_sandbox.mrp_sales_effect
    WHERE CLOSE_MONTH BETWEEN '{FYTD_MONTH_ENDS[0]}' AND '{FYTD_MONTH_ENDS[-1]}'
      AND (REP_TYPE = 'AE' OR ACCOUNT_RVP = OPPORTUNITY_OWNER)
    GROUP BY CLOSE_MONTH, OPPORTUNITY_OWNER, ACCOUNT_RVP
    """


def q_rvp_self_owned_deals():
    return f"""
    SELECT CLOSE_MONTH, OPPORTUNITY_OWNER, OPPORTUNITY_NAME, ACCOUNT_NAME, CLOSED_ARR
    FROM load.finance_sandbox.mrp_sales_effect
    WHERE CLOSE_MONTH BETWEEN '{FYTD_MONTH_ENDS[0]}' AND '{FYTD_MONTH_ENDS[-1]}'
      AND ACCOUNT_RVP = OPPORTUNITY_OWNER
      AND CLOSED_ARR <> 0
    ORDER BY CLOSE_MONTH
    """


def q_sales_targets():
    months_list = ",".join(f"'{m}'" for m in FULL_YEAR_MONTHS)
    return f"""
    SELECT ROLE_TYPE, NAME, MANAGER_NAME, MONTH_START, SUM(MONTHLY_QUOTA_FROM_PRORATED) as TARGET
    FROM load.finance_sandbox.mrp_sales_targets
    WHERE MONTH_START IN ({months_list})
      AND ROLE_TYPE IN ('AE','RVP')
      AND (IS_ACTIVE = TRUE OR IS_ACTIVE IS NULL)
    GROUP BY ROLE_TYPE, NAME, MANAGER_NAME, MONTH_START
    """


# ─── PRE-PROCESS ────────────────────────────────────────────────────────────
def preprocess_pipeline(df):
    deals = []
    for _, r in df.iterrows():
        owner = alias(r.get("OWNER_NAME"))
        if owner in EXCLUDED_REPS:
            continue
        deals.append({
            "name": r.get("OPPORTUNITY_NAME"),
            "account": r.get("ACCOUNT_NAME"),
            "owner": owner,
            "rvp": alias(r.get("RVP_NAME") or ""),
            "stage": r.get("STAGE_NAME"),
            "arr": float(r.get("ARR") or 0),
            "close_dt": parse_date(r.get("CLOSE_DT")),
            "fq": r.get("CLOSE_FISCAL_PERIOD") or "",
        })
    return deals


def preprocess_closed(df):
    reps = {}
    for _, r in df.iterrows():
        rep = alias(r.get("OPPORTUNITY_OWNER"))
        if rep in EXCLUDED_REPS:
            continue
        mgr = alias(r.get("ACCOUNT_RVP") or "")
        rec = reps.setdefault(rep, {"mgr": mgr, "total": 0.0})
        rec["total"] += float(r.get("CLOSED_ARR") or 0)
        if mgr:
            rec["mgr"] = mgr
    return reps


def preprocess_rvp_self_deals(df):
    out = []
    for _, r in df.iterrows():
        owner = alias(r.get("OPPORTUNITY_OWNER"))
        if owner in EXCLUDED_REPS:
            continue
        out.append({
            "owner": owner,
            "name": r.get("OPPORTUNITY_NAME"),
            "account": r.get("ACCOUNT_NAME"),
            "close_month": parse_date(r.get("CLOSE_MONTH")),
            "arr": float(r.get("CLOSED_ARR") or 0),
        })
    return out


def preprocess_targets(df):
    ae_manager, ae_annual, ae_ytd = {}, {}, {}
    rvp_annual, rvp_ytd = {}, {}
    ytd_months = set(FYTD_MONTH_STARTS)
    for _, r in df.iterrows():
        name = alias(r.get("NAME") or "")
        val = float(r.get("TARGET") or 0)
        mgr = alias(r.get("MANAGER_NAME") or "")
        m = parse_date(r.get("MONTH_START"))
        role = r.get("ROLE_TYPE")
        if role == "AE":
            if name in EXCLUDED_REPS:
                continue
            if mgr and name not in ae_manager:
                ae_manager[name] = mgr
            ae_annual[name] = ae_annual.get(name, 0) + val
            if m in ytd_months:
                ae_ytd[name] = ae_ytd.get(name, 0) + val
        elif role == "RVP":
            rvp_annual[name] = rvp_annual.get(name, 0) + val
            if m in ytd_months:
                rvp_ytd[name] = rvp_ytd.get(name, 0) + val
    return {
        "ae_manager": ae_manager, "ae_annual": ae_annual, "ae_ytd": ae_ytd,
        "ae_names": list(ae_annual.keys()),
        "rvp_annual": rvp_annual, "rvp_ytd": rvp_ytd,
    }


def preprocess_customer_health(df):
    rows = []
    for _, r in df.iterrows():
        def num(v):
            return float(v) if _isnum(v) and v != "" else None
        rows.append({
            "name": r.get("CUSTOMER_NAME"),
            "arr": float(r.get("ARR") or 0),
            "contract_type": r.get("CONTRACT_TYPE"),
            "contract_start": r.get("CONTRACT_START_DT") or "",
            "contract_end": r.get("CONTRACT_END_DT") or "",
            "vertical": r.get("VERTICAL") or "",
            "p_score": num(r.get("P_SCORE")),
            "a_score": num(r.get("A_SCORE")),
            "m_score": num(r.get("M_SCORE")),
            "e_score": num(r.get("E_SCORE")),
            "overall_score": num(r.get("OVERALL_SCORE")),
            "health_color": (r.get("HEALTH_COLOR") or "").lower(),
        })
    return rows


# ─── SALES PERFORMANCE: KPI CARDS ──────────────────────────────────────────
def build_cards(deals, closed, tgt):
    fy_deals = [d for d in deals if is_current_fy(d["fq"])]
    open_pipe_fy = sum(d["arr"] for d in fy_deals)
    all_time_pipe = sum(d["arr"] for d in deals)
    closed_fytd = sum(r["total"] for r in closed.values())
    annual_quota = sum(tgt["ae_annual"].values())
    remaining = annual_quota - closed_fytd
    coverage = open_pipe_fy / remaining if remaining > 0 else None
    late = [d for d in deals if d["stage"] in LATE_STAGES]
    late_val = sum(d["arr"] for d in late)

    by_rep_fy = {}
    for d in fy_deals:
        by_rep_fy[d["owner"]] = by_rep_fy.get(d["owner"], 0) + d["arr"]
    below_3x, reps_with_quota = 0, 0
    for name in tgt["ae_names"]:
        rem = tgt["ae_annual"].get(name, 0) - closed.get(name, {}).get("total", 0)
        if rem <= 0:
            continue
        reps_with_quota += 1
        if (by_rep_fy.get(name, 0) / rem) < 3:
            below_3x += 1

    fy_num = (FY_START_YEAR + 1) % 100
    return [
        {"label": "Open pipe (closing this FY)", "value": K(open_pipe_fy),
         "sub": f"{len(fy_deals)} deals, Stage 1+ · {K(all_time_pipe)} all-time"},
        {"label": "Closed-won FYTD", "value": K(closed_fytd),
         "sub": f"Feb–{TODAY.strftime('%b')} FY{fy_num}"},
        {"label": "Company coverage", "value": f"{coverage:.1f}x" if coverage is not None else "—",
         "sub": "FY open pipe ÷ (quota − closed)",
         "cls": "good" if (coverage is not None and coverage >= 3) else ("warn" if (coverage is not None and coverage < 1.5) else "")},
        {"label": "Late-stage value", "value": K(late_val),
         "sub": f"{len(late)} deals in Selection/Negotiation"},
        {"label": "Reps under 3x coverage", "value": f"{below_3x} / {reps_with_quota}",
         "sub": "of reps with remaining quota",
         "cls": "warn" if (reps_with_quota and below_3x > reps_with_quota / 2) else ""},
    ]


# ─── SALES PERFORMANCE: REP PRODUCTIVITY TABLE ─────────────────────────────
def build_rep_table_html(deals, closed, tgt, rvp_self_deals):
    by_rep = {}
    for d in deals:
        rec = by_rep.setdefault(d["owner"], {"rvp": d["rvp"], "stages": {}, "total": 0.0, "fy_total": 0.0})
        rec["stages"][d["stage"]] = rec["stages"].get(d["stage"], 0) + d["arr"]
        rec["total"] += d["arr"]
        if is_current_fy(d["fq"]):
            rec["fy_total"] += d["arr"]
        if d["rvp"]:
            rec["rvp"] = d["rvp"]

    all_reps = set(by_rep.keys()) | set(closed.keys()) | set(tgt["ae_names"])
    mgr_groups = {}
    for rep in all_reps:
        mgr = (by_rep.get(rep, {}).get("rvp") or closed.get(rep, {}).get("mgr")
               or tgt["ae_manager"].get(rep) or "Unassigned")
        mgr_groups.setdefault(mgr, []).append(rep)

    sorted_mgrs = sorted(
        mgr_groups.keys(),
        key=lambda mgr: -sum(by_rep.get(r, {}).get("total", 0) for r in mgr_groups[mgr]),
    )

    h = """<colgroup>
    <col style="width:14%">
    <col style="width:6.5%"><col style="width:6.5%"><col style="width:6%">
    <col style="width:5%"><col style="width:5%"><col style="width:5%"><col style="width:5%"><col style="width:5%">
    <col style="width:7.5%"><col style="width:7%"><col style="width:6.5%">
    <col style="width:6.5%"><col style="width:6.5%"><col style="width:7.5%">
    </colgroup>
    <thead>
    <tr class="grp-hdr">
      <th class="th-lbl grp-rep" rowspan="2">Rep</th>
      <th class="grp-1" colspan="3">YTD vs. quota</th>
      <th class="grp-2" colspan="8">Open pipeline — this FY</th>
      <th class="grp-3" colspan="3">Total pipeline — all dates</th>
    </tr>
    <tr>
      <th>Closed FYTD</th><th>Quota YTD</th><th>% to Quota</th>
      <th>S1</th><th>S2</th><th>S3</th><th>S4</th><th>S5</th>
      <th>Open Pipe</th><th>Gap to Quota</th><th>FY Coverage</th>
      <th>Total Pipe</th><th>Annual Quota</th><th>Annual Coverage</th>
    </tr>
    </thead><tbody>"""

    gT = {"s": [0, 0, 0, 0, 0], "openFY": 0.0, "allTime": 0.0, "closed": 0.0, "annual": 0.0, "ytdq": 0.0}

    for mgr in sorted_mgrs:
        reps = sorted(mgr_groups[mgr], key=lambda r: -by_rep.get(r, {}).get("total", 0))
        mS = [0.0, 0.0, 0.0, 0.0, 0.0]
        m_open_fy = m_all_time = m_closed = 0.0
        rep_rows = ""
        for rep in reps:
            d = by_rep.get(rep, {"stages": {}, "total": 0.0, "fy_total": 0.0})
            stages = d["stages"]
            s1 = stages.get("Stage 1 - Discovery", 0)
            s2 = stages.get("Stage 2 - Solution Identification", 0)
            s3 = stages.get("Stage 3 - Validation", 0)
            s4 = stages.get("Stage 4 – Proposal & Plan", 0) + stages.get("Stage 4 – Selection", 0)
            s5 = stages.get("Stage 5 – Negotiation", 0)
            open_fy = d["fy_total"]
            all_time = d["total"]
            cl = closed.get(rep, {}).get("total", 0)
            annual = tgt["ae_annual"].get(rep, 0)
            ytdq = tgt["ae_ytd"].get(rep, 0)
            remaining = annual - cl
            cov_fy = open_fy / remaining if remaining > 0 else None
            cov_annual = all_time / remaining if remaining > 0 else None
            pct_to_quota = cl / ytdq if ytdq > 0 else None
            mS[0] += s1; mS[1] += s2; mS[2] += s3; mS[3] += s4; mS[4] += s5
            m_open_fy += open_fy; m_all_time += all_time; m_closed += cl

            rep_rows += f"""<tr class="ae-row">
        <td class="lbl-indent">{esc(rep)}</td>
        <td>{K(cl)}</td><td>{K(ytdq) if ytdq else '—'}</td><td>{PCT(pct_to_quota) if pct_to_quota is not None else '—'}</td>
        <td>{K(s1) if s1 else '—'}</td><td>{K(s2) if s2 else '—'}</td><td>{K(s3) if s3 else '—'}</td><td>{K(s4) if s4 else '—'}</td><td>{K(s5) if s5 else '—'}</td>
        <td>{K(open_fy)}</td><td>{K(remaining) if annual else '—'}</td><td class="{cov_class(cov_fy)}">{f'{cov_fy:.1f}x' if cov_fy is not None else '—'}</td>
        <td>{K(all_time)}</td><td>{K(annual) if annual else '—'}</td>
        <td class="{cov_class(cov_annual)}">{f'{cov_annual:.1f}x' if cov_annual is not None else '—'}</td>
      </tr>"""

        m_annual = tgt["rvp_annual"].get(mgr)
        if m_annual is None:
            m_annual = sum(tgt["ae_annual"].get(r, 0) for r in reps)
        m_ytdq = tgt["rvp_ytd"].get(mgr)
        if m_ytdq is None:
            m_ytdq = sum(tgt["ae_ytd"].get(r, 0) for r in reps)
        m_remaining = m_annual - m_closed
        m_cov_fy = m_open_fy / m_remaining if m_remaining > 0 else None
        m_cov_annual = m_all_time / m_remaining if m_remaining > 0 else None
        m_pct_to_quota = m_closed / m_ytdq if m_ytdq > 0 else None

        h += f"""<tr class="mgr-row">
      <td class="lbl-sub">{esc(mgr)}</td>
      <td>{K(m_closed)}</td><td>{K(m_ytdq) if m_ytdq else '—'}</td><td>{PCT(m_pct_to_quota) if m_pct_to_quota is not None else '—'}</td>
      <td>{K(mS[0]) if mS[0] else '—'}</td><td>{K(mS[1]) if mS[1] else '—'}</td><td>{K(mS[2]) if mS[2] else '—'}</td><td>{K(mS[3]) if mS[3] else '—'}</td><td>{K(mS[4]) if mS[4] else '—'}</td>
      <td>{K(m_open_fy)}</td><td>{K(m_remaining) if m_annual else '—'}</td><td class="{cov_class(m_cov_fy)}">{f'{m_cov_fy:.1f}x' if m_cov_fy is not None else '—'}</td>
      <td>{K(m_all_time)}</td><td>{K(m_annual) if m_annual else '—'}</td>
      <td class="{cov_class(m_cov_annual)}">{f'{m_cov_annual:.1f}x' if m_cov_annual is not None else '—'}</td>
    </tr>"""
        h += rep_rows

        for i in range(5):
            gT["s"][i] += mS[i]
        gT["openFY"] += m_open_fy; gT["allTime"] += m_all_time; gT["closed"] += m_closed
        gT["annual"] += m_annual; gT["ytdq"] += m_ytdq

    g_remaining = gT["annual"] - gT["closed"]
    g_cov_fy = gT["openFY"] / g_remaining if g_remaining > 0 else None
    g_cov_annual = gT["allTime"] / g_remaining if g_remaining > 0 else None
    g_pct_to_quota = gT["closed"] / gT["ytdq"] if gT["ytdq"] > 0 else None

    h += f"""<tr class="total-row">
    <td class="lbl-sub">Total</td>
    <td>{K(gT['closed'])}</td><td>{K(gT['ytdq'])}</td><td>{PCT(g_pct_to_quota) if g_pct_to_quota is not None else '—'}</td>
    <td>{K(gT['s'][0])}</td><td>{K(gT['s'][1])}</td><td>{K(gT['s'][2])}</td><td>{K(gT['s'][3])}</td><td>{K(gT['s'][4])}</td>
    <td>{K(gT['openFY'])}</td><td>{K(g_remaining)}</td><td class="{cov_class(g_cov_fy)}">{f'{g_cov_fy:.1f}x' if g_cov_fy is not None else '—'}</td>
    <td>{K(gT['allTime'])}</td><td>{K(gT['annual'])}</td>
    <td class="{cov_class(g_cov_annual)}">{f'{g_cov_annual:.1f}x' if g_cov_annual is not None else '—'}</td>
  </tr>"""
    h += "</tbody>"

    footnote = ""
    if rvp_self_deals:
        items = "; ".join(
            f"{esc(d['owner'])} — {esc(d['name'])} ({esc(d['account'])}) — {K(d['arr'])} — closed {fmt_date_short(d['close_month'])}"
            for d in rvp_self_deals
        )
        n = len(rvp_self_deals)
        footnote = (
            f"Note: {n} deal{'s' if n > 1 else ''} closed where the RVP was also the Opportunity Owner "
            f"{'are' if n > 1 else 'is'} included in that RVP's actuals above (this is why an RVP's total can "
            f"exceed the sum of their AEs' totals): {items}"
        )

    return f'<table id="repTable">{h}</table>', footnote


# ─── CUSTOMER HEALTH: CARDS + TABLE ─────────────────────────────────────────
def build_health_cards(rows):
    total_arr = sum(r["arr"] for r in rows)
    return {
        "total": len(rows),
        "arr_label": f"${js_round(total_arr / 1e6):,}M",
        "red": sum(1 for r in rows if r["health_color"] == "red"),
        "yellow": sum(1 for r in rows if r["health_color"] == "yellow"),
        "green": sum(1 for r in rows if r["health_color"] == "green"),
    }


HEALTH_HEADERS = ["Customer", "Vertical", "ARR", "Type", "Contract Start", "Contract End",
                   "P Score", "A Score", "M Score", "E Score", "Overall Score"]

HEALTH_SORT_KEYS = {
    "Customer": lambda r: (r["name"] or "").lower(),
    "Vertical": lambda r: (r["vertical"] or "").lower(),
    "ARR": lambda r: r["arr"],
    "Type": lambda r: (r["contract_type"] or "").lower(),
    "Contract Start": lambda r: r["contract_start"] or "",
    "Contract End": lambda r: r["contract_end"] or "",
    "P Score": lambda r: r["p_score"] if r["p_score"] is not None else -1,
    "A Score": lambda r: r["a_score"] if r["a_score"] is not None else -1,
    "M Score": lambda r: r["m_score"] if r["m_score"] is not None else -1,
    "E Score": lambda r: r["e_score"] if r["e_score"] is not None else -1,
    "Overall Score": lambda r: r["overall_score"] if r["overall_score"] is not None else -1,
}


def build_health_table_html(rows):
    thead = "<thead><tr>" + "".join(f"<th>{h}</th>" for h in HEALTH_HEADERS) + "</tr></thead>"
    body = "<tbody>"
    for r in rows:
        arr_k = f"${js_round(r['arr'] / 1e3):,}K"
        row_cls = f"row-{r['health_color']}" if r["health_color"] in ("red", "yellow", "green") else ""
        body += f"""<tr class="{row_cls}">
      <td title="{esc(r['name'])}">{esc(r['name'])}</td>
      <td class="td-left">{esc(r['vertical']) if r['vertical'] else '<span style="color:#9ba3af">—</span>'}</td>
      <td>{arr_k}</td>
      <td class="td-center">{tag_html(r['contract_type'])}</td>
      <td>{fmt_date_short(r['contract_start'])}</td>
      <td>{fmt_contract_end(r['contract_end'])}</td>
      <td>{fmt_score(r['p_score'])}</td>
      <td>{fmt_score(r['a_score'])}</td>
      <td>{fmt_score(r['m_score'])}</td>
      <td>{fmt_score(r['e_score'])}</td>
      <td>{fmt_score(r['overall_score'])}</td>
    </tr>"""
    body += "</tbody>"
    return f'<table class="custarr-table">{thead}{body}</table>'


PAME_LEGEND_HTML = """
<div class="table-card health-legend">
  <div class="hl-col">
    <div class="hl-title">PAME score components (weighted average → Overall Score)</div>
    <div class="hl-row"><span class="hl-key">P — Protect</span><span class="hl-wt">50%</span><span class="hl-def">Churn risk: usage health, renewal status, workflow activity</span></div>
    <div class="hl-row"><span class="hl-key">M — Manage</span><span class="hl-wt">25%</span><span class="hl-def">Relationship health: call sentiment, exec engagement, multithreading</span></div>
    <div class="hl-row"><span class="hl-key">A — Accelerate</span><span class="hl-wt">20%</span><span class="hl-def">Adoption: consumption growth, platform/workflow engagement</span></div>
    <div class="hl-row"><span class="hl-key">E — Expand</span><span class="hl-wt">5%</span><span class="hl-def">Growth signals: renewal count, upsells, active add-ons</span></div>
  </div>
  <div class="hl-col">
    <div class="hl-title">Overall grade color rubric</div>
    <div class="hl-row"><span class="health-dot health-red"></span><span class="hl-def">Any auto-red trigger fires — login collapse, bad renewal, workflow gone dark, senior job change, or 2+ loose risk flags — regardless of score, or overall score &lt; 40</span></div>
    <div class="hl-row"><span class="health-dot health-yellow"></span><span class="hl-def">No auto-red trigger, but overall score &lt; 65, P score &lt; 55, or any of A/M/E score &lt; 30</span></div>
    <div class="hl-row"><span class="health-dot health-green"></span><span class="hl-def">No auto-red trigger and none of the yellow conditions above</span></div>
  </div>
</div>
"""

# ─── WEEKLY SUMMARY BLOCKS (pulled live from the deployed HTML dashboard) ──
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_weekly_summaries():
    try:
        resp = requests.get(GITHUB_INDEX_URL, timeout=10)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        return None, None, str(e)

    def extract(start, end):
        i, j = text.find(start), text.find(end)
        if i == -1 or j == -1:
            return None
        return text[i + len(start):j].strip()

    return extract(WEEKLY_SUMMARY_START, WEEKLY_SUMMARY_END), extract(HEALTH_SUMMARY_START, HEALTH_SUMMARY_END), None


# ─── CSS (subset of index.html's styles — tab/loading-chrome dropped since
# Streamlit provides its own) ────────────────────────────────────────────────
CSS = """
<style>
.stat-card, .custarr-stat, .table-card { background:#fff; border-radius:8px; box-shadow:0 1px 4px rgba(0,0,0,.08); }
.stat-card { padding:14px 18px; }
.stat-card .sc-label { font-size:10.5px; color:#6b7280; font-weight:600; text-transform:uppercase; letter-spacing:.4px; margin-bottom:6px; }
.stat-card .sc-value { font-size:22px; font-weight:700; color:#0C0C0C; }
.stat-card .sc-sub { font-size:11px; color:#9ba3af; margin-top:3px; }
.stat-card.warn .sc-value { color:#b91c1c; }
.stat-card.good .sc-value { color:#15803d; }

.section-title { font-size:14px; font-weight:700; color:#0C0C0C; margin:22px 0 10px; display:flex; align-items:center; gap:8px; }
.section-title .hint { font-weight:400; font-size:11px; color:#9ba3af; }

.table-card { padding:16px 18px; margin-bottom:14px; overflow-x:auto; }

.custarr-stat { padding:12px 18px; min-width:130px; }
.custarr-stat .cs-label { font-size:10.5px; color:#6b7280; font-weight:600; text-transform:uppercase; letter-spacing:.4px; margin-bottom:4px; }
.custarr-stat .cs-value { font-size:18px; font-weight:700; color:#1a1a2e; }
.custarr-stat .cs-sub { font-size:10.5px; color:#9ba3af; margin-top:2px; }

.tag { display:inline-block; font-size:10px; font-weight:600; border-radius:4px; padding:2px 7px; white-space:nowrap; }
.tag-sub { background:#CAEFEF; color:#155e75; }
.tag-con { background:#fef3c7; color:#92400e; }
.tag-unk { background:#f1f5f9; color:#6b7280; }

table { width:100%; border-collapse:collapse; }
th, td { padding:7px 10px; text-align:right; white-space:nowrap; font-size:12.5px; }
td.lbl-indent { text-align:left; font-weight:400; color:#374151; padding-left:26px; }
td.lbl-sub { text-align:left; font-weight:700; color:#1a1a2e; }
thead th { background:#f8fafc; color:#6b7280; font-size:10.5px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; border-bottom:2px solid #e5e7eb; }
thead th.th-lbl { text-align:left; }
tbody tr.mgr-row { background:#f8fafc; border-top:1px solid #e5e7eb; }
tbody tr.mgr-row td { font-weight:700; }
tbody tr.ae-row td { border-top:1px solid #f1f5f9; }
tbody tr.total-row td { border-top:2px solid #0C0C0C; font-weight:700; }
.cov-good { color:#15803d; font-weight:700; }
.cov-warn { color:#b45309; font-weight:700; }
.cov-bad { color:#b91c1c; font-weight:700; }

#repTable { table-layout:fixed; }
#repTable th, #repTable td { padding:5px 3px; font-size:10.8px; white-space:normal; word-break:break-word; line-height:1.25; }
#repTable td.lbl-indent, #repTable td.lbl-sub { padding-left:10px; }
#repTable thead tr.grp-hdr th { font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.3px; padding:5px 3px; border-bottom:1px solid rgba(0,0,0,.06); text-align:center; }
#repTable thead tr.grp-hdr th.grp-rep { background:#f8fafc; text-align:left; }
#repTable thead tr.grp-hdr th.grp-1 { background:#eef2f7; color:#475569; }
#repTable thead tr.grp-hdr th.grp-2 { background:#e7f5f7; color:#0e6a76; }
#repTable thead tr.grp-hdr th.grp-3 { background:#fbfbe0; color:#7a6c00; }

.rep-footnote { font-size:11px; color:#9ba3af; margin:4px 0 20px; line-height:1.5; }
.contract-end-soon { color:#dc2626; font-weight:600; }
.contract-end-near  { color:#d97706; font-weight:600; }

.health-dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:7px; vertical-align:middle; }
.health-red { background:#dc2626; }
.health-yellow { background:#d97706; }
.health-green { background:#15803d; }

.health-legend { display:flex; gap:28px; flex-wrap:wrap; }
.hl-col { flex:1; min-width:280px; }
.hl-title { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.3px; color:#6b7280; margin-bottom:8px; }
.hl-row { display:flex; align-items:baseline; gap:8px; margin-bottom:5px; font-size:11.5px; line-height:1.4; }
.hl-row .health-dot { margin-top:2px; }
.hl-key { font-weight:700; color:#1a1a2e; min-width:92px; flex:none; }
.hl-wt { color:#0e8a99; font-weight:700; min-width:30px; flex:none; }
.hl-def { color:#6b7280; }

.custarr-table th { background:#f8fafc; color:#6b7280; font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.4px; padding:8px 10px; border-bottom:2px solid #e5e7eb; white-space:nowrap; }
.custarr-table th:first-child { text-align:left; }
.custarr-table td { padding:6px 10px; font-size:12px; border-bottom:1px solid #f1f5f9; white-space:nowrap; }
.custarr-table td:first-child { text-align:left; color:#374151; max-width:260px; overflow:hidden; text-overflow:ellipsis; }
.custarr-table td:not(:first-child) { text-align:right; }
.custarr-table td.td-center { text-align:center; }
.custarr-table td.td-left { text-align:left; }
.custarr-table tbody tr.row-red td { background:#fbe9e9 !important; }
.custarr-table tbody tr.row-yellow td { background:#fdf3d9 !important; }
.custarr-table tbody tr.row-green td { background:#e7f5ea !important; }
.custarr-table tbody tr:nth-child(even) td { background:#fcfcfc; }
.custarr-count { font-size:11px; color:#9ba3af; margin-top:4px; }
</style>
"""

# ─── APP ─────────────────────────────────────────────────────────────────
# Guarded behind main()/__main__ so the pure logic above (formatters, FYTD math,
# preprocess_*, build_cards, build_rep_table_html, build_health_table_html) can be
# unit-tested by importing this module without triggering Snowflake calls or Streamlit
# widget rendering — `streamlit run app.py` still executes this normally since Python
# sets __name__ == "__main__" in that case.
def main():
    st.markdown(CSS, unsafe_allow_html=True)

    top_l, top_r = st.columns([5, 1])
    with top_l:
        st.markdown("### Amperity — Real-Time Dashboards")
        st.caption("Live · queried on every page load (cached 10 min) · " + TODAY.strftime("%B %-d, %Y"))
    with top_r:
        if st.button("🔄 Refresh now", use_container_width=True):
            run_query.clear()
            fetch_weekly_summaries.clear()
            st.rerun()

    try:
        with st.spinner("Querying Snowflake…"):
            pipe_df = run_query(Q_PIPELINE)
            closed_df = run_query(q_closed_fytd())
            tgt_df = run_query(q_sales_targets())
            health_df = run_query(Q_CUSTOMER_HEALTH)
            rvp_self_df = run_query(q_rvp_self_owned_deals())
    except Exception as e:
        st.error(f"Data load failed: {e}")
        st.stop()

    deals = preprocess_pipeline(pipe_df)
    closed = preprocess_closed(closed_df)
    tgt = preprocess_targets(tgt_df)
    health_rows = preprocess_customer_health(health_df)
    rvp_self_deals = preprocess_rvp_self_deals(rvp_self_df)
    sales_summary_html, health_summary_html, summary_fetch_err = fetch_weekly_summaries()

    tab_sales, tab_health = st.tabs(["Sales Performance", "Customer Health"])

    with tab_sales:
        cards = build_cards(deals, closed, tgt)
        cols = st.columns(len(cards))
        for col, c in zip(cols, cards):
            col.markdown(f"""<div class="stat-card {c.get('cls', '')}">
              <div class="sc-label">{c['label']}</div>
              <div class="sc-value">{c['value']}</div>
              <div class="sc-sub">{c['sub']}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown(
            '<div class="section-title">Weekly pipeline changes '
            '<span class="hint">Auto-generated Friday mornings, week over week</span></div>',
            unsafe_allow_html=True,
        )
        if sales_summary_html:
            st.markdown(f'<div class="table-card">{sales_summary_html}</div>', unsafe_allow_html=True)
        else:
            st.info("Couldn't fetch the weekly summary from the live dashboard"
                    + (f" ({summary_fetch_err})" if summary_fetch_err else "") + ". Tables below are still live.")

        st.markdown(
            '<div class="section-title">Rep productivity '
            '<span class="hint">Open pipe by stage, closed FYTD, coverage vs. remaining quota</span></div>',
            unsafe_allow_html=True,
        )
        rep_table_html, footnote = build_rep_table_html(deals, closed, tgt, rvp_self_deals)
        st.markdown(f'<div class="table-card">{rep_table_html}</div>', unsafe_allow_html=True)
        if footnote:
            st.markdown(f'<div class="rep-footnote">{footnote}</div>', unsafe_allow_html=True)

    with tab_health:
        hc = build_health_cards(health_rows)
        stat_defs = [
            ("Total Customers", str(hc["total"]), "active ARR", None),
            ("Total ARR", hc["arr_label"], "current", None),
            ("Red", str(hc["red"]), "health_color", "red"),
            ("Yellow", str(hc["yellow"]), "health_color", "yellow"),
            ("Green", str(hc["green"]), "health_color", "green"),
        ]
        cols = st.columns(5)
        for col, (label, value, sub, dot) in zip(cols, stat_defs):
            dot_html = f'<span class="health-dot health-{dot}"></span>' if dot else ""
            col.markdown(f"""<div class="custarr-stat">
              <div class="cs-label">{dot_html}{label}</div>
              <div class="cs-value">{value}</div>
              <div class="cs-sub">{sub}</div>
            </div>""", unsafe_allow_html=True)

        st.markdown(
            '<div class="section-title">Customer health — weekly summary '
            '<span class="hint">Auto-generated Monday mornings · health_color changes and score movers, week over week</span></div>',
            unsafe_allow_html=True,
        )
        if health_summary_html:
            st.markdown(f'<div class="table-card">{health_summary_html}</div>', unsafe_allow_html=True)
        else:
            st.info("Couldn't fetch the weekly summary from the live dashboard"
                    + (f" ({summary_fetch_err})" if summary_fetch_err else "") + ". Table below is still live.")

        st.markdown(
            '<div class="section-title">Customer health '
            '<span class="hint">ARR base from Salesforce · P/A/M/E + overall score and health_color from '
            'PROD.ACCOUNT360.ACCOUNT_HEALTH (latest snapshot)</span></div>',
            unsafe_allow_html=True,
        )
        st.markdown(PAME_LEGEND_HTML, unsafe_allow_html=True)

        c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 1])
        search = c1.text_input("Search", key="ch_search", placeholder="Search customers…", label_visibility="collapsed")
        type_filter = c2.selectbox("Type", ["All", "Subscription", "Consumption"], key="ch_type")
        health_filter = c3.selectbox("Health", ["All", "red", "yellow", "green"], key="ch_health",
                                      format_func=lambda x: x.capitalize())
        sort_col = c4.selectbox("Sort by", HEALTH_HEADERS, index=2, key="ch_sort")
        desc = c5.checkbox("Descending", value=True, key="ch_desc")

        filtered = [
            r for r in health_rows
            if (not search or search.lower() in (r["name"] or "").lower())
            and (type_filter == "All" or r["contract_type"] == type_filter)
            and (health_filter == "All" or r["health_color"] == health_filter)
        ]
        filtered.sort(key=HEALTH_SORT_KEYS[sort_col], reverse=desc)

        st.markdown(
            f'<div class="table-card" style="max-height:640px; overflow-y:auto;">{build_health_table_html(filtered)}</div>',
            unsafe_allow_html=True,
        )
        st.markdown(f'<div class="custarr-count">Showing {len(filtered)} of {len(health_rows)} customers</div>',
                    unsafe_allow_html=True)


if __name__ == "__main__":
    main()
