<script setup lang="ts">
import { Database, FileUp, Plus } from 'lucide-vue-next'

import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'

const listColumns = ['名称', '类型', '角色', '状态', 'Profile', '最近探查', '操作']

const connectorScope = [
  { label: '基线必过（PRD §8.4）', value: 'MySQL / PostgreSQL / Oracle / Doris / ClickHouse' },
  { label: '超集纳入验收（Q6）', value: 'SQL Server / Hive / HDFS' },
  { label: '数据源形态', value: '本地文件 · S3/MinIO 对象存储 · REST API' },
  { label: '文件格式', value: 'CSV / Excel / JSON / Parquet + JSONL / ORC / XML / Markdown / PDF' },
]

const profileColumns = ['字段名', '类型', '可空', '主键', '近似统计', '样本（脱敏）']

const credentialActions = ['迁移 Provider', '轮换（暂存→验证→激活）', '轮换回退', '销毁']
</script>

<template>
  <div class="connections">
    <PanelCard title="连接列表" subtitle="类型 · Source/Sink 角色 · 连通性测试状态 · Profile 状态">
      <template #actions>
        <button class="dip-btn" disabled title="M2（T-10 / T-14）接入连接登记后启用">
          <Plus :size="14" /> 新建连接
        </button>
        <button class="dip-btn dip-btn--ghost" disabled title="M2（T-16）接入受管文件资产后启用">
          <FileUp :size="14" /> 上传文件
        </button>
      </template>
      <table class="dip-table">
        <thead>
          <tr>
            <th v-for="column in listColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="7" class="connections__empty">
              <Database :size="14" /> 尚未登记连接。连接与 Profile 探查由 M2（T-10 ~ T-17）提供。
            </td>
          </tr>
        </tbody>
      </table>
    </PanelCard>

    <div class="connections__row">
      <PanelCard title="连接详情" subtitle="凭据只以 Secret 引用形式展示，不回显明文">
        <dl class="dip-kv">
          <dt>名称</dt>
          <dd class="dip-mono">—</dd>
          <dt>类型 / 角色</dt>
          <dd class="dip-mono">—</dd>
          <dt>主机端口</dt>
          <dd class="dip-mono">—</dd>
          <dt>数据库</dt>
          <dd class="dip-mono">—</dd>
          <dt>Secret 引用</dt>
          <dd class="dip-mono">vault://&lt;引用标识&gt;</dd>
        </dl>
        <p class="dip-pending">
          G-07：凭据永不进入模型上下文、API 响应与普通日志；本页<strong>不提供明文查看入口</strong>。
          连通性测试与元数据探查是两个独立动作，测试过程也不回显秘密。
        </p>
      </PanelCard>

      <PanelCard title="数据源与格式范围" subtitle="基线清单是下限而非上限，超集同样纳入验收">
        <dl class="dip-kv">
          <template v-for="item in connectorScope" :key="item.label">
            <dt>{{ item.label }}</dt>
            <dd>{{ item.value }}</dd>
          </template>
        </dl>
      </PanelCard>
    </div>

    <PanelCard title="Profile 视图" subtitle="受管元数据事实，有时效：过期或非就绪的 Profile 不可用于生成">
      <table class="dip-table">
        <thead>
          <tr>
            <th v-for="column in profileColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colspan="6" class="connections__empty">
              尚未发起元数据探查。探查只执行受限期查询，不做无界全表 COUNT，
              也不允许模型生成探查 SQL。
            </td>
          </tr>
        </tbody>
      </table>
      <p class="dip-demo">
        脱敏样本示例：邮箱 <span class="dip-mono">a***@ex***.com</span> ·
        手机 <span class="dip-mono">138****1234</span> ·
        姓名 <span class="dip-mono">张*</span>
      </p>
      <p class="dip-pending">样本脱敏后入库，<strong>原文不落盘</strong>，界面与 API 均不提供原文查看入口。</p>
    </PanelCard>

    <PanelCard title="凭据生命周期" subtitle="全部为受管申请，不在本页直接生效">
      <div class="connections__actions">
        <button
          v-for="action in credentialActions"
          :key="action"
          class="dip-btn dip-btn--ghost"
          disabled
          title="凭据迁移/轮换/回退/销毁由 M2（T-15）提供，且必须走申请—审批—提交三阶段"
        >
          {{ action }}
        </button>
        <StatusBadge text="受管副作用" tone="purple" />
      </div>
      <p class="dip-pending">
        FR-23：凭据迁移、轮换、回退、销毁均属受管副作用，不存在绕过 Harness 三阶段协议的
        直接执行路径；每次操作都会产生对应的准备单、审批与能力记录。
      </p>
    </PanelCard>
  </div>
</template>

<style scoped>
.connections {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.connections__row {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  align-items: start;
}

.connections__empty {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 14px;
  color: var(--dip-muted);
  font-size: 11.5px;
}

.connections__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.dip-demo {
  font-size: 11.5px;
  color: var(--dip-sub);
}
</style>
