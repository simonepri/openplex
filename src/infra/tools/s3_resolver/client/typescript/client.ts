/**
 * Resolves virtual S3 URIs into physical storage coordinates for TypeScript runtimes.
 */

import fs from "node:fs";
import process from "node:process";

export const DEFAULT_RESOLVER_URL = "http://s3-gateway.s3-system.svc:10080/resolve";
export const DEFAULT_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token";

export interface ResolvedS3TargetInit {
  mode: string;
  uri: string;
  bucket: string;
  key: string;
  endpointUrl: string;
  region: string;
  authType: string;
}

export class ResolvedS3Target {
  public readonly mode: string;
  public readonly uri: string;
  public readonly bucket: string;
  public readonly key: string;
  public readonly endpointUrl: string;
  public readonly region: string;
  public readonly authType: string;

  public constructor(init: ResolvedS3TargetInit) {
    this.mode = init.mode;
    this.uri = init.uri;
    this.bucket = init.bucket;
    this.key = init.key;
    this.endpointUrl = init.endpointUrl;
    this.region = init.region;
    this.authType = init.authType;
  }

  public get isDirect(): boolean {
    return this.mode === "direct" || this.mode === "direct_federated";
  }

  public get cleanEndpoint(): string {
    return this.endpointUrl.replace(/^https?:\/\//u, "");
  }

  public get scheme(): "https" | "http" {
    return this.endpointUrl.startsWith("https://") ? "https" : "http";
  }

  public s3ClientConfig(): { endpoint: string; region: string; forcePathStyle: boolean } {
    return { endpoint: this.endpointUrl, region: this.region, forcePathStyle: true };
  }
}

export interface S3ResolverClientOptions {
  resolverUrl?: string;
  tokenPath?: string;
  timeoutMs?: number;
}

export class PermissionError extends Error {
  public readonly status: number;

  public constructor(message: string, status: number) {
    super(message);
    this.name = "PermissionError";
    this.status = status;
  }
}

interface ResolverPayload {
  mode?: string;
  uri?: string;
  bucket?: string;
  key?: string;
  endpoint_url?: string;
  region?: string;
  auth?: { type?: string };
}

export class S3ResolverClient {
  private readonly resolverUrl: string;
  private readonly tokenPath: string;
  private readonly timeoutMs: number;
  private readonly cache = new Map<string, ResolvedS3Target>();

  public constructor(opts?: S3ResolverClientOptions) {
    this.resolverUrl = opts?.resolverUrl || process.env.S3_RESOLVER_URL || DEFAULT_RESOLVER_URL;
    this.tokenPath = opts?.tokenPath || DEFAULT_TOKEN_PATH;
    this.timeoutMs = opts?.timeoutMs || 5000;
  }

  public static fallback(uri: string): ResolvedS3Target {
    const trimmed = uri.slice(5);
    const slash = trimmed.indexOf("/");
    const bucket = slash === -1 ? trimmed : trimmed.slice(0, slash);
    const key = slash === -1 ? "" : trimmed.slice(slash + 1);
    const endpointUrl =
      process.env.AWS_ENDPOINT_URL_S3 ||
      process.env.AWS_ENDPOINT_URL ||
      "http://s3-gateway.s3-system.svc:10080";
    return new ResolvedS3Target({
      mode: "fallback",
      uri,
      bucket,
      key,
      endpointUrl,
      region: process.env.AWS_REGION || "us-east-1",
      authType: "gateway_credential",
    });
  }

  private static parsePayload(uri: string, data: ResolverPayload): ResolvedS3Target {
    return new ResolvedS3Target({
      mode: data.mode || "direct",
      uri: data.uri || uri,
      bucket: data.bucket || "",
      key: data.key || "",
      endpointUrl: data.endpoint_url || "",
      region: data.region || "us-east-1",
      authType: data.auth?.type || "ambient_workload_identity",
    });
  }

  private readToken(): string | null {
    try {
      return fs.readFileSync(this.tokenPath, "utf8").trim();
    } catch {
      return null;
    }
  }

  private async fetchTarget(
    uri: string,
    headers: Record<string, string>,
  ): Promise<ResolvedS3Target> {
    const res = await fetch(`${this.resolverUrl}?uri=${encodeURIComponent(uri)}`, {
      headers,
      signal: AbortSignal.timeout(this.timeoutMs),
    });

    if (res.status === 401 || res.status === 403) {
      throw new PermissionError(
        `s3 resolver authorization failed with status ${res.status}: unauthorized access for uri "${uri}"`,
        res.status,
      );
    }

    if (!res.ok) {
      return S3ResolverClient.fallback(uri);
    }

    const data = (await res.json()) as ResolverPayload;
    return S3ResolverClient.parsePayload(uri, data);
  }

  public async resolve(uri: string): Promise<ResolvedS3Target> {
    if (!uri.startsWith("s3://")) {
      throw new Error(`URI must start with s3://: "${uri}"`);
    }

    const cached = this.cache.get(uri);
    if (cached) {
      return cached;
    }

    const headers: Record<string, string> = { Accept: "application/json" };
    const token = this.readToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    try {
      const target = await this.fetchTarget(uri, headers);
      this.cache.set(uri, target);
      return target;
    } catch (error) {
      if (
        error instanceof PermissionError ||
        (error instanceof Error && error.name === "PermissionError")
      ) {
        throw error;
      }
      return S3ResolverClient.fallback(uri);
    }
  }
}

let defaultClient: S3ResolverClient | null = null;

export function getDefaultClient(): S3ResolverClient {
  return (defaultClient ??= new S3ResolverClient());
}

export function resolveS3Uri(uri: string, resolverUrl?: string): Promise<ResolvedS3Target> {
  return resolverUrl
    ? new S3ResolverClient({ resolverUrl }).resolve(uri)
    : getDefaultClient().resolve(uri);
}
