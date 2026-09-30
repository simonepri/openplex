// Initializes client-side OpenTelemetry instrumentation, document load tracking, and Web Vitals reporting in SvelteKit.

import { type Context, context, trace } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { DocumentLoadInstrumentation } from "@opentelemetry/instrumentation-document-load";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { UserInteractionInstrumentation } from "@opentelemetry/instrumentation-user-interaction";
import { XMLHttpRequestInstrumentation } from "@opentelemetry/instrumentation-xml-http-request";
import { Resource } from "@opentelemetry/resources";
import { BatchSpanProcessor, WebTracerProvider } from "@opentelemetry/sdk-trace-web";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";
import { type Metric, onCLS, onFCP, onINP, onLCP, onTTFB } from "web-vitals";

if (globalThis.window !== undefined) {
  const serviceName = "svelte-web-browser";

  const exporter = new OTLPTraceExporter({
    url: "/v1/traces",
  });

  const provider = new WebTracerProvider({
    resource: new Resource({
      [ATTR_SERVICE_NAME]: serviceName,
    }),
    spanProcessors: [new BatchSpanProcessor(exporter)],
  });

  provider.register();

  registerInstrumentations({
    instrumentations: [
      new DocumentLoadInstrumentation(),
      new UserInteractionInstrumentation({
        eventNames: ["click", "input", "submit"],
      }),
      new FetchInstrumentation({
        propagateTraceHeaderCorsUrls: [/.*/u],
      }),
      new XMLHttpRequestInstrumentation({
        propagateTraceHeaderCorsUrls: [/.*/u],
      }),
    ],
  });

  const webVitalsTracer = trace.getTracer("web-vitals-instrumentation");
  let webVitalsContext: Context | undefined;
  let isContextCreated = false;

  const createWebVitalsContext = (): Context | undefined => {
    if (!isContextCreated) {
      const parentSpan = webVitalsTracer.startSpan("web-vitals");
      webVitalsContext = trace.setSpan(context.active(), parentSpan);
      parentSpan.end();
      isContextCreated = true;
    }
    return webVitalsContext;
  };

  const createWebVitalsSpan = (metric: Metric): void => {
    const ctx = createWebVitalsContext();
    if (!ctx) {
      return;
    }

    const span = webVitalsTracer.startSpan(metric.name, undefined, ctx);
    span.setAttributes({
      "page.title": document.title,
      "url.full": globalThis.location.href,
      "web_vital.delta": metric.delta,
      "web_vital.id": metric.id,
      "web_vital.name": metric.name,
      "web_vital.navigationType": metric.navigationType,
      "web_vital.rating": metric.rating,
      "web_vital.value": metric.value,
    });
    span.end();
  };

  onFCP(createWebVitalsSpan);
  onINP(createWebVitalsSpan);
  onTTFB(createWebVitalsSpan);
  onLCP(createWebVitalsSpan);
  onCLS(createWebVitalsSpan);
}
