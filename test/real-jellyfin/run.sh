#!/bin/sh
# LomboVisa's gateway against a THROWAWAY Jellyfin and a real Caddy. Never point it at your own server.
# Needs docker, python3 and internet access (images and the country database). Cleans up its containers on exit.
set -eu
cd "$(dirname "$0")/../.."
root=$PWD; s=$root/.scratch-jf; here=$root/test/real-jellyfin
rm -rf "$s"; mkdir -p "$s/config" "$s/cache" "$s/data"
cleanup() { docker rm -f lf-visa-caddy lf-visa-gw lf-visa-jf >/dev/null 2>&1 || true; docker network rm lf-visa-net >/dev/null 2>&1 || true; }
cleanup; trap cleanup EXIT
docker network create --subnet 172.31.250.0/24 lf-visa-net >/dev/null
docker build -q -t lf-visa:test . >/dev/null
docker run -d --name lf-visa-jf --network lf-visa-net --ip 172.31.250.10 --network-alias jellyfin --cpus 2 --memory 2g \
  --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$s/config:/config" -v "$s/cache:/cache" "${JELLYFIN_IMAGE:-jellyfin/jellyfin:12.4}" >/dev/null
export LF_SCRATCH="$s"
python3 "$here/check.py" setup
printf '%s\n' "LOMBO_VISA_JELLYFIN_URL=http://jellyfin:8096" "LOMBO_VISA_JELLYFIN_API_KEY=$(cat "$s/key")" \
  "LOMBO_VISA_HOME_COUNTRIES=CA" "LOMBO_VISA_DRY_RUN=false" "LOMBO_VISA_BLOCK_SESSIONS=true" "LOMBO_VISA_GATEWAY_MODE=enforce" \
  "LOMBO_VISA_GATEWAY_BIND=0.0.0.0:8099" "LOMBO_VISA_TRUSTED_PROXIES=172.31.250.0/24" "LOMBO_VISA_AUDIT_FILE=/data/audit.jsonl" > "$s/env"
chmod 600 "$s/env"
visa() { docker run --rm --network lf-visa-net --user "$(id -u):$(id -g)" --env-file "$s/env" -v "$s/data:/data" lf-visa:test "$@"; }
visa geoip update
docker run -d --name lf-visa-gw --network lf-visa-net --ip 172.31.250.20 --network-alias lombo-visa-gateway --cpus 1 --memory 256m \
  --user "$(id -u):$(id -g)" --read-only --env-file "$s/env" -v "$s/data:/data" lf-visa:test gateway >/dev/null
# The shipped example, with a plain-HTTP site. The two test-only lines let the checker play a visitor from anywhere.
{ printf '{\n\tservers {\n\t\ttrusted_proxies static private_ranges\n\t\ttrusted_proxies_strict\n\t}\n}\n'
  sed -e 's/^jellyfin\.example\.com {/:80 {/' -e 's/^\(\t*\)\(reverse_proxy [^ ]*\)$/\1\2 {\n\1\theader_up X-Forwarded-For {client_ip}\n\1}/' examples/Caddyfile
} > "$s/Caddyfile"
docker run -d --name lf-visa-caddy --network lf-visa-net --ip 172.31.250.30 --cpus 1 --memory 256m -p 127.0.0.1:18180:80 \
  -v "$s/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2 >/dev/null
rc=0; python3 "$here/check.py" run || rc=$?
echo "--- gateway log (last lines)"; docker logs lf-visa-gw 2>&1 | tail -8
exit $rc
