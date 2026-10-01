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
import { computed, ref } from 'vue'
import BaseToolCall from './BaseToolCall.vue'

import WebSearchTool from './renderers/WebSearchTool.vue'
import ListKbsTool from './renderers/ListKbsTool.vue'
import QueryKbTool from './renderers/QueryKbTool.vue'
import FindKbDocumentTool from './renderers/FindKbDocumentTool.vue'
import OpenKbDocumentTool from './renderers/OpenKbDocumentTool.vue'
import CalculatorTool from './renderers/CalculatorTool.vue'
import TodoListTool from './renderers/TodoListTool.vue'
import TaskTool from './renderers/TaskTool.vue'
import SubagentLifecycleTool from './renderers/SubagentLifecycleTool.vue'
import ImageTool from './renderers/ImageTool.vue'
import WriteFileTool from './renderers/WriteFileTool.vue'
import ReadFileTool from './renderers/ReadFileTool.vue'
import ListDirectoryTool from './renderers/ListDirectoryTool.vue'
import SearchFileContentTool from './renderers/SearchFileContentTool.vue'
import SearchFileTool from './renderers/SearchFileTool.vue'
import GrepTool from './renderers/GrepTool.vue'
import GlobTool from './renderers/GlobTool.vue'
import EditFileTool from './renderers/EditFileTool.vue'
import MysqlQueryTool from './renderers/MysqlQueryTool.vue'
import MysqlDescribeTableTool from './renderers/MysqlDescribeTableTool.vue'
import MysqlListTablesTool from './renderers/MysqlListTablesTool.vue'
import AskUserQuestionTool from './renderers/AskUserQuestionTool.vue'
import ExecuteTool from './renderers/ExecuteTool.vue'
import OcrParseFileTool from './renderers/OcrParseFileTool.vue'
import RememberMemoryTool from './renderers/RememberMemoryTool.vue'
import { getToolCallId, isHiddenToolCall } from './toolRegistry'

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
