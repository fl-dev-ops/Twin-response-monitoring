// One shared password for the whole site (HTTP basic auth; any username).
// The password is the Pages secret DASHBOARD_PASSWORD: wrangler pages secret put DASHBOARD_PASSWORD --project-name twin-response

const REALM = "Twin Response Monitoring";

function sameText(a, b) {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

function suppliedPassword(request) {
  const [scheme, encoded] = (request.headers.get("Authorization") || "").split(" ");
  if (scheme !== "Basic" || !encoded) return null;
  try {
    const decoded = atob(encoded);
    return decoded.slice(decoded.indexOf(":") + 1);
  } catch {
    return null;
  }
}

export async function onRequest({ request, env, next }) {
  if (!env.DASHBOARD_PASSWORD) return new Response("DASHBOARD_PASSWORD is not set on this Pages project.", { status: 503 });
  const password = suppliedPassword(request);
  if (password === null || !sameText(password, env.DASHBOARD_PASSWORD)) {
    return new Response("Password required.", {
      status: 401,
      headers: { "WWW-Authenticate": `Basic realm="${REALM}", charset="UTF-8"`, "Cache-Control": "no-store" },
    });
  }
  const res = await next();
  const out = new Response(res.body, res);
  out.headers.set("Cache-Control", "no-store");
  out.headers.set("X-Robots-Tag", "noindex, nofollow");
  return out;
}
