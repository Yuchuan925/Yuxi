<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import {
  Plus,
  RefreshCw,
  Trash2,
  Edit3,
  Bot,
  Microscope,
  MoreVertical,
  Upload,
  Info,
  SlidersHorizontal,
  Wrench,
  MoreHorizontal
} from 'lucide-vue-next'

import { agentApi } from '@/apis/agent_api'
import { userApi } from '@/apis/user_api'
import AgentRuntimeConfigForm from '@/components/AgentRuntimeConfigForm.vue'
import ShareConfigForm from '@/components/ShareConfigForm.vue'
import { isBuiltinAgent, useAgentStore } from '@/stores/agent'
import { useUserStore } from '@/stores/user'
import PageShoulder from '@/components/shared/PageShoulder.vue'
import InfoCard from '@/components/shared/InfoCard.vue'
import ExtensionCardGrid from '@/components/extensions/ExtensionCardGrid.vue'
import defaultAgentIcon from '@/assets/defaults/agent.png'
import { MAX_IMAGE_UPLOAD_SIZE_BYTES, MAX_IMAGE_UPLOAD_SIZE_MB } from '@/utils/upload_limits'

const userStore = useUserStore()
const agentStore = useAgentStore()
const agentLoading = ref(false)
const saving = ref(false)
const searchQuery = ref('')

const agentBackendOptions = ref([])

const showAgentModal = ref(false)
const editingAgentId = ref(null)
const agentModalActiveTab = ref('basic')
const agentIconUploading = ref(false)
const agentShareConfigFormRef = ref(null)
const runtimeConfigFormRef = ref(null)
const agentShareConfig = ref({ access_level: 'user', department_ids: [], user_uids: [] })
const agentForm = reactive({
  slug: '',
  name: '',
  backend_id: 'ChatbotAgent',
  description: '',
  icon: ''
})

const runtimeAgentModalTabs = ['model', 'tools', 'other']
const agentModalMenuItems = computed(() => {
  const items = [{ key: 'basic', label: '基本信息', icon: Info }]
  if (editingAgentId.value) {
    items.push(
      { key: 'model', label: '模型配置', icon: SlidersHorizontal },
      { key: 'tools', label: '工具配置', icon: Wrench },
      { key: 'other', label: '其他配置', icon: MoreHorizontal }
    )
  }
  return items
})
const runtimeConfigSegment = computed(() =>
  runtimeAgentModalTabs.includes(agentModalActiveTab.value) ? agentModalActiveTab.value : 'model'
)
const isRuntimeAgentModalTab = (key) => runtimeAgentModalTabs.includes(key)

const filteredAgents = computed(() => {
  const keyword = searchQuery.value.trim().toLowerCase()
  const list = agentStore.agents || []
  const filtered = keyword
    ? list.filter(
        (agent) =>
          String(agent.name || '').toLowerCase().includes(keyword) ||
          String(agent.id || '').toLowerCase().includes(keyword) ||
          String(agent.backend_id || '').toLowerCase().includes(keyword)
      )
    : list
  return [...filtered].sort((a, b) => {
    if (isBuiltinAgent(a) !== isBuiltinAgent(b)) return isBuiltinAgent(a) ? -1 : 1
    return String(a.name || a.id).localeCompare(String(b.name || b.id), 'zh-CN')
  })
})

const agentStats = computed(() => ({
  total: agentStore.agents.length,
  builtin: agentStore.agents.filter(isBuiltinAgent).length,
  manageable: agentStore.agents.filter((agent) => agent.can_manage).length,
  global: agentStore.agents.filter((agent) => agent.share_config?.access_level === 'global').length
}))
const getDefaultBackendId = () => agentBackendOptions.value[0]?.value || 'ChatbotAgent'

const getInitialShareConfig = () => ({
  access_level: userStore.isAdmin ? 'global' : 'user',
  department_ids: [],
  user_uids: userStore.uid ? [userStore.uid] : []
})

const normalizeShareConfigForPayload = () => {
  if (isBuiltinAgent({ id: editingAgentId.value })) {
    return { access_level: 'global', department_ids: [], user_uids: [] }
  }
  const config = agentShareConfig.value || getInitialShareConfig()
  const accessLevel = userStore.isAdmin ? config.access_level : 'user'
  return {
    access_level: accessLevel,
    department_ids: accessLevel === 'department' ? config.department_ids || [] : [],
    user_uids: accessLevel === 'user' ? config.user_uids || [] : []
  }
}

const isEditingBuiltinAgent = computed(() => isBuiltinAgent({ id: editingAgentId.value }))
const canEditAgentShareConfig = computed(() => !isEditingBuiltinAgent.value)
const getAgentShareAllowedLevels = () => {
  if (isEditingBuiltinAgent.value) return ['global']
  return userStore.isAdmin ? ['global', 'department', 'user'] : ['user']
}

const canManageAgent = (agent) => !!agent?.can_manage
const agentModalTitle = computed(() => (editingAgentId.value ? '编辑智能体' : '新增智能体'))
const getAgentIconSrc = (agent) => agent?.icon || defaultAgentIcon
const agentPreviewIcon = computed(() => agentForm.icon?.trim() || defaultAgentIcon)
const selectedBackendOption = computed(() =>
  agentBackendOptions.value.find((backend) => backend.value === agentForm.backend_id)
)
const selectedBackendLabel = computed(() => selectedBackendOption.value?.label || agentForm.backend_id || '未选择')
const selectedBackendIcon = computed(() => {
  const backendText = `${agentForm.backend_id} ${selectedBackendLabel.value}`.toLowerCase()
  return backendText.includes('deep') || backendText.includes('search') ? Microscope : Bot
})

// ============ Agent Operations ============
const loadAgentBackends = async () => {
  try {
    const response = await agentApi.getAgentBackends()
    agentBackendOptions.value = (response.backends || []).map((backend) => ({
      label: backend.name || backend.backend_id,
      value: backend.backend_id
    }))
  } catch (error) {
    message.error(error.message || '加载智能体后端失败')
  }
}

const loadAgents = async () => {
  agentLoading.value = true
  try {
    await agentStore.fetchAgents()
  } catch (error) {
    message.error(error.message || '加载智能体失败')
  } finally {
    agentLoading.value = false
  }
}

const resetAgentForm = () => {
  Object.assign(agentForm, {
    slug: '',
    name: '',
    backend_id: getDefaultBackendId(),
    description: '',
    icon: ''
  })
  agentShareConfig.value = getInitialShareConfig()
}

const openCreateAgentModal = () => {
  editingAgentId.value = null
  agentModalActiveTab.value = 'basic'
  resetAgentForm()
  agentStore.resetAgentConfig()
  showAgentModal.value = true
}

const openEditAgentModal = async (agent) => {
  if (!canManageAgent(agent)) return
  const detail = await agentStore.fetchAgentDetail(agent.id, true)
  editingAgentId.value = detail.id
  agentModalActiveTab.value = 'basic'
  Object.assign(agentForm, {
    slug: detail.id || detail.slug || '',
    name: detail.name || '',
    backend_id: detail.backend_id || 'ChatbotAgent',
    description: detail.description || '',
    icon: detail.icon || ''
  })
  agentShareConfig.value = isBuiltinAgent(detail)
    ? { access_level: 'global', department_ids: [], user_uids: [] }
    : detail.share_config || getInitialShareConfig()
  await agentStore.selectAgent(detail.id)
  showAgentModal.value = true
}

const closeAgentModal = () => {
  if (saving.value || agentIconUploading.value) return
  showAgentModal.value = false
}

const beforeAgentIconUpload = (file) => {
  if (!file.type.startsWith('image/')) {
    message.error('只能上传图片文件')
    return false
  }

  if (file.size > MAX_IMAGE_UPLOAD_SIZE_BYTES) {
    message.error(`图片大小不能超过 ${MAX_IMAGE_UPLOAD_SIZE_MB}MB`)
    return false
  }

  uploadAgentIcon(file)
  return false
}

const uploadAgentIcon = async (file) => {
  agentIconUploading.value = true
  try {
    const data = await userApi.uploadImage(file)
    agentForm.icon = data.image_url || data.url || ''
    message.success('图标上传成功')
  } catch (error) {
    message.error(error.message || '图标上传失败')
  } finally {
    agentIconUploading.value = false
  }
}

const buildAgentPayload = () => {
  const payload = {
    name: agentForm.name.trim(),
    description: agentForm.description.trim() || null,
    icon: agentForm.icon.trim() || null,
    share_config: normalizeShareConfigForPayload()
  }

  if (!editingAgentId.value) {
    payload.slug = agentForm.slug.trim() || undefined
    payload.backend_id = agentForm.backend_id
  }

  return payload
}

const saveAgent = async () => {
  if (!agentForm.name.trim()) {
    agentModalActiveTab.value = 'basic'
    message.error('请填写智能体名称')
    return
  }

  const validation = canEditAgentShareConfig.value ? agentShareConfigFormRef.value?.validate?.() : null
  if (validation && !validation.valid) {
    agentModalActiveTab.value = 'basic'
    message.error(validation.message)
    return
  }

  saving.value = true
  try {
    const payload = buildAgentPayload()
    if (editingAgentId.value) {
      const validatedConfig = runtimeConfigFormRef.value?.validateAndFilterConfig?.()
      if (validatedConfig && JSON.stringify(validatedConfig) !== JSON.stringify(agentStore.agentConfig)) {
        agentStore.updateAgentConfig(validatedConfig)
      }
      if (agentStore.hasConfigChanges) {
        payload.config_json = { context: agentStore.agentConfig }
      }
      await agentStore.updateAgentProfile(editingAgentId.value, payload)
      agentStore.originalAgentConfig = { ...agentStore.agentConfig }
      message.success('智能体已保存')
    } else {
      await agentStore.createAgent(payload)
      message.success('智能体已创建')
    }
    showAgentModal.value = false
  } catch (error) {
    message.error(error.message || '保存智能体失败')
  } finally {
    saving.value = false
  }
}

const deleteAgent = async (agent) => {
  if (isBuiltinAgent(agent)) {
    message.warning('内置智能体不能删除')
    return
  }
  Modal.confirm({
    title: `删除 ${agent.name}`,
    content: '删除后不可恢复，已绑定该智能体的历史对话仍保留原始绑定信息。',
    okText: '删除',
    okType: 'danger',
    cancelText: '取消',
    async onOk() {
      try {
        await agentStore.deleteAgent(agent.id)
        message.success('智能体已删除')
      } catch (error) {
        message.error(error.message || '删除智能体失败')
      }
    }
  })
}

onMounted(async () => {
  await Promise.all([loadAgentBackends(), loadAgents()])
})

defineExpose({
  loading: agentLoading,
  stats: agentStats,
  refresh: loadAgents
})
</script>

<template>
  <div class="agent-manage-panel">
        <PageShoulder v-model:search="searchQuery" search-placeholder="搜索智能体...">
          <template #actions>
            <a-button type="primary" class="lucide-icon-btn" @click="openCreateAgentModal">
              <Plus :size="14" />
              新增智能体
            </a-button>
            <a-button class="lucide-icon-btn" @click="loadAgents" :loading="agentLoading">
              <RefreshCw :size="14" :class="{ spinning: agentLoading }" />
            </a-button>
          </template>
        </PageShoulder>

        <ExtensionCardGrid :min-width="320">
          <InfoCard
            v-for="agent in filteredAgents"
            :key="agent.id"
            :title="agent.name"
            :subtitle="agent.slug || agent.id"
            :description="agent.description || '暂无描述'"
            :default-icon="Bot"
            :tags="agent.backend_id ? [{ name: agent.backend_id, color: 'blue' }] : []"
            class="config-card agent-card"
            @click="canManageAgent(agent) && openEditAgentModal(agent)"
          >
            <template #icon>
              <img
                class="agent-card-icon-image"
                :src="getAgentIconSrc(agent)"
                :alt="`${agent.name || '智能体'}图标`"
              />
            </template>

            <template #status>
              <a-dropdown v-if="canManageAgent(agent)" :trigger="['click']" placement="bottomRight">
                <template #overlay>
                  <a-menu>
                    <a-menu-item key="edit" @click.stop="openEditAgentModal(agent)">
                      <span class="agent-card-menu-item">
                        <Edit3 :size="14" />
                        编辑智能体
                      </span>
                    </a-menu-item>
                    <a-menu-item
                      key="delete"
                      :disabled="isBuiltinAgent(agent)"
                      @click.stop="deleteAgent(agent)"
                    >
                      <span class="agent-card-menu-item" :class="{ danger: !isBuiltinAgent(agent) }">
                        <Trash2 :size="14" />
                        删除智能体
                      </span>
                    </a-menu-item>
                  </a-menu>
                </template>
                <a-button
                  type="text"
                  size="small"
                  class="agent-card-menu-trigger"
                  aria-label="智能体操作"
                  @click.stop
                >
                  <MoreVertical :size="16" />
                </a-button>
              </a-dropdown>
            </template>
          </InfoCard>
        </ExtensionCardGrid>

    <!-- Agent Edit Modal -->
    <a-modal
      v-model:open="showAgentModal"
      class="agent-edit-modal"
      :width="920"
      :footer="null"
      :closable="false"
      @cancel="closeAgentModal"
    >
      <template #title>
        <div class="agent-modal-titlebar">
          <span class="agent-modal-title">{{ agentModalTitle }}</span>
          <div class="agent-modal-actions">
            <a-button :disabled="saving" @click="closeAgentModal">取消</a-button>
            <a-button type="primary" :loading="saving" @click="saveAgent">保存</a-button>
          </div>
        </div>
      </template>
      <div class="agent-modal-content">
        <aside class="agent-modal-sidebar" aria-label="智能体配置分组">
          <button
            v-for="item in agentModalMenuItems"
            :key="item.key"
            type="button"
            class="agent-modal-nav-item"
            :class="{ active: agentModalActiveTab === item.key }"
            @click="agentModalActiveTab = item.key"
          >
            <span class="nav-item-main">
              <component :is="item.icon" :size="16" />
              <span>{{ item.label }}</span>
            </span>
            <span v-if="item.key === 'model' && agentStore.hasConfigChanges" class="nav-dirty-dot" />
          </button>
        </aside>

        <div class="agent-modal-main">
          <section v-show="agentModalActiveTab === 'basic'" class="agent-modal-section">
            <div class="agent-profile-header">
              <div class="agent-icon-preview" aria-label="智能体图标、名称与后端">
                <div class="agent-profile-main">
                  <a-upload
                    :show-upload-list="false"
                    :before-upload="beforeAgentIconUpload"
                    :disabled="agentIconUploading"
                    accept="image/*"
                  >
                    <div class="agent-icon-upload" :class="{ uploading: agentIconUploading }">
                      <img :src="agentPreviewIcon" :alt="`${agentForm.name || '智能体'}图标`" />
                      <div class="agent-icon-mask">
                        <RefreshCw v-if="agentIconUploading" :size="14" class="spinning" />
                        <Upload v-else :size="14" />
                        <span>{{ agentForm.icon ? '更换' : '上传' }}</span>
                      </div>
                    </div>
                  </a-upload>
                  <div class="agent-icon-preview-text">
                    <input
                      v-model="agentForm.name"
                      class="agent-inline-name-input"
                      type="text"
                      placeholder="点击输入智能体名称"
                      aria-label="智能体名称"
                    />
                    <input
                      v-if="!editingAgentId"
                      v-model="agentForm.slug"
                      class="agent-inline-slug-input"
                      type="text"
                      placeholder="标识可选，留空自动生成"
                      aria-label="智能体标识"
                    />
                    <span v-else class="agent-inline-slug">{{ agentForm.slug || editingAgentId }}</span>
                  </div>
                </div>
                <div class="agent-backend-summary" aria-label="智能体后端">
                  <component :is="selectedBackendIcon" :size="16" />
                  <div class="agent-backend-text">
                    <span class="agent-backend-label">后端</span>
                    <a-select
                      v-if="!editingAgentId"
                      v-model:value="agentForm.backend_id"
                      class="agent-backend-select"
                      size="small"
                      :options="agentBackendOptions"
                    />
                    <span v-else class="agent-backend-name">{{ selectedBackendLabel }}</span>
                  </div>
                </div>
              </div>
            </div>
            <div class="modal-form">
              <label class="form-label full-width">
                <span>描述</span>
                <a-textarea v-model:value="agentForm.description" :rows="3" placeholder="可选" />
              </label>
            </div>

            <div v-if="canEditAgentShareConfig" class="share-config-block">
              <div class="section-heading">
                <span>共享权限</span>
              </div>
              <ShareConfigForm
                ref="agentShareConfigFormRef"
                v-model="agentShareConfig"
                :auto-select-user-dept="true"
                :allowed-access-levels="getAgentShareAllowedLevels()"
              />
            </div>
          </section>

          <section
            v-if="editingAgentId"
            v-show="isRuntimeAgentModalTab(agentModalActiveTab)"
            class="agent-modal-section runtime-section"
          >
            <div v-if="agentStore.hasConfigChanges" class="runtime-dirty-row">
              <span class="dirty-hint">有未保存修改</span>
            </div>
            <AgentRuntimeConfigForm
              ref="runtimeConfigFormRef"
              :segment="runtimeConfigSegment"
              :show-segmented="false"
            />
          </section>
        </div>
      </div>
    </a-modal>
  </div>
</template>


<style lang="less" scoped>
.agent-manage-panel {
  height: 100%;
  min-height: 0;
}

.agent-card {
  :deep(.info-card-icon) {
    border-color: var(--main-100);
    background:
      radial-gradient(circle at 35% 25%, rgba(255, 255, 255, 0.9), transparent 38%),
      linear-gradient(145deg, var(--main-10), var(--gray-0));
  }
}

.agent-card-icon-image {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.agent-card-menu-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: var(--gray-600);

  &:hover,
  &:focus {
    color: var(--main-700);
    background: var(--main-50);
  }
}

.agent-card-menu-item {
  display: inline-flex;
  align-items: center;
  gap: 8px;

  &.danger {
    color: var(--color-error-700);
  }
}

.agent-modal-titlebar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  width: 100%;
}

.agent-modal-title {
  color: var(--gray-900);
  font-size: 16px;
  font-weight: 600;
}

.agent-modal-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;

  :deep(.ant-btn) {
    min-width: 64px;
    border-radius: 8px;
    font-weight: 500;
  }

  :deep(.ant-btn-primary) {
    border-color: var(--main-700);
    background: var(--main-700);

    &:hover,
    &:focus {
      border-color: var(--main-800);
      background: var(--main-800);
    }
  }
}

.agent-modal-content {
  display: grid;
  grid-template-columns: 164px minmax(0, 1fr);
  height: min(72vh, 640px);
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: var(--gray-0);
}

.agent-modal-sidebar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  padding: 14px 10px;
  overflow-y: auto;
  border-right: 1px solid var(--gray-150);
  background: linear-gradient(180deg, var(--gray-50), var(--main-10));
}

.agent-modal-nav-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-height: 40px;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: 10px;
  background: transparent;
  color: var(--gray-700);
  font-size: 13px;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
  transition:
    background 0.16s ease,
    border-color 0.16s ease,
    color 0.16s ease;

  &:hover {
    background: var(--gray-0);
    color: var(--gray-900);
  }

  &:focus-visible {
    outline: 2px solid var(--main-100);
    outline-offset: 1px;
    border-color: var(--main-200);
  }

  &.active {
    border-color: var(--main-100);
    background: var(--gray-0);
    color: var(--main-800);
    font-weight: 600;
  }
}

.nav-item-main {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  gap: 8px;

  svg {
    flex-shrink: 0;
    color: var(--gray-500);
  }
}

.agent-modal-nav-item.active .nav-item-main svg {
  color: var(--main-700);
}

.nav-dirty-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-warning-600);
}

.agent-modal-main {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 20px 20px;
}

.agent-modal-section {
  min-height: 0;
  background: var(--gray-0);
}

.runtime-section {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;

  :deep(.agent-runtime-config-form) {
    display: flex;
    flex: 1;
    flex-direction: column;
    min-height: 0;
    background: transparent;
  }

  :deep(.runtime-config-content) {
    flex: 1;
    min-width: 0;
    min-height: 0;
    padding: 0;
    overflow-y: auto;
  }
}

.section-heading {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  color: var(--gray-900);
  font-size: 14px;
  font-weight: 600;
}

.agent-profile-header {
  margin-bottom: 16px;
}

.agent-icon-preview {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-width: 0;
  gap: 16px;
  padding: 10px 12px 10px 10px;
  border: 1px solid var(--gray-150);
  border-radius: 12px;
  background: linear-gradient(135deg, var(--gray-0), var(--main-10));

  :deep(.ant-upload) {
    display: block;
  }
}

.agent-profile-main {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  gap: 10px;
}

.agent-icon-upload {
  position: relative;
  width: 48px;
  height: 48px;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
  cursor: pointer;

  img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  &:hover .agent-icon-mask,
  &.uploading .agent-icon-mask {
    opacity: 1;
  }
}

.agent-icon-mask {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  background: rgba(0, 0, 0, 0.48);
  color: var(--gray-0);
  font-size: 11px;
  opacity: 0;
  transition: opacity 0.16s ease;
}

.agent-icon-preview-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  gap: 4px;
  line-height: 1.25;
}

.agent-inline-name-input {
  width: 220px;
  max-width: 100%;
  padding: 2px 4px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--gray-900);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;

  &::placeholder {
    color: var(--gray-400);
  }

  &:hover,
  &:focus {
    border-color: var(--main-200);
    background: var(--gray-0);
    outline: none;
  }
}

.agent-inline-slug,
.agent-inline-slug-input {
  width: 220px;
  max-width: 100%;
  overflow: hidden;
  color: var(--gray-500);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-inline-slug-input {
  padding: 1px 4px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;

  &::placeholder {
    color: var(--gray-400);
  }

  &:hover,
  &:focus {
    border-color: var(--main-200);
    background: var(--gray-0);
    outline: none;
  }
}

.agent-backend-summary {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  gap: 8px;
  min-height: 40px;
  padding: 7px 10px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  background: var(--gray-0);
  color: var(--main-700);
}

.agent-backend-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
  line-height: 1.2;
}

.agent-backend-label {
  color: var(--gray-500);
  font-size: 11px;
}

.agent-backend-name {
  max-width: 180px;
  overflow: hidden;
  color: var(--gray-800);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-backend-select {
  min-width: 160px;

  :deep(.ant-select-selector) {
    border-color: var(--gray-150) !important;
    border-radius: 8px !important;
  }
}

.share-config-block {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--gray-150);
}

.runtime-dirty-row {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 10px;
}

.dirty-hint {
  color: var(--color-warning-700);
  font-size: 12px;
  font-weight: 500;
}

.modal-form {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.form-label {
  display: flex;
  flex-direction: column;
  gap: 6px;

  > span {
    color: var(--gray-700);
    font-size: 12px;
    font-weight: 500;
  }
}

.full-width {
  grid-column: 1 / -1;
}

.spinning {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 768px) {
  .agent-modal-content {
    grid-template-columns: 1fr;
    height: min(76vh, 640px);
  }

  .agent-modal-sidebar {
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    border-right: none;
    border-bottom: 1px solid var(--gray-150);
  }

  .agent-modal-nav-item {
    flex: 0 0 auto;
    width: auto;
    white-space: nowrap;
  }

  .agent-modal-titlebar {
    align-items: flex-start;
    flex-direction: column;
  }

  .agent-modal-actions {
    align-self: flex-end;
  }

  .agent-icon-preview {
    align-items: flex-start;
    flex-direction: column;
  }

  .agent-backend-summary {
    width: 100%;
  }

  .form-row {
    grid-template-columns: 1fr;
  }
}
</style>
