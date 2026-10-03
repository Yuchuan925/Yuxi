<template>
  <component
    :is="currentRenderer"
    v-if="currentRenderer"
    :tool-call="toolCall"
    :appearance="appearance"
    :default-expanded="defaultExpanded"
    ref="toolRendererRef"
  />
  <BaseToolCall
    v-else-if="!isHidden"
    :tool-call="toolCall"
    :appearance="appearance"
    :default-expanded="defaultExpanded"
  />
</template>

<script setup>
import { computed, defineAsyncComponent, ref } from 'vue'
import BaseToolCall from './BaseToolCall.vue'
import ToolRendererUnavailable from './ToolRendererUnavailable.vue'
import { getToolCallId, isHiddenToolCall } from './toolRegistry'

const createRenderer = (loader) => defineAsyncComponent({
  loader,
  loadingComponent: BaseToolCall,
  errorComponent: ToolRendererUnavailable,
  delay: 0,
  timeout: 10000,
  suspensible: false
})

const WebSearchTool = createRenderer(() => import('./renderers/WebSearchTool.vue'))
const ListKbsTool = createRenderer(() => import('./renderers/ListKbsTool.vue'))
const QueryKbTool = createRenderer(() => import('./renderers/QueryKbTool.vue'))
const FindKbDocumentTool = createRenderer(() => import('./renderers/FindKbDocumentTool.vue'))
const OpenKbDocumentTool = createRenderer(() => import('./renderers/OpenKbDocumentTool.vue'))
const CalculatorTool = createRenderer(() => import('./renderers/CalculatorTool.vue'))
const TodoListTool = createRenderer(() => import('./renderers/TodoListTool.vue'))
const TaskTool = createRenderer(() => import('./renderers/TaskTool.vue'))
const SubagentLifecycleTool = createRenderer(() => import('./renderers/SubagentLifecycleTool.vue'))
const ImageTool = createRenderer(() => import('./renderers/ImageTool.vue'))
const WriteFileTool = createRenderer(() => import('./renderers/WriteFileTool.vue'))
const ReadFileTool = createRenderer(() => import('./renderers/ReadFileTool.vue'))
const ListDirectoryTool = createRenderer(() => import('./renderers/ListDirectoryTool.vue'))
const SearchFileContentTool = createRenderer(() => import('./renderers/SearchFileContentTool.vue'))
const SearchFileTool = createRenderer(() => import('./renderers/SearchFileTool.vue'))
const GrepTool = createRenderer(() => import('./renderers/GrepTool.vue'))
const GlobTool = createRenderer(() => import('./renderers/GlobTool.vue'))
const EditFileTool = createRenderer(() => import('./renderers/EditFileTool.vue'))
const MysqlQueryTool = createRenderer(() => import('./renderers/MysqlQueryTool.vue'))
const MysqlDescribeTableTool = createRenderer(() => import('./renderers/MysqlDescribeTableTool.vue'))
const MysqlListTablesTool = createRenderer(() => import('./renderers/MysqlListTablesTool.vue'))
const AskUserQuestionTool = createRenderer(() => import('./renderers/AskUserQuestionTool.vue'))
const ExecuteTool = createRenderer(() => import('./renderers/ExecuteTool.vue'))
const OcrParseFileTool = createRenderer(() => import('./renderers/OcrParseFileTool.vue'))
const RememberMemoryTool = createRenderer(() => import('./renderers/RememberMemoryTool.vue'))

const props = defineProps({
  toolCall: {
    type: Object,
    required: true
  },
  appearance: {
    type: String,
    default: 'card'
  },
  defaultExpanded: {
    type: Boolean,
    default: false
  }
})

const toolId = computed(() => getToolCallId(props.toolCall))

const TOOL_RENDERERS = {
  ask_user_question: AskUserQuestionTool,
  bash: ExecuteTool,
  calculator: CalculatorTool,
  cmd: ExecuteTool,
  edit_file: EditFileTool,
  execute: ExecuteTool,
  find_kb_document: FindKbDocumentTool,
  glob: GlobTool,
  grep: GrepTool,
  list_directory: ListDirectoryTool,
  list_kbs: ListKbsTool,
  ls: ListDirectoryTool,
  mysql_describe_table: MysqlDescribeTableTool,
  mysql_list_tables: MysqlListTablesTool,
  mysql_query: MysqlQueryTool,
  ocr_parse_file: OcrParseFileTool,
  open_kb_document: OpenKbDocumentTool,
  query_kb: QueryKbTool,
  read_file: ReadFileTool,
  remember_memory: RememberMemoryTool,
  replace: EditFileTool,
  run_shell_command: ExecuteTool,
  search_file: SearchFileTool,
  search_file_content: SearchFileContentTool,
  subagent_await: SubagentLifecycleTool,
  subagent_cancel: SubagentLifecycleTool,
  subagent_events: SubagentLifecycleTool,
  subagent_start: SubagentLifecycleTool,
  subagent_status: SubagentLifecycleTool,
  task: TaskTool,
  web_search: WebSearchTool,
  tavily_search: WebSearchTool,
  doubao_search: WebSearchTool,
  text_to_img_qwen_image: ImageTool,
  write_file: WriteFileTool,
  write_todos: TodoListTool
}

const currentRenderer = computed(() => TOOL_RENDERERS[toolId.value] || null)
const isHidden = computed(() => isHiddenToolCall(props.toolCall))

const toolRendererRef = ref(null)
const refreshGraph = () => {
  if (toolRendererRef.value && typeof toolRendererRef.value.refreshGraph === 'function') {
    toolRendererRef.value.refreshGraph()
  }
}

defineExpose({ refreshGraph })
</script>

<style lang="less" scoped></style>
