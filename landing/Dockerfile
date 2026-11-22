# Static site for remed.appsforlife.app — served by nginx (no build step).
# Deploy in Coolify:
#   New Resource → Dockerfile → pick this repo,
#   Docker Build Context Directory: /landing
#   Port: 80  → then point remed.appsforlife.app (A/CNAME in Cloudflare) at it.
FROM nginx:1.27-alpine

COPY index.html terms.html /usr/share/nginx/html/
COPY assets/ /usr/share/nginx/html/assets/

EXPOSE 80
