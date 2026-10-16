<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
}>()
</script>

<template>
  <section class="panel">
    <header class="panel__head">
      <div class="panel__headings">
        <h2 class="panel__title">{{ title }}</h2>
        <p v-if="subtitle" class="panel__subtitle">{{ subtitle }}</p>
      </div>
      <div class="panel__actions">
        <slot name="actions" />
      </div>
    </header>
    <slot />
  </section>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: var(--dip-card);
  border: 1px solid var(--dip-border);
  border-radius: 10px;
}

.panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.panel__headings {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.panel__title {
  font-size: 14px;
  font-weight: 700;
  line-height: 1.3;
}

.panel__subtitle {
  font-size: 11.5px;
  color: var(--dip-muted);
  line-height: 1.4;
}

.panel__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
</style>
