import { randomUUID } from 'node:crypto';

const DEFAULT_TIMEOUT_MS = 60_000;
const MAX_SUCCESS_BODY_BYTES = 8 * 1024 * 1024;
const MAX_ERROR_BODY_BYTES = 64 * 1024;
const IDEMPOTENCY_KEY_PATTERN = /^[A-Za-z0-9._:-]{1,200}$/;

export interface OamClientOptions {
  baseUrl: string;
  userId: string;
  apiKey: string;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface RecallInput {
  query: string;
  recallLevel?: 'fast' | 'normal';
  includeChunks?: boolean;
}

export interface AddInput {
  content: string;
  eventTime?: string;
  learningEligible?: boolean;
  idempotencyKey?: string;
}

export type AddDocumentInput =
  | {
      documentId: string;
      updateMode: 'replace' | 'append';
      text: string;
      url?: never;
      mediaType?: never;
      idempotencyKey?: string;
    }
  | {
      documentId: string;
      updateMode: 'replace';
      url: string;
      mediaType: 'text/html' | 'application/pdf' | 'image/png' | 'image/jpeg';
      text?: never;
      idempotencyKey?: string;
    };

export interface RemoveInput {
  memoryId: string;
  memoryType: 'basic' | 'insight';
}

export type JsonObject = Record<string, unknown>;

export class OamApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryable: boolean | null;

  constructor(
    status: number,
    code: string,
    requestId: string | null = null,
    retryable: boolean | null = null,
  ) {
    super(`${code} (HTTP ${status})${requestId ? ` request_id=${requestId}` : ''}`);
    this.name = 'OamApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.retryable = retryable;
  }
}

export interface OamClient {
  recall(input: RecallInput): Promise<JsonObject>;
  add(input: AddInput): Promise<JsonObject>;
  addDocument(input: AddDocumentInput): Promise<JsonObject>;
  getOperation(operationId: string): Promise<JsonObject>;
  getProfile(): Promise<JsonObject>;
  remove(input: RemoveInput): Promise<JsonObject>;
}

export function createOamClient(options: OamClientOptions): OamClient {
  const baseUrl = normalizeBaseUrl(options.baseUrl);
  const fetchImpl = options.fetchImpl ?? fetch;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const userId = requireValue(options.userId, 'OAM_USER_ID');
  const apiKey = requireValue(options.apiKey, 'OAM_USER_API_KEY');
  const userPath = encodeURIComponent(userId);

  async function request(
    path: string,
    requestOptions: {
      method?: string;
      body?: JsonObject;
      idempotencyKey?: string;
    } = {},
  ): Promise<JsonObject> {
    const headers: Record<string, string> = {
      accept: 'application/json',
      authorization: `Bearer ${apiKey}`,
    };
    if (requestOptions.body) headers['content-type'] = 'application/json';
    if (requestOptions.idempotencyKey) headers['idempotency-key'] = requestOptions.idempotencyKey;

    let response: Response;
    try {
      response = await fetchImpl(`${baseUrl}${path}`, {
        method: requestOptions.method ?? 'GET',
        headers,
        body: requestOptions.body ? JSON.stringify(requestOptions.body) : undefined,
        redirect: 'error',
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (error) {
      if (error instanceof Error && ['TimeoutError', 'AbortError'].includes(error.name)) {
        throw new OamApiError(0, 'RequestTimeout', null, true);
      }
      throw new OamApiError(0, 'NetworkError', null, true);
    }

    const requestId = response.headers.get('x-request-id');
    let text: string;
    try {
      text = await readBoundedBody(
        response,
        response.ok ? MAX_SUCCESS_BODY_BYTES : MAX_ERROR_BODY_BYTES,
        requestId,
      );
    } catch (error) {
      if (error instanceof Error && ['TimeoutError', 'AbortError'].includes(error.name)) {
        throw new OamApiError(0, 'RequestTimeout', requestId, true);
      }
      throw error;
    }

    let parsed: unknown = {};
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new OamApiError(
          response.status,
          response.ok ? 'InvalidResponse' : 'HttpError',
          requestId,
          false,
        );
      }
    }

    if (!response.ok) {
      const detail = errorDetail(parsed);
      throw new OamApiError(
        response.status,
        typeof detail.code === 'string' ? detail.code : 'HttpError',
        requestId,
        typeof detail.retryable === 'boolean' ? detail.retryable : null,
      );
    }

    return asObject(parsed);
  }

  return {
    recall(input) {
      return request('/v1/recall', {
        method: 'POST',
        body: {
          user_id: userId,
          query: input.query,
          recall_level: input.recallLevel ?? 'fast',
          include: { chunks: input.includeChunks ?? true },
        },
      });
    },

    async add(input) {
      return request('/v1/retain', {
        method: 'POST',
        idempotencyKey: resolveIdempotencyKey(input.idempotencyKey),
        body: {
          user_id: userId,
          input_type: 'interaction',
          messages: [
            {
              role: 'user',
              content: input.content,
              event_time: input.eventTime ?? new Date().toISOString(),
              learning_eligible: input.learningEligible ?? true,
            },
          ],
        },
      });
    },

    async addDocument(input) {
      const body: JsonObject =
        'text' in input && input.text !== undefined
          ? {
              input_type: 'document',
              document_id: input.documentId,
              update_mode: input.updateMode,
              text: input.text,
            }
          : {
              input_type: 'document_url',
              document_id: input.documentId,
              update_mode: input.updateMode,
              media_type: input.mediaType,
              url: input.url,
            };
      return request(`/v1/users/${userPath}/resources`, {
        method: 'POST',
        idempotencyKey: resolveIdempotencyKey(input.idempotencyKey),
        body,
      });
    },

    getOperation(operationId) {
      return request(
        `/v1/users/${userPath}/operations/${encodeURIComponent(operationId)}`,
      );
    },

    getProfile() {
      return request(`/v1/users/${userPath}/profile`);
    },

    remove(input) {
      const collection = input.memoryType === 'insight' ? 'insights' : 'memories';
      return request(
        `/v1/users/${userPath}/${collection}/${encodeURIComponent(input.memoryId)}`,
        { method: 'DELETE' },
      );
    },
  };
}

function normalizeBaseUrl(raw: string): string {
  const value = requireValue(raw, 'OAM_BASE_URL').replace(/\/+$/, '');
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('OAM_BASE_URL must be a valid absolute URL');
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error('OAM_BASE_URL must use http or https');
  }
  if (parsed.username || parsed.password) {
    throw new Error('OAM_BASE_URL must not contain credentials');
  }
  if (parsed.search || parsed.hash) {
    throw new Error('OAM_BASE_URL must not contain a query or fragment');
  }
  return value;
}

function requireValue(value: string, name: string): string {
  if (typeof value !== 'string' || !value.trim()) {
    throw new Error(`${name} is required`);
  }
  return value.trim();
}

function resolveIdempotencyKey(value: string | undefined): string {
  if (!value) return randomUUID();
  if (!IDEMPOTENCY_KEY_PATTERN.test(value)) {
    throw new OamApiError(0, 'InvalidIdempotencyKey', null, false);
  }
  return value;
}

async function readBoundedBody(
  response: Response,
  limit: number,
  requestId: string | null,
): Promise<string> {
  if (!response.body) return '';
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) {
        await reader.cancel();
        throw new OamApiError(response.status, 'ResponseTooLarge', requestId, false);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  return Buffer.concat(chunks).toString('utf8');
}

function errorDetail(parsed: unknown): { code?: unknown; retryable?: unknown } {
  if (!parsed || typeof parsed !== 'object') return {};
  const detail = (parsed as Record<string, unknown>).detail;
  return detail && typeof detail === 'object'
    ? (detail as { code?: unknown; retryable?: unknown })
    : {};
}

function asObject(parsed: unknown): JsonObject {
  return parsed && typeof parsed === 'object' ? (parsed as JsonObject) : {};
}
