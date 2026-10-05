<script setup>
import { onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { agentApi } from '@/apis/agent_api'
import { skillApi } from '@/apis/skill_api'
import MarkdownPreview from '@/modules/workspace/ui/MarkdownPreview.vue'

const props = defineProps({ agentSlug: { type: String, required: true } })
const emit = defineEmits(['navigate', 'busy'])
const binding = ref(null)
const content = ref('')
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const fileInput = ref(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    binding.value = await agentApi.getAgentBoundSkill(props.agentSlug)
    await loadPreview()
  } catch (cause) {
    error.value = cause.message || '加载专属 Skill 失败'
  } finally {
    loading.value = false
  }
}

async function loadPreview() {
  content.value = ''
  if (binding.value?.skill) {
    const result = await skillApi.getSkillFile(binding.value.skill.slug, 'SKILL.md')
    content.value = result.data.content
  }
}

async function create() {
  if (saving.value) return
  saving.value = true
  emit('busy', true)
  try {
    binding.value = await agentApi.createAgentBoundSkill(props.agentSlug)
    await load()
    message.success('已创建操作指南')
  } catch (cause) {
    message.error(cause.message || '创建失败')
  } finally {
    saving.value = false
    emit('busy', false)
  }
}

async function upload(file) {
  if (saving.value) return
  saving.value = true
  emit('busy', true)
  try {
    binding.value = await agentApi.uploadAgentBoundSkill(
      props.agentSlug,
      file,
      binding.value?.revision
    )
    await load()
    message.success('专属 Skill 已更新')
  } catch (cause) {
    message.error(cause.message || '上传失败；请刷新后重试')
  } finally {
    saving.value = false
    emit('busy', false)
  }
}

function selectPackage(event) {
  if (saving.value) return
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.zip') || file.size > 10 * 1024 * 1024) {
    message.error('请选择不超过 10 MiB 的 ZIP 文件')
    return
  }
  if (binding.value?.skill) {
    Modal.confirm({
      title: '替换专属 Skill？',
      content: 'ZIP 将替换当前全部文件。请先在详情页导出需要保留的内容。',
      okText: '替换',
      cancelText: '取消',
      onOk: () => upload(file)
    })
  } else {
    upload(file)
  }
}

function openDetail() {
  if (saving.value || loading.value || !binding.value?.skill) return
  emit('navigate', {
    name: 'ExtensionSkillDetail',
    params: { slug: binding.value.skill.slug },
    query: { agent: props.agentSlug }
  })
}

onMounted(load)
</script>

<template>
  <section class="bound-skill-panel">
    <header class="bound-skill-header">
      <div>
        <h3>专属 Skill</h3>
        <p>操作流程与资源，运行时自动加载。</p>
      </div>
      <a-space v-if="binding?.skill && !error" :size="4">
        <a-button type="text" :disabled="saving || loading" @click="load">刷新</a-button>
        <a-button :disabled="saving || loading" @click="openDetail">{{ binding.can_manage ? '编辑' : '查看文件' }}</a-button>
      </a-space>
    </header>
    <a-spin v-if="loading" aria-label="加载专属 Skill" />
    <a-alert v-else-if="error" type="error" :message="error" show-icon>
      <template #action><a-button size="small" @click="load">重试</a-button></template>
    </a-alert>
    <template v-else>
      <div v-if="binding?.skill" class="bound-skill-preview" aria-label="SKILL.md 只读预览">
        <span class="preview-filename">SKILL.md</span>
        <MarkdownPreview :content="content" compact />
      </div>
      <a-empty v-else description="尚未创建专属 Skill" />
      <div v-if="binding?.can_manage" class="bound-skill-actions">
        <a-button v-if="!binding.skill" type="primary" :loading="saving" @click="create"
          >创建 Skill</a-button
        >
        <a-button type="text" :disabled="saving" @click="fileInput.click()">{{
          binding.skill ? '替换 ZIP' : '导入 ZIP'
        }}</a-button>
        <input ref="fileInput" type="file" accept=".zip" hidden @change="selectPackage" />
      </div>
    </template>
  </section>
</template>

<style scoped>
.bound-skill-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.bound-skill-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
h3 {
  margin: 0 0 6px;
  font-size: 15px;
  color: var(--gray-900);
}
p {
  margin: 0;
  font-size: 13px;
  color: var(--gray-600);
}
.bound-skill-preview {
  padding: 18px 20px;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  overflow-wrap: anywhere;
}
.preview-filename {
  display: block;
  margin-bottom: 16px;
  font-size: 12px;
  color: var(--gray-500);
  font-family: monospace;
}
.bound-skill-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
@media (max-width: 600px) {
  .bound-skill-header {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .bound-skill-preview {
    padding: 14px;
  }
}
</style>
