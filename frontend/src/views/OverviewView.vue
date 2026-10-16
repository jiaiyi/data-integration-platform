<script setup lang="ts">
import { computed } from 'vue'

import KpiCard from '../components/KpiCard.vue'
import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'
import { serverState } from '../workspace'

const kpis = computed(() => [
  { label: '连接数', value: '—', hint: 'T-49' },
  { label: 'Pipeline 数', value: '—', hint: 'T-50' },
  { label: '待审批门禁', value: '—', hint: 'T-52' },
  { label: '运行成功率', value: '—', hint: 'T-52' },
])

const resourceGroups = [
  { label: '源端连接', milestone: 'M2' },
  { label: '目标端连接', milestone: 'M2' },
  { label: '文件资产', milestone: 'M2' },
  { label: '凭据引用', milestone: 'M2' },
]

const recentColumns = ['运行标识', '状态', '读取行数', '质量结论', '耗时']
</script>

<template>
  <div class="overview">
    <div class="overview__kpis">
      <KpiCard
        v-for="item in kpis"
        :key="item.label"
        :label="item.label"
        :value="item.value"
        :hint="item.hint"
      />
    </div>

    <div class="overview__row">
      <PanelCard title="待办审批门禁" subtitle="按提交时间排序，区分数据维度与安全维度">
        <div v-if="!serverState.projects.length" class="dip-pending">
          尚未选择工作区。先在右上角完成首次初始化并选择工作区与操作者。
        </div>
        <div v-else class="dip-pending">
          准备单与审批槽位由 M5（T-33 / T-34）提供，当前无可展示的待办事实。
        </div>
      </PanelCard>

      <PanelCard title="核心资源分布" subtitle="只读视图">
        <ul class="distribution">
          <li v-for="group in resourceGroups" :key="group.label">
            <span>{{ group.label }}</span>
            <span class="distribution__value">
              <span class="dip-mono">—</span>
              <StatusBadge :text="group.milestone" tone="neutral" />
            </span>
          </li>
        </ul>
      </PanelCard>
    </div>

    <PanelCard title="最近运行" subtitle="点击可下钻到运行中心查看执行详情">
      <table class="dip-table">
        <thead>
          <tr>
            <th v-for="column in recentColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="5" class="overview__empty">
              执行记录由 M5（T-37 / T-38）产生，当前无运行事实。
            </td>
          </tr>
        </tbody>
      </table>
    </PanelCard>

    <p class="dip-pending">
      PRD §7.1「明确不做」：本页<strong>不提供任何直接提交作业的按钮</strong>。受管执行只能经
      Pipeline Studio 冻结版本 → 双人审批 → 操作员 Commit 进入，不存在从总览直达引擎的入口。
    </p>
  </div>
</template>

<style scoped>
.overview {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.overview__kpis {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
}

.overview__row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  align-items: start;
}

.distribution {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
}

.distribution li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.distribution__value {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--dip-sub);
}

.overview__empty {
  color: var(--dip-muted);
  font-size: 11.5px;
}
</style>
