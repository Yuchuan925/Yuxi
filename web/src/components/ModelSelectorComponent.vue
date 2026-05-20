<template>
  <a-dropdown trigger="click" :open="dropdownOpen" @open-change="handleOpenChange">
    <div class="model-select" :class="modelSelectClasses" @click.prevent>
      <div class="model-select-content">
        <div class="model-info">
          <a-tooltip :title="displayModelTooltip" placement="right">
            <span class="model-text text"> {{ displayModelText }} </span>
          </a-tooltip>
        </div>
        <div class="model-status-controls">
          <span
            v-if="state.currentModelStatus"
            class="model-status-indicator"
            :class="state.currentModelStatus.status"
            :title="getCurrentModelStatusTooltip()"
          >
            {{ modelStatusIcon }}
          </span>
          <a-button
            :size="buttonSize"
            type="text"
            :loading="state.checkingStatus"
            @click.stop="checkCurrentModelStatus"
            :disabled="state.checkingStatus"
            class="status-check-button"
          >
            {{ state.checkingStatus ? '检查中...' : '检查' }}
          </a-button>
        </div>
      </div>
    </div>
    <template #overlay>
      <div class="model-dropdown" @click.stop>
        <div class="model-search">
          <a-input v-model:value="modelSearchKeyword" placeholder="搜索模型" allow-clear @keydown.stop>
            <template #suffix>
              <a-tooltip title="刷新缓存">
                <button
                  :disabled="state.refreshingCache"
                  class="cache-refresh-button"
                  @mousedown.prevent.stop
                  @click.stop="refreshCache"
                >
                  <RefreshCw :size="13" :class="{ spin: state.refreshingCache }" />
                </button>
              </a-tooltip>
            </template>
          </a-input>
        </div>
        <a-menu class="scrollable-menu">
          <a-menu-item v-if="loadingV2Models" key="loading" disabled>加载中...</a-menu-item>
          <a-menu-item v-else-if="!hasFilteredModels" key="empty" disabled>暂无匹配模型</a-menu-item>
          <template v-else>
            <a-menu-item-group
              v-for="(providerData, providerId) in filteredV2Models"
              :key="`v2-${providerId}`"
            >
              <template #title>
                <span>{{ providerId }}</span>
              </template>
              <a-menu-item
                v-for="model in providerData.models"
                :key="model.spec"
                @click="handleSelectV2Model(model.spec)"
              >
                {{ model.display_name }}
              </a-menu-item>
            </a-menu-item-group>
          </template>
        </a-menu>
      </div>
    </template>
  </a-dropdown>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { modelProviderApi } from '@/apis/system_api'
import { RefreshCw } from 'lucide-vue-next'
import { useModelStatus } from '@/composables/useModelStatus'

const props = defineProps({
  model_spec: {
    type: String,
    default: ''
  },
  placeholder: {
    type: String,
    default: '请选择模型'
  },
  size: {
    type: String,
    default: 'small',
    validator: (value) => ['small', 'middle', 'large'].includes(value)
  },
  displayName: {
    type: String,
    default: 'full',
    validator: (value) => ['full', 'short', 'mini'].includes(value)
  }
})

const emit = defineEmits(['select-model'])

// v2 模型数据：每次展开下拉时实时从后端拉取
const v2Models = ref({})
const loadingV2Models = ref(false)
const dropdownOpen = ref(false)
const modelSearchKeyword = ref('')

const filteredV2Models = computed(() => {
  const keyword = modelSearchKeyword.value.trim().toLowerCase()
  if (!keyword) return v2Models.value

  return Object.entries(v2Models.value).reduce((result, [providerId, providerData]) => {
    const models = (providerData.models || []).filter((model) => {
      return [providerId, model.spec, model.model_id, model.display_name].some((value) =>
        String(value || '')
          .toLowerCase()
          .includes(keyword)
      )
    })

    if (models.length) {
      result[providerId] = { ...providerData, models }
    }

    return result
  }, {})
})

const hasFilteredModels = computed(() => {
  return Object.values(filteredV2Models.value).some((providerData) => providerData.models?.length)
})

// 拉取 v2 模型列表
const fetchV2Models = async () => {
  if (loadingV2Models.value) return
  loadingV2Models.value = true
  try {
    const response = await modelProviderApi.getV2Models('chat')
    if (response.success) {
      v2Models.value = response.data || {}
    }
  } catch (error) {
    console.warn('Failed to load v2 models:', error)
  } finally {
    loadingV2Models.value = false
  }
}

// 下拉展开时触发实时刷新（仅在打开瞬间触发，关闭时忽略）
const handleOpenChange = (open) => {
  dropdownOpen.value = open
  if (open) fetchV2Models()
}

// 强制刷新缓存
const refreshCache = async () => {
  if (state.refreshingCache) return
  state.refreshingCache = true
  try {
    await modelProviderApi.refreshModelCache()
    // 刷新后重新拉取模型列表
    await fetchV2Models()
  } catch (error) {
    console.error('Failed to refresh cache:', error)
  } finally {
    state.refreshingCache = false
  }
}

// 状态管理
useModelStatus()
const state = reactive({
  currentModelStatus: null,
  checkingStatus: false,
  refreshingCache: false
})

const resolvedSize = computed(() => props.size || 'small')
const modelSelectClasses = computed(() => ({
  'model-select--middle': resolvedSize.value === 'middle',
  'model-select--large': resolvedSize.value === 'large'
}))
const buttonSize = computed(() => {
  if (resolvedSize.value === 'large') return 'large'
  if (resolvedSize.value === 'middle') return 'middle'
  return 'small'
})

const extractModelName = (spec) => {
  const separatorIndex = spec.indexOf(':')
  return separatorIndex >= 0 ? spec.slice(separatorIndex + 1) : spec
}

const displayModelText = computed(() => {
  const spec = props.model_spec
  if (!spec) return props.placeholder

  const modelName = extractModelName(spec)
  if (props.displayName === 'mini') {
    return modelName.includes('/') ? modelName.split('/').pop() : modelName
  }
  if (props.displayName === 'short') return modelName
  return spec
})

const displayModelTooltip = computed(() => props.model_spec || props.placeholder)

// 检查当前模型状态
const checkCurrentModelStatus = async () => {
  const spec = props.model_spec
  if (!spec) return

  try {
    state.checkingStatus = true
    const response = await modelProviderApi.getModelStatusBySpec(spec)
    if (response.data) {
      state.currentModelStatus = response.data
    } else {
      state.currentModelStatus = null
    }
  } catch (error) {
    console.error(`检查模型 ${spec} 状态失败:`, error)
    state.currentModelStatus = { status: 'error', message: error.message }
  } finally {
    state.checkingStatus = false
  }
}

const modelStatusIcon = computed(() => {
  const status = state.currentModelStatus
  if (!status) return '○'
  if (status.status === 'available') return '✓'
  if (status.status === 'unavailable') return '✗'
  if (status.status === 'error') return '⚠'
  return '○'
})

const getCurrentModelStatusTooltip = () => {
  const status = state.currentModelStatus
  if (!status) return '状态未知'

  let statusText = ''
  if (status.status === 'available') statusText = '可用'
  else if (status.status === 'unavailable') statusText = '不可用'
  else if (status.status === 'error') statusText = '错误'

  const message = status.message || '无详细信息'
  return `${statusText}: ${message}`
}

// 选择 v2 模型的方法
const handleSelectV2Model = (spec) => {
  emit('select-model', spec)
  dropdownOpen.value = false
}
</script>

<style lang="less" scoped>
@import '@/assets/css/model-selector-common.less';

// 状态检查按钮
.status-check-button {
  font-size: 12px;
  padding: 0 4px;
}

// 缓存刷新按钮
.cache-refresh-button {
  font-size: 12px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  background-color: transparent;
  border: none;
  outline: none;
  cursor: pointer;
}

.model-dropdown {
  min-width: 280px;
  max-width: 420px;
  overflow: hidden;
  background: var(--gray-0);
  border-radius: 8px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08);
}

.model-search {
  padding: 8px;
  border-bottom: 1px solid var(--gray-100);
}

:deep(.ant-dropdown-menu) {
  &.scrollable-menu {
    max-height: 260px;
    overflow-y: auto;
    box-shadow: none;
  }
}
</style>
