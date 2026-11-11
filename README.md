# Twin Response Monitoring

How fast the twin answers in prod voice calls, built hourly from Langfuse and published to Cloudflare Pages behind one shared password.

- `scripts/latency_report.py` reads production spans from Langfuse (voice agent + chat brain) and writes one HTML page.
- `.github/workflows/refresh.yml` runs it every hour (and on demand) and deploys the page to the Pages project `twin-response`.
- `functions/_middleware.js` asks for the password (`DASHBOARD_PASSWORD`) on every request.

Run it locally: `LANGFUSE_PUBLIC_KEY=… LANGFUSE_SECRET_KEY=… LANGFUSE_BASE_URL=https://us.cloud.langfuse.com python3 scripts/latency_report.py --out site/index.html`

## One-time setup

1. **Cloudflare:** create the Pages project once:
   `npx wrangler login` then `npx wrangler pages project create twin-response --production-branch=main`.
2. **Password:** `npx wrangler pages secret put DASHBOARD_PASSWORD --project-name twin-response`.
3. **API token:** Cloudflare dashboard → My Profile → API Tokens → Create Token → template "Edit Cloudflare Workers"
   (or a custom token with *Account → Cloudflare Pages → Edit*). Note the Account ID from the dashboard sidebar.
4. **GitHub secrets** (repo → Settings → Secrets and variables → Actions):
   `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`.
5. **First run:** Actions → Refresh dashboard → Run workflow. The page is at `https://twin-response.pages.dev`.

Refresh by hand any time from the same Run workflow button. Change the password with step 2; it applies on the next request.
