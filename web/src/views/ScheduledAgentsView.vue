<script setup>
import { computed, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { Plus, Pause, Play, Trash2 } from 'lucide-vue-next'
import { useAgentStore } from '@/stores/agent'
import { scheduledAgentApi } from '@/apis/scheduled_agent_api'

const agentStore = useAgentStore()
const jobs = ref([])
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const editing = ref(null)

const availableAgents = computed(() => (agentStore.agents || []).filter((agent) => !agent.is_subagent))
const emptyForm = () => ({
  name: '',
  agent_slug: availableAgents.value[0]?.slug || availableAgents.value[0]?.id || '',
  prompt: '',
  cron_expression: '0 9 * * *',
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
  enabled: true
})
const form = ref(emptyForm())

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    jobs.value = (await scheduledAgentApi.list()).jobs || []
  } catch (err) {
    error.value = err.message || '加载定时任务失败'
  } finally {
    loading.value = false
  }
}

const openCreate = () => {
  form.value = emptyForm()
  editing.value = null
}

const openEdit = (job) => {
  editing.value = job.id
  form.value = {
    name: job.name,
    agent_slug: job.agent_slug,
    prompt: job.prompt,
    cron_expression: job.cron_expression,
    timezone: job.timezone,
    enabled: job.enabled
  }
}

const save = async () => {
  saving.value = true
  error.value = ''
  try {
    if (editing.value) await scheduledAgentApi.update(editing.value, form.value)
    else await scheduledAgentApi.create(form.value)
    message.success(editing.value ? '定时任务已更新' : '定时任务已创建')
    editing.value = null
    form.value = emptyForm()
    await load()
  } catch (err) {
    error.value = err.message || '保存定时任务失败'
  } finally {
    saving.value = false
  }
}

const toggle = async (job) => {
  try {
    await scheduledAgentApi.update(job.id, { enabled: !job.enabled })
    await load()
  } catch (err) {
    message.error(err.message || '更新任务状态失败')
  }
}

const remove = (job) => {
  Modal.confirm({
    title: '删除定时任务？',
    content: '删除只会停止未来触发，不会删除已经产生的 AgentRun。',
    okText: '删除',
    cancelText: '取消',
    okType: 'danger',
    async onOk() {
      await scheduledAgentApi.remove(job.id)
      await load()
    }
  })
}

onMounted(async () => {
  if (!agentStore.isInitialized) await agentStore.initialize()
  await load()
})
</script>

<template>
  <main class="scheduled-agents-page">
    <header class="page-heading">
      <div>
        <p class="eyebrow">自动化</p>
        <h1>定时 Agent</h1>
        <p>让可访问的 Agent 按固定时间执行提示词，结果会保留在对应运行记录中。</p>
      </div>
      <button class="primary-button" type="button" @click="openCreate"><Plus :size="16" />新建任务</button>
    </header>

    <p v-if="error" class="error-message" role="alert">{{ error }}</p>

    <section class="scheduled-layout">
      <form class="schedule-form" @submit.prevent="save">
        <h2>{{ editing ? '编辑任务' : '新建任务' }}</h2>
        <label>名称<input v-model="form.name" required maxlength="255" placeholder="例如：每日工作摘要" /></label>
        <label>Agent<select v-model="form.agent_slug" required><option v-for="agent in availableAgents" :key="agent.slug || agent.id" :value="agent.slug || agent.id">{{ agent.name }}</option></select></label>
        <label>提示词<textarea v-model="form.prompt" required maxlength="32000" rows="7" placeholder="每天执行什么任务？" /></label>
        <label>Cron 表达式<input v-model="form.cron_expression" required maxlength="100" placeholder="0 9 * * *" /><small>5 段格式，例如每天 09:00。</small></label>
        <label>时区<input v-model="form.timezone" required maxlength="64" placeholder="Asia/Shanghai" /></label>
        <label class="switch-row"><input v-model="form.enabled" type="checkbox" />启用任务</label>
        <div class="form-actions"><button v-if="editing" class="secondary-button" type="button" @click="openCreate">取消</button><button class="primary-button" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存任务' }}</button></div>
      </form>

      <section class="job-list" aria-live="polite">
        <div v-if="loading" class="empty-state">正在加载…</div>
        <div v-else-if="!jobs.length" class="empty-state">还没有定时任务，创建一个让 Agent 自动工作。</div>
        <article v-for="job in jobs" v-else :key="job.id" class="job-card">
          <div class="job-card-head"><div><span class="status-pill" :class="{ disabled: !job.enabled }">{{ job.enabled ? '已启用' : '已停用' }}</span><h2>{{ job.name }}</h2><p>{{ job.agent_slug }} · {{ job.cron_expression }} · {{ job.timezone }}</p></div><div class="job-actions"><button type="button" :aria-label="job.enabled ? '停用任务' : '启用任务'" @click="toggle(job)"><Pause v-if="job.enabled" :size="16" /><Play v-else :size="16" /></button><button type="button" aria-label="删除任务" @click="remove(job)"><Trash2 :size="16" /></button></div></div>
          <p class="job-prompt">{{ job.prompt }}</p>
          <div class="job-meta"><span>下次执行：{{ job.next_run_at || '等待调度' }}</span><span>最近执行：{{ job.last_run_at || '尚未执行' }}</span><button type="button" @click="openEdit(job)">编辑</button></div>
          <div v-if="job.runs?.length" class="run-history"><div v-for="run in job.runs.slice(0, 3)" :key="run.id"><span>{{ run.scheduled_for }}</span><strong :class="`run-${run.status}`">{{ run.status }}</strong><span v-if="run.error_message">{{ run.error_message }}</span></div></div>
        </article>
      </section>
    </section>
  </main>
</template>

<style lang="less" scoped>
.scheduled-agents-page { min-height: 100%; padding: 44px 6vw 72px; color: var(--gray-1000); background: var(--gray-0); }
.page-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; margin: 0 auto 32px; max-width: 1180px; }
.eyebrow { margin: 0 0 8px; color: var(--primary-color); font-size: 12px; letter-spacing: .14em; text-transform: uppercase; }
h1 { margin: 0; font-size: clamp(30px, 4vw, 48px); letter-spacing: -.04em; } .page-heading p:last-child { margin: 10px 0 0; color: var(--gray-600); }
.scheduled-layout { display: grid; grid-template-columns: minmax(280px, 360px) minmax(0, 1fr); gap: 24px; max-width: 1180px; margin: auto; }
.schedule-form, .job-card { border: 1px solid var(--gray-200); border-radius: 14px; background: var(--gray-0); box-shadow: 0 10px 30px rgb(0 0 0 / 4%); }
.schedule-form { padding: 22px; align-self: start; } .schedule-form h2 { margin: 0 0 18px; font-size: 18px; }
.schedule-form label { display: grid; gap: 7px; margin: 14px 0; color: var(--gray-700); font-size: 13px; } input, textarea, select { width: 100%; box-sizing: border-box; padding: 10px 11px; border: 1px solid var(--gray-300); border-radius: 8px; color: inherit; background: var(--gray-0); font: inherit; } textarea { resize: vertical; } small { color: var(--gray-500); }
.switch-row { display: flex !important; grid-template-columns: none !important; align-items: center; gap: 8px !important; } .switch-row input { width: auto; } .form-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 20px; }
.primary-button, .secondary-button, .job-actions button, .job-meta button { display: inline-flex; align-items: center; gap: 7px; border: 0; border-radius: 8px; padding: 10px 13px; cursor: pointer; font: inherit; } .primary-button { color: white; background: var(--primary-color); } .secondary-button, .job-actions button, .job-meta button { color: var(--gray-700); background: var(--gray-100); } button:disabled { opacity: .6; cursor: wait; }
.job-list { display: grid; gap: 14px; } .job-card { padding: 20px; } .job-card-head { display: flex; justify-content: space-between; gap: 16px; } .job-card h2 { margin: 9px 0 4px; font-size: 19px; } .job-card p { margin: 0; color: var(--gray-600); font-size: 13px; } .status-pill { display: inline-block; padding: 4px 8px; border-radius: 99px; color: var(--success-color); background: color-mix(in srgb, var(--success-color) 12%, transparent); font-size: 12px; } .status-pill.disabled { color: var(--gray-600); background: var(--gray-100); }
.job-actions { display: flex; gap: 6px; } .job-actions button { padding: 8px; } .job-prompt { margin: 18px 0 !important; padding: 12px; border-left: 3px solid var(--primary-color); color: var(--gray-700) !important; background: var(--gray-50); white-space: pre-wrap; } .job-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; color: var(--gray-500); font-size: 12px; } .job-meta button { margin-left: auto; padding: 5px 8px; } .run-history { display: grid; gap: 6px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--gray-200); color: var(--gray-500); font-size: 12px; } .run-history div { display: flex; gap: 10px; } .run-history strong { color: var(--gray-800); } .run-completed { color: var(--success-color) !important; } .run-failed, .run-cancelled { color: var(--error-color) !important; } .empty-state { padding: 50px 20px; border: 1px dashed var(--gray-300); border-radius: 14px; text-align: center; color: var(--gray-500); } .error-message { max-width: 1180px; margin: 0 auto 18px; color: var(--error-color); }
@media (max-width: 760px) { .scheduled-layout { grid-template-columns: 1fr; } .page-heading { align-items: flex-start; flex-direction: column; } .scheduled-agents-page { padding: 28px 18px 50px; } }
</style>
