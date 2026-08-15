<template>
  <div class="home-agent-preview">
    <!-- 窗口头部：复刻智能体对话的标题栏 -->
    <div class="preview-header">
      <span class="agent-avatar"><Bot :size="18" /></span>
      <span class="agent-name">语析助手</span>
      <span class="agent-status"><span class="status-dot"></span>在线</span>
      <span class="demo-tag">演示</span>
    </div>

    <div class="preview-body" ref="bodyRef">
      <div v-for="(message, index) in messages" :key="index" class="message" :class="message.role">
        <span class="message-avatar" v-if="message.role === 'assistant'"><Bot :size="14" /></span>
        <div class="message-content">
          <div class="bubble">
            <template v-if="message.role === 'user'">{{ message.text }}</template>
            <template v-else>
              <p v-for="(paragraph, pIndex) in message.paragraphs" :key="pIndex">
                {{ paragraph }}
              </p>
              <ul v-if="message.bullets?.length">
                <li v-for="(bullet, bIndex) in message.bullets" :key="bIndex">{{ bullet }}</li>
              </ul>
              <!-- 来源 chips：点击展开假来源列表 -->
              <div v-if="message.sources" class="source-chips">
                <button
                  type="button"
                  class="source-chip"
                  :class="{ expanded: expandedSources === index }"
                  @click="toggleSources(index)"
                >
                  <BookOpen :size="12" />
                  {{ message.sources.length }} 个来源
                  <ChevronDown :size="12" class="chip-arrow" :class="{ rotated: expandedSources === index }" />
                </button>
                <div v-if="expandedSources === index" class="source-list">
                  <div v-for="source in message.sources" :key="source.title" class="source-item">
                    <FileText :size="13" />
                    <span class="source-title">{{ source.title }}</span>
                  </div>
                </div>
              </div>
            </template>
          </div>
          <span v-if="message.role === 'assistant' && message.finishedAt" class="message-time">{{
            message.finishedAt
          }}</span>
        </div>
        <span class="message-avatar user-avatar" v-if="message.role === 'user'">我</span>
      </div>

      <!-- 打字指示器 -->
      <div v-if="isTyping" class="message assistant">
        <span class="message-avatar"><Bot :size="14" /></span>
        <div class="message-content">
          <div class="bubble typing-bubble">
            <span class="typing-dots"><i></i><i></i><i></i></span>
          </div>
        </div>
      </div>
    </div>

    <!-- 预设问题：v1 可点击区域之一 -->
    <div class="suggestions">
      <button
        v-for="suggestion in suggestions"
        :key="suggestion"
        type="button"
        class="suggestion-chip"
        :disabled="isTyping"
        @click="askSuggestion(suggestion)"
      >
        {{ suggestion }}
      </button>
    </div>

    <!-- 输入区：复刻真实聊天输入框 -->
    <div class="preview-input-row">
      <input
        v-model="draft"
        class="preview-input"
        type="text"
        placeholder="问点什么？使用 @ 可以提及哦~"
        :disabled="isTyping"
        @keydown.enter.prevent="sendDraft"
      />
      <button
        type="button"
        class="send-button"
        :class="{ enabled: draft.trim() }"
        :disabled="isTyping || !draft.trim()"
        aria-label="发送"
        @click="sendDraft"
      >
        <Send :size="15" />
      </button>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onUnmounted, reactive, ref } from 'vue'
import { BookOpen, Bot, ChevronDown, FileText, Send } from 'lucide-vue-next'

/**
 * 首页 Agent 聊天界面复刻（第一层：静态展示 + 少量交互）。
 *
 * 所有数据均为本地假数据，不请求任何接口；样式变量直接复用 base.css。
 * 后续加深复刻（工具调用、附件、审批等）时只需扩展下方假数据与消息渲染，
 * 不需要改动首页结构。
 */

// 预设问题：v1 仅提供 3 个可点击入口
const suggestions = ['总结我的知识库要点', '帮我生成一份周报', '搜索并整理相关文档']

// 假回复：按问题关键词命中，未命中时使用兜底回复
const cannedReplies = [
  {
    keywords: ['周报', '周'],
    paragraphs: ['本周完成了知识库问答链路的优化与知识图谱构建的并发重试机制。'],
    bullets: ['检索链路：向量与图谱融合召回，长文档分块策略收敛', '图谱构建：失败自动重试，进度持久化可断点恢复', '体验：对话消息补充执行耗时与相对时间展示'],
    sources: [{ title: 'changelog.md' }, { title: 'roadmap.md' }]
  },
  {
    keywords: ['文档', '整理', '搜索'],
    paragraphs: ['已从工作区检索到相关文档，重点包括产品说明、架构说明与知识库接入指南。'],
    bullets: ['ARCHITECTURE.md：系统边界与主要运行链路', '产品文档：RAG 与知识图谱的配置方式', '接入指南：知识库创建、解析与图谱构建流程'],
    sources: [{ title: 'ARCHITECTURE.md' }, { title: '产品手册.md' }, { title: '接入指南.md' }]
  }
]
const fallbackReply = {
  paragraphs: ['我可以帮你处理知识库问答、文档整理与知识图谱构建等任务。'],
  bullets: ['上传文档到知识库，自动完成解析与分块', '构建知识图谱，支持社区聚类与可视化', '对话中自动检索相关来源并展示出处'],
  sources: [{ title: '知识库使用说明.md' }]
}

const messages = ref([
  {
    role: 'user',
    text: '帮我整理知识库产品文档的要点'
  },
  {
    role: 'assistant',
    paragraphs: ['好的，已为你整理知识库产品文档的核心要点：'],
    bullets: ['RAG 与知识图谱融合检索，答案附带可追溯来源', '支持多种文档格式解析与图谱构建', '对话线程支持暂停、恢复与审批等多人协作'],
    sources: [{ title: '产品介绍.md' }, { title: '架构总览.md' }]
  }
])
const draft = ref('')
const isTyping = ref(false)
const expandedSources = ref(-1)
const bodyRef = ref(null)
let typingTimer = null
let streamTimer = null

const toggleSources = (index) => {
  expandedSources.value = expandedSources.value === index ? -1 : index
}

const scrollToBottom = async () => {
  await nextTick()
  if (bodyRef.value) bodyRef.value.scrollTop = bodyRef.value.scrollHeight
}

const pickReply = (text) => {
  const matched = cannedReplies.find((reply) =>
    reply.keywords.some((keyword) => text.includes(keyword))
  )
  return matched || fallbackReply
}

const formatNow = () => {
  const now = new Date()
  const pad = (value) => String(value).padStart(2, '0')
  return `${pad(now.getHours())}:${pad(now.getMinutes())}`
}

const pushUserMessage = (text) => {
  messages.value.push({ role: 'user', text })
  expandedSources.value = -1
}

// 模拟回复：打字指示器 → 分段逐字流式输出 → 结束时间
const streamReply = async (reply) => {
  isTyping.value = true
  await scrollToBottom()
  await new Promise((resolve) => {
    typingTimer = setTimeout(resolve, 700)
  })
  isTyping.value = false

  // 使用 reactive 包装，流式变异经 proxy 触发响应式更新
  const replyMessage = reactive({
    role: 'assistant',
    paragraphs: [],
    bullets: reply.bullets,
    sources: reply.sources
  })
  messages.value.push(replyMessage)

  const allParagraphs = [...reply.paragraphs, ...(reply.bullets || []).map((bullet) => `- ${bullet}`)]
  const fullText = allParagraphs.join('\n')
  let cursor = 0
  await new Promise((resolve) => {
    streamTimer = setInterval(() => {
      cursor += 2
      const chunk = fullText.slice(0, cursor)
      const lines = chunk.split('\n')
      const paragraphLines = []
      const bulletLines = []
      for (const line of lines) {
        if (line.startsWith('- ')) {
          bulletLines.push(line.slice(2))
        } else if (line) {
          paragraphLines.push(line)
        }
      }
      // 流式阶段逐字渲染到单一段落，结束后按结构拆分为段落与列表
      replyMessage.paragraphs = [lines.join('\n')]
      replyMessage.bullets = []
      if (cursor >= fullText.length) {
        replyMessage.paragraphs = paragraphLines
        replyMessage.bullets = bulletLines
        replyMessage.finishedAt = formatNow()
        clearInterval(streamTimer)
        resolve()
      }
    }, 24)
  })
  await scrollToBottom()
}

const askSuggestion = (text) => {
  if (isTyping.value) return
  pushUserMessage(text)
  void streamReply(pickReply(text))
}

const sendDraft = () => {
  const text = draft.value.trim()
  if (!text || isTyping.value) return
  draft.value = ''
  askSuggestion(text)
}

onUnmounted(() => {
  clearTimeout(typingTimer)
  clearInterval(streamTimer)
})
</script>

<style lang="less" scoped>
.home-agent-preview {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  height: 400px;
  max-height: 60vh;
  overflow: hidden;
  border: 1px solid var(--gray-150);
  border-radius: 16px;
  background: color-mix(in srgb, var(--gray-0) 88%, transparent);
  box-shadow: 0 12px 32px -18px var(--shadow-1);
}

.preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-25);

  .agent-avatar {
    width: 28px;
    height: 28px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    color: var(--gray-0);
    background: var(--main-600);
  }

  .agent-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    font-size: 13px;
    font-weight: 600;
    color: var(--gray-900);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .agent-status {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: var(--gray-500);

    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--color-success-500);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-success-500) 18%, transparent);
    }
  }
}

.preview-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  scrollbar-width: thin;

  &::-webkit-scrollbar {
    width: 5px;
  }

  &::-webkit-scrollbar-thumb {
    border-radius: 3px;
    background: var(--gray-200);
  }
}

.message {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  max-width: 100%;

  &.user {
    flex-direction: row-reverse;

    .bubble {
      color: var(--gray-0);
      background: var(--main-600);
    }
  }

  .message-avatar {
    flex: 0 0 24px;
    width: 24px;
    height: 24px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    color: var(--gray-0);
    background: var(--main-600);
    font-size: 11px;
  }

  .user-avatar {
    background: var(--gray-400);
  }

  .message-content {
    min-width: 0;
    max-width: 82%;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .bubble {
    padding: 9px 12px;
    border-radius: 12px;
    font-size: 13px;
    line-height: 1.65;
    word-break: break-word;
    color: var(--gray-800);
    background: var(--gray-50);
    border: 1px solid var(--gray-100);

    p {
      margin: 0 0 6px;
      white-space: pre-line;

      &:last-child {
        margin-bottom: 0;
      }
    }

    ul {
      margin: 6px 0 0;
      padding-left: 18px;

      li {
        margin-bottom: 2px;
      }
    }
  }

  .message-time {
    padding-left: 2px;
    font-size: 11px;
    color: var(--gray-400);
  }
}

.typing-bubble {
  display: inline-flex;
  align-items: center;
}

.typing-dots {
  display: inline-flex;
  gap: 4px;

  i {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--gray-400);
    animation: typingBounce 1.2s ease-in-out infinite;

    &:nth-child(2) {
      animation-delay: 0.15s;
    }

    &:nth-child(3) {
      animation-delay: 0.3s;
    }
  }
}

@keyframes typingBounce {
  0%,
  60%,
  100% {
    transform: translateY(0);
    opacity: 0.5;
  }
  30% {
    transform: translateY(-3px);
    opacity: 1;
  }
}

.source-chips {
  margin-top: 8px;
}

.source-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 8px;
  border: 1px solid var(--main-100);
  border-radius: 999px;
  background: var(--main-30);
  color: var(--main-700);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover,
  &.expanded {
    background: var(--main-50);
  }

  .chip-arrow {
    transition: transform 0.2s ease;

    &.rotated {
      transform: rotate(180deg);
    }
  }
}

.source-list {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.source-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 8px;
  font-size: 12px;
  color: var(--gray-600);
  background: var(--gray-25);

  .source-title {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.demo-tag {
  flex-shrink: 0;
  padding: 2px 7px;
  border: 1px solid var(--main-100);
  border-radius: 999px;
  background: var(--main-30);
  color: var(--main-700);
  font-size: 11px;
  line-height: 1.4;
}

.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 0 14px 10px;
}

.suggestion-chip {
  padding: 5px 10px;
  border: 1px solid var(--gray-150);
  border-radius: 999px;
  background: var(--gray-25);
  color: var(--gray-600);
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s ease;

  &:hover:not(:disabled) {
    border-color: var(--main-200);
    color: var(--main-700);
    background: var(--main-30);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.preview-input-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px 14px;
  border-top: 1px solid var(--gray-100);
  background: var(--gray-25);
}

.preview-input {
  flex: 1;
  min-width: 0;
  height: 34px;
  padding: 0 12px;
  border: 1px solid var(--gray-150);
  border-radius: 10px;
  outline: none;
  background: var(--gray-0);
  color: var(--gray-900);
  font-size: 13px;
  transition: border-color 0.15s ease;

  &::placeholder {
    color: var(--gray-400);
  }

  &:focus {
    border-color: var(--main-300);
  }

  &:disabled {
    opacity: 0.6;
  }
}

.send-button {
  width: 34px;
  height: 34px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 10px;
  background: var(--gray-100);
  color: var(--gray-400);
  cursor: pointer;
  transition: all 0.15s ease;

  &.enabled {
    background: var(--main-600);
    color: var(--gray-0);

    &:hover {
      background: var(--main-700);
    }
  }

  &:disabled {
    cursor: not-allowed;
  }
}
</style>
