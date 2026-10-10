<template>
  <div class="cooperation-attention">
    <span class="attention-announcement" role="status" aria-live="polite" aria-atomic="true">
      {{ announcement }}
    </span>
    <a-popover
      v-if="actions.length"
      trigger="click"
      placement="topLeft"
      :after-visible-change="focusList"
      :open="expanded && actions.length > 1"
      @open-change="handleOpenChange"
    >
      <template #content>
        <div
          :id="listId"
          ref="listRef"
          class="attention-list"
          role="group"
          aria-label="协作会话待办"
          @keydown.esc.stop.prevent="closeList"
        >
          <button
            v-for="item in actions"
            :key="item.sessionId"
            type="button"
            class="attention-item"
            :aria-label="`${item.name} ${item.label}，${item.action}`"
            @click="openAction(item)"
          >
            <span class="attention-name" :title="item.name">{{ item.name }}</span>
            <span class="attention-label">{{ item.label }}</span>
            <span class="attention-action">{{ item.action }} <ArrowRight :size="14" /></span>
          </button>
        </div>
      </template>
      <button
        ref="pillRef"
        type="button"
        class="attention-pill"
        :aria-label="announcement"
        :aria-expanded="actions.length > 1 ? expanded : undefined"
        :aria-controls="expanded ? listId : undefined"
        @keydown.esc.stop.prevent="closeList"
        @click="handleClick"
      >
        <CircleHelp class="attention-icon" :size="16" aria-hidden="true" />
        <span class="attention-summary" :title="announcement">{{ summary }}</span>
        <span class="attention-action">{{
          actions.length === 1 ? actions[0].action : '查看待办'
        }}</span>
        <ArrowRight :size="14" aria-hidden="true" />
      </button>
    </a-popover>
    <div v-if="error" class="attention-error">
      <span>协作状态更新失败{{ actions.length ? '，显示最近已知待办' : '' }}</span>
      <button type="button" :title="error" @click="emit('refresh')">重试</button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, useId } from 'vue'
import { ArrowRight, CircleHelp } from '@lucide/vue'

const props = defineProps({
  actions: { type: Array, default: () => [] },
  error: { type: String, default: '' }
})
const emit = defineEmits(['open', 'refresh'])
const expanded = ref(false)
const listId = useId()
const listRef = ref(null)
const pillRef = ref(null)
const summary = computed(() => {
  if (props.actions.length === 1) {
    const item = props.actions[0]
    return `${item.name} ${item.label}`
  }
  const approvals = props.actions.filter((item) => item.label === '待审批').length
  const answers = props.actions.length - approvals
  const counts = []
  if (approvals) counts.push(`${approvals} 待审批`)
  if (answers) counts.push(`${answers} 待回答`)
  return `${props.actions.length} 个协作任务需要你处理 · ${counts.join(' / ')}`
})
const announcement = computed(() => (props.actions.length ? summary.value : '没有待处理的协作请求'))

/** 打开目标会话，保留待办直到持久状态更新。 */
function openAction(item) {
  expanded.value = false
  emit('open', item.sessionId)
}

/** 单项直达，多项由浮层提供明确选择。 */
function handleClick() {
  if (props.actions.length === 1) openAction(props.actions[0])
}

/** 记录浮层显示意图，单项请求始终直接导航。 */
function handleOpenChange(open) {
  expanded.value = open && props.actions.length > 1
}

/** 浮层完成显示与定位后聚焦，避免挂载时仍处于隐藏状态。 */
function focusList(open) {
  if (open && expanded.value) listRef.value?.querySelector('button')?.focus()
}

/** 键盘关闭浮层后返回触发入口。 */
function closeList() {
  expanded.value = false
  pillRef.value?.focus()
}
</script>

<style scoped lang="less">
.cooperation-attention {
  min-width: 0;
  &:has(.attention-pill),
  &:has(.attention-error) {
    margin-bottom: 8px;
  }
}
.attention-announcement {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}
.attention-pill,
.attention-item {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  min-height: 40px;
  padding: 8px 12px;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  &:focus-visible {
    outline: 2px solid var(--main-color);
    outline-offset: 2px;
  }
  > svg {
    flex-shrink: 0;
  }
}
.attention-pill {
  border: 1px solid var(--gray-150);
  border-radius: 999px;
  background: var(--gray-0);
  color: var(--color-text-secondary);
  &:hover {
    border-color: var(--gray-200);
    background: var(--gray-25);
  }
  .attention-action {
    color: var(--color-text);
  }
}
.attention-icon {
  color: var(--color-warning-700);
}
.attention-summary,
.attention-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.attention-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  font-weight: 500;
}
.attention-list {
  width: min(360px, calc(100vw - 56px));
  max-height: 280px;
  overflow-y: auto;
}
.attention-item {
  width: 100%;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--color-text);
  &:hover {
    background: var(--gray-50);
  }
  .attention-name {
    flex: 1;
  }
  .attention-action {
    color: var(--main-color);
  }
}
.attention-label {
  flex-shrink: 0;
  color: var(--color-warning-900);
}
.attention-error {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  color: var(--color-text-secondary);
  font-size: 12px;
  button {
    flex-shrink: 0;
    border: 0;
    background: transparent;
    color: var(--main-color);
    cursor: pointer;
  }
}
</style>
