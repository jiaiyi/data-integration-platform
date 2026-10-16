/** 与服务端 `api/schemas.py` 对应的响应与请求契约。 */

export type ProjectRole = 'ADMIN' | 'DATA_ENGINEER' | 'APPROVER' | 'VIEWER'

export type HarnessRole =
  | 'DATA_ENGINEER'
  | 'DATA_APPROVER'
  | 'SECURITY_APPROVER'
  | 'OPERATOR'
  | 'AUDITOR'

export type UserStatus = 'ACTIVE' | 'DISABLED'
export type ProjectStatus = 'ACTIVE' | 'ARCHIVED'

export interface HealthResponse {
  status: string
  service: string
  version: string
  environment: string
  auth_mode: string
  policy_bundle_version: string
}

export interface UserResponse {
  id: string
  subject: string
  display_name: string
  status: UserStatus
}

export interface ProjectResponse {
  id: string
  name: string
  status: ProjectStatus
  created_at: string
}

export interface BootstrapResponse {
  user: UserResponse
  project: ProjectResponse
}

export interface MembershipResponse {
  id: string
  project_id: string
  user_id: string
  role: ProjectRole
}

export interface RoleGrantResponse {
  id: string
  project_id: string
  user_id: string
  role: HarnessRole
}
