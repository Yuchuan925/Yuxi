<template>
  <div class="thread-message-list">
    <template
      v-for="(group, groupIndex) in messageGroups"
      :key="group.displayKey || group.run?.run_id || `group-${groupIndex}`"
    >
      <template
        v-for="(displayItem, itemIndex) in displayItemsList[groupIndex]"
        :key="displayItem.key"
      >
        <AgentMessageComponent
          v-if="displayItem.type === 'message'"
          :message="displayItem.message"
          :is-processing="isDisplayMessageProcessing(group, displayItem)"
          :show-refs="false"
          :hide-tool-calls="true"
          :mention="{}"
        />
        <ToolCallsGroupComponent
          v-else
          :tool-calls="displayItem.toolCalls"
          :entries="displayItem.entries"
          :is-active="isToolGroupActive(group, itemIndex, displayItemsList[groupIndex])"
        />
      </template>
      <div v-if="!group.messages.length && group.run" class="thread-message-list-empty">
        {{ formatEmptyRunStatus(group.run.status) }}
      </div>
    </template>
    <div v-if="messageGroups.length === 0" class="thread-message-list-empty">暂无消息</div>
  </div>
</template>

<script setup>
import {
  formatEmptyRunStatus,
  groupRunContinuations
} from '@/modules/session/model/runProcessGrouping'
import { computed } from 'vue'
import AgentMessageComponent from '@/modules/session/ui/AgentMessageComponent.vue'
import ToolCallsGroupComponent from '@/modules/session/ui/ToolCallsGroupComponent.vue'
import { MessageProcessor } from '@/modules/session/model/messageProcessor'
import { getMessageGroupDisplayItems } from '@/modules/session/model/messageGrouping'

const props = defineProps({
  runs: { type: Array, default: () => [] },
  messages: {
    type: Array,
    default: () => []
  },
  ongoingMessages: {
    type: Array,
    default: () => []
  },
  isProcessing: {
    type: Boolean,
    default: false
  },
  enrichToolCalls: {
    type: Function,
    default: null
  }
})

const historyRunGroups = computed(() =>
  MessageProcessor.convertServerHistoryToMessages(props.messages, props.runs)
)

const runGroups = computed(() => {
  if (!props.ongoingMessages.length) return historyRunGroups.value
  const liveRunId = props.ongoingMessages.find((message) => message.run_id)?.run_id
  const liveGroup = { messages: props.ongoingMessages, status: 'streaming' }
  if (liveRunId && historyRunGroups.value.some((group) => group.run?.run_id === liveRunId)) {
    return historyRunGroups.value.map((group) =>
      group.run?.run_id === liveRunId ? { ...group, ...liveGroup } : group
    )
  }
  return [...historyRunGroups.value, liveGroup]
})

const messageGroups = computed(() => groupRunContinuations(runGroups.value))

const displayItemsList = computed(() =>
  messageGroups.value.map((group) =>
    getMessageGroupDisplayItems(
      group,
      props.enrichToolCalls ? { enrichToolCalls: props.enrichToolCalls } : {}
    )
  )
)

const isDisplayMessageProcessing = (group, displayItem) =>
  Boolean(
    props.isProcessing &&
    displayItem?.type === 'message' &&
    group?.status === 'streaming' &&
    displayItem.sourceIndex === group.messages.length - 1
  )

const isToolGroupActive = (group, itemIndex, displayItems) =>
  Boolean(
    props.isProcessing && group?.status === 'streaming' && itemIndex === displayItems.length - 1
  )
</script>

<style lang="less" scoped>
.thread-message-list {
  display: flex;
  flex-direction: column;
}

.thread-message-list-empty {
  padding: 24px 0;
  text-align: center;
  color: var(--gray-500);
  font-size: 13px;
}
</style>
