# Exec Dashboard (Sales Performance — Real-Time) — setup

Live dashboard: queries Snowflake on every page load, no baked-in data. Same architecture
as `people-dashboard-repo` and `mrp_reports` — a static `index.html` plus one Vercel
serverless function (`api/sql.js`) that authenticates to Snowflake with a JWT key pair.

This started as the `realtime-dashboard/` subfolder inside `mrp-lovable` (the `mrp_reports`
repo), pulled out into its own standalone repo so it can deploy to its own Vercel project
(`exec_dashboard`) independent of the monthly financial dashboard's release cadence.

## 1. Push this to GitHub

```
cd exec-dashboard-repo
git init
git add .
git commit -m "Live Sales Performance real-time dashboard"
git branch -M main
git remote add origin https://github.com/ryangravel-blip/exec-dashboard.git
git push -u origin main
```

(Create the empty `exec-dashboard` repo on GitHub first, or swap in whatever repo name you
prefer.)

## 2. Connect to the exec_dashboard Vercel project

In the Vercel dashboard, open the `exec_dashboard` project -> Settings -> Git, and connect it
to this new repo (or, if it's currently pointed at `mrp_reports`, switch the connected repo).
No framework preset needed — `vercel.json` handles the build (it just copies `index.html`
into `dist/`).

## 3. Set environment variables

In `exec_dashboard`'s Settings -> Environment Variables, add:

| Variable | Value |
|---|---|
| `SNOWFLAKE_ACCOUNT` | `XPAHNKF-AMPERITY_DATA_WAREHOUSE` |
| `SNOWFLAKE_USERNAME` | `RYAN.GRAVEL@AMPERITY.COM` |
| `SNOWFLAKE_WAREHOUSE` | `READ` |
| `SNOWFLAKE_ROLE` | `FINANCE_SU` |
| `SNOWFLAKE_DATABASE` | defaults to `load` if unset — only set if a query needs otherwise |
| `SNOWFLAKE_PRIVATE_KEY` | the PEM private key contents, with `\n` for line breaks |

**On the private key:** don't commit a key file to this repo (unlike `mrp-lovable`, which has
`rsa_key.p8` committed directly — a known issue there, worth cleaning up separately). Either
reuse the existing key pair and paste its private key into `SNOWFLAKE_PRIVATE_KEY`, or generate
a fresh key pair scoped to this dashboard and register the public key on the Snowflake user
(`ALTER USER ... SET RSA_PUBLIC_KEY = '...'`).

## 4. Redeploy

Trigger a deploy (push a commit, or redeploy from the Vercel dashboard) after the env vars are
set — Vercel doesn't apply new env vars to an already-running deployment.
