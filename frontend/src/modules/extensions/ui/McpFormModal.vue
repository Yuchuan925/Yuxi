<template>
  <a-modal
    v-model:open="visible"
    @ok="handleFormSubmit"
    :confirmLoading="formLoading"
    @cancel="visible = false"
    :maskClosable="false"
    :keyboard="!formLoading"
    :closable="!formLoading"
    :body-style="{ maxHeight: 'min(68dvh, 600px)', overflowY: 'auto', overflowX: 'hidden' }"
    width="min(680px, calc(100vw - 32px))"
    centered
    class="mcp-form-modal"
  >
    <template #title>
      <div class="mcp-form-heading">
        <span class="mcp-form-icon"><Plug :size="18" aria-hidden="true" /></span>
        <div>
          <span>{{ editMode ? '编辑 MCP' : '添加 MCP' }}</span>
          <p>连接外部服务，供智能体调用工具</p>
        </div>
      </div>
    </template>
    <a-form layout="vertical" class="extension-form mcp-form" :disabled="formLoading">
      <div class="mcp-form-grid">
        <a-form-item label="名称" html-for="mcp-form-name" required class="form-item">
          <a-input id="mcp-form-name" v-model:value="form.name" placeholder="如：文档搜索" />
        </a-form-item>
        <a-form-item label="标识" html-for="mcp-form-slug" required class="form-item">
          <a-input
            id="mcp-form-slug"
            v-model:value="form.slug"
            placeholder="如：docs-search"
            :disabled="editMode"
          />
        </a-form-item>
      </div>
      <a-form-item label="描述" html-for="mcp-form-description" class="form-item">
        <a-textarea
          id="mcp-form-description"
          v-model:value="form.description"
          placeholder="简要说明这个服务能做什么（可选）"
          :rows="2"
        />
      </a-form-item>
      <div class="mcp-form-grid endpoint-row">
        <a-form-item label="服务地址" html-for="mcp-form-url" required class="form-item">
          <a-input id="mcp-form-url" v-model:value="form.url" placeholder="https://example.com/mcp" />
        </a-form-item>
        <a-form-item label="传输方式" html-for="mcp-form-transport" required class="form-item">
          <a-select id="mcp-form-transport" v-model:value="form.transport">
            <a-select-option value="http">Streamable HTTP</a-select-option>
            <a-select-option value="sse">SSE</a-select-option>
          </a-select>
        </a-form-item>
      </div>
      <div class="mcp-form-grid appearance-row">
        <a-form-item label="图标" html-for="mcp-form-icon" class="form-item">
          <a-input id="mcp-form-icon" v-model:value="form.icon" placeholder="Emoji" :maxlength="2" />
        </a-form-item>
        <a-form-item label="标签" html-for="mcp-form-tags" class="form-item">
          <a-select
            id="mcp-form-tags"
            v-model:value="form.tags"
            mode="tags"
            placeholder="输入后回车添加（可选）"
          />
        </a-form-item>
      </div>
      <div class="mcp-connection-options">
        <button
          type="button"
          class="connection-options-toggle"
          :aria-expanded="connectionOptionsOpen"
          aria-controls="mcp-connection-options-content"
          @click="connectionOptionsOpen = !connectionOptionsOpen"
        >
          <ChevronRight :size="14" aria-hidden="true" />
          <span>高级连接选项</span>
          <span class="connection-options-hint">请求头与超时</span>
        </button>
        <CollapseTransition>
          <div v-show="connectionOptionsOpen" id="mcp-connection-options-content" class="connection-options-content">
            <a-form-item label="HTTP 请求头" html-for="mcp-form-headers" class="form-item">
              <a-textarea
                id="mcp-form-headers"
                v-model:value="form.headersText"
                placeholder='JSON 格式，如：{"Authorization": "Bearer <token>"}'
                :rows="3"
              />
            </a-form-item>
            <div class="mcp-form-grid">
              <a-form-item label="HTTP 超时（秒）" html-for="mcp-form-timeout" class="form-item">
                <a-input-number id="mcp-form-timeout" v-model:value="form.timeout" :min="1" :max="300" placeholder="默认" />
              </a-form-item>
              <a-form-item label="SSE 读取超时（秒）" html-for="mcp-form-read-timeout" class="form-item">
                <a-input-number id="mcp-form-read-timeout" v-model:value="form.sse_read_timeout" :min="1" :max="300" placeholder="默认" />
              </a-form-item>
            </div>
          </div>
        </CollapseTransition>
      </div>
    </a-form>
  </a-modal>
</template>

<script setup>
import { ref, reactive, computed, watch } from 'vue'
import { message } from 'ant-design-vue'
import { ChevronRight, Plug } from '@lucide/vue'
import { mcpApi } from '@/apis/mcp_api'
import { parseMcpManifest } from '@/modules/extensions/model/mcpManifest'
import CollapseTransition from '@/shared/ui/CollapseTransition.vue'

const props = defineProps({
  open: { type: Boolean, default: false },
  editMode: { type: Boolean, default: false },
  editData: { type: Object, default: null }
})

const emit = defineEmits(['update:open', 'submitted'])

const formLoading = ref(false)
const connectionOptionsOpen = ref(false)

const visible = computed({
  get: () => props.open,
  set: (val) => { if (!formLoading.value) emit('update:open', val) }
})

const form = reactive({
  slug: '',
  name: '',
  description: '',
  transport: 'http',
  url: '',
  headersText: '',
  timeout: null,
  sse_read_timeout: null,
  tags: [],
  icon: ''
})

watch(
  () => props.open,
  (val) => {
    if (val) connectionOptionsOpen.value = props.editMode
    if (val && props.editData) {
      Object.assign(form, {
        slug: props.editData.slug || '',
        name: props.editData.name || '',
        description: props.editData.description || '',
        transport: props.editData.transport === 'streamable_http' ? 'http' : props.editData.transport || 'http',
        url: props.editData.url || '',
        headersText: props.editData.headers ? JSON.stringify(props.editData.headers, null, 2) : '',
        timeout: props.editData.timeout,
        sse_read_timeout: props.editData.sse_read_timeout,
        tags: props.editData.tags || [],
        icon: props.editData.icon || ''
      })
    } else if (val && !props.editData) {
      Object.assign(form, {
        slug: '',
        name: '',
        description: '',
        transport: 'http',
        url: '',
        headersText: '',
        timeout: null,
        sse_read_timeout: null,
        tags: [],
        icon: ''
      })
    }
  },
  { immediate: true }
)

const handleFormSubmit = async () => {
  if (formLoading.value) return
  try {
    formLoading.value = true
    let headers = null
    if (form.headersText.trim()) {
      try {
        headers = JSON.parse(form.headersText)
      } catch {
        message.error('请求头 JSON 格式错误')
        return
      }
    }
    if (!form.slug?.trim()) {
      message.error('MCP 标识不能为空')
      return
    }
    if (!form.name?.trim()) {
      message.error('MCP 名称不能为空')
      return
    }
    const entry = {
      type: form.transport,
      url: form.url,
      ...(form.headersText.trim() && { headers }),
      ...(form.timeout != null && { timeout: form.timeout }),
      ...(form.sse_read_timeout != null && { sse_read_timeout: form.sse_read_timeout }),
      extra_data: {
        name: form.name,
        ...(form.description && { description: form.description }),
        ...(form.tags.length && { tags: form.tags }),
        ...(form.icon && { icon: form.icon })
      }
    }
    const [data] = parseMcpManifest(JSON.stringify({ mcpServers: { [form.slug]: entry } }))
    let submitted
    if (props.editMode) {
      const { slug, ...updateData } = data
      const result = await mcpApi.updateMcpServer(props.editData?.slug || slug, updateData)
      if (result.success) {
        submitted = result.data
        message.success('MCP 更新成功')
      } else {
        message.error(result.message || '更新失败')
        return
      }
    } else {
      const result = await mcpApi.createMcpServer(data)
      if (result.success) {
        submitted = result.data
        message.success('MCP 创建成功')
      } else {
        message.error(result.message || '创建失败')
        return
      }
    }
    formLoading.value = false
    visible.value = false
    emit('submitted', submitted)
  } catch (err) {
    message.error(err.message || '操作失败')
  } finally {
    formLoading.value = false
  }
}
</script>

<style lang="less" scoped>
.mcp-form-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-right: 32px;
  color: var(--gray-900);
  font-weight: 600;

  p {
    margin: 3px 0 0;
    color: var(--gray-600);
    font-size: 12px;
    font-weight: 400;
  }
}

.mcp-form-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  background: var(--gray-100);
  color: var(--gray-700);
}

.mcp-form {
  padding-top: 16px;

  :deep(.ant-input-number), :deep(.ant-select) { width: 100%; }
  :deep(.ant-form-item-label > label) { color: var(--gray-800); font-size: 13px; }
}

.mcp-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;

  > .form-item { min-width: 0; }
  &.endpoint-row { grid-template-columns: minmax(0, 1fr) 190px; }
  &.appearance-row { grid-template-columns: 100px minmax(0, 1fr); }
}

.mcp-connection-options {
  border-top: 1px solid var(--gray-150);

  .connection-options-toggle {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    min-height: 44px;
    padding: 0;
    border: 0;
    background: transparent;
    color: var(--gray-800);
    font: inherit;
    font-size: 13px;
    text-align: left;
    cursor: pointer;

    &:hover { color: var(--main-700); }
    &:focus-visible { outline: 2px solid var(--main-300); outline-offset: -2px; }
    svg { flex-shrink: 0; transition: transform 0.25s ease; }
    &[aria-expanded='true'] svg { transform: rotate(90deg); }
  }

  .connection-options-content { padding-top: 8px; }
  .connection-options-hint { margin-left: auto; color: var(--gray-600); font-size: 12px; }
  .mcp-form-grid .form-item { margin-bottom: 0; }
}

@media (prefers-reduced-motion: reduce) {
  .mcp-connection-options .connection-options-content,
  .mcp-connection-options .connection-options-toggle svg { transition: none; }
}

@media (max-width: 600px) {
  .mcp-form-grid, .mcp-form-grid.endpoint-row { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .mcp-form-grid.appearance-row { gap: 12px; }
  .mcp-connection-options .mcp-form-grid { gap: 16px; }
  .mcp-form :deep(.ant-input), .mcp-form :deep(.ant-select-selector), .mcp-form :deep(.ant-input-number) { min-height: 40px; }
  :global(.mcp-form-modal .ant-modal-footer .ant-btn) { min-height: 44px; }
}
</style>
