// Anonymous installation counter for the Comfort add-ons (Golvvärme Comfort, NIBE Comfort).
//
// The add-ons send {app, id, version} every 6 hours. `id` is a random value the add-on made up
// on first start; nothing else about the installation is sent. IP addresses are never stored:
// a salted, daily-rotating hash is kept in memory only, to limit how many new ids one address
// can register per day.
//
//   POST /v1/ping    {"app": "...", "id": "<32 hex>", "version": "0.6.2"}  -> 204
//   POST /v1/forget  {"app": "...", "id": "<32 hex>"}                       -> 204 (used when sharing is turned off)
//   GET  /v1/stats?app=...  -> {"app", "active_7d", "active_30d", "total", "versions", "updated"}

import { DurableObject } from "cloudflare:workers";

export const APPS = ["golvvarme_comfort", "nibe_comfort"];
const DAY = 86400;
const STATS_CACHE_SECONDS = 300;
const NEW_IDS_PER_IP_PER_DAY = 5;
const FORGET_AFTER_DAYS = 365;

export function parse(body, needVersion) {
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const { app, id, version } = body;
  if (!APPS.includes(app)) return null;
  if (typeof id !== "string" || !/^[0-9a-f]{32}$/.test(id)) return null;
  if (!needVersion) return { app, id };
  if (typeof version !== "string" || !/^[0-9A-Za-z.+_-]{1,32}$/.test(version)) return null;
  return { app, id, version };
}

export class Installs extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`CREATE TABLE IF NOT EXISTS installs (
      app TEXT NOT NULL, id TEXT NOT NULL, version TEXT NOT NULL,
      first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, PRIMARY KEY (app, id))`);
    this.cache = new Map(); // app -> {at, value}
    this.newIds = new Map(); // "day:iphash" -> count, memory only
    this.salt = crypto.randomUUID();
  }

  async ipKey(ip, now) {
    const day = Math.floor(now / DAY);
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${this.salt}:${day}:${ip}`));
    return `${day}:${[...new Uint8Array(digest).slice(0, 8)].map((b) => b.toString(16).padStart(2, "0")).join("")}`;
  }

  async ping(app, id, version, ip, now = Math.floor(Date.now() / 1000)) {
    const known = this.sql.exec("SELECT 1 FROM installs WHERE app = ? AND id = ?", app, id).toArray().length > 0;
    if (known) {
      this.sql.exec("UPDATE installs SET version = ?, last_seen = ? WHERE app = ? AND id = ?", version, now, app, id);
      return true;
    }
    const key = await this.ipKey(ip || "", now);
    const count = this.newIds.get(key) || 0;
    if (count >= NEW_IDS_PER_IP_PER_DAY) return false;
    if (this.newIds.size > 10000) this.newIds.clear();
    this.newIds.set(key, count + 1);
    this.sql.exec("INSERT INTO installs (app, id, version, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)", app, id, version, now, now);
    this.cache.delete(app);
    return true;
  }

  forget(app, id) {
    this.sql.exec("DELETE FROM installs WHERE app = ? AND id = ?", app, id);
    this.cache.delete(app);
  }

  stats(app, now = Math.floor(Date.now() / 1000)) {
    const hit = this.cache.get(app);
    if (hit && now - hit.at < STATS_CACHE_SECONDS) return hit.value;
    this.sql.exec("DELETE FROM installs WHERE last_seen < ?", now - FORGET_AFTER_DAYS * DAY);
    const one = (q, ...a) => this.sql.exec(q, ...a).one().n;
    const versions = {};
    for (const row of this.sql.exec(
      "SELECT version, COUNT(*) AS n FROM installs WHERE app = ? AND last_seen >= ? GROUP BY version ORDER BY n DESC LIMIT 20",
      app, now - 7 * DAY)) versions[row.version] = row.n;
    const value = {
      app,
      active_7d: one("SELECT COUNT(*) AS n FROM installs WHERE app = ? AND last_seen >= ?", app, now - 7 * DAY),
      active_30d: one("SELECT COUNT(*) AS n FROM installs WHERE app = ? AND last_seen >= ?", app, now - 30 * DAY),
      total: one("SELECT COUNT(*) AS n FROM installs WHERE app = ?", app),
      versions,
      updated: new Date(now * 1000).toISOString(),
    };
    this.cache.set(app, { at: now, value });
    return value;
  }
}

const json = (value, status = 200, extra = {}) =>
  new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", ...extra } });

async function readBody(request) {
  const text = await request.text();
  if (text.length > 1024) return null;
  try { return JSON.parse(text); } catch { return null; }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const store = env.INSTALLS.get(env.INSTALLS.idFromName("global"));
    if (request.method === "POST" && (url.pathname === "/v1/ping" || url.pathname === "/v1/forget")) {
      const ping = url.pathname === "/v1/ping";
      const item = parse(await readBody(request), ping);
      if (!item) return json({ error: "bad request" }, 400);
      if (ping) {
        const ok = await store.ping(item.app, item.id, item.version, request.headers.get("CF-Connecting-IP"));
        return ok ? new Response(null, { status: 204 }) : json({ error: "too many new ids" }, 429);
      }
      await store.forget(item.app, item.id);
      return new Response(null, { status: 204 });
    }
    if (request.method === "GET" && url.pathname === "/v1/stats") {
      const app = url.searchParams.get("app");
      if (!APPS.includes(app)) return json({ error: "unknown app" }, 404);
      return json(await store.stats(app), 200, { "Cache-Control": "public, max-age=300" });
    }
    if (request.method === "GET" && url.pathname === "/") {
      return new Response("Anonym räknare för Golvvärme Comfort och NIBE Comfort. Se /v1/stats?app=golvvarme_comfort\n",
        { headers: { "Content-Type": "text/plain; charset=utf-8" } });
    }
    return json({ error: "not found" }, 404);
  },
};
