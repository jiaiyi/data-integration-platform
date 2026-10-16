/**
 * 工作区与操作者选择（FR-98 / T-47）。
 *
 * 浏览器**只**保存当前工作区与操作者选择；Artifact、审批结果、Capability、运行状态
 * 与 Secret 一律不落本地存储。刷新后从 API 重新恢复事实。
 */

import { reactive } from 'vue'

import { ApiError, api } from './api'
import type { MembershipResponse, ProjectResponse } from './types'

const STORAGE_KEY = 'dip.console.workspace'

interface Selection {
  actorId: string
  projectId: string
}

function loadSelection(): Selection {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const parsed: unknown = JSON.parse(raw)
      if (parsed && typeof parsed === 'object') {
        const candidate = parsed as Partial<Selection>
        return {
          actorId: typeof candidate.actorId === 'string' ? candidate.actorId : '',
          projectId: typeof candidate.projectId === 'string' ? candidate.projectId : '',
        }
      }
    }
  } catch {
    // 本地存储损坏时退回未选状态；企业事实仍以服务端为准，不影响正确性。
  }
  return { actorId: '', projectId: '' }
}

/** 持久化的两项选择。 */
export const selection = reactive<Selection>(loadSelection())

/** 服务端事实的内存缓存，仅存活于当前页面会话。 */
export const serverState = reactive({
  serviceName: '',
  environment: '',
  authMode: '',
  version: '',
  policyBundleVersion: '',
  projects: [] as ProjectResponse[],
  members: [] as MembershipResponse[],
  loading: false,
  error: '',
})

function persist(): void {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ actorId: selection.actorId, projectId: selection.projectId }),
  )
}

export function selectActor(actorId: string): void {
  selection.actorId = actorId.trim()
  persist()
  void refresh()
}

export function selectProject(projectId: string): void {
  selection.projectId = projectId
  persist()
  void refresh()
}

export async function refresh(): Promise<void> {
  serverState.error = ''
  try {
    const health = await api.health()
    serverState.serviceName = health.service
    serverState.environment = health.environment
    serverState.authMode = health.auth_mode
    serverState.version = health.version
    serverState.policyBundleVersion = health.policy_bundle_version
  } catch (error) {
    serverState.error = describe(error)
    return
  }

  if (!selection.actorId) {
    serverState.projects = []
    serverState.members = []
    return
  }

  serverState.loading = true
  try {
    serverState.projects = await api.listProjects(selection.actorId)
    if (!serverState.projects.some((item) => item.id === selection.projectId)) {
      selection.projectId = serverState.projects[0]?.id ?? ''
      persist()
    }
    serverState.members = selection.projectId
      ? await api.listMembers(selection.projectId, selection.actorId)
      : []
  } catch (error) {
    serverState.projects = []
    serverState.members = []
    serverState.error = describe(error)
  } finally {
    serverState.loading = false
  }
}

export function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message}（HTTP ${error.status}）`
  }
  return error instanceof Error ? error.message : '未知错误'
}

export function currentProject(): ProjectResponse | undefined {
  return serverState.projects.find((item) => item.id === selection.projectId)
}

export function currentActorDisplay(): string {
  const member = serverState.members.find((item) => item.user_id === selection.actorId)
  return member ? `${selection.actorId.slice(0, 8)}…（${member.role}）` : selection.actorId
}
