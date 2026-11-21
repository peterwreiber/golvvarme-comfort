# comfort-stats

Anonym räknare för Golvvärme Comfort och NIBE Comfort. Tar emot `{app, id, version}` (id = slumpat av tillägget) och svarar med antal aktiva installationer. Sparar inga IP-adresser.

- `POST /v1/ping` `{"app","id","version"}` → 204
- `POST /v1/forget` `{"app","id"}` → 204
- `GET /v1/stats?app=golvvarme_comfort` → `{"active_7d","active_30d","total","versions","updated"}`

Test lokalt (wrangler dev, ingen Cloudflare): `test/smoke.sh`. Publicera: `CLOUDFLARE_ACCOUNT_ID=… CLOUDFLARE_API_TOKEN=… npx wrangler deploy`.
