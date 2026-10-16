<script setup lang="ts">
defineProps<{
  label: string
  value: string
  hint?: string
}>()
</script>

<template>
  <div class="kpi">
    <span class="kpi__label">{{ label }}</span>
    <div class="kpi__row">
      <span class="kpi__value">{{ value }}</span>
      <span v-if="hint" class="kpi__hint">{{ hint }}</span>
    </div>
  </div>
</template>

<style scoped>
.kpi {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  background: var(--dip-card);
  border: 1px solid var(--dip-border);
  border-radius: 10px;
}

.kpi__label {
  font-size: 12px;
  color: var(--dip-sub);
}

.kpi__row {
  display: flex;
  align-items: baseline;
  gap: 8px;
}

.kpi__value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.2;
}

.kpi__hint {
  font-size: 11px;
  font-weight: 600;
  color: var(--dip-muted);
}
</style>
