<script setup lang="ts">
type Tone = 'neutral' | 'primary' | 'success' | 'warning' | 'danger' | 'purple' | 'teal'

withDefaults(defineProps<{ text: string; tone?: Tone; title?: string }>(), {
  tone: 'neutral',
})
</script>

<template>
  <span class="badge" :class="`badge--${tone}`" :title="title">{{ text }}</span>
</template>

<style scoped>
.badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  line-height: 1.2;
  white-space: nowrap;
}

.badge--neutral {
  background: #f1f5f9;
  color: var(--dip-sub);
}

.badge--primary {
  background: var(--dip-primary-soft);
  color: var(--dip-primary);
}

.badge--success {
  background: var(--dip-green-soft);
  color: var(--dip-green);
}

.badge--warning {
  background: var(--dip-amber-soft);
  color: var(--dip-amber);
}

.badge--danger {
  background: var(--dip-red-soft);
  color: var(--dip-red);
}

.badge--purple {
  background: var(--dip-purple-soft);
  color: var(--dip-purple);
}

.badge--teal {
  background: var(--dip-teal-soft);
  color: var(--dip-teal);
}
</style>
