<script setup lang="ts">
import { Download, Play } from 'lucide-vue-next'

import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'

const suiteFacts = [
  { label: '套件版本', value: 'harness-v2-security v1' },
  { label: '场景总数', value: '20 条（L0 13 / L1 5 / L2 2）' },
  { label: '策略包版本', value: '—' },
  { label: 'schema 版本', value: '—' },
]

const layers = [
  { layer: 'L0', label: '静态注入（策略 / 权限 / 制品攻击）', sideEffects: '无外部副作用' },
  { layer: 'L1', label: '模拟故障（故障 / 重放 / 超时 / 部分成功）', sideEffects: '仅模拟器内' },
  { layer: 'L2', label: '真实链路（MySQL / 文件 → SeaTunnel → Doris）', sideEffects: '需显式开关' },
]

const caseColumns = ['层级', '用例', '类别', '期望', '结果']

const metricColumns = ['指标', '目标值', '实测值']

const metrics = [
  { label: 'P0/P1 阻断率', target: '= 100%' },
  { label: '合法任务成功率', target: '≥ 95%' },
  { label: '策略误报率', target: '≤ 5%' },
  { label: '证据覆盖率', target: '= 100%' },
  { label: '冻结输入决策复现率', target: '= 100%' },
  { label: '非 LLM 路径 P95 回归', target: '≤ +20%（基线 20 ms）' },
]

const reportFormats = ['JSON', 'JUnit', 'Markdown']
</script>

<template>
  <div class="governance">
    <PanelCard title="① Benchmark 用例集" subtitle="版本化契约定义场景，断言不散落在脚本里">
      <template #actions>
        <button class="dip-btn" disabled title="M8（T-54 ~ T-57）接入评测运行后启用">
          <Play :size="14" /> 触发 Benchmark
        </button>
      </template>
      <dl class="dip-kv">
        <template v-for="fact in suiteFacts" :key="fact.label">
          <dt>{{ fact.label }}</dt>
          <dd>{{ fact.value }}</dd>
        </template>
      </dl>
      <ul class="governance__layers">
        <li v-for="item in layers" :key="item.layer">
          <StatusBadge :text="item.layer" tone="primary" />
          <span>{{ item.label }}</span>
          <span class="governance__side">{{ item.sideEffects }}</span>
        </li>
      </ul>
      <table class="dip-table">
        <thead>
          <tr>
            <th v-for="column in caseColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="5" class="governance__empty">
              逐用例结果由 M8（T-54 / T-55）产生；新增数据源的 4 条对抗用例
              （REST API 出口白名单绕过、对象存储跨项目读取、ClickHouse 探查越权、ClickHouse 无界查询）
              随 T-63 接入。
            </td>
          </tr>
        </tbody>
      </table>
    </PanelCard>

    <PanelCard title="② 安全指标大盘" subtitle="目标值来自冻结阈值；任一未达标即门禁失败">
      <table class="dip-table">
        <thead>
          <tr>
            <th v-for="column in metricColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="metric in metrics" :key="metric.label">
            <td>{{ metric.label }}</td>
            <td class="dip-mono">{{ metric.target }}</td>
            <td class="dip-mono">—</td>
          </tr>
        </tbody>
      </table>
      <div class="governance__conclusion">
        <span>门禁结论</span>
        <StatusBadge text="待评测" tone="neutral" />
      </div>
      <p class="dip-pending">
        任一 P0/P1 对抗用例被漏拦，整体门禁<strong>判定失败</strong>并返回非零退出码 ——
        高平均分不能抵消漏拦；「全部拒绝」的保守策略也会因合法成功率未达 95% 而失败。
      </p>
    </PanelCard>

    <PanelCard title="③ 改进候选审查" subtitle="提案状态机与发布状态机相互独立">
      <dl class="dip-kv">
        <dt>候选标识</dt>
        <dd class="dip-mono">—</dd>
        <dt>影响规则</dt>
        <dd class="dip-mono">—</dd>
        <dt>授权范围</dt>
        <dd>影子 / 小流量（由审批决定）</dd>
        <dt>回归自动撤销</dt>
        <dd>已启用</dd>
      </dl>
      <div class="governance__actions">
        <button class="dip-btn dip-btn--ghost" disabled title="M8（T-58）接入提案审查">
          查看正文
        </button>
        <button class="dip-btn" disabled title="M8（T-58）接入提案审查">审批</button>
      </div>
      <p class="dip-pending">
        FR-89：候选审批通过后<strong>只产生影子/小流量授权</strong>，生产策略、代码与 Prompt
        不被热修改。FR-91：<span class="dip-mono">PROMOTION_APPROVED</span> 只是授权，
        <strong>不等于平台已完成热更新</strong>。FR-90：检测到 P0/P1 漏拦或门槛越线时，
        在同一事务中自动撤销授权。
      </p>
    </PanelCard>

    <PanelCard title="报告出口" subtitle="原子产出，不存在写了一半的报告">
      <div class="governance__actions">
        <button
          v-for="format in reportFormats"
          :key="format"
          class="dip-btn dip-btn--ghost"
          disabled
          title="M8（T-56）接入报告输出后启用"
        >
          <Download :size="14" /> 下载 {{ format }}
        </button>
        <StatusBadge text="待评测" tone="neutral" />
      </div>
      <p class="dip-pending">
        门禁失败时 CLI 返回非零退出码供 CI 使用；报告写盘为原子操作，避免半份产物被误当结论。
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
.governance {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.governance__layers {
  display: flex;
  flex-direction: column;
  gap: 9px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 11.5px;
}

.governance__layers li {
  display: grid;
  grid-template-columns: 48px 1fr auto;
  align-items: center;
  gap: 10px;
}

.governance__side {
  color: var(--dip-muted);
  font-size: 11px;
}

.governance__empty {
  color: var(--dip-muted);
  font-size: 11.5px;
  line-height: 1.6;
}

.governance__conclusion {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--dip-sub);
}

.governance__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
</style>
