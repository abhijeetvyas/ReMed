# Static site for appsforlife.app — served by nginx (no build step).
# Deploy in Coolify:
#   New Resource → Dockerfile → pick this repo,
#   Docker Build Context Directory: /appsforlife
#   Port: 80  → then point appsforlife.app (the main domain, in Cloudflare) at it.
FROM nginx:1.27-alpine

COPY index.html /usr/share/nginx/html/

EXPOSE 80
