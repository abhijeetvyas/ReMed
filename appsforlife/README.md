# AppsForLife — main domain site

Minimal landing page served at https://appsforlife.app (AppsForLife / Coolify).

Self-contained single file — no shared assets, no build step. It reuses the
ReMed theme (light editorial: Source Serif 4 headlines, Inter body,
JetBrains Mono eyebrows, black & white palette).

```
appsforlife/
├── index.html   # the whole page (styles inlined)
└── Dockerfile   # nginx:alpine static server, for Coolify
```

When a new app ships, add another `.app-card` entry to the apps grid in
`index.html` and remove the "More apps are on the way." placeholder once
there are several.

## Deploy to Coolify

1. Push the repo; in Coolify: **New Resource → Dockerfile**, select this repo.
2. Set **Docker Build Context Directory** to `/appsforlife` and **Port** to `80`.
3. In the resource's **Domains**, set `https://appsforlife.app` — use a separate
   resource from the ReMed landing (which uses build context `/landing` and the
   `remed.appsforlife.app` domain).
