<script setup lang="ts">
import {
  Activity,
  Database,
  GitBranch,
  LayoutDashboard,
  RefreshCw,
  Server,
  ShieldCheck,
  Sparkles,
} from 'lucide-vue-next'
import { computed, onMounted, onUnmounted, ref } from 'vue'

import ConnectionsView from './views/ConnectionsView.vue'
import GovernanceView from './views/GovernanceView.vue'
import OperationsView from './views/OperationsView.vue'
import OverviewView from './views/OverviewView.vue'
import StudioView from './views/StudioView.vue'
import {
  refresh,
  selectActor,
  selectProject,
  selection,
  serverState,
} from './workspace'

const VIEW_KEYS = ['overview', 'connections', 'studio', 'operations', 'governance'] as const
type ViewKey = (typeof VIEW_KEYS)[number]

const views = [
  {
    key: 'overview',
    label: '总览',
    icon: LayoutDashboard,
    title: '总览工作台',
    subtitle: '项目概况与待办入口',
    component: OverviewView,
  },
  {
    key: 'connections',
    label: '数据连接',
    icon: Database,
    title: '数据连接',
    subtitle: '连接登记、元数据探查与凭据生命周期',
    component: ConnectionsView,
  },
  {
    key: 'studio',
    label: 'Pipeline Studio',
    icon: Sparkles,
    title: 'Pipeline Studio',
    subtitle: '需求澄清、方案生成与确定性门禁',
    component: StudioView,
  },
  {
    key: 'operations',
    label: '运行中心',
    icon: Activity,
    title: '运行中心',
    subtitle: '准备单、执行监控与受管处置',
    component: OperationsView,
  },
  {
    key: 'governance',
    label: '安全治理',
    icon: ShieldCheck,
    title: '安全治理',
    subtitle: 'Benchmark、指标大盘与改进候选',
    component: GovernanceView,
  },
] as const

function readHashView(): ViewKey {
  const raw = window.location.hash.replace(/^#\/?/, '')
  return (VIEW_KEYS as readonly string[]).includes(raw) ? (raw as ViewKey) : 'overview'
}

const activeKey = ref<ViewKey>(readHashView())
const activeView = computed(() => views.find((item) => item.key === activeKey.value) ?? views[0])
const actorDraft = ref(selection.actorId)

function syncHash(): void {
  activeKey.value = readHashView()
}

function activate(key: ViewKey): void {
  activeKey.value = key
  window.location.hash = `/${key}`
}

function applyActor(): void {
  selectActor(actorDraft.value)
}

function onProjectChange(event: Event): void {
  const target = event.target as HTMLSelectElement
  selectProject(target.value)
}

function onMemberChange(event: Event): void {
  const target = event.target as HTMLSelectElement
  actorDraft.value = target.value
  selectActor(target.value)
}

const needsBootstrap = computed(
  () => Boolean(selection.actorId) && !serverState.loading && serverState.projects.length === 0,
)

onMounted(() => {
  window.addEventListener('hashchange', syncHash)
  void refresh()
})

onUnmounted(() => {
  window.removeEventListener('hashchange', syncHash)
})
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand">
        <span class="brand__mark">ETL</span>
        <span class="brand__text">
          <span class="brand__name">数据集成平台</span>
          <span class="brand__sub">受管 ETL-Agent 控制台</span>
        </span>
      </div>

      <nav class="nav">
        <button
          v-for="item in views"
          :key="item.key"
          type="button"
          class="nav__item"
          :class="{ 'nav__item--active': item.key === activeKey }"
          @click="activate(item.key)"
        >
          <component :is="item.icon" :size="16" />
          <span>{{ item.label }}</span>
        </button>
      </nav>

      <div class="sidebar__spacer" />

      <footer class="sidebar__footer">
        <span class="sidebar__meta">
          <GitBranch :size="12" />
          <span class="dip-mono">{{ serverState.policyBundleVersion || '—' }}</span>
        </span>
        <span class="sidebar__meta">
          <Server :size="12" />
          <span>环境 {{ serverState.environment || '—' }} · v{{ serverState.version || '—' }}</span>
        </span>
      </footer>
    </aside>

    <main class="main">
      <header class="topbar">
        <div class="topbar__heading">
          <h1>{{ activeView.title }}</h1>
          <p>{{ activeView.subtitle }}</p>
        </div>
        <div class="topbar__selectors">
          <label class="selector">
            <span class="selector__label">工作区</span>
            <select
              class="dip-select"
              :value="selection.projectId"
              :disabled="serverState.projects.length === 0"
              @change="onProjectChange"
            >
              <option v-if="serverState.projects.length === 0" value="">未选择</option>
              <option v-for="item in serverState.projects" :key="item.id" :value="item.id">
                {{ item.name }}
              </option>
            </select>
          </label>

          <label class="selector">
            <span class="selector__label">操作者</span>
            <select
              v-if="serverState.members.length"
              class="dip-select"
              :value="selection.actorId"
              @change="onMemberChange"
            >
              <option v-for="member in serverState.members" :key="member.user_id" :value="member.user_id">
                {{ member.user_id.slice(0, 8) }}… · {{ member.role }}
              </option>
            </select>
            <input
              v-else
              v-model="actorDraft"
              class="dip-select"
              placeholder="粘贴用户 UUID"
              @keyup.enter="applyActor"
            />
          </label>

          <button type="button" class="dip-btn dip-btn--ghost" @click="refresh">
            <RefreshCw :size="14" /> 刷新事实
          </button>
        </div>
      </header>

      <div class="content">
        <div v-if="serverState.error" class="banner banner--error">
          控制面请求失败：{{ serverState.error }}
        </div>
        <div v-else-if="!selection.actorId" class="banner">
          尚未选择操作者。首次使用请先调用
          <span class="dip-mono">POST /api/v1/bootstrap</span>（需带
          <span class="dip-mono">X-Bootstrap-Token</span>），再把返回的管理员
          <span class="dip-mono">user.id</span> 粘贴到右上角。
        </div>
        <div v-else-if="needsBootstrap" class="banner">
          当前操作者不属于任何工作区。若尚未初始化，请先调用
          <span class="dip-mono">POST /api/v1/bootstrap</span>；已初始化则确认粘贴的是项目成员的用户 UUID。
        </div>

        <component :is="activeView.component" />
      </div>
    </main>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  min-height: 100vh;
  background: var(--dip-bg);
}

.sidebar {
  display: flex;
  flex-direction: column;
  width: var(--dip-sidebar-width);
  flex-shrink: 0;
  background: var(--dip-card);
  border-right: 1px solid #edf1f6;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 12px 14px;
}

.brand__mark {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: var(--dip-primary);
  color: #ffffff;
  font-size: 11px;
  font-weight: 700;
}

.brand__text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.brand__name {
  font-size: 13.5px;
  font-weight: 700;
  line-height: 1.2;
}

.brand__sub {
  font-size: 10px;
  color: var(--dip-muted);
  line-height: 1.2;
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-top: 1px solid #edf1f6;
}

.nav__item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--dip-sub);
  font-size: 12.5px;
  text-align: left;
}

.nav__item:hover {
  background: #f8fafc;
}

.nav__item--active {
  background: var(--dip-primary-soft);
  color: var(--dip-primary);
  font-weight: 600;
}

.sidebar__spacer {
  flex: 1;
  border-top: 1px solid #edf1f6;
}

.sidebar__footer {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px 14px;
  background: #fafbfc;
  border-top: 1px solid #edf1f6;
}

.sidebar__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--dip-muted);
}

.main {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: var(--dip-topbar-height);
  padding: 0 22px;
  background: var(--dip-card);
  border-bottom: 1px solid var(--dip-border);
}

.topbar__heading h1 {
  font-size: 16px;
  font-weight: 700;
  line-height: 1.3;
}

.topbar__heading p {
  font-size: 11px;
  color: var(--dip-muted);
  line-height: 1.3;
  margin-top: 3px;
}

.topbar__selectors {
  display: flex;
  align-items: center;
  gap: 10px;
}

.selector {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--dip-border);
  border-radius: 8px;
  background: #f8fafc;
}

.selector__label {
  font-size: 11px;
  color: var(--dip-muted);
}

.selector .dip-select {
  border: none;
  background: transparent;
  padding: 0;
}

.content {
  display: flex;
  flex-direction: column;
  gap: 14px;
  flex: 1;
  padding: 18px;
  min-width: 0;
}

.banner {
  padding: 11px 14px;
  border: 1px solid var(--dip-border);
  border-radius: 8px;
  background: var(--dip-primary-soft);
  color: var(--dip-primary);
  font-size: 11.5px;
  line-height: 1.6;
}

.banner--error {
  border-color: #fecaca;
  background: var(--dip-red-soft);
  color: var(--dip-red);
}
</style>
