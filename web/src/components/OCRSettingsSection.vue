<template>
  <section class="ocr-settings-section" aria-labelledby="ocr-settings-title">
    <header class="header-section">
      <div>
        <div id="ocr-settings-title" class="section-title">OCR 引擎配置</div>
        <p class="section-description">选择默认引擎，配置自托管端点和凭证来源。</p>
      </div>
      <a-button :loading="loading" @click="loadConfigs">刷新</a-button>
    </header>

    <div class="default-panel">
      <div>
        <div class="default-label">默认 OCR 引擎</div>
        <div class="default-description">未指定引擎时使用此配置。</div>
      </div>
      <a-select :value="defaultEngine" class="default-select" @change="setDefaultEngine">
        <a-select-option v-for="engine in enabledConfigs" :key="engine.engine_id" :value="engine.engine_id">
          {{ engine.display_name }}
        </a-select-option>
      </a-select>
    </div>

    <a-spin :spinning="loading">
      <div class="engine-list">
        <section v-for="engine in configs" :key="engine.engine_id" class="engine-card">
          <header class="engine-summary-row">
            <div class="engine-copy">
              <div class="engine-name-row">
                <strong>{{ engine.display_name }}</strong>
                <span v-if="engine.is_default" class="default-badge">默认</span>
              </div>
              <span class="engine-meta">{{ engineDescription(engine.engine_id) }}</span>
            </div>
            <div class="engine-summary-status">
              <span v-if="supportsCredential(engine)" :class="['credential-badge', engine.credential_status]">
                {{ credentialStatusText(engine.credential_status) }}
              </span>
              <a-switch
                size="small"
                :checked="engine.enabled"
                :loading="savingEngine === engine.engine_id"
                :disabled="engine.is_default"
                @change="setEngineEnabled(engine, $event)"
              />
            </div>
          </header>

          <div v-if="supportsEndpoint(engine) || supportsCredential(engine)" class="engine-content">
            <div class="form-grid">
              <label v-if="supportsEndpoint(engine)" class="field full-field">
                <span class="field-label">服务端点</span>
                <a-input v-model:value="engine.endpoint" :placeholder="endpointExamples[engine.engine_id]" allow-clear />
                <small>仅自托管服务需要配置；云服务端点由系统固定。</small>
              </label>

              <label v-if="supportsCredential(engine)" class="field">
                <span class="field-label">凭证来源</span>
                <a-select v-model:value="engine.credential_source" @change="clearCredential(engine)">
                  <a-select-option v-for="source in engine.credential_sources" :key="source" :value="source">
                    {{ credentialSourceLabels[source] }}
                  </a-select-option>
                </a-select>
              </label>

              <label v-if="engine.credential_source === 'database'" class="field">
                <span class="field-label">API 密钥</span>
                <a-input-password v-model:value="engine.credential_value" placeholder="请输入或更新密钥" autocomplete="new-password" />
                <small>密钥以明文保存到业务数据库，请自行控制数据库访问权限。</small>
              </label>

              <label v-else-if="engine.credential_source === 'environment'" class="field">
                <span class="field-label">环境变量</span>
                <a-input :value="engine.credential_ref" readonly />
                <small>运行时从服务端环境变量读取，不保存密钥。</small>
              </label>
            </div>
            <div class="engine-actions">
              <a-button type="primary" :loading="savingEngine === engine.engine_id" @click="saveEngine(engine)">
                保存配置
              </a-button>
            </div>
          </div>
        </section>
      </div>
    </a-spin>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ocrApi } from '@/apis/system_api'

const configs = ref([])
const loading = ref(false)
const savingEngine = ref('')
const credentialSourceLabels = { environment: '环境变量', database: '页面填写并保存' }
const endpointExamples = { mineru_ocr: 'http://mineru:30001', pp_structure_v3_ocr: 'http://paddlex:8080' }
const descriptions = {
  rapid_ocr: '本地运行，无需服务配置',
  mineru_ocr: '自托管 MinerU 服务',
  mineru_official: 'MinerU 官方云服务',
  pp_structure_v3_ocr: '自托管 PaddleX 服务',
  deepseek_ocr: 'DeepSeek OCR 云服务',
  paddleocr_vl_1_6: 'PaddleOCR-VL 云服务',
  paddleocr_pp_ocrv6: 'PP-OCRv6 云服务'
}

const defaultEngine = computed(() => configs.value.find((engine) => engine.is_default)?.engine_id)
const enabledConfigs = computed(() => configs.value.filter((engine) => engine.enabled || engine.is_default))
const supportsEndpoint = (engine) => Boolean(engine.endpoint_editable)
const supportsCredential = (engine) => engine.credential_sources?.length > 0
const engineDescription = (engineId) => descriptions[engineId] || 'OCR 引擎配置'
const credentialStatusText = (status) => ({ configured: '凭证已配置', missing: '凭证缺失', not_required: '无需凭证' })[status] || '状态未知'

const loadConfigs = async () => {
  loading.value = true
  try {
    const data = await ocrApi.getConfigs()
    configs.value = (data.configs || []).map((engine) => ({ ...engine, credential_value: '' }))
  } catch (error) {
    message.error(error.message || '加载 OCR 配置失败')
  } finally {
    loading.value = false
  }
}

const saveEngine = async (engine, overrides = {}) => {
  savingEngine.value = engine.engine_id
  try {
    const payload = {
      enabled: engine.enabled,
      is_default: engine.is_default,
      credential_source: engine.credential_source || null,
      ...overrides
    }
    if (supportsEndpoint(engine)) payload.endpoint = engine.endpoint || null
    if (engine.credential_source === 'database' && engine.credential_value) payload.credential_value = engine.credential_value
    await ocrApi.updateConfig(engine.engine_id, payload)
    message.success(`${engine.display_name} 配置已保存`)
    await loadConfigs()
  } catch (error) {
    message.error(error.message || '保存 OCR 配置失败')
  } finally {
    savingEngine.value = ''
  }
}

const setEngineEnabled = (engine, enabled) => saveEngine(engine, { enabled })
const setDefaultEngine = (engineId) => {
  const engine = configs.value.find((item) => item.engine_id === engineId)
  if (engine) saveEngine(engine, { enabled: true, is_default: true })
}
const clearCredential = (engine) => {
  engine.credential_value = ''
}

onMounted(loadConfigs)
</script>

<style scoped lang="less">
.ocr-settings-section { display: flex; flex-direction: column; color: var(--color-text); }
.header-section, .default-panel, .engine-summary-row, .engine-actions { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.header-section { align-items: flex-end; margin-bottom: 16px; }
.section-title { margin: 12px 0; color: var(--gray-900); font-size: 16px; font-weight: 500; }
.section-description, .default-description, .engine-meta, small { color: var(--color-text-secondary); font-size: 12px; line-height: 1.5; }
.section-description { margin: 0; font-size: 13px; }
.default-panel { margin-bottom: 16px; padding: 12px 16px; border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-25); }
.default-label, .field-label { font-size: 13px; font-weight: 500; }
.default-select { width: min(300px, 48%); }
.engine-list { overflow: hidden; border: 1px solid var(--gray-150); border-radius: 8px; background: var(--gray-0); }
.engine-card + .engine-card { border-top: 1px solid var(--gray-100); }
.engine-summary-row { min-height: 64px; padding: 0 14px; }
.engine-copy { min-width: 0; flex: 1; }
.engine-name-row { display: flex; align-items: center; gap: 7px; }
.engine-name-row strong { font-size: 14px; font-weight: 500; }
.engine-meta { display: block; margin-top: 3px; }
.engine-summary-status { display: flex; align-items: center; gap: 10px; }
.default-badge, .credential-badge { width: fit-content; padding: 2px 7px; border-radius: 999px; font-size: 10px; }
.default-badge { color: var(--main-700); background: var(--main-50); }
.credential-badge { color: var(--gray-600); background: var(--gray-100); }
.credential-badge.missing { color: var(--red-600); background: var(--red-50); }
.engine-content { padding: 0 14px 16px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px 16px; padding-top: 4px; }
.field { display: flex; flex-direction: column; gap: 6px; }
.full-field { grid-column: 1 / -1; }
.engine-actions { justify-content: flex-end; margin-top: 16px; }
@media (max-width: 680px) { .form-grid { grid-template-columns: 1fr; } .full-field { grid-column: auto; } .default-panel { align-items: flex-start; flex-direction: column; } .default-select { width: 100%; } }
</style>
