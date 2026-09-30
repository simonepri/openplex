// Implements server-side OpenTelemetry tracing, HTTP request context propagation, and span lifecycle hooks for SvelteKit.

import { SpanStatusCode, context, propagation, trace } from "@opentelemetry/api";
import { Resource } from "@opentelemetry/resources";
import {
  NodeTracerProvider,
  SimpleSpanProcessor,
  type ReadableSpan,
  type SpanExporter,
} from "@opentelemetry/sdk-trace-node";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";
import type { Handle } from "@sveltejs/kit";

function hrTimeToNano(hrTime: [number, number]): string {
  const [seconds, nanos] = hrTime;
  return String(BigInt(seconds) * 1_000_000_000n + BigInt(nanos));
}

interface AnyValue {
  arrayValue?: { values: AnyValue[] };
  boolValue?: boolean;
  doubleValue?: number;
  intValue?: string;
  stringValue?: string;
}

type AttributeInput = string | number | boolean | AttributeInput[];

function toAnyValue(val: AttributeInput): AnyValue {
  if (typeof val === "number") {
    if (Number.isInteger(val)) {
      return { intValue: String(val) };
    }
    return { doubleValue: val };
  }
  if (typeof val === "boolean") {
    return { boolValue: val };
  }
  if (Array.isArray(val)) {
    return { arrayValue: { values: val.map((item) => toAnyValue(item)) } };
  }
  return { stringValue: String(val) };
}

class FetchSpanExporter implements SpanExporter {
  private readonly environment: string;
  private readonly serviceName: string;
  private readonly team: string;
  private readonly url: string;

  public constructor(url: string, serviceName: string, team: string, environment: string) {
    this.url = url;
    this.serviceName = serviceName;
    this.team = team;
    this.environment = environment;
  }

  public export(
    spans: ReadableSpan[],
    resultCallback: (result: { code: number; error?: Error }) => void,
  ): void {
    void this.sendSpans(spans, resultCallback);
  }

  private async sendSpans(
    spans: ReadableSpan[],
    resultCallback: (result: { code: number; error?: Error }) => void,
  ): Promise<void> {
    const resourceSpans = [
      {
        resource: {
          attributes: [
            { key: "service.name", value: { stringValue: this.serviceName } },
            { key: "team", value: { stringValue: this.team } },
            { key: "environment", value: { stringValue: this.environment } },
          ],
        },
        scopeSpans: [
          {
            scope: { name: this.serviceName },
            spans: spans.map((span) => {
              const attributes = Object.entries(span.attributes).map(([attrKey, attrVal]) => {
                let cleanVal: AttributeInput = "";
                if (
                  typeof attrVal === "string" ||
                  typeof attrVal === "number" ||
                  typeof attrVal === "boolean"
                ) {
                  cleanVal = attrVal;
                } else if (Array.isArray(attrVal)) {
                  cleanVal = attrVal.filter(
                    (item): item is string | number | boolean =>
                      item !== null && item !== undefined,
                  );
                }
                return {
                  key: attrKey,
                  value: toAnyValue(cleanVal),
                };
              });
              const { kind = 1 } = span;
              return {
                attributes,
                endTimeUnixNano: hrTimeToNano(span.endTime),
                kind,
                name: span.name,
                parentSpanId: span.parentSpanId,
                spanId: span.spanContext().spanId,
                startTimeUnixNano: hrTimeToNano(span.startTime),
                status: {
                  code: span.status.code,
                  message: span.status.message,
                },
                traceId: span.spanContext().traceId,
              };
            }),
          },
        ],
      },
    ];

    try {
      const response = await fetch(this.url, {
        body: JSON.stringify({ resourceSpans }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      if (response.ok) {
        resultCallback({ code: 0 });
      } else {
        resultCallback({ code: 1, error: new Error(`HTTP ${response.status}`) });
      }
    } catch (error) {
      const err = error instanceof Error ? error : new Error(String(error));
      resultCallback({ code: 1, error: err });
    }
  }

  public async shutdown(): Promise<void> {
    if (this.url.length > 0) {
      await Promise.resolve();
    }
  }

  public async forceFlush(): Promise<void> {
    if (this.url.length > 0) {
      await Promise.resolve();
    }
  }
}

const serviceName = process.env.OTEL_SERVICE_NAME || "svelte-web";
const rawEndpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
const team = process.env.TEAM || "examples";
const environment = process.env.ENVIRONMENT || "production";

const provider = new NodeTracerProvider({
  resource: new Resource({
    [ATTR_SERVICE_NAME]: serviceName,
    environment,
    team,
  }),
});

if (rawEndpoint) {
  const endpoint = rawEndpoint.replace(/\/$/u, "");
  let tracesUrl = endpoint;
  if (!endpoint.endsWith("/v1/traces")) {
    tracesUrl = `${endpoint}/v1/traces`;
  }
  provider.addSpanProcessor(
    new SimpleSpanProcessor(new FetchSpanExporter(tracesUrl, serviceName, team, environment)),
  );
}
provider.register();

const tracer = trace.getTracer(serviceName);

export const handle: Handle = async ({ event, resolve }): Promise<Response> => {
  const { pathname: path } = event.url;
  if (path.startsWith("/v1/traces")) {
    return resolve(event);
  }

  const { method } = event.request;
  const carrier: Record<string, string> = {};
  for (const [headerKey, headerValue] of event.request.headers) {
    carrier[headerKey] = headerValue;
  }
  const extractedContext = propagation.extract(context.active(), carrier);

  return await context.with(extractedContext, () =>
    tracer.startActiveSpan(`HTTP ${method} ${path}`, async (span) => {
      span.setAttribute("http.method", method);
      span.setAttribute("http.url", event.url.href);
      span.setAttribute("http.target", path);

      try {
        const response = await resolve(event);
        span.setAttribute("http.status_code", response.status);
        if (response.status >= 500) {
          span.setStatus({ code: SpanStatusCode.ERROR });
        }
        return response;
      } catch (error) {
        span.setStatus({
          code: SpanStatusCode.ERROR,
          message: error instanceof Error ? error.message : String(error),
        });
        throw error;
      } finally {
        span.end();
        try {
          await provider.forceFlush();
        } catch (flushError) {
          if (flushError instanceof Error) {
            span.recordException(flushError);
          }
        }
      }
    }),
  );
};
