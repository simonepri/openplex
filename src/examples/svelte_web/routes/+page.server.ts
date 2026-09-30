// Implements server-side data loading, persistent page view counters via PostgreSQL, and SSR span instrumentation.

import { hostname } from "node:os";
import { trace } from "@opentelemetry/api";
import postgres from "postgres";

const tracer = trace.getTracer("svelte-web");

let ephemeralCount = 0;
let sqlClient: ReturnType<typeof postgres> | undefined;
let initPromise: Promise<void> | undefined;

function getSql(): ReturnType<typeof postgres> | undefined {
  const databaseUrl = process.env.DATABASE_URL;
  if (!databaseUrl) {
    return undefined;
  }
  if (!sqlClient) {
    sqlClient = postgres(databaseUrl, {
      connect_timeout: 5,
      idle_timeout: 20,
      max: 5,
    });
  }
  return sqlClient;
}

async function ensureTable(sql: ReturnType<typeof postgres>): Promise<void> {
  if (!initPromise) {
    initPromise = (async (): Promise<void> => {
      await sql`
        CREATE TABLE IF NOT EXISTS page_views (
          id INT PRIMARY KEY,
          count BIGINT NOT NULL
        )
      `;
      await sql`
        INSERT INTO page_views (id, count)
        VALUES (1, 0)
        ON CONFLICT (id) DO NOTHING
      `;
    })();
  }
  try {
    await initPromise;
  } catch (error) {
    initPromise = undefined;
    throw error;
  }
}

function incrementViews(): Promise<{ views: number; storage: string }> {
  return tracer.startActiveSpan("postgresql.increment_views", async (span) => {
    try {
      const sql = getSql();
      if (!sql) {
        ephemeralCount += 1;
        span.setAttribute("db.system", "in-memory");
        return { storage: "ephemeral", views: ephemeralCount };
      }

      try {
        await ensureTable(sql);
        const rows = await sql<{ count: string }[]>`
          UPDATE page_views
          SET count = count + 1
          WHERE id = 1
          RETURNING count;
        `;
        const [firstRow] = rows;
        let count = 0;
        if (firstRow && firstRow.count) {
          count = Math.trunc(Number(firstRow.count));
        } else {
          ephemeralCount += 1;
          count = ephemeralCount;
        }
        span.setAttribute("db.system", "postgresql");
        span.setAttribute("app.views", count);
        return { storage: "postgresql", views: count };
      } catch (error) {
        if (error instanceof Error) {
          span.recordException(error);
        } else {
          span.recordException(new Error(String(error)));
        }
        ephemeralCount += 1;
        return { storage: "ephemeral (fallback)", views: ephemeralCount };
      }
    } finally {
      span.end();
    }
  });
}

export interface PageData {
  renderedAt: string;
  server: string;
  storage: string;
  views: number;
}

export async function load(): Promise<PageData> {
  const { views, storage } = await incrementViews();
  return {
    renderedAt: new Date().toISOString(),
    server: hostname(),
    storage,
    views,
  };
}
