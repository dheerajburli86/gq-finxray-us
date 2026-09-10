import "jsr:@supabase/functions-js/edge-runtime.d.ts";

// Serves an earnings call transcript as raw JSON, straight out of raw_filings.
//
// This exists because the alert needs a source link a subscriber can actually
// tap. FMP's /api/ endpoint answers 401 without a key, embedding our key would
// publish a paid credential to every subscriber, and FMP's public transcript
// pages 404. We already store the full transcript text, so we serve our own.
//
// verify_jwt is off by design: this is opened from a Telegram message, where
// there is no way to attach an Authorization header.
//
// Deploy:  supabase functions deploy transcript --no-verify-jwt

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body, null, 2), {
    status,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
  });
}

async function lookup(ticker: string, year: string, quarter: string) {
  const params = new URLSearchParams({
    select: "ticker,company_name,raw_text,filed_at,extra",
    filing_type: "eq.EARNINGS_TRANSCRIPT",
    ticker: `eq.${ticker}`,
    order: "filed_at.desc",
    limit: "1",
  });
  if (year) params.set("extra->>year", `eq.${year}`);
  if (quarter) params.set("extra->>quarter", `eq.${quarter}`);

  const res = await fetch(`${SUPABASE_URL}/rest/v1/raw_filings?${params}`, {
    headers: { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}` },
  });
  if (!res.ok) return null;
  const rows = await res.json();
  return Array.isArray(rows) && rows.length ? rows[0] : null;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });

  const url = new URL(req.url);
  const ticker = (url.searchParams.get("ticker") || "").trim().toUpperCase();
  const year = (url.searchParams.get("year") || "").trim();
  const quarter = (url.searchParams.get("quarter") || "").trim().replace(/^Q/i, "");

  if (!ticker) {
    return json({
      error: "ticker query parameter is required",
      usage: "?ticker=AVGO&year=2026&quarter=3",
    }, 400);
  }

  try {
    // Fall back to the newest transcript we hold for the ticker rather than
    // dead-ending, so a link built with a slightly-off fiscal quarter still
    // resolves to something readable instead of an empty result.
    let row = await lookup(ticker, year, quarter);
    let exact = true;
    if (!row) {
      row = await lookup(ticker, "", "");
      exact = false;
    }

    if (!row) {
      return json({
        symbol: ticker,
        found: false,
        message: `No earnings call transcript stored for ${ticker} yet.`,
      }, 200);
    }

    const extra = row.extra || {};
    const content = row.raw_text || "";

    return json({
      symbol: row.ticker,
      company: row.company_name || row.ticker,
      year: extra.year ?? null,
      quarter: extra.quarter ?? null,
      period: extra.year && extra.quarter ? `Q${extra.quarter} FY${extra.year}` : null,
      date: row.filed_at,
      title: extra.title ?? null,
      characters: content.length,
      exact_match: exact,
      content,
    });
  } catch (e) {
    return json({ symbol: ticker, error: String(e) }, 500);
  }
});
