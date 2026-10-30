// Typed client for the backend (backend/src/sparring_partner/api/sessions.py).

export type Mode = "debug" | "review" | "explore";

export type SessionState =
  | "intent"
  | "properties_confirmed"
  | "exploring"
  | "predicting"
  | "hinting"
  | "verifying"
  | "postmortem";

export type Property = { id: string; statement: string; predicate: string };
export type Param = { name: string; kind: string; description: string };

export type ShownValue = { kind: "value" | "error" | "timeout"; text: string };

export type TraceRow = { step: number; vars: Record<string, string>; marked: boolean };
export type TraceTable = {
  rows: TraceRow[];
  output: string | null;
  exception: string | null;
  truncated_before: boolean;
};

export type MessageData = {
  // reveal / nudge
  outcome?: string;
  call?: string;
  predicted?: ShownValue;
  returned?: ShownValue;
  // level 3 hint
  prose?: string;
  table?: TraceTable;
  // fix_result
  inputs_checked?: number;
};

export type Message = {
  role: "coach" | "user";
  kind: string;
  level: number;
  text: string;
  data: MessageData | null;
};

export type Session = {
  id: string;
  mode: Mode;
  language: string;
  state: SessionState;
  function: string;
  intent: string;
  code: string;
  params: Param[];
  properties: Property[];
  hint_level: number;
  failures_found: number;
  has_postmortem: boolean;
  messages: Message[];
  created_at: number;
};

export type PredictionResult = {
  outcome: "revealed" | "agreed" | "matches_code" | "both_differ";
  message: string;
  actual: string;
  predicted: ShownValue;
  returned: ShownValue;
  state: SessionState;
  hint_level: number;
};

export type FixResult = {
  outcome: "passed" | "failed" | "special_cased" | "invalid";
  message: string;
  inputs_checked: number;
  state: SessionState;
  hint_level: number;
  next_question: string | null;
};

export type Postmortem = {
  bug_class: string | null;
  bug_classes: (string | null)[];
  how_to_spot: string;
  other_edge_cases: string[];
  hint_level_reached: number;
  fix_attempts: number;
  inputs_checked: number;
  cases: { call: string; bug_class: string | null; hint_level: number }[];
};

export type GateLogEntry = {
  at: number;
  kind: string;
  level: number;
  text: string;
  allowed: boolean;
  layer: string;
  rule: string | null;
  reason: string;
};

export type StreamEvent = {
  seq: number;
  type: "state" | "progress" | "error" | "message";
  message: string;
  data: Record<string, unknown>;
  at: number;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly detail: unknown,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`/api/sessions${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError(res.status, describeError(res.status, payload?.detail), payload?.detail);
  }
  return payload as T;
}

function describeError(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI request validation errors
    return detail.map((d: { msg?: string }) => d.msg ?? "invalid input").join("; ");
  }
  if (detail && typeof detail === "object" && "message" in detail) {
    return String((detail as { message: unknown }).message);
  }
  return `HTTP ${status}`;
}

export const api = {
  create: (code: string, intent: string, mode: Mode) =>
    request<Session>("POST", "", { code, intent, mode }),
  get: (id: string) => request<Session>("GET", `/${id}`),
  confirmProperties: (id: string, properties: { id: string | null; statement: string }[]) =>
    request<Session>("PUT", `/${id}/properties`, { properties }),
  predictionQuestion: (id: string) =>
    request<{ question: string; call: string }>("GET", `/${id}/prediction`),
  predict: (id: string, answer: string) =>
    request<PredictionResult>("POST", `/${id}/prediction`, { answer }),
  hint: (id: string) => request<{ level: number; text: string }>("POST", `/${id}/hint`),
  fix: (id: string, code: string) => request<FixResult>("POST", `/${id}/fix`, { code }),
  postmortem: (id: string) => request<Postmortem>("GET", `/${id}/postmortem`),
  gateLog: (id: string) => request<GateLogEntry[]>("GET", `/${id}/gate-log`),
};

/** Progress events for a session: the backlog first, then live. Returns a closer. */
export function subscribe(id: string, onEvent: (event: StreamEvent) => void): () => void {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/api/sessions/${id}/stream`);
  ws.onmessage = (msg) => onEvent(JSON.parse(msg.data) as StreamEvent);
  return () => ws.close();
}
