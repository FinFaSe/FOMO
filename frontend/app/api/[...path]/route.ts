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
  if (process.env.TRACKER_ACCESS_TOKEN) headers["X-Tracker-Token"] = process.env.TRACKER_ACCESS_TOKEN;
  try {
    const body = request.method === "GET" ? undefined : await request.text();
    if (body && new TextEncoder().encode(body).length > 3_000_000) {
      return NextResponse.json({ detail: "Importación demasiado grande" }, { status: 413 });
    }
    const response = await fetch(`${backend}/api/${path.join("/")}${request.nextUrl.search}`, {
      method: request.method, headers, body, cache: "no-store", signal: AbortSignal.timeout(180_000),
    });
    return new NextResponse(await response.text(), {
      status: response.status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ detail: "Backend no disponible o consulta agotada. Comprueba FastAPI y el estado de la ingesta." }, { status: 502 });
  }
}

export { proxy as GET, proxy as POST, proxy as PATCH };
