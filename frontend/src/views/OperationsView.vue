<script setup lang="ts">
import { RotateCcw, Trash2, XCircle } from 'lucide-vue-next'

import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'

const preparationFacts = [
  { label: '输入指纹', value: '—', mono: true },
  { label: 'Artifact SHA256', value: '—', mono: true },
  { label: '风险等级', value: '—', mono: false },
  { label: '有效期至', value: '—', mono: true },
  { label: '预算配额', value: '—', mono: true },
  { label: '影响范围', value: '—', mono: false },
  { label: '回滚方案', value: '—', mono: false },
]

const riskFacts = [
  { label: '错误拒绝率', target: '阈值 50%' },
  { label: '输出放大比', target: '阈值 2.0×' },
  { label: '最大读取行数', target: '上限 1,000 万行' },
  { label: '最大写入字节', target: '上限 10 GiB' },
  { label: '最大执行时长', target: '上限 3600 秒' },
  { label: '最小质量行数', target: '下限 100 行' },
]

const errorCodeColumns = ['错误码', '占比', '违规原因']
const governanceItems = [
  { label: '凭据到期告警', milestone: 'T-15 / T-39' },
  { label: 'Worker 心跳与能力上报', milestone: 'T-39' },
  { label: 'Outbox 积压', milestone: 'T-30 / T-39' },
  { label: '调度失火记录', milestone: 'T-23' },
]
</script>

<template>
  <div class="operations">
    <PanelCard title="① 准备单与审批面板" subtitle="展示冻结事实，而非只有任务名">
      <dl class="dip-kv">
        <template v-for="fact in preparationFacts" :key="fact.label">
          <dt>{{ fact.label }}</dt>
          <dd :class="{ 'dip-mono': fact.mono }">{{ fact.value }}</dd>
        </template>
      </dl>
      <div class="operations__slots">
        <div class="operations__slot">
          <span>数据审批槽</span>
          <StatusBadge text="待分配" tone="neutral" />
        </div>
        <div class="operations__slot">
          <span>安全审批槽</span>
          <StatusBadge text="待分配" tone="neutral" />
        </div>
      </div>
      <div class="operations__actions">
        <button class="dip-btn" disabled title="SOD-01：申请人不可自批">通过</button>
        <button class="dip-btn dip-btn--ghost" disabled title="SOD-01：申请人不可自批">驳回</button>
      </div>
      <p class="dip-pending">
        SOD-01：申请人的审批按钮<strong>置灰并标注原因</strong>；SOD-02：P0 风险操作的
        数据审批槽与安全审批槽必须由不同人担任，因此两个槽位不接受同一账号。
        审批绑定 Artifact 摘要，内容变化即审批失效（SOD-04）。
      </p>
    </PanelCard>

    <div class="operations__row">
      <PanelCard title="② 执行监控" subtitle="指标只取引擎稳定字段；引擎未提供的指标留空，不用 0 冒充">
        <div class="operations__status">
          <StatusBadge text="无运行中的执行" tone="neutral" />
          <span class="dip-mono">—</span>
        </div>
        <ul class="operations__metrics">
          <li v-for="item in riskFacts" :key="item.label">
            <span>{{ item.label }}</span>
            <span class="dip-mono">—</span>
            <span class="operations__target">{{ item.target }}</span>
          </li>
        </ul>
      </PanelCard>

      <PanelCard title="质量分流" subtitle="有效数据写入影子表；错误数据写入错误表并记录违规原因">
        <div class="operations__quality">
          <span>有效行 <span class="dip-mono">—</span></span>
          <span>错误行 <span class="dip-mono">—</span></span>
        </div>
        <table class="dip-table">
          <thead>
            <tr>
              <th v-for="column in errorCodeColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td colspan="3" class="operations__empty">
                错误码分布由 M6（T-41）产生；样本摘要不含原始值。
              </td>
            </tr>
          </tbody>
        </table>
      </PanelCard>
    </div>

    <PanelCard title="③ 失败快照与诊断" subtitle="先脱敏，再按失败分类与失败阶段归类">
      <dl class="dip-kv">
        <dt>失败阶段</dt>
        <dd class="dip-mono">—</dd>
        <dt>失败分类</dt>
        <dd class="dip-mono">—</dd>
        <dt>根因说明</dt>
        <dd>—</dd>
        <dt>修复建议</dt>
        <dd>—</dd>
      </dl>
      <p class="dip-pending">
        诊断输出<strong>不含原始业务数据与凭据</strong>；分类取值见
        CONFIGURATION / CREDENTIALS / CONNECTIVITY / SOURCE_SCHEMA / SINK_WRITE /
        ENGINE / OBSERVABILITY / CANCELLATION / UNKNOWN（M6 T-44）。
      </p>
    </PanelCard>

    <div class="operations__row">
      <PanelCard title="受管处置" subtitle="取消 / 回滚 / 清理均为受管申请，不由页面直接生效">
        <div class="operations__actions">
          <button class="dip-btn dip-btn--ghost" disabled title="M5（T-33）接入受管取消">
            <XCircle :size="14" /> 取消执行
          </button>
          <button class="dip-btn dip-btn--ghost" disabled title="M6（T-45）接入受管回滚">
            <RotateCcw :size="14" /> 发起回滚
          </button>
          <button class="dip-btn dip-btn--ghost" disabled title="M6（T-45）接入受管清理">
            <Trash2 :size="14" /> 发起清理
          </button>
        </div>
        <p class="dip-pending">
          FR-64：取消请求成功<strong>不代表引擎已停</strong>，状态停留在
          <span class="dip-mono">CANCELLING</span> 并持续对账，确认后才进终态。
          FR-74：同一目标表上回滚与清理互斥，不并发破坏。
        </p>
      </PanelCard>

      <PanelCard title="④ 治理区" subtitle="凭据告警 · Worker 心跳 · Outbox 积压">
        <ul class="operations__governance">
          <li v-for="item in governanceItems" :key="item.label">
            <span>{{ item.label }}</span>
            <span class="operations__milestone">{{ item.milestone }}</span>
          </li>
        </ul>
      </PanelCard>
    </div>
  </div>
</template>

<style scoped>
.operations {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.operations__row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  align-items: start;
}

.operations__slots {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.operations__slot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--dip-border);
  border-radius: 8px;
  background: #fbfcfe;
  font-size: 11.5px;
  color: var(--dip-sub);
}

.operations__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.operations__status {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--dip-sub);
}

.operations__metrics {
  display: flex;
  flex-direction: column;
  gap: 9px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
}

.operations__metrics li {
  display: grid;
  grid-template-columns: 1fr 56px 96px;
  align-items: center;
  gap: 10px;
}

.operations__target {
  color: var(--dip-muted);
  font-size: 11px;
  text-align: right;
}

.operations__quality {
  display: flex;
  gap: 18px;
  font-size: 12px;
  color: var(--dip-sub);
}

.operations__empty {
  color: var(--dip-muted);
  font-size: 11.5px;
}

.operations__governance {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 12px;
}

.operations__governance li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.operations__milestone {
  color: var(--dip-muted);
  font-size: 10.5px;
}
</style>
