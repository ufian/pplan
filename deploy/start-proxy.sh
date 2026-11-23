#!/bin/sh
# Run on the public host after provisioning its certificate and nginx.conf.
set -eu
docker run -d --name pplan-proxy --restart unless-stopped --network host \
  --read-only --tmpfs /var/cache/nginx --tmpfs /var/run --tmpfs /tmp \
  --cap-drop ALL --cap-add NET_BIND_SERVICE --cap-add CHOWN --cap-add SETUID --cap-add SETGID \
  --security-opt no-new-privileges \
  -v /opt/pplan-proxy/nginx.conf:/etc/nginx/nginx.conf:ro \
  -v /var/www/pplan-acme:/var/www/acme:ro \
  -v /etc/letsencrypt:/etc/letsencrypt:ro \
  nginx@sha256:caf4e1e032a20f9c3c5e28fc7f25ffd768ad8677b4975c54193075997b3a97e3
