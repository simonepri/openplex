/**
 * Tests TypeScript S3 resolver client parsing, HTTP mock requests, and resolution logic.
 */

import assert from "node:assert/strict";
import { once } from "node:events";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import { DEFAULT_RESOLVER_URL, S3ResolverClient, resolveS3Uri } from "./client.ts";

const TARGET_URI = "s3://cell-aws-usw2/home/team-a/data.parquet";

function createMockServer(): {
  server: http.Server;
  getState: () => { requestCount: number; receivedAuth: string | undefined };
  resetCount: () => void;
} {
  let requestCount = 0;
  let receivedAuth: string | undefined;
  const server = http.createServer((req, res) => {
    requestCount += 1;
    receivedAuth = req.headers.authorization;
    if (req.url?.includes("team-a")) {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          mode: "direct",
          uri: TARGET_URI,
          bucket: "prod-cell-aws-usw2-home",
          key: "home/team-a/data.parquet",
          endpoint_url: "https://s3.us-west-2.amazonaws.com",
          region: "us-west-2",
          auth: { type: "ambient_workload_identity" },
        }),
      );
    } else {
      res.writeHead(403, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "forbidden" }));
    }
  });
  return {
    server,
    getState: () => ({ requestCount, receivedAuth }),
    resetCount: () => {
      requestCount = 0;
    },
  };
}

describe("S3ResolverClient direct resolution and caching", () => {
  const mock = createMockServer();
  let serverUrl: string;

  before(async () => {
    mock.server.listen(0, "127.0.0.1");
    await once(mock.server, "listening");
    const addr = mock.server.address() as { port: number };
    serverUrl = `http://127.0.0.1:${addr.port}/resolve`;
  });

  after(async () => {
    mock.server.close();
    await once(mock.server, "close");
  });

  it("resolves direct target with properties and helpers", async () => {
    const client = new S3ResolverClient({ resolverUrl: serverUrl, tokenPath: "/nonexistent" });
    const target = await client.resolve(TARGET_URI);

    assert.equal(target.mode, "direct");
    assert.equal(target.isDirect, true);
    assert.equal(target.bucket, "prod-cell-aws-usw2-home");
    assert.equal(target.key, "home/team-a/data.parquet");
    assert.equal(target.endpointUrl, "https://s3.us-west-2.amazonaws.com");
    assert.equal(target.cleanEndpoint, "s3.us-west-2.amazonaws.com");
    assert.equal(target.scheme, "https");
    assert.equal(target.region, "us-west-2");
    assert.equal(target.authType, "ambient_workload_identity");
    assert.deepEqual(target.s3ClientConfig(), {
      endpoint: "https://s3.us-west-2.amazonaws.com",
      region: "us-west-2",
      forcePathStyle: true,
    });
  });

  it("caches resolved targets to avoid redundant network calls", async () => {
    const client = new S3ResolverClient({ resolverUrl: serverUrl, tokenPath: "/nonexistent" });
    mock.resetCount();
    const t1 = await client.resolve(TARGET_URI);
    const t2 = await client.resolve(TARGET_URI);
    assert.strictEqual(t1, t2);
    assert.equal(mock.getState().requestCount, 1);
  });

  it("propagates ambient service account token when present", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "s3-res-test-"));
    const tokenFile = path.join(tmpDir, "token");
    fs.writeFileSync(tokenFile, "mock-ts-jwt-token");
    try {
      const client = new S3ResolverClient({ resolverUrl: serverUrl, tokenPath: tokenFile });
      await client.resolve(TARGET_URI);
      assert.equal(mock.getState().receivedAuth, "Bearer mock-ts-jwt-token");
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  it("convenience function resolveS3Uri works with resolver override", async () => {
    const target = await resolveS3Uri(TARGET_URI, serverUrl);
    assert.equal(target.mode, "direct");
  });
});

describe("S3ResolverClient fallback and validation", () => {
  it("falls back gracefully on network or resolver error", async () => {
    const client = new S3ResolverClient({
      resolverUrl: "http://127.0.0.1:1/resolve",
      timeoutMs: 100,
    });
    const target = await client.resolve("s3://cell-aws-usw2/scratch/output.log");
    assert.equal(target.mode, "fallback");
    assert.equal(target.isDirect, false);
    assert.equal(target.bucket, "cell-aws-usw2");
    assert.equal(target.key, "scratch/output.log");
    assert.equal(target.authType, "gateway_credential");
    assert.equal(target.endpointUrl, "http://s3-gateway.s3-system.svc:10080");
    assert.equal(DEFAULT_RESOLVER_URL, "http://s3-gateway.s3-system.svc:10080/resolve");
  });

  it("rejects non-s3 URIs", async () => {
    await assert.rejects(
      () => new S3ResolverClient().resolve("gcs://bucket/key"),
      /URI must start with s3:\/\//u,
    );
  });
});
