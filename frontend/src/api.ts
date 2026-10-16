/**
 * 控制面 HTTP 客户端。
 *
 * 只负责请求与错误归一化：不在本地缓存 Artifact、审批结果、Capability、运行状态或
 * Secret（FR-98）。企业状态一律以服务端为真相源，刷新后重新拉取。
 */

import type {
  HealthResponse,
  MembershipResponse,
  ProjectResponse,
  RoleGrantResponse,
} from './types'

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''
const API_PREFIX = '/api/v1'

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  body?: unknown
  actorId?: string
  bootstrapToken?: string
}

/** FastAPI 的校验错误体是数组，领域错误体是 `{detail, trace_id}`；两者都要能读。 */
function extractDetail(payload: unknown, status: number): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail
    if (typeof detail === 'string') {
      return detail
    }
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (item && typeof item === 'object' && 'msg' in item) {
            const loc = 'loc' in item && Array.isArray((item as { loc: unknown }).loc)
              ? (item as { loc: unknown[] }).loc.join('.')
              : ''
            return loc ? `${loc}: ${String((item as { msg: unknown }).msg)}` : String((item as { msg: unknown }).msg)
          }
          return String(item)
        })
        .join('; ')
    }
  }
  return `请求失败（HTTP ${status}）`
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {}
  if (options.actorId) {
    headers['X-Actor-Id'] = options.actorId
  }
  if (options.bootstrapToken) {
    headers['X-Bootstrap-Token'] = options.bootstrapToken
  }
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })

  if (response.status === 204) {
    return undefined as T
  }
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    throw new ApiError(response.status, extractDetail(payload, response.status))
  }
  return payload as T
}

export const api = {
  health: () => request<HealthResponse>('/health'),

  listProjects: (actorId: string) =>
    request<ProjectResponse[]>(`${API_PREFIX}/projects`, { actorId }),

  listMembers: (projectId: string, actorId: string) =>
    request<MembershipResponse[]>(`${API_PREFIX}/projects/${projectId}/members`, { actorId }),

  listRoleGrants: (projectId: string, actorId: string) =>
    request<RoleGrantResponse[]>(`${API_PREFIX}/projects/${projectId}/role-grants`, { actorId }),

  createProject: (name: string, actorId: string) =>
    request<ProjectResponse>(`${API_PREFIX}/projects`, {
      method: 'POST',
      body: { name },
      actorId,
    }),
}
