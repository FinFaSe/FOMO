import { NextRequest, NextResponse } from "next/server";

export const dynamic = "force-dynamic";
const backend = process.env.BACKEND_URL || "http://127.0.0.1:8000";

async function proxy(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const { path } = await context.params;
  if (path.some(part => !/^[a-zA-Z0-9_.-]+$/.test(part))) {
    return NextResponse.json({ detail: "Ruta inválida" }, { status: 400 });
  }
  if (request.method !== "GET") {
    const origin = request.headers.get("origin");
    if (origin && origin !== request.nextUrl.origin && new URL(origin).host !== request.headers.get("host")) {
      return NextResponse.json({ detail: "Origen no permitido" }, { status: 403 });
    }
  }
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  // The proxy proves itself with the shared token, which also lets the API trust the forwarded client address.
  if (process.env.TRACKER_ACCESS_TOKEN) headers["X-Tracker-Token"] = process.env.TRACKER_ACCESS_TOKEN;
  // Session cookie (TRACKER_AUTH=on) and personal tokens travel to the API; nothing else from the browser does.
  const cookie = request.headers.get("cookie");
  if (cookie) headers["Cookie"] = cookie;
  const authorization = request.headers.get("authorization");
  if (authorization) headers["Authorization"] = authorization;
  const forwarded = request.headers.get("x-forwarded-for") || request.headers.get("x-real-ip");
  if (forwarded) headers["X-Forwarded-For"] = forwarded;
  try {
    const body = request.method === "GET" ? undefined : await request.text();
    if (body && new TextEncoder().encode(body).length > 3_000_000) {
      return NextResponse.json({ detail: "Importación demasiado grande" }, { status: 413 });
    }
    const response = await fetch(`${backend}/api/${path.join("/")}${request.nextUrl.search}`, {
      method: request.method, headers, body, cache: "no-store", signal: AbortSignal.timeout(180_000),
    });
    const out = new NextResponse(await response.text(), {
      status: response.status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
    for (const value of response.headers.getSetCookie()) out.headers.append("Set-Cookie", value);
    const retry = response.headers.get("retry-after");
    if (retry) out.headers.set("Retry-After", retry);
    return out;
  } catch {
    return NextResponse.json({ detail: "Backend no disponible o consulta agotada. Comprueba FastAPI y el estado de la ingesta." }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH };
