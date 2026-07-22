<template>
  <section class="ocr-settings-section" aria-labelledby="ocr-settings-title">
    <header class="header-section">
      <div class="header-content">
        <div id="ocr-settings-title" class="section-title">OCR 引擎配置</div>
        <p class="section-description">
          选择默认识别方式，并配置各引擎的连接信息与任务参数。
        </p>
      </div>
      <a-button :loading="loading" aria-label="刷新 OCR 配置" @click="loadConfigs()">
        刷新
      </a-button>
    </header>

    <div class="default-panel">
      <div>
        <div class="default-label">默认 OCR 引擎</div>
        <div class="default-description">上传和临时附件未指定 OCR 时使用此配置</div>
      </div>
      <a-select
        :value="defaultEngine"
        :loading="Boolean(savingEngine)"
        class="default-select"
        aria-label="选择默认 OCR 引擎"
        @change="setDefaultEngine"
      >
        <a-select-option
          v-for="engine in enabledConfigs"
          :key="engine.engine_id"
          :value="engine.engine_id"
        >
          {{ defaultOptionLabel(engine) }}
        </a-select-option>
      </a-select>
    </div>

    <a-spin :spinning="loading">
      <div class="engine-list">
        <section
          v-for="engine in configurableEngines"
          :key="engine.engine_id"
          :class="['engine-card', { expanded: expandedEngineId === engine.engine_id }]"
        >
          <header class="engine-summary-row">
            <button
              type="button"
              class="engine-summary"
              :aria-expanded="expandedEngineId === engine.engine_id"
              :aria-controls="`ocr-engine-${engine.engine_id}`"
              @click="toggleEngine(engine.engine_id)"
            >
              <ChevronDown :size="16" class="expand-icon" aria-hidden="true" />
              <span class="engine-copy">
                <span class="engine-name-row">
                  <strong>{{ engine.display_name }}</strong>
                  <span v-if="engine.is_default" class="default-badge">默认</span>
                </span>
                <span class="engine-meta">{{ engineDescription(engine.engine_id) }}</span>
              </span>
            </button>

            <div class="engine-summary-status">
              <span
                v-if="supportsCredential(engine.engine_id) && engine.credential_status === 'missing'"
                :class="['credential-badge', engine.credential_status]"
              >
                {{ credentialStatusText(engine.credential_status) }}
              </span>
              <a-switch
                size="small"
                :checked="engine.enabled"
                :loading="savingEngine === engine.engine_id"
                :disabled="engine.is_default"
                :title="engine.is_default ? '默认引擎不能停用，请先更换默认引擎' : undefined"
                :aria-label="`${engine.display_name}${engine.enabled ? '已启用' : '已停用'}`"
                @change="setEngineEnabled(engine, $event)"
              />
            </div>
          </header>

          <div
            v-if="expandedEngineId === engine.engine_id"
            :id="`ocr-engine-${engine.engine_id}`"
            class="engine-content"
          >
            <section
              v-if="supportsEndpoint(engine.engine_id) || supportsCredential(engine.engine_id)"
              class="form-section"
            >
              <div class="form-section-heading">
                <div>
                  <h4>连接与凭证</h4>
                  <p>连接信息只在运行时使用，不会写入文件处理快照。</p>
                </div>
                <span
                  v-if="supportsCredential(engine.engine_id)"
                  :class="['credential-badge', engine.credential_status]"
                >
                  <ShieldCheck :size="13" />
                  {{ credentialStatusText(engine.credential_status) }}
                </span>
              </div>

              <div class="form-grid">
                <label v-if="supportsEndpoint(engine.engine_id)" class="field full-field">
                  <span class="field-label">服务端点</span>
                  <a-input
                    v-model:value="engine.endpoint"
                    :placeholder="endpointExample(engine.engine_id)"
                    allow-clear
                  />
                  <small>填写自托管 OCR 服务的访问地址，例如 {{ endpointExample(engine.engine_id) }}。</small>
                </label>

                <template v-if="supportsCredential(engine.engine_id)">
                  <label class="field">
                    <span class="field-label">凭证来源</span>
                    <a-select
                      :value="engine.credential_source"
                      :disabled="fixedCredentialSource(engine.engine_id)"
                      @change="updateCredentialSource(engine, $event)"
                    >
                      <a-select-option
                        v-for="option in credentialOptions(engine.engine_id)"
                        :key="option.value"
                        :value="option.value"
                      >
                        {{ option.label }}
                      </a-select-option>
                    </a-select>
                    <small v-if="fixedCredentialSource(engine.engine_id)">
                      DeepSeek OCR 固定复用模型供应商凭证，不支持切换。
                    </small>
                  </label>
                  <label v-if="engine.credential_source === 'database'" class="field">
                    <span class="field-label">API 密钥</span>
                    <a-input-password
                      v-model:value="engine.credential_value"
                      :placeholder="databaseCredentialConfigured(engine) ? '已保存，留空则不修改' : '请输入 API 密钥'"
                      autocomplete="new-password"
                    />
                    <small>密钥保存后不会再次显示；需要变更时输入新密钥覆盖。</small>
                  </label>
                  <label v-else class="field">
                    <span class="field-label">{{ credentialReferenceLabel(engine.credential_source) }}</span>
                    <a-input :value="engine.credential_ref" readonly />
                    <small>{{ credentialReferenceHelp(engine.credential_source) }}</small>
                  </label>
                </template>
              </div>
            </section>

            <section class="form-section">
              <div class="form-section-heading">
                <div>
                  <h4>识别参数</h4>
                  <p>作为新任务默认值，文件级参数仍可覆盖。</p>
                </div>
              </div>

              <div class="form-grid">
                <label
                  v-for="field in parameterFields[engine.engine_id] || []"
                  :key="field.key"
                  :class="['field', { 'switch-field': field.type === 'boolean' }]"
                >
                  <span class="field-copy">
                    <span class="field-label-row">
                      <span class="field-label">{{ field.label }}</span>
                      <a-tooltip v-if="field.help" :title="field.help">
                        <button
                          type="button"
                          class="field-help"
                          :aria-label="`${field.label}说明`"
                        >
                          <CircleHelp :size="13" aria-hidden="true" />
                        </button>
                      </a-tooltip>
                    </span>
                  </span>
                  <a-switch
                    v-if="field.type === 'boolean'"
                    size="small"
                    :checked="engine.default_params[field.key]"
                    :aria-label="field.label"
                    @change="engine.default_params[field.key] = $event"
                  />
                  <a-input-number
                    v-else-if="field.type === 'number'"
                    v-model:value="engine.default_params[field.key]"
                    :min="field.min"
                    :max="field.max"
                    :step="field.step || 1"
                    class="full-width"
                  />
                  <a-input
                    v-else
                    :value="displayParam(engine, field)"
                    @update:value="updateParam(engine, field, $event)"
                  />
                </label>
              </div>
            </section>

            <footer class="engine-actions">
              <div
                v-if="healthResults[engine.engine_id]"
                :class="['health-result', healthResults[engine.engine_id].status]"
                role="status"
                aria-live="polite"
              >
                <CircleCheck v-if="healthResults[engine.engine_id].status === 'success'" :size="15" />
                <TriangleAlert v-else :size="15" />
                <span>{{ healthResults[engine.engine_id].message }}</span>
              </div>
              <div class="action-buttons">
                <a-button
                  :loading="checkingEngine === engine.engine_id"
                  @click="checkHealth(engine)"
                >
                  检测连接
                </a-button>
                <a-button
                  type="primary"
                  :loading="savingEngine === engine.engine_id"
                  @click="saveEngine(engine)"
                >
                  保存配置
                </a-button>
              </div>
            </footer>
          </div>
        </section>
      </div>
    </a-spin>
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ChevronDown, CircleCheck, CircleHelp, ShieldCheck, TriangleAlert } from 'lucide-vue-next'
import { ocrApi } from '@/apis/system_api'

const configs = ref([])
const expandedEngineId = ref('')
const loading = ref(false)
const savingEngine = ref('')
const checkingEngine = ref('')
const healthResults = ref({})

const parameterFields = {
  rapid_ocr: [
    {
      key: 'det_box_thresh',
      label: '文本检测置信度阈值',
      help: '值越高越严格，可减少误检，但可能漏掉浅色或模糊文字。范围 0–1。',
      type: 'number',
      min: 0,
      max: 1,
      step: 0.05
    },
    {
      key: 'zoom_x',
      label: 'PDF 横向渲染倍率',
      help: '提高倍率可改善小字识别，但会增加内存占用和处理时间。',
      type: 'number',
      min: 0.1,
      max: 10,
      step: 0.1
    },
    {
      key: 'zoom_y',
      label: 'PDF 纵向渲染倍率',
      help: '提高倍率可改善小字识别，但会增加内存占用和处理时间。',
      type: 'number',
      min: 0.1,
      max: 10,
      step: 0.1
    }
  ],
  mineru_ocr: [
    { key: 'timeout_seconds', label: '请求超时时间（秒）', type: 'number', min: 1, max: 7200 },
    {
      key: 'lang_list',
      label: '文档语言',
      help: '使用 MinerU 语言代码，多个值用逗号分隔，例如 ch, en。',
      type: 'list'
    },
    { key: 'formula_enable', label: '公式识别', type: 'boolean' },
    { key: 'table_enable', label: '表格识别', type: 'boolean' },
    { key: 'image_analysis', label: '图片与图表分析', type: 'boolean' }
  ],
  mineru_official: [
    {
      key: 'max_wait_seconds',
      label: '任务最长等待时间（秒）',
      help: '单个识别任务允许等待云端返回结果的最长时间。',
      type: 'number',
      min: 1,
      max: 7200
    },
    {
      key: 'poll_interval_seconds',
      label: '结果轮询间隔（秒）',
      help: '检查任务是否完成的时间间隔；设置过短会增加请求次数。',
      type: 'number',
      min: 0.1,
      max: 60,
      step: 0.5
    },
    {
      key: 'language',
      label: '文档语言',
      help: '使用 MinerU 支持的语言代码，例如 ch、en。',
      type: 'text'
    },
    {
      key: 'is_ocr',
      label: '强制 OCR',
      help: '关闭时优先使用文档已有文本层；扫描件通常需要开启。',
      type: 'boolean'
    },
    {
      key: 'enable_formula',
      label: '公式识别',
      help: '识别并保留文档中的数学公式。',
      type: 'boolean'
    },
    {
      key: 'enable_table',
      label: '表格识别',
      help: '识别表格结构并输出为可读格式。',
      type: 'boolean'
    }
  ],
  pp_structure_v3_ocr: [
    { key: 'timeout_seconds', label: '请求超时时间（秒）', type: 'number', min: 1, max: 3600 },
    { key: 'use_table_recognition', label: '表格识别', type: 'boolean' },
    { key: 'use_formula_recognition', label: '公式识别', type: 'boolean' },
    { key: 'use_seal_recognition', label: '印章识别', type: 'boolean' }
  ],
  deepseek_ocr: [
    {
      key: 'pdf_dpi',
      label: 'PDF 渲染清晰度（DPI）',
      help: '数值越高，小字更清晰，但上传体积、耗时和内存占用也会增加。',
      type: 'number',
      min: 72,
      max: 600
    },
    {
      key: 'max_tokens',
      label: '单次识别最大输出 Token',
      help: '限制单次识别可返回的文本长度；设置过小可能导致结果被截断。',
      type: 'number',
      min: 1,
      max: 32768
    },
    {
      key: 'temperature',
      label: '生成随机度',
      help: '文档识别建议保持较低值，以获得更稳定的结果。',
      type: 'number',
      min: 0,
      max: 2,
      step: 0.1
    },
    {
      key: 'timeout_seconds',
      label: '请求超时时间（秒）',
      help: '超过该时间仍未完成时终止本次识别请求。',
      type: 'number',
      min: 1,
      max: 1800
    }
  ],
  paddleocr_vl_1_6: [
    { key: 'poll_interval_seconds', label: '结果轮询间隔（秒）', type: 'number', min: 0.1, max: 60, step: 0.5 },
    { key: 'max_wait_seconds', label: '任务最长等待时间（秒）', type: 'number', min: 1, max: 7200 },
    { key: 'useDocOrientationClassify', label: '文档方向分类', type: 'boolean' },
    { key: 'useDocUnwarping', label: '文档形变矫正', type: 'boolean' },
    { key: 'useChartRecognition', label: '图表识别', type: 'boolean' }
  ],
  paddleocr_pp_ocrv6: [
    { key: 'poll_interval_seconds', label: '结果轮询间隔（秒）', type: 'number', min: 0.1, max: 60, step: 0.5 },
    { key: 'max_wait_seconds', label: '任务最长等待时间（秒）', type: 'number', min: 1, max: 7200 },
    { key: 'useDocOrientationClassify', label: '文档方向分类', type: 'boolean' },
    { key: 'useDocUnwarping', label: '文档形变矫正', type: 'boolean' },
    { key: 'useTextlineOrientation', label: '文本行方向识别', type: 'boolean' }
  ]
}

const credentialSourceOptions = {
  environment: '环境变量',
  database: '页面填写并保存',
  model_provider: '模型供应商凭证'
}

const engineDescriptions = {
  rapid_ocr: '本地运行，无需服务配置，适合隐私优先和常规文档',
  mineru_ocr: '连接自托管 MinerU 服务，适合私有部署',
  mineru_official: '使用 MinerU 官方云服务，需要 API 凭证',
  pp_structure_v3_ocr: '连接自托管 PaddleX 服务，适合版面与表格分析',
  deepseek_ocr: '使用云端模型解析文档，复用模型供应商凭证',
  paddleocr_vl_1_6: '使用 PaddleOCR 云服务理解复杂文档，需要 API Token',
  paddleocr_pp_ocrv6: '使用 PaddleOCR 云服务识别文字，需要 API Token'
}

const configurableEngines = computed(() =>
  configs.value.filter((engine) => engine.engine_id !== 'disable')
)
const enabledConfigs = computed(() =>
  configs.value.filter((engine) => engine.engine_id === 'disable' || engine.enabled)
)
const defaultEngine = computed(() => configs.value.find((engine) => engine.is_default)?.engine_id)
const endpointExamples = {
  mineru_ocr: 'http://mineru:30001',
  pp_structure_v3_ocr: 'http://paddlex:8080'
}
// 引擎能力由管理员接口下发，前端只保留展示文案和端点示例。
const findEngine = (engineId) => configs.value.find((engine) => engine.engine_id === engineId)
const supportsEndpoint = (engineId) => Boolean(findEngine(engineId)?.endpoint_editable)
const endpointExample = (engineId) => endpointExamples[engineId]
const supportsCredential = (engineId) => Boolean(findEngine(engineId)?.credential_sources?.length)
const credentialOptions = (engineId) =>
  (findEngine(engineId)?.credential_sources || []).map((source) => ({
    value: source,
    label: credentialSourceOptions[source]
  }))
const fixedCredentialSource = (engineId) => Boolean(findEngine(engineId)?.credential_source_fixed)
const databaseCredentialConfigured = (engine) =>
  engine.credential_statuses?.database === 'configured'
const defaultOptionLabel = (engine) =>
  engine.engine_id === 'disable' ? '不使用 OCR（仅提取文本层）' : engine.display_name
const engineDescription = (engineId) => engineDescriptions[engineId] || '配置此 OCR 引擎的连接与识别参数'

const toggleEngine = (engineId) => {
  expandedEngineId.value = expandedEngineId.value === engineId ? '' : engineId
}

const loadConfigs = async (silent = false) => {
  if (!silent) loading.value = true
  try {
    const data = await ocrApi.getConfigs()
    configs.value = (data.configs || []).map((engine) => ({
      ...engine,
      credential_value: '',
      default_params: { ...(engine.default_params || {}) }
    }))
    const availableEngines = configs.value.filter((engine) => engine.engine_id !== 'disable')
    if (!availableEngines.some((engine) => engine.engine_id === expandedEngineId.value)) {
      expandedEngineId.value =
        availableEngines.find((engine) => engine.is_default)?.engine_id ||
        availableEngines[0]?.engine_id ||
        ''
    }
  } catch (error) {
    message.error(error.message || '加载 OCR 配置失败')
  } finally {
    if (!silent) loading.value = false
  }
}

const saveEngine = async (engine, overrides = {}) => {
  savingEngine.value = engine.engine_id
  try {
    const payload = {
      enabled: engine.enabled,
      is_default: engine.is_default,
      credential_source: engine.credential_source || null,
      default_params: engine.default_params,
      ...overrides
    }
    if (supportsEndpoint(engine.engine_id)) payload.endpoint = engine.endpoint || null
    if (engine.credential_source === 'database' && engine.credential_value) {
      payload.credential_value = engine.credential_value
    }
    await ocrApi.updateConfig(engine.engine_id, payload)
    message.success(`${engine.display_name} 配置已保存`)
    await loadConfigs(true)
    return true
  } catch (error) {
    message.error(error.message || '保存 OCR 配置失败')
    return false
  } finally {
    savingEngine.value = ''
  }
}

const setEngineEnabled = async (engine, enabled) => {
  const previousEnabled = engine.enabled
  engine.enabled = enabled
  expandedEngineId.value = engine.engine_id

  const saved = await saveEngine(engine, { enabled })
  if (!saved) engine.enabled = previousEnabled
}

const setDefaultEngine = async (engineId) => {
  const engine = configs.value.find((item) => item.engine_id === engineId)
  if (engine) {
    expandedEngineId.value = engineId === 'disable' ? '' : engineId
    await saveEngine(engine, { is_default: true, enabled: true })
  }
}

const checkHealth = async (engine) => {
  checkingEngine.value = engine.engine_id
  try {
    const result = await ocrApi.checkHealth(engine.engine_id)
    healthResults.value[engine.engine_id] = {
      status: ['healthy', 'configured', 'ok'].includes(result.status) ? 'success' : 'warning',
      message: result.message || result.status || '检查完成'
    }
  } catch (error) {
    healthResults.value[engine.engine_id] = {
      status: 'error',
      message: error.message || '健康检查失败，请检查端点与凭证配置'
    }
  } finally {
    checkingEngine.value = ''
  }
}

const updateCredentialSource = (engine, source) => {
  engine.credential_source = source
  engine.credential_ref = engine.credential_refs?.[source] || null
  engine.credential_value = ''
  engine.credential_status = engine.credential_statuses?.[source] || 'missing'
}

const displayParam = (engine, field) => {
  const value = engine.default_params[field.key]
  return field.type === 'list' && Array.isArray(value) ? value.join(', ') : value
}

const updateParam = (engine, field, value) => {
  engine.default_params[field.key] =
    field.type === 'list'
      ? String(value)
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean)
      : value
}

const credentialStatusText = (status) =>
  ({ configured: '凭证已配置', missing: '凭证缺失', not_required: '无需凭证' })[status] || '状态未知'

const credentialReferenceLabel = (source) =>
  source === 'model_provider' ? '模型供应商' : '环境变量名称'

const credentialReferenceHelp = (source) =>
  source === 'model_provider'
    ? '运行时复用该模型供应商中已配置的 API 密钥。'
    : '运行时读取服务器环境变量，数据库不保存密钥。'

onMounted(() => loadConfigs())
</script>

<style scoped lang="less">
.ocr-settings-section {
  display: flex;
  flex-direction: column;
  color: var(--color-text);
}

.header-section,
.default-panel,
.engine-summary-row,
.form-section-heading,
.engine-actions,
.action-buttons,
.health-result,
.credential-badge,
.engine-name-row {
  display: flex;
  align-items: center;
}

.header-section,
.default-panel,
.engine-summary-row,
.form-section-heading,
.engine-actions {
  justify-content: space-between;
}

.header-section {
  align-items: flex-end;
  gap: 16px;
  margin-bottom: 16px;
}

.form-section-heading h4 {
  margin: 0;
  color: var(--color-text);
}

.header-content {
  min-width: 0;
  flex: 1;
}

.section-title {
  margin: 12px 0;
  color: var(--gray-900);
  font-size: 16px;
  font-weight: 500;
  line-height: 1.4;
}

.section-description {
  margin: 0;
  color: var(--gray-600);
  font-size: 14px;
  line-height: 1.4;
}

.form-section-heading p {
  margin: 4px 0 0;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.5;
}

.default-panel {
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  gap: 20px;
  margin-bottom: 16px;
  padding: 12px 16px;
  background: var(--gray-25);
}

.default-label {
  font-size: 13px;
  font-weight: 500;
}

.default-description {
  margin-top: 2px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.default-select {
  width: min(300px, 48%);
}

.engine-list {
  display: flex;
  overflow: hidden;
  flex-direction: column;
  border: 1px solid var(--gray-150);
  border-radius: 8px;
  background: var(--gray-0);
}

.engine-card {
  overflow: hidden;
  background: var(--gray-0);
  transition: background-color 160ms ease;
}

.engine-card + .engine-card {
  border-top: 1px solid var(--gray-100);
}

.engine-summary-row {
  min-height: 64px;
  gap: 12px;
  padding: 0 14px 0 8px;
}

.engine-summary {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 10px;
  align-self: stretch;
  padding: 10px 8px;
  border: 0;
  color: var(--color-text);
  text-align: left;
  background: transparent;
  cursor: pointer;
}

.engine-summary:focus-visible {
  outline: 2px solid var(--main-500);
  outline-offset: -2px;
}

.expand-icon {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  color: var(--gray-500);
  transition: transform 160ms ease;
}

.engine-card.expanded .expand-icon {
  transform: rotate(180deg);
}

.engine-copy {
  min-width: 0;
}

.engine-name-row {
  gap: 7px;
}

.engine-name-row strong {
  overflow: hidden;
  font-size: 14px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.engine-meta {
  display: block;
  margin-top: 3px;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.4;
}

.default-badge,
.credential-badge {
  width: fit-content;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 500;
}

.default-badge {
  padding: 1px 6px;
  color: var(--main-700);
  background: var(--main-50);
}

.engine-summary-status {
  display: flex;
  min-width: 132px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}

.credential-badge {
  gap: 4px;
  padding: 2px 7px;
  color: var(--gray-600);
  background: var(--gray-100);
}

.credential-badge.configured {
  color: var(--color-success-700);
  background: var(--color-success-50);
}

.credential-badge.missing {
  color: var(--color-warning-900);
  background: var(--color-warning-50);
}

.engine-content {
  padding: 0 16px 14px;
  border-top: 1px solid var(--gray-100);
  background: var(--gray-0);
}

.form-section {
  padding: 16px 0 2px;
}

.form-section + .form-section {
  margin-top: 14px;
  border-top: 1px solid var(--gray-100);
}

.form-section-heading {
  gap: 16px;
  margin-bottom: 12px;
}

.form-section-heading h4 {
  font-size: 13px;
  font-weight: 600;
}

.form-section-heading p {
  margin-top: 2px;
  font-size: 11px;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 14px;
}

.field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
}

.field-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
}

.field-label-row {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 5px;
}

.field-label {
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

.field-help {
  display: inline-flex;
  width: 18px;
  height: 18px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  color: var(--gray-400);
  background: transparent;
  cursor: help;
}

.field-help:hover,
.field-help:focus-visible {
  color: var(--gray-600);
}

.field-help:focus-visible {
  border-radius: 3px;
  outline: 2px solid var(--main-500);
  outline-offset: 1px;
}

.field small {
  color: var(--color-text-tertiary);
  font-size: 11px;
  line-height: 1.45;
}

.full-field {
  grid-column: 1 / -1;
}

.full-width {
  width: 100%;
}

.switch-field {
  height: 46px;
  min-height: 46px;
  align-self: end;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  padding: 0 10px;
  border: 1px solid var(--gray-150);
  border-radius: 6px;
  background: var(--gray-25);
}

.engine-actions {
  gap: 16px;
  margin: 16px -16px -14px;
  padding: 12px 16px;
  border-top: 1px solid var(--gray-100);
  background: var(--gray-25);
}

.action-buttons,
.health-result {
  gap: 8px;
}

.action-buttons {
  margin-left: auto;
}

:deep(.ant-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

.health-result {
  min-width: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.health-result.success {
  color: var(--color-success-700);
}

.health-result.warning,
.health-result.error {
  color: var(--color-warning-900);
}

@media (max-width: 620px) {
  .header-section,
  .default-panel,
  .engine-summary-row,
  .form-section-heading,
  .engine-actions {
    align-items: stretch;
    flex-direction: column;
  }

  .default-select {
    width: 100%;
  }

  .engine-summary-row {
    gap: 0;
    padding: 0 10px 10px;
  }

  .engine-summary-status {
    min-width: 0;
    justify-content: flex-end;
    padding: 0 8px;
  }

  .form-grid {
    grid-template-columns: 1fr;
  }

  .full-field {
    grid-column: auto;
  }

  .action-buttons {
    width: 100%;
    margin-left: 0;
  }

  .action-buttons :deep(.ant-btn) {
    flex: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .engine-card,
  .expand-icon {
    transition: none;
  }
}
</style>
