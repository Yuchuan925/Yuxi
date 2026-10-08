<template>
  <BaseToolCall
    v-for="(target, index) in targets"
    :key="target.sessionId || index"
    :tool-call="toolCall"
    :appearance="appearance"
    :header-action="() => target.sessionId ? cooperationView.openSession(target.sessionId) : cooperationView.openTree()"
    hide-params
  >
    <template #header>
      <div class="cooperation-tool-heading">
        <span class="cooperation-tool-action">{{ action }}</span>
        <FallbackAvatar
          v-if="target.sessionId"
          :name="target.name"
          :seed="target.sessionId"
          kind="agent"
          :size="18"
          shape="rounded"
          decorative
        />
        <span v-if="target.name" class="cooperation-tool-name" :title="target.name">{{ target.name }}</span>
        <span v-if="toolCall.error_message" class="cooperation-tool-error">{{ toolCall.error_message }}</span>
      </div>
    </template>
  </BaseToolCall>
</template>

<script setup>
import { computed } from 'vue'
import FallbackAvatar from '@/shared/ui/FallbackAvatar.vue'
import BaseToolCall from '../BaseToolCall.vue'
import { getToolCallId } from '../toolRegistry'
import { COOPERATION_ACTIONS, getCooperationToolTargets } from '@/modules/session/model/cooperationToolView'

const props = defineProps({
  toolCall: { type: Object, required: true },
  appearance: { type: String, default: 'timeline' },
  defaultExpanded: { type: Boolean, default: false },
  cooperationView: { type: Object, required: true }
})
const action = computed(() => COOPERATION_ACTIONS[getToolCallId(props.toolCall)])
const targets = computed(() => {
  const items = getCooperationToolTargets(props.toolCall, props.cooperationView)
  return items.length ? items : [{ name: '' }]
})
</script>

<style scoped lang="less">
.cooperation-tool-heading {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.cooperation-tool-action {
  flex-shrink: 0;
}
.cooperation-tool-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cooperation-tool-error {
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
