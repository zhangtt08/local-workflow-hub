export type Risk = 'low' | 'medium' | 'high';

export type Tool = {
  id: string;
  key: string;
  title: string;
  kind: string;
  category: string;
  description: string;
  command: string[];
  source: string;
  cwd: string | null;
  tags: string[];
  args: { name: string; kind: string; required: boolean; description: string }[];
  risk: Risk;
  permissions: string[];
  loc: string | null;
  language: string | null;
  enabled: boolean;
  starred: boolean;
  notes: string;
  alias: string;
  invocable: boolean;
  blueprint: boolean;
  call_hint: string | null;
  found_at: string;
  invocations: number;
  last_invoked: string | null;
  probe?: { output?: string; at?: string };
};

export type Facets = {
  total: number; enabled: number; starred: number;
  categories: Record<string, number>; kinds: Record<string, number>; risks: Record<string, number>;
};

export type ToolList = { total: number; offset: number; limit: number; items: Tool[]; facets: Facets };

export type ValueSpec = { type: string; required: boolean; description: string };

export type Skill = {
  id: string; name: string; version: string; description: string; category: string; tags: string[];
  inputs: Record<string, ValueSpec>; outputs: Record<string, ValueSpec>;
  executor: { type: string; entry: string; args: string[]; cwd: string | null };
  permissions: string[]; risk_level: Risk; enabled: boolean; timeout_seconds: number;
  metadata: Record<string, unknown>;
};

export type ToolDetail = {
  tool: Tool;
  skill: Skill | null;
  invocation: { endpoint: string; fields: Record<string, ValueSpec>; generic: string[]; example: unknown };
  snippets: Record<string, string>;
  audit: Activity[];
};

export type Activity = {
  at: string; type: string; tool?: string; actor?: string; run_id?: string; status?: string;
  exit_code?: number | null; command_line?: string; duration_ms?: number; decision?: string;
  approval?: string; hint?: string;
};

export type RunSummary = {
  id: string; trace_id: string; workflow_id: string; workflow_version_id: string; status: string;
  started_at: string | null; finished_at: string | null; kind: string; catalog_key: string | null;
  nodes: { id: string; skill: string; status: string; attempt: number; error: string | null }[];
  inputs: Record<string, unknown>; outputs: Record<string, unknown>; error: string | null;
};

export type Run = {
  id: string; trace_id: string; workflow_id: string; workflow_version_id: string; status: string;
  inputs: Record<string, unknown>; outputs: Record<string, unknown>;
  error: { message: string; code: string; details?: Record<string, unknown>; retryable?: boolean } | null;
  started_at: string | null; finished_at: string | null;
  node_runs: Record<string, {
    id: string; node_id: string; skill_id: string; skill_version: string; status: string; attempt: number;
    inputs: Record<string, unknown>; started_at: string | null; finished_at: string | null;
    result: {
      status: string; outputs: Record<string, unknown>; logs: string[];
      error: { code: string; message: string; retryable: boolean; details: Record<string, unknown> } | null;
      artifacts: { id: string; uri: string; media_type: string; size_bytes: number | null }[];
    } | null;
  }>;
};

export type WorkflowRow = { id: string; name: string; version: number; nodes: number; edges: number; inputs: string[]; file: string };
export type Approval = { id: string; tool: string; title: string; risk: string; status: string; created_at: string; payload: Record<string, unknown> };
export type Overview = {
  generated_at: string; catalog: Facets; last_scan: Record<string, unknown>; scan_state: { running: boolean; phase: string; found: number };
  settings: Record<string, unknown>; skills: { count: number; items: string[] };
  workflows: { count: number; items: WorkflowRow[] }; activity: Activity[]; recent_runs: RunSummary[];
  approvals: Approval[]; service: { static_ui: boolean; python: string; version: string };
};
export type AgentInfo = {
  name: string; version: string; base_url: string; protocol: string[];
  auth: { required: boolean; header: string | null };
  counts: Record<string, number>; endpoints: Record<string, string>;
  conventions: Record<string, unknown>; risk: { approval_mode: boolean; high_risk_enabled: boolean };
};
export type SystemInfo = {
  app: { version: string; frozen: boolean; desktop_mode: boolean };
  machine: Record<string, string | null>;
  paths: Record<string, string>; counts: Facets; executors: string[]; token_required: boolean;
};
export type InvokeResult = {
  status: string; run_id: string | null; trace_id?: string; task_id?: string; poll?: string;
  tool?: { key: string; skill_id: string; title: string; risk: Risk };
  outputs?: Record<string, string | number | boolean | null>;
  artifacts?: { uri: string; media_type: string; size_bytes: number | null }[];
  logs?: string[]; approval?: Approval; hint?: string;
  error?: { code: string; message: string; details: Record<string, unknown> } | null;
};

export class ApiError extends Error {
  code: string;
  details: Record<string, unknown>;
  constructor(message: string, code = 'unknown_error', details: Record<string, unknown> = {}) {
    super(message);
    this.code = code;
    this.details = details;
  }
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (exc) {
    throw new ApiError(`本地服务无响应：${exc instanceof Error ? exc.message : exc}`, 'external_service_error');
  }
  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { raw: text };
    }
  }
  if (!response.ok) {
    const err = (body as { error?: { message?: string; code?: string; details?: Record<string, unknown> } })?.error;
    throw new ApiError(err?.message || `请求失败 ${response.status}`, err?.code || 'unknown_error', err?.details || {});
  }
  return body as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, payload?: unknown) =>
    request<T>(path, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(payload ?? {}) }),
  put: <T,>(path: string, payload?: unknown) =>
    request<T>(path, { method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify(payload ?? {}) }),
  patch: <T,>(path: string, payload?: unknown) =>
    request<T>(path, { method: 'PATCH', headers: JSON_HEADERS, body: JSON.stringify(payload ?? {}) }),
  del: <T,>(path: string) => request<T>(path, { method: 'DELETE' }),
  text: async (path: string): Promise<string> => {
    const response = await fetch(path);
    if (!response.ok) throw new ApiError(`读取失败 ${response.status}`, 'external_service_error');
    return response.text();
  },

  health: () => api.get<{ status: string; tools: number; enabled: number }>('/health'),
  overview: () => api.get<Overview>('/api/v1/overview'),
  tools: (query: Record<string, string | number | boolean | undefined>) => {
    const params = new URLSearchParams();
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== '' && v !== 'all') params.set(k, String(v));
    });
    const qs = params.toString();
    return api.get<ToolList>(`/api/v1/tools${qs ? `?${qs}` : ''}`);
  },
  tool: (key: string) => api.get<ToolDetail>(`/api/v1/tools/${key}`),
  invoke: (key: string, inputs: Record<string, unknown>, actor = 'workflowhub-ui', wait = true) =>
    api.post<InvokeResult>(`/api/v1/tools/${key}/invoke`, { inputs, actor, wait }),
  probe: (key: string) => api.post<{ ok: boolean; output: string }>(`/api/v1/tools/${key}/probe`),
  adopt: (key: string) => api.post<{ id: string; path: string; skills_now: number; hint: string }>(`/api/v1/tools/${key}/adopt`),
  setState: (key: string, body: { enabled?: boolean; starred?: boolean }) => api.patch<Tool>(`/api/v1/tools/${key}/state`, body),
  setNote: (key: string, body: { notes: string; alias?: string }) => api.put<Tool>(`/api/v1/tools/${key}/note`, body),
  addCustom: (body: Record<string, unknown>) => api.post<Tool>('/api/v1/tools', body),
  remove: (key: string) => api.del<{ status: string; key: string }>(`/api/v1/tools/${key}`),
  scan: (roots: string[] | null, wait: boolean) =>
    api.post<{ result: Record<string, unknown>; stats: Record<string, unknown> }>('/api/v1/discovery/scan', { roots, wait }),
  scanStatus: () => api.get<{ scan_state: { running: boolean; phase: string; found: number }; last_scan: Record<string, unknown>; errors: string[]; roots: string[]; stats: Record<string, unknown> }>('/api/v1/discovery/status'),
  task: (id: string) => api.get<{ id: string; status: string; result?: InvokeResult; error?: { message: string } }>(`/api/v1/tasks/${id}`),
  settings: () => api.get<{ settings: Record<string, unknown>; defaults: Record<string, unknown> }>('/api/v1/settings'),
  saveSettings: (patch: Record<string, unknown>) => api.put<{ settings: Record<string, unknown> }>('/api/v1/settings', { patch }),
  activity: (limit = 40) => api.get<{ items: Activity[] }>(`/api/v1/activity?limit=${limit}`),
  runs: (limit = 40, status?: string) => api.get<{ items: RunSummary[] }>(`/api/v1/runs?limit=${limit}${status ? `&status=${status}` : ''}`),
  run: (id: string) => api.get<Run>(`/api/v1/runs/${id}`),
  workflows: () => api.get<{ items: WorkflowRow[] }>('/api/v1/workflows'),
  workflow: (id: string, version: number) => api.get<Record<string, unknown>>(`/api/v1/workflows/${id}?version=${version}`),
  validateWorkflow: (body: Record<string, unknown>) => api.post<{ valid: boolean; errors: string[]; warnings: string[]; nodes: { id: string; skill: string | null }[] }>('/api/v1/workflows/validate', body),
  saveWorkflow: (body: Record<string, unknown>) => api.post<{ workflow_version_id: string; warnings: string[]; valid: boolean }>('/api/v1/workflows', body),
  runWorkflow: (id: string, version: number, inputs: Record<string, unknown>) =>
    api.post<Run>(`/api/v1/workflows/${id}/run`, { version, inputs, actor: 'workflowhub-ui' }),
  skills: () => api.get<{ items: Skill[]; total: number }>('/api/v1/skills'),
  approvals: () => api.get<{ pending: Approval[] }>('/api/v1/approvals'),
  decide: (id: string, approve: boolean) => api.post<Approval>(`/api/v1/approvals/${id}/decide`, { approve }),
  agentInfo: () => api.get<AgentInfo>('/api/v1/agent/info'),
  mcpConfig: () => api.get<Record<string, unknown>>('/api/v1/agent/mcp-config'),
  guide: () => api.text('/api/v1/agent/guide'),
  quickstart: () => api.text('/api/v1/agent/quickstart'),
  schemaCount: () => api.get<{ count: number }>('/api/v1/tools.json'),
  system: () => api.get<SystemInfo>('/api/v1/system/info'),
  search: (query: string) => api.get<{ skills: { id: string; name: string; category: string; description: string; risk: string }[] }>(`/api/v1/search?query=${encodeURIComponent(query)}&top_k=12`),
  file: (path: string) => api.get<{ path: string; size: number; text: string; truncated: boolean }>(`/api/v1/files?path=${encodeURIComponent(path)}`),
  windowState: () => api.get<{ desktop_mode: boolean; state: WindowState | null }>('/api/v1/window/state'),
  windowAction: (action: string, payload?: Record<string, unknown>) =>
    api.post<{ ok: boolean; state?: WindowState; reason?: string }>('/api/v1/window/action', { action, payload }),
  windowMeasure: (innerHeight: number, dpr: number) =>
    api.post<{ desktop_mode: boolean; state?: WindowState }>('/api/v1/window/measure', { inner_height: innerHeight, device_pixel_ratio: dpr }),
};

export type WindowState = {
  hwnd: number | null; x: number; y: number; width: number; height: number; bar: number; dpr: number;
  maximized: boolean; minimized: boolean; alive: boolean; work_area: number[];
};

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '从未';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(diff)) return iso;
  if (diff < 60) return `${Math.max(0, Math.round(diff))} 秒前`;
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 30) return `${Math.round(diff / 86400)} 天前`;
  return iso.slice(0, 10);
}

export function clockTime(iso: string | null | undefined): string {
  if (!iso) return '--:--:--';
  const date = new Date(iso);
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

export const KIND_LABELS: Record<string, string> = {
  cli: '可执行程序', script: '脚本工具', app: '已安装应用', 'project-script': '项目入口',
  skill: 'Skill 包', 'workflow-blueprint': 'CI 蓝图', custom: '手动登记',
};

export const RISK_LABELS: Record<string, string> = {
  low: '低风险', medium: '中风险', high: '高风险',
};

export const RISK_HINT: Record<string, string> = {
  low: '只读取信息或产出文件，误用影响小。',
  medium: '可能联网、写入或改变项目状态。',
  high: '能执行任意代码或改动系统，请确认后再让 Agent 使用。',
};
