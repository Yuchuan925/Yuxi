<template>
  <div class="agent-view">
    <ConversationWorkspace
      ref="workspaceRef"
      :single-mode="false"
      :initial-project-id="draftProjectId"
      :is-new-conversation="!threadId"
      @thread-change="handleThreadChange"
    >
      <template #input-actions-left="{ hasActiveThread, isCreatingThread }">
        <ConversationAgentPicker
          :has-active-thread="hasActiveThread"
          :is-creating-thread="isCreatingThread"
          :start-new-conversation="startNewConversation"
        />
      </template>
    </ConversationWorkspace>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ConversationWorkspace from '@/modules/conversation/ui/ConversationWorkspace.vue'
import ConversationAgentPicker from '@/modules/agents/ui/ConversationAgentPicker.vue'
import { useAgentStore } from '@/modules/agents/model/agent'
import { handleChatError } from '@/shared/lib/errorHandler'
import { createThreadRouteCoordinator } from './agent/threadRouteCoordinator'

const route = useRoute()
const router = useRouter()
const agentStore = useAgentStore()
const workspaceRef = ref(null)
const threadId = computed(() =>
  typeof route.params.thread_id === 'string' ? route.params.thread_id : ''
)
const agentId = computed(() =>
  typeof route.query.agent_id === 'string' ? route.query.agent_id : ''
)
const draftProjectId = computed(() => {
  if (threadId.value) return ''
  return typeof route.query.project_id === 'string' ? route.query.project_id : ''
})

const coordinator = createThreadRouteCoordinator({
  initializeAgents: async () => {
    if (!agentStore.isInitialized) await agentStore.initialize()
  },
  selectAgent: (id) => agentStore.selectAgent(id),
  rejectThread: () => router.replace({ name: 'AgentComp' }),
  consumeAgent: () => {
    const query = { ...route.query }
    delete query.agent_id
    return router.replace({ name: 'AgentComp', query })
  },
  onError: (error) => handleChatError(error, 'load')
})

watch(
  [workspaceRef, threadId, agentId],
  ([selection, threadId, agentId]) => {
    void coordinator.sync({ threadId, agentId }, selection)
  },
  { immediate: true }
)
onBeforeUnmount(() => coordinator.dispose())

/** 智能体创建后的新会话准备由页面协调。 */
async function startNewConversation() {
  return workspaceRef.value?.selectThreadFromRoute('')
}

/** 将工作区的新会话身份同步到路由。 */
function handleThreadChange(id) {
  if (coordinator.isSyncing()) return
  const nextThreadId = id || ''
  if (threadId.value === nextThreadId) return
  const location = nextThreadId
    ? { name: 'AgentCompWithThreadId', params: { thread_id: nextThreadId } }
    : { name: 'AgentComp' }
  void router.replace(location)
}
</script>

<style scoped>
.agent-view {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100vh;
  overflow: hidden;
}
</style>
