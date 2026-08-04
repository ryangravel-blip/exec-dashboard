# Exec Dashboard — Streamlit version

A Streamlit port of `../index.html` (the live "Sales Performance / Customer Health"
dashboard). Same Snowflake tables, same FYTD/quota/coverage/health-score logic — just a
different rendering layer, deployed on Streamlit Community Cloud instead of Vercel.

## What's identical to the HTML dashboard
- All 5 Snowflake queries (pipeline, closed FYTD, sales targets, customer health,
  RVP-self-owned deals), including the dynamic FYTD window that auto-extends every month.
- KPI cards, Rep Productivity table (RVP-own-quota logic, Gap to Quota column, RVP
  self-owned-deal footnote), Customer Health table (P/A/M/E scores, health_color row tinting,
  PAME legend, search/type/health filters, sortable columns).

## One deliberate difference
The **"Weekly pipeline changes"** and **"Customer health — weekly summary"** narrative
blocks are not recomputed here — this app fetches them live from the deployed
`index.html` on GitHub (`raw.githubusercontent.com/ryangravel-blip/exec-dashboard/main/index.html`),
which the `exec-dashboard-weekly-refresh` scheduled task already updates every Monday. This
keeps the diffing logic in one place instead of maintaining two copies. Everything else
(cards, tables) queries Snowflake directly and is fully live.

## Deploy to Streamlit Community Cloud

1. Go to [share.streamlit.io](https://share.streamlit.io) and sign in with the GitHub
   account that owns `ryangravel-blip/exec-dashboard`.
2. Click **New app** → select repo `ryangravel-blip/exec-dashboard`, branch `main`,
   main file path `streamlit-app/app.py`.
3. Before (or right after) deploying, open **Settings → Secrets** on the app and paste
   in the contents of `.streamlit/secrets.toml.example`, filled in with real values —
   same Snowflake key pair as the Vercel `exec_dashboard` project's environment variables
   (Vercel → `exec_dashboard` → Settings → Environment Variables), or a fresh key pair
   registered on the Snowflake user.
4. Deploy. The app auto-redeploys on every push to `main` — including the weekly
   refresh task's Monday commits (harmless; it's just picking up the new summary text via
   the GitHub fetch above, not a code change).

## Local development

```
cd streamlit-app
pip install -r requirements.txt
mkdir -p .streamlit && cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml with real credentials
streamlit run app.py
```

## Notes
- Query results are cached 10 minutes (`st.cache_data(ttl=600)`) to avoid hammering
  Snowflake on every Streamlit rerun (a rerun happens on every widget interaction, unlike
  the HTML version which only queries once per page load). Use the **Refresh now** button
  to force a re-query.
- Unlike the Vercel dashboard, this app has no access control by default — anyone with the
  URL can view it once deployed. Streamlit Community Cloud supports restricting an app to
  specific viewers (App settings → Sharing) if that's needed; not configured here.
