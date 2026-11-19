# Flame Wise Tattoo Collective – Invoices

Self-hosted invoice generator for the Ugreen NAS. Runs as one small container (~60 MB RAM).

- Accounts with roles: **Admin** (everything), **Staff** (all invoices), **Artist** (only their own invoices)
- Customizable: logo, accent color, business info, HST #, numbering, tax, default notes/terms, quick-add items, e-Transfer wording, email wording
- Clients get a private link to view, print/PDF and pay
- Payment: Interac e-Transfer to pay@flamewiseink.ca by default (main; can be changed per invoice), Helcim card checkout (optional)
- Card payments are confirmed with Helcim's API and marked paid automatically
- e-Transfers: record them yourself with "Record a payment"
- CSV export for bookkeeping

## How updates work

1. Code changes are pushed to `main` on GitHub.
2. GitHub Actions runs the tests, builds the image and pushes it to `ghcr.io/ar-va/flamewise-invoices`.
3. The workflow writes the new image tag into `docker-compose.yml` and commits it.
4. Portainer checks the repo every 5 minutes, sees the new commit, pulls the image and restarts the container.

If the tests fail, nothing is deployed.

## Install (about 30 minutes, once)

1. **GitHub token.** GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate. Scopes: `repo` and `read:packages`. No expiry or 1 year. Copy it.
2. **Registry in Portainer.** Portainer → Registries → Add registry → Custom registry. Name `GitHub`, URL `ghcr.io`, Authentication on, username = your GitHub username, password = the token.
3. **Stack from Git.** Portainer → Stacks → Add stack → name `invoices` → Repository.
   - Repository URL: `https://github.com/ar-va/web-invoice-flamewiseink-com`
   - Reference: `refs/heads/main`, Compose path: `docker-compose.yml`
   - Authentication on: GitHub username + the token
   - GitOps updates on → Polling → every `5m`. Turn on **Re-pull image**.
   - Environment variables: `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `BASE_URL` (later: `HELCIM_API_TOKEN`, `SECURE_COOKIES`)
   - Deploy the stack.
4. **Check it runs.** Open `http://<NAS-IP>:8095` and log in.
5. **Publish it.** Cloudflare Zero Trust → Networks → Tunnels → your tunnel → Public hostname → add `invoices.flamewiseink.ca` → service `http://<NAS-IP>:8095`.
6. **Lock cookies to https.** Stack → Environment variables → `SECURE_COOKIES` = `true` → Update the stack.
7. **Set up branding.** In the app: Settings → logo, HST #, quick-add items → Save. Then Accounts → add each artist.

Secrets (passwords, Helcim token) live only in Portainer's environment variables, never in the repo.

## Turn on Helcim card payments

1. Helcim → All Tools → Integrations → API Access Configurations → New.
2. Give it access to process transactions (needed for HelcimPay.js) and read transactions (used to verify payments). Copy the API token.
3. Paste it into `HELCIM_API_TOKEN` in the Portainer stack → Update the stack.
4. Settings page should show **API token: connected**.
5. Test: make a $1.00 invoice to yourself → Mark as sent → open the client link → Pay by card → it should flip to **Paid**. Refund it in Helcim afterwards.

Optional: Settings → "Pass Helcim's convenience fee to the client" applies Helcim's convenience fee rate to card checkouts (the fee must also be set up in your Helcim account). The invoice is credited the invoice amount; the extra fee is noted on the payment.

## Daily use

1. **+ New invoice** → pick client (past clients autofill) → add lines (or quick-add buttons) → Create.
2. **Mark as sent** → Copy link / Open in email app / Email (if SMTP is set).
3. e-Transfer arrives → open the invoice → **Record a payment** → method e-Transfer → Save.
4. Card payments record themselves.

Statuses: Draft (client can't open the link yet) → Sent → Partly paid → Paid. Overdue = sent and past due. Void = cancelled.

## Backups

Everything lives in the Docker volume `invoices_invoices_data` (Portainer → Volumes). On disk: `/volume1/@docker/volumes/invoices_invoices_data/_data` (`invoices.db` + `uploads/`). Quick manual backup: Portainer → Containers → invoices → Console → `cp /data/invoices.db /data/backup-$(date +%F).db`.

## Optional extra protection

Cloudflare Zero Trust → Access → Applications → protect `invoices.flamewiseink.ca` for staff only, and add a bypass policy for paths `/i/*`, `/static/*`, `/uploads/*` so clients can still open and pay invoices.

## Updating

Push to `main`. Within about 10 minutes the new version is live. Data is kept in the volume.

To force it right away: Portainer → Stacks → invoices → Pull and redeploy.
To roll back: revert the last "Deploy ..." commit on GitHub.

## Files

- `app/app.py` – the application
- `app/templates/` – pages (`_doc.html` is the invoice layout itself)
- `app/static/` – styles and scripts
- `app/Dockerfile` – image recipe
- `docker-compose.yml` – Portainer stack (image tag set by GitHub Actions)
- `.github/workflows/build.yml` – test, build, deploy
- `tests/test_app.py` – automated tests
