<template>
  <div class="graph-section" v-if="isGraphSupported">
    <div class="graph-container-compact">
      <div v-if="!isGraphSupported" class="graph-disabled">
        <div class="disabled-content">
          <h4>知识图谱不可用</h4>
          <p>当前知识库类型 "{{ kbTypeLabel }}" 不支持知识图谱功能。</p>
          <p>只有 Milvus 类型的知识库支持知识图谱。</p>
        </div>
      </div>
      <div v-else class="graph-wrapper">
        <GraphCanvas
          ref="graphRef"
          :graph-data="graph.graphData"
          @node-click="graph.handleNodeClick"
          @edge-click="graph.handleEdgeClick"
          @canvas-click="graph.handleCanvasClick"
        >
          <template #top>
            <div class="compact-actions">
              <div class="actions-left">
                <a-input
                  v-model:value="searchInput"
                  placeholder="搜索实体"
                  style="width: 240px"
                  @keydown.enter="onSearch"
                  allow-clear
                >
                  <template #suffix>
                    <component
                      :is="graph.fetching ? Loader2 : Search"
                      :size="14"
                      class="search-suffix-icon"
                      @click="onSearch"
                    />
                  </template>
                </a-input>
                <a-button
                  class="action-btn"
                  @click="loadGraph"
                  title="刷新"
                >
                  <RefreshCw :size="16" :class="{ spin: graph.fetching }" />
                </a-button>
              </div>
              <div class="actions-right">
                <a-button
                  v-if="isMilvus"
                  class="action-btn"
                  @click="showBuildPanel = !showBuildPanel; showSettings = false"
                  title="索引管理"
                >
                  <Database :size="16" />
                </a-button>
                <a-button
                  class="action-btn"
                  @click="showSettings = !showSettings; showBuildPanel = false"
                  title="设置"
                >
                  <Settings :size="16" />
                </a-button>
              </div>
            </div>
          </template>
        </GraphCanvas>

        <!-- 详情浮动卡片 -->
        <GraphDetailPanel
          :visible="graph.showDetailDrawer"
          :item="graph.selectedItem"
          :type="graph.selectedItemType"
          @close="graph.handleCanvasClick"
        />

        <!-- 设置浮动面板 -->
        <transition name="slide-fade">
          <div v-if="showSettings" class="floating-panel settings-panel">
            <div class="panel-header">
              <span class="panel-title">图谱设置</span>
            </div>
            <div class="panel-body">
              <a-form layout="vertical">
                <a-form-item label="最大节点数 (limit)">
                  <a-input-number
                    v-model:value="subgraphParams.maxNodes"
                    :min="10"
                    :max="1000"
                    :step="10"
                    style="width: 100%"
                  />
                </a-form-item>
                <a-form-item label="搜索深度 (depth)">
                  <a-input-number
                    v-model:value="subgraphParams.maxDepth"
                    :min="1"
                    :max="5"
                    :step="1"
                    style="width: 100%"
                  />
                </a-form-item>
                <a-form-item label="排除 Chunk 节点">
                  <a-switch v-model:checked="subgraphParams.excludeChunk" />
                </a-form-item>
                <a-form-item>
                  <a-button type="primary" @click="applySettings" style="width: 100%"> 应用 </a-button>
                </a-form-item>
              </a-form>
            </div>
          </div>
        </transition>

        <!-- 索引管理浮动面板 -->
        <transition name="slide-fade">
          <div v-if="isMilvus && showBuildPanel" class="floating-panel build-panel">
            <div class="panel-header">
              <span class="panel-title">索引管理</span>
            </div>
            <div class="panel-body">
              <div class="status-row">
                <span class="status-label">状态</span>
                <a-tag :color="graphBuildStatus?.locked ? 'green' : 'orange'" size="small">
                  {{ graphBuildStatus?.locked ? '已配置' : '未配置' }}
                </a-tag>
              </div>
              <div class="stats-grid">
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.total_chunks ?? '-' }}</span>
                  <span class="stat-label">总 Chunk</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.pending_chunks ?? '-' }}</span>
                  <span class="stat-label">待构建</span>
                </div>
                <div class="stat-item">
                  <span class="stat-value">{{ graphBuildStatus?.indexed_chunks ?? '-' }}</span>
                  <span class="stat-label">已构建</span>
                </div>
              </div>
              <div class="build-actions">
                <a-button
                  v-if="!graphBuildStatus?.locked"
                  type="primary"
                  block
                  @click="showGraphConfig = true"
                >
                  配置抽取器
                </a-button>
                <a-button
                  v-else
                  type="primary"
                  block
                  :disabled="!graphBuildStatus?.pending_chunks"
                  @click="startGraphBuild"
                >
                  开始索引
                </a-button>
                <div class="actions-secondary">
                  <a-button size="small" type="text" :loading="graphBuildLoading" @click="loadGraphBuildStatus">
                    刷新
                  </a-button>
                  <a-button size="small" type="text" danger @click="confirmResetGraph">重置</a-button>
                </div>
              </div>
            </div>
          </div>
        </transition>
      </div>
    </div>

    <a-modal v-model:open="showGraphConfig" title="配置图谱抽取器" width="520px" @ok="configureGraphBuild">
      <a-form layout="vertical">
        <a-form-item label="抽取器类型">
          <a-radio-group v-model:value="graphConfigForm.extractor_type">
            <a-radio-button value="llm">LLM</a-radio-button>
            <a-radio-button value="spacy">spaCy</a-radio-button>
          </a-radio-group>
        </a-form-item>
        <template v-if="graphConfigForm.extractor_type === 'llm'">
          <a-form-item label="模型">
            <ModelSelectorComponent
              :model_spec="graphConfigForm.model_spec"
              placeholder="选择抽取模型"
              @select-model="(spec) => (graphConfigForm.model_spec = spec)"
            />
          </a-form-item>
          <a-form-item label="自定义 Prompt">
            <a-textarea
              v-model:value="graphConfigForm.prompt"
              :rows="6"
              placeholder="留空使用默认抽取 Prompt，可在自定义 Prompt 中加入 schema 约束"
            />
          </a-form-item>
        </template>
        <template v-else>
          <a-form-item label="spaCy 模型">
            <a-input v-model:value="graphConfigForm.spacy_model" placeholder="zh_core_web_sm" />
          </a-form-item>
          <a-form-item label="实体类型过滤">
            <a-input v-model:value="graphConfigForm.entity_labels_text" placeholder="可选，逗号分隔" />
          </a-form-item>
        </template>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onUnmounted, reactive } from 'vue'
import { useDatabaseStore } from '@/stores/database'
import { useTaskerStore } from '@/stores/tasker'
import {
  RefreshCw,
  Settings,
  Search,
  Loader2,
  Database
} from 'lucide-vue-next'
import GraphCanvas from '@/components/GraphCanvas.vue'
import GraphDetailPanel from '@/components/GraphDetailPanel.vue'
import { getKbTypeLabel } from '@/utils/kb_utils'
import { unifiedApi } from '@/apis/graph_api'
import { graphBuildApi } from '@/apis/knowledge_api'
import { Modal, message } from 'ant-design-vue'
import ModelSelectorComponent from '@/components/ModelSelectorComponent.vue'
import { useGraph } from '@/composables/useGraph'

const GRAPH_BUILD_TASK_TYPE = 'knowledge_graph_index'
const MILVUS_KB_TYPE = 'milvus'
const GRAPH_SUPPORTED_KB_TYPES = new Set([MILVUS_KB_TYPE])

const props = defineProps({
  active: {
    type: Boolean,
    default: false
  }
})

const store = useDatabaseStore()
const taskerStore = useTaskerStore()

const databaseId = computed(() => store.databaseId)
const kbType = computed(() => store.database.kb_type)
const kbTypeLabel = computed(() => getKbTypeLabel(kbType.value || 'milvus'))
const isMilvus = computed(() => kbType.value?.toLowerCase() === MILVUS_KB_TYPE)

const graphRef = ref(null)
const showSettings = ref(false)
const showBuildPanel = ref(false)
const subgraphParams = reactive({
  maxNodes: 50,
  maxDepth: 2,
  excludeChunk: false,
})
const searchInput = ref('')
const graphBuildStatus = ref(null)
const graphBuildLoading = ref(false)
const showGraphConfig = ref(false)
const graphConfigForm = reactive({
  extractor_type: 'llm',
  model_spec: '',
  prompt: '',
  spacy_model: 'zh_core_web_sm',
  entity_labels_text: ''
})

const graph = reactive(useGraph(graphRef))

// 计算属性：是否支持知识图谱
const isGraphSupported = computed(() => GRAPH_SUPPORTED_KB_TYPES.has(kbType.value?.toLowerCase()))

let pendingLoadTimer = null
let graphStatusRequestSeq = 0
let graphLoadRequestSeq = 0

const getErrorDetail = (e, fallback) => {
  return e?.response?.data?.detail || e?.response?.data?.message || e?.message || fallback
}

const loadGraphBuildStatus = async () => {
  if (!databaseId.value || !isMilvus.value) return
  const requestSeq = ++graphStatusRequestSeq
  const currentDatabaseId = databaseId.value
  graphBuildLoading.value = true
  try {
    const status = await graphBuildApi.getStatus(currentDatabaseId)
    if (requestSeq === graphStatusRequestSeq && currentDatabaseId === databaseId.value) {
      graphBuildStatus.value = status
    }
  } catch (e) {
    console.error('Failed to load graph build status:', e)
    message.error('加载图谱构建状态失败')
  } finally {
    if (requestSeq === graphStatusRequestSeq) {
      graphBuildLoading.value = false
    }
  }
}

const parseCommaSeparatedValues = (value) => {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

const buildExtractorOptions = () => {
  if (graphConfigForm.extractor_type === 'spacy') {
    return {
      model: graphConfigForm.spacy_model,
      entity_labels: parseCommaSeparatedValues(graphConfigForm.entity_labels_text)
    }
  }

  const result = {
    model_spec: graphConfigForm.model_spec
  }
  if (graphConfigForm.prompt.trim()) {
    result.prompt = graphConfigForm.prompt
  }
  return result
}

const configureGraphBuild = async () => {
  try {
    await graphBuildApi.configure(databaseId.value, {
      extractor_type: graphConfigForm.extractor_type,
      extractor_options: buildExtractorOptions()
    })
    message.success('图谱抽取配置已锁定')
    showGraphConfig.value = false
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to configure graph build:', e)
    message.error(getErrorDetail(e, '配置图谱抽取失败'))
  }
}

const startGraphBuild = async () => {
  try {
    const data = await graphBuildApi.startIndex(databaseId.value, 20)
    message.success(data.message || '图谱构建任务已提交')
    if (data.task_id) {
      taskerStore.registerQueuedTask({
        task_id: data.task_id,
        name: `图谱构建 (${databaseId.value})`,
        task_type: GRAPH_BUILD_TASK_TYPE,
        message: data.message,
        payload: { db_id: databaseId.value }
      })
    }
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to start graph build:', e)
    message.error(getErrorDetail(e, '提交图谱构建任务失败'))
  }
}

const confirmResetGraph = () => {
  Modal.confirm({
    title: '清空并重建图谱',
    content: '将删除该知识库在 Neo4j 中的图谱，重置 Chunk 图谱状态，并清空抽取结果与配置。',
    okText: '确认重置',
    cancelText: '取消',
    onOk: resetGraphBuild
  })
}

const resetGraphBuild = async () => {
  try {
    await graphBuildApi.reset(databaseId.value, {
      clear_extraction_result: true,
      clear_config: true
    })
    message.success('图谱构建状态已重置')
    graph.clearGraph()
    await loadGraphBuildStatus()
  } catch (e) {
    console.error('Failed to reset graph build:', e)
    message.error(getErrorDetail(e, '重置图谱构建状态失败'))
  }
}

const loadGraph = async () => {
  if (!databaseId.value || !isGraphSupported.value) return

  const requestSeq = ++graphLoadRequestSeq
  const currentDatabaseId = databaseId.value
  graph.fetching = true
  try {
    const res = await unifiedApi.getSubgraph({
      db_id: currentDatabaseId,
      node_label: searchInput.value || '*',
      max_nodes: subgraphParams.maxNodes,
      max_depth: subgraphParams.maxDepth,
      exclude_chunk: subgraphParams.excludeChunk,
    })

    if (requestSeq === graphLoadRequestSeq && currentDatabaseId === databaseId.value && res.success && res.data) {
      graph.updateGraphData(res.data.nodes, res.data.edges)
    }
  } catch (e) {
    console.error('Failed to load graph:', e)
    message.error('加载图谱失败')
  } finally {
    if (requestSeq === graphLoadRequestSeq) {
      graph.fetching = false
    }
  }
}

const applySettings = () => {
  showSettings.value = false
  loadGraph()
}

const onSearch = () => {
  loadGraph()
}

const scheduleGraphLoad = (delay = 200) => {
  if (!props.active || !isGraphSupported.value || !databaseId.value) {
    return
  }

  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
  }
  pendingLoadTimer = setTimeout(async () => {
    pendingLoadTimer = null
    await nextTick()
    if (props.active && isGraphSupported.value && databaseId.value) {
      await loadGraph()
    }
  }, delay)
}

watch(
  () => props.active,
  (active) => {
    if (active) {
      if (isMilvus.value) {
        loadGraphBuildStatus()
      }
      scheduleGraphLoad()
    }
  },
  { immediate: true }
)

watch(databaseId, () => {
  graphStatusRequestSeq += 1
  graphLoadRequestSeq += 1
  graph.clearGraph()
  graphBuildStatus.value = null
  if (isMilvus.value) {
    loadGraphBuildStatus()
  }
  if (isGraphSupported.value) {
    scheduleGraphLoad(300)
  }
})

watch(isGraphSupported, (supported) => {
  if (!supported) {
    graph.clearGraph()
    graphBuildStatus.value = null
    return
  }
  if (isMilvus.value) {
    loadGraphBuildStatus()
  }
  scheduleGraphLoad(200)
})

onUnmounted(() => {
  if (pendingLoadTimer) {
    clearTimeout(pendingLoadTimer)
    pendingLoadTimer = null
  }
})
</script>

<style scoped lang="less">
.graph-section {
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  position: relative;
}

.graph-container-compact {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

.graph-wrapper {
  height: 100%;
  width: 100%;
  position: relative;
}

.compact-actions {
  position: absolute;
  top: 10px;
  left: 10px;
  right: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  pointer-events: none; /* Let clicks pass through empty areas */

  .actions-left,
  .actions-right {
    pointer-events: auto; /* Re-enable clicks for buttons/inputs */
    display: flex;
    align-items: center;
    gap: 4px;
    background: var(--color-trans-light);
    backdrop-filter: blur(12px);
    padding: 2px;
    border-radius: 8px;
    box-shadow: 0 0 4px 0px var(--shadow-2);
  }

  :deep(.ant-input-affix-wrapper) {
    padding: 4px 11px;
    border-radius: 6px;
    border-color: transparent;
    box-shadow: none;
    background: var(--color-trans-light);

    &:hover,
    &:focus,
    &-focused {
      background: var(--main-0);
      border-color: var(--primary-color);
    }

    input {
      background: transparent;
    }
  }

  .action-btn {
    width: 32px;
    height: 32px;
    padding: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    background: transparent;
    color: var(--gray-600);
    border-radius: 6px;
    box-shadow: none;

    &:hover {
      background: var(--shadow-1);
      color: var(--primary-color);
    }
  }

  .search-suffix-icon {
    cursor: pointer;
  }

  .spin {
    animation: spin 1s linear infinite;
  }
}

.graph-disabled {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
}

.disabled-content {
  text-align: center;
  color: var(--gray-400);

  h4 {
    margin-bottom: 8px;
  }
}

.floating-panel {
  position: absolute;
  top: 50px;
  right: 10px;
  width: 300px;
  max-height: calc(100% - 60px);
  overflow-y: auto;
  z-index: 100;
  background: var(--color-trans-light);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-radius: 8px;
  border: 1px solid var(--gray-100);
  font-size: 13px;

  .panel-header {
    display: flex;
    align-items: center;
    padding: 10px 14px;
    border-bottom: 1px solid var(--gray-200);

    .panel-title {
      font-size: 13px;
      font-weight: 600;
      color: var(--gray-1000);
    }
  }

  .panel-body {
    padding: 10px 14px;
  }
}

.build-panel {
  .status-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;

    .status-label {
      color: var(--gray-600);
      font-size: 12px;
    }
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-bottom: 12px;
  }

  .stat-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 6px 4px;
    border-radius: 4px;
    background: var(--gray-50);

    .stat-value {
      font-size: 15px;
      font-weight: 600;
      color: var(--gray-1000);
      line-height: 1.2;
    }

    .stat-label {
      font-size: 11px;
      color: var(--gray-500);
      margin-top: 2px;
    }
  }

  .build-actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .actions-secondary {
    display: flex;
    justify-content: space-between;
  }
}

.slide-fade-enter-active {
  transition: all 0.25s ease-out;
}

.slide-fade-leave-active {
  transition: all 0.2s cubic-bezier(1, 0.5, 0.8, 1);
}

.slide-fade-enter-from,
.slide-fade-leave-to {
  transform: translateX(20px);
  opacity: 0;
}
</style>
