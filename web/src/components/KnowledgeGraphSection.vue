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
        <div v-if="isMilvus" class="graph-build-panel">
          <div class="build-status">
            <a-tag :color="graphBuildStatus?.locked ? 'green' : 'orange'">
              {{ graphBuildStatus?.locked ? '已配置' : '未配置' }}
            </a-tag>
            <span>总 Chunk：{{ graphBuildStatus?.total_chunks ?? '-' }}</span>
            <span>待构建：{{ graphBuildStatus?.pending_chunks ?? '-' }}</span>
            <span>已构建：{{ graphBuildStatus?.indexed_chunks ?? '-' }}</span>
          </div>
          <div class="build-actions">
            <a-button size="small" :loading="graphBuildLoading" @click="loadGraphBuildStatus">刷新状态</a-button>
            <a-button
              v-if="!graphBuildStatus?.locked"
              size="small"
              type="primary"
              @click="showGraphConfig = true"
            >
              配置抽取器
            </a-button>
            <a-button
              v-else
              size="small"
              type="primary"
              :disabled="!graphBuildStatus?.pending_chunks"
              @click="startGraphBuild"
            >
              开始索引
            </a-button>
            <a-button size="small" danger @click="confirmResetGraph">重置</a-button>
          </div>
        </div>
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
                      :is="graph.fetching ? LoadingOutlined : SearchOutlined"
                      @click="onSearch"
                    />
                  </template>
                </a-input>
                <a-button
                  class="action-btn"
                  :icon="h(ReloadOutlined)"
                  :loading="graph.fetching"
                  @click="loadGraph"
                  title="刷新"
                />
              </div>
              <div class="actions-right">
                <a-button
                  class="action-btn"
                  :icon="h(SettingOutlined)"
                  @click="showSettings = true"
                  title="设置"
                />
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
          style="top: 50px; right: 10px"
        />
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

    <!-- 设置模态框 -->
    <a-modal v-model:open="showSettings" title="图谱设置" :footer="null" width="300px">
      <div class="settings-form">
        <a-form layout="vertical">
          <a-form-item label="最大节点数 (limit)">
            <a-input-number
              v-model:value="graphLimit"
              :min="10"
              :max="1000"
              :step="10"
              style="width: 100%"
            />
          </a-form-item>
          <a-form-item label="搜索深度 (depth)">
            <a-input-number
              v-model:value="graphDepth"
              :min="1"
              :max="5"
              :step="1"
              style="width: 100%"
            />
          </a-form-item>
          <a-form-item>
            <a-button type="primary" @click="applySettings" style="width: 100%"> 应用 </a-button>
          </a-form-item>
        </a-form>
      </div>
    </a-modal>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onUnmounted, reactive, h } from 'vue'
import { useDatabaseStore } from '@/stores/database'
import { useTaskerStore } from '@/stores/tasker'
import {
  ReloadOutlined,
  SettingOutlined,
  SearchOutlined,
  LoadingOutlined
} from '@ant-design/icons-vue'
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
const graphLimit = ref(50)
const graphDepth = ref(2)
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
      max_nodes: graphLimit.value,
      max_depth: graphDepth.value
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

.graph-build-panel {
  position: absolute;
  left: 10px;
  right: 10px;
  top: 52px;
  z-index: 5;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--color-trans-light);
  backdrop-filter: blur(4px);
  box-shadow: 0 0 4px 0 var(--shadow-2);

  .build-status,
  .build-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .build-status {
    color: var(--gray-700);
    font-size: 13px;
  }
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
    backdrop-filter: blur(4px);
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
</style>
