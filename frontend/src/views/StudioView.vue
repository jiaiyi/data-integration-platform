<script setup lang="ts">
import { GitCompareArrows, Sparkles } from 'lucide-vue-next'

import PanelCard from '../components/PanelCard.vue'
import StatusBadge from '../components/StatusBadge.vue'

const gates = [
  { label: '连接器合法性', milestone: 'T-20' },
  { label: 'DAG 结构', milestone: 'T-20' },
  { label: '字段与类型对齐', milestone: 'T-20' },
  { label: 'SQL 分流', milestone: 'T-20' },
  { label: '质量错误码可追溯', milestone: 'T-20' },
  { label: 'Secret 占位符', milestone: 'T-20' },
  { label: 'Doris 投影', milestone: 'T-20' },
  { label: '真实引擎检查', milestone: 'T-21' },
]

const reviewBlocks = [
  { title: 'EtlPlan（结构化）', detail: '源/目标映射 · 转换步骤 · 质量规则与错误码 · 增量水位列 · 运行预算' },
  { title: 'SeaTunnel HOCON', detail: '语法高亮渲染；配置中只保存 Secret 引用与占位符，不含明文' },
  { title: 'DAG 拓扑图', detail: 'Source → Filter → Mask → Join → Sink' },
  { title: '字段映射 Diff / 脱敏前后结构对比', detail: '同屏可比，供审批人核对将要执行的内容' },
]
</script>

<template>
  <div class="studio">
    <div class="studio__top">
      <PanelCard title="① 需求输入与澄清" subtitle="缺参数不得自动填充，Agent 必须中断提问">
        <template #actions>
          <button class="dip-btn" disabled title="M3（T-18 / T-19）接入生成链路后启用">
            <Sparkles :size="14" /> 生成方案
          </button>
        </template>
        <dl class="dip-kv studio__form">
          <dt>源端</dt>
          <dd><select class="dip-select" disabled><option>M2 就绪的 Profile 列表</option></select></dd>
          <dt>目标端</dt>
          <dd><select class="dip-select" disabled><option>M2 就绪的 Profile 列表</option></select></dd>
          <dt>需求</dt>
          <dd>
            <textarea
              class="studio__textarea"
              rows="4"
              disabled
              placeholder="例如：把订单和用户同步到宽表，金额必须为正，邮箱脱敏，错误数据单独保存，每天凌晨执行。"
            ></textarea>
          </dd>
        </dl>
      </PanelCard>

      <PanelCard title="澄清对话（LangGraph）" subtitle="从中断处恢复，不从零重跑">
        <div class="dip-pending">
          必要参数缺失时 AgentRun 进入 <span class="dip-mono">WAITING_INPUT</span> 并返回澄清问题，
          补充信息后从同一 <span class="dip-mono">thread_id</span> 恢复（FR-26 / FR-27）。
        </div>
        <div class="studio__repair">
          <span>修复轮次</span>
          <span class="dip-mono">— / 3</span>
          <StatusBadge text="候选总量上限 3 份" tone="primary" />
        </div>
        <p class="dip-pending">
          候选完整性失败、确定性门禁失败、真实引擎检查失败<strong>三类失败共享同一计数</strong>，
          不存在各自重试的预算空间；耗尽后安全终止，不降级放行。
        </p>
      </PanelCard>
    </div>

    <PanelCard title="② 方案配置与拓扑审查" subtitle="审批人看到的就是将要执行的内容">
      <div class="studio__review">
        <article v-for="block in reviewBlocks" :key="block.title" class="studio__block">
          <h3>{{ block.title }}</h3>
          <p>{{ block.detail }}</p>
          <div class="studio__canvas">由 M3（T-19 ~ T-22）填充</div>
        </article>
      </div>
    </PanelCard>

    <PanelCard title="③ 门禁校验与审批提交" subtitle="逐项展示结论；失败项给出修复建议">
      <ul class="studio__gates">
        <li v-for="gate in gates" :key="gate.label">
          <StatusBadge text="待校验" tone="neutral" />
          <span>{{ gate.label }}</span>
          <span class="studio__milestone">{{ gate.milestone }}</span>
        </li>
      </ul>
      <div class="studio__summary">
        <StatusBadge text="DRAFT" tone="neutral" />
        <span>版本摘要 SHA256</span>
        <span class="dip-mono">—</span>
        <button class="dip-btn" disabled title="未通过门禁时禁用；冻结需先完成真实引擎检查（FR-35）">
          冻结并提交审批
        </button>
      </div>
      <p class="dip-pending">
        FR-35：静态门禁通过后仍须经真实 SeaTunnel 检查才能冻结；引擎不可用时冻结被阻止并明确报错，
        <strong>不允许跳过引擎检查直接冻结</strong>。
      </p>
    </PanelCard>

    <PanelCard title="④ 调度配置" subtitle="触发时刻与水位窗口随版本一并冻结">
      <div class="studio__schedule">
        <label>模式 <select class="dip-select" disabled><option>BATCH / WATERMARK_INCREMENTAL</option></select></label>
        <label>触发 <input class="dip-select dip-mono" disabled value="—" /></label>
        <label>失火策略 <select class="dip-select" disabled><option>SKIP / FIRE_ONCE</option></select></label>
      </div>
      <p class="dip-pending">
        版本约束：修改需求将创建 <strong>V+1</strong>，不覆盖本版本，也不提供覆盖旧版本的操作（FR-39）。
      </p>
    </PanelCard>

    <div class="studio__footer">
      <span class="studio__footer-label">当前版本</span>
      <StatusBadge text="—" tone="neutral" title="版本号由 M3（T-22）冻结后产生" />
      <button class="dip-btn dip-btn--ghost" disabled title="M7（T-51）接入跨版本 Diff">
        <GitCompareArrows :size="14" /> 对比版本
      </button>
    </div>
  </div>
</template>

<style scoped>
.studio {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.studio__top {
  display: grid;
  grid-template-columns: 1.25fr 1fr;
  gap: 14px;
  align-items: start;
}

.studio__form {
  grid-template-columns: 72px 1fr;
}

.studio__textarea {
  width: 100%;
  padding: 10px;
  border: 1px solid var(--dip-border);
  border-radius: 8px;
  background: #f8fafc;
  color: var(--dip-sub);
  font-size: 12px;
  resize: vertical;
}

.studio__repair {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--dip-sub);
}

.studio__review {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.studio__block {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border: 1px solid var(--dip-border);
  border-radius: 8px;
}

.studio__block h3 {
  font-size: 12.5px;
  font-weight: 700;
}

.studio__block p {
  font-size: 11px;
  color: var(--dip-muted);
  line-height: 1.5;
}

.studio__canvas {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 84px;
  border: 1px dashed var(--dip-border-strong);
  border-radius: 6px;
  background: #fbfcfe;
  color: var(--dip-muted);
  font-size: 11px;
}

.studio__gates {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 11.5px;
}

.studio__gates li {
  display: flex;
  align-items: center;
  gap: 8px;
}

.studio__milestone {
  color: var(--dip-muted);
  font-size: 10.5px;
}

.studio__summary {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11.5px;
  color: var(--dip-sub);
}

.studio__schedule {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 11.5px;
  color: var(--dip-sub);
}

.studio__schedule label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.studio__footer {
  display: flex;
  align-items: center;
  gap: 10px;
}

.studio__footer-label {
  font-size: 11.5px;
  color: var(--dip-sub);
}
</style>
