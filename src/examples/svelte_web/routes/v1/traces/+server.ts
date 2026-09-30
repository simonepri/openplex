// Proxies client-side OpenTelemetry trace payloads from browsers to the in-cluster OpenTelemetry Collector.

import type { RequestHandler } from "./$types";

export const POST: RequestHandler = async ({ request }) => {
  const endpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
  if (!endpoint) {
    return new Response(undefined, { status: 204 });
  }

  let targetUrl = endpoint;
  if (!endpoint.endsWith("/v1/traces")) {
    targetUrl = `${endpoint.replace(/\/$/u, "")}/v1/traces`;
  }
  const body = await request.arrayBuffer();

  try {
    const response = await fetch(targetUrl, {
      body,
      headers: {
        "content-type": request.headers.get("content-type") || "application/x-protobuf",
      },
      method: "POST",
    });

    return new Response(response.body, {
      headers: {
        "content-type": response.headers.get("content-type") || "application/json",
      },
      status: response.status,
    });
  } catch {
    return new Response(undefined, { status: 502 });
  }
};
