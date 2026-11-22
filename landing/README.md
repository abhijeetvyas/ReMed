# ReMed landing site

Static site served at https://remit.appsforlife.app (AppsForLife / Coolify).

```
landing/
├── index.html      # the landing page
├── terms.html      # Terms of Service
├── assets/
│   └── style.css   # shared styles (dark, mobile-first)
└── Dockerfile      # nginx:alpine static server, for Coolify
```

## Deploy to Coolify

1. Push the repo; in Coolify: **New Resource → Dockerfile**, select this repo.
2. Set **Docker Build Context Directory** to `/landing` and **Port** to `80`.
3. In the resource's **Domains**, set `https://remit.appsforlife.app` — Coolify
   issues the certificate via its proxy (your Coolify instance already does
   this for the main domain), so nothing needs to change in Cloudflare DNS
   beyond the `remit` record you already created.

## Editing the channel links

The Telegram deep link lives in the buttons across `index.html` / `terms.html`
and in the comment block near the bottom of each page:

- `https://t.me/Reminderformedicinebot?start=web` — opens the bot directly in
  the Telegram app if installed; otherwise t.me shows an "Open in Telegram"
  page. The `start=web` payload tells the bot the user came from the website.

WhatsApp (future): the "coming soon" button is already styled (`.btn-wa`).
When ready, switch it from `<span>` to `<a>` and set
`href="https://wa.me/<number>?text=hi"` — same universal-link behaviour
(opens the WhatsApp app if installed, else wa.me's web page).

## Local preview

```bash
cd landing && python3 -m http.server 8080
```
