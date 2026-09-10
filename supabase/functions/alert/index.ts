import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// Serves one alert as raw JSON: /functions/v1/alert?id=<uuid>
//
// Every feature gets a tappable source link this way, including the ones with
// no upstream document to point at. A large block print, an insider summary and
// an upcoming earnings date are all events we computed or aggregated rather than
// fetched from a filing, so there is no vendor URL that describes them -- but the
// underlying numbers are on the alert row, and this returns them.
//
// verify_jwt is off by design: opened from a Telegram message, which cannot
// attach an Authorization header.
//
// Deploy:  supabase functions deploy alert --no-verify-jwt

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// alerts.extra is our own scratch space and accumulates delivery bookkeeping.
// None of it belongs in a publicly readable response, so the payload is built
// from a deny-list rather than handing back extra wholesale.
const PRIVATE_KEYS = [
  "user_id", "chat_id", "telegram", "token", "apikey", "api_key",
  "secret", "requeued_from", "requeued_at", "delivered_at",
];

function isPrivate(key: string) {
  const k = key.toLowerCase();
  return PRIVATE_KEYS.some((p) => k.includes(p));
}

function scrub(obj: Record<string, unknown>) {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(obj || {})) {
    if (!isPrivate(k)) out[k] = v;
  }
  return out;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
  });
}

async function rest(path: string) {
  const res = await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}` },
  });
  if (!res.ok) return null;
  const rows = await res.json();
  return Array.isArray(rows) && rows.length ? rows[0] : null;
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  const id = (new URL(req.url).searchParams.get("id") || "").trim();
  if (!UUID.test(id)) {
    return json({ error: "a valid alert id is required", usage: "?id=<uuid>" }, 400);
  }

  try {
    const row = await rest(
      `alerts?id=eq.${id}&select=id,ticker,summary,impact,source,filing_type,filing_url,link,extra,created_at&limit=1`,
    );
    if (!row) return json({ id, found: false, message: "No such alert." }, 200);

    const extra = scrub(row.extra || {});
    const body: Record<string, unknown> = {
      id: row.id,
      ticker: row.ticker,
      company: extra.company_name ?? null,
      impact: row.impact,
      feature: extra.feature_name ?? null,
      source: row.source,
      filing_type: row.filing_type,
      published_at: row.created_at,
      source_url: row.filing_url || row.link || null,
      summary: row.summary,
      data: extra,
    };

    // An earnings transcript alert is a ~100-word summary of a ~40,000-char
    // call. Anyone opening the JSON for one wants the call itself, so inline it.
    if (row.filing_type === "EARNINGS_TRANSCRIPT") {
      const params = new URLSearchParams({
        select: "raw_text",
        filing_type: "eq.EARNINGS_TRANSCRIPT",
        ticker: `eq.${row.ticker}`,
        order: "filed_at.desc",
        limit: "1",
      });
      if (extra.year) params.set("extra->>year", `eq.${extra.year}`);
      if (extra.quarter) params.set("extra->>quarter", `eq.${extra.quarter}`);
      const t = await rest(`raw_filings?${params}`);
      if (t?.raw_text) body.transcript = t.raw_text;
    }

    return json(body);
  } catch (e) {
    return json({ id, error: String(e) }, 500);
  }
});
