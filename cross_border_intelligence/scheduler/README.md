# Cloudflare scheduler

Deploy this directory with Wrangler. Store `GITHUB_TOKEN`, `SCHEDULER_TEST_SECRET`, and
`GITHUB_TOKEN_EXPIRES_AT` as Cloudflare Secrets. Never commit their values.

The default cron expressions are UTC. Beijing 08:07, 08:37, and 09:07 handle the
previous day. Beijing 12:07 scans the previous seven days and resumes the oldest
missing date only when no workflow run is active.
