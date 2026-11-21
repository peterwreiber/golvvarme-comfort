#!/bin/bash
# Kör Workern lokalt (wrangler dev, ingen Cloudflare) och testar endpoints.
set -u
cd "$(dirname "$0")/.."
P=8791; B=http://127.0.0.1:$P
rm -rf .wrangler/state-test
npx wrangler dev --port $P --ip 127.0.0.1 --persist-to .wrangler/state-test >/tmp/claude-0/csw-dev.log 2>&1 &
W=$!; trap 'kill $W 2>/dev/null' EXIT
for i in $(seq 60); do curl -fs $B/ >/dev/null 2>&1 && break; sleep 0.5; done
fel=0; ok() { if [ "$2" = "$3" ]; then echo "OK   $1"; else echo "FEL  $1: fick '$2' väntade '$3'"; fel=1; fi; }
st() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; Bid=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
ok "ping ny"        "$(st -X POST $B/v1/ping -d "{\"app\":\"golvvarme_comfort\",\"id\":\"$A\",\"version\":\"0.6.2\"}")" 204
ok "ping igen"      "$(st -X POST $B/v1/ping -d "{\"app\":\"golvvarme_comfort\",\"id\":\"$A\",\"version\":\"0.6.3\"}")" 204
ok "ping annan"     "$(st -X POST $B/v1/ping -d "{\"app\":\"golvvarme_comfort\",\"id\":\"$Bid\",\"version\":\"0.6.2\"}")" 204
ok "ping nibe"      "$(st -X POST $B/v1/ping -d "{\"app\":\"nibe_comfort\",\"id\":\"$A\",\"version\":\"0.2.59\"}")" 204
ok "okänd app"      "$(st -X POST $B/v1/ping -d "{\"app\":\"x\",\"id\":\"$A\",\"version\":\"1\"}")" 400
ok "dåligt id"      "$(st -X POST $B/v1/ping -d '{"app":"nibe_comfort","id":"abc","version":"1"}')" 400
ok "dålig version"  "$(st -X POST $B/v1/ping -d "{\"app\":\"nibe_comfort\",\"id\":\"$A\",\"version\":\"<script>\"}")" 400
ok "ej json"        "$(st -X POST $B/v1/ping -d 'nej')" 400
ok "för stort"      "$(st -X POST $B/v1/ping -d "$(head -c 2000 /dev/zero | tr '\0' a)")" 400
s=$(curl -s "$B/v1/stats?app=golvvarme_comfort")
ok "stats aktiva"   "$(jq -c '[.active_7d,.active_30d,.total]' <<<"$s")" "[2,2,2]"
ok "stats versioner" "$(jq -cS '.versions' <<<"$s")" '{"0.6.2":1,"0.6.3":1}'
ok "nibe separat"   "$(curl -s "$B/v1/stats?app=nibe_comfort" | jq .total)" 1
ok "stats okänd"    "$(st "$B/v1/stats?app=x")" 404
ok "forget"         "$(st -X POST $B/v1/forget -d "{\"app\":\"nibe_comfort\",\"id\":\"$A\"}")" 204
# 5 nya id per IP och dygn: 2 + 1 (nibe) redan använda, forget räknas inte tillbaka
for n in 1 2; do st -X POST $B/v1/ping -d "{\"app\":\"golvvarme_comfort\",\"id\":\"$(printf 'c%031d' $n)\",\"version\":\"1\"}" >/dev/null; done
ok "spärr nya id"   "$(st -X POST $B/v1/ping -d "{\"app\":\"golvvarme_comfort\",\"id\":\"$(printf 'd%031d' 9)\",\"version\":\"1\"}")" 429
ok "känt id ok ändå" "$(st -X POST $B/v1/ping -d "{\"app\":\"golvvarme_comfort\",\"id\":\"$A\",\"version\":\"0.6.3\"}")" 204
ok "404"            "$(st $B/annat)" 404
exit $fel
