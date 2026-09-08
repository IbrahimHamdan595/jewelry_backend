# Secret rotation runbook (first written for NEX-46)

Written because the pre-`.dockerignore` images embedded `.env` in a layer:
anyone who could pull an old image reads every secret below. Rotation is
what protects us — old registry images keep the old values forever.

**Do this outside shop hours.** Rotating `JWT_SECRET` logs everyone out.

Order matters; each step says where the new value must land **before** the
service restarts.

## 1. Neon database password

1. Neon console → the project's role → *Reset password*. Copy the new
   `DATABASE_URL`.
2. **Immediately** update `DATABASE_URL` in Render (backend service →
   Environment). Render restarts the service; if the env var is updated
   first, it comes up connected. If the app restarts between reset and
   update it cannot connect — fix is just completing step 2.
3. Update your local `.env`.

## 2. JWT_SECRET (backend + frontend together)

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(64))"`
2. Set the same value in **both** places before either restarts:
   - Render → backend → `JWT_SECRET`
   - Vercel → frontend → `JWT_SECRET` (the Next middleware verifies the
     same token — mismatched values make it reject every session)
3. Redeploy the Vercel frontend (env changes need a redeploy).
4. Everyone is logged out once. Expected.
5. Update local `.env`.

## 3. Cloudflare R2 keys

1. Cloudflare dashboard → R2 → *Manage API tokens* → create a new token
   scoped to the bucket, note key id + secret.
2. Update `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` in Render, then
   **revoke the old token** only after the service has restarted and an
   image upload works.
3. Update local `.env`.

## 4. GOLD_API_KEY

1. goldapi.io dashboard → regenerate the key (or create a new account
   token and delete the old).
2. Update `GOLD_API_KEY` in Render and local `.env`. The poller picks it
   up on restart; verify via the next successful rate poll (or the
   Discord alert staying quiet).

## 5. Discord webhook

1. Discord → channel settings → Integrations → delete the old webhook,
   create a new one.
2. Update `DISCORD_WEBHOOK_URL` in Render and local `.env`.

## 6. Seeded admin password

The seeder only creates the admin if missing (`app/seed.py`), so changing
the env var does **not** change the live password. Both are needed:

1. Log in as the admin and change the password through the app
   (`POST /api/auth/change-password` / the profile UI). This writes an
   auth-audit entry, as intended.
2. Also update `SEED_ADMIN_PASSWORD` in Render and local `.env` so a
   future re-seed on a fresh DB doesn't resurrect the leaked value.

## Afterwards

- Confirm POS login, an image upload, and a rate poll all work.
- Do **not** bother deleting old images from the registry as a security
  measure — treat their contents as public; the rotation above is the fix.
