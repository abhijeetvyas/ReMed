# ReMed landing site

Static site served at https://remit.appsforlife.app (AppsForLife / Coolify).

```
landing/
├── index.html      # the landing page
├── terms.html      # Terms of Service
├── assets/
│   ├── style.css   # shared styles (light, editorial)
│   └── brand/      # logo & favicon assets (see below)
├── logo-options.html  # working file — logo concepts review, NOT deployed
└── Dockerfile      # nginx:alpine static server, for Coolify
```

## Logo / favicon assets (`assets/brand/`)

| File | Purpose |
|---|---|
| `logo.svg` | vector master of the capsule mark (light background) |
| `favicon.svg` | favicon for modern browsers |
| `favicon-32.png` / `favicon-16.png` | PNG fallbacks (Safari tab) |
| `apple-touch-icon.png` | 180px, iOS home-screen icon |
| `icon-master.svg` | 512px source used to regenerate the PNGs |

Regenerate the PNGs after editing `icon-master.svg` (macOS):

```bash
cd assets/brand
qlmanage -t -s 512 -o /tmp icon-master.svg
sips -z 180 180 /tmp/icon-master.svg.png --out apple-touch-icon.png
sips -z 32 32  /tmp/icon-master.svg.png --out favicon-32.png
sips -z 16 16  /tmp/icon-master.svg.png --out favicon-16.png
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
