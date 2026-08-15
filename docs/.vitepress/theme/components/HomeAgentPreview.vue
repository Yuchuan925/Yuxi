<script setup>
import { nextTick, onUnmounted, reactive, ref } from 'vue'

/**
 * 文档站首页（YuxiHome）hero 区的 Agent 聊天界面复刻（第一层：静态展示 + 少量交互）。
 *
 * 所有数据均为本地假数据，不请求任何接口；颜色变量复用 custom.css 引入的 base.css，
 * 深浅色主题自动适配。文档站无 lucide-vue-next 与 less 依赖，图标使用内联 SVG path，
 * 样式为纯 CSS。后续加深复刻（工具调用、附件、审批等）时只需扩展下方假数据与消息渲染。
 */

// lucide 图标 path（stroke 跟随 currentColor，尺寸由各处 CSS 控制）
const icons = {
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
  bookOpen: '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
  chevronDown: '<path d="m6 9 6 6 6-6"/>',
  fileText: '<path d="M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"/><path d="M14 2v5a1 1 0 0 0 1 1h5"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  send: '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z"/><path d="m21.854 2.147-10.94 10.939"/>'
}

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

<template>
  <div class="home-agent-preview">
    <!-- 窗口头部：复刻智能体对话的标题栏 -->
    <div class="preview-header">
      <span class="agent-avatar">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.bot" />
      </span>
      <span class="agent-name">语析助手</span>
      <span class="agent-status"><span class="status-dot"></span>在线</span>
      <span class="demo-tag">演示</span>
    </div>

    <div class="preview-body" ref="bodyRef">
      <div v-for="(message, index) in messages" :key="index" class="message" :class="message.role">
        <span class="message-avatar" v-if="message.role === 'assistant'">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.bot" />
        </span>
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
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.bookOpen" />
                  {{ message.sources.length }} 个来源
                  <svg class="chip-arrow" :class="{ rotated: expandedSources === index }" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.chevronDown" />
                </button>
                <div v-if="expandedSources === index" class="source-list">
                  <div v-for="source in message.sources" :key="source.title" class="source-item">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.fileText" />
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
        <span class="message-avatar">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.bot" />
        </span>
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
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" v-html="icons.send" />
      </button>
    </div>
  </div>
</template>

<style scoped>
/* hero 区为居中排版，聊天窗口内部恢复左对齐 */
.home-agent-preview {
  position: relative;
  z-index: 1;
  display: flex;
  flex-direction: column;
  height: 440px;
  max-height: 64vh;
  max-width: 780px;
  margin: 0 auto;
  overflow: hidden;
  text-align: left;
  border: 1px solid var(--gray-150);
  border-radius: 16px;
  background: color-mix(in srgb, var(--gray-0) 88%, transparent);
  box-shadow: 0 24px 60px -28px rgba(0, 0, 0, 0.25);
  font-size: 14px;
}

/* ===== 窗口头部 ===== */
.preview-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--gray-100);
  background: var(--gray-25);
}

.preview-header .agent-avatar {
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  color: var(--gray-0);
  background: var(--main-600);
}

.preview-header .agent-avatar svg {
  width: 18px;
  height: 18px;
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
}

.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-success-500);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-success-500) 18%, transparent);
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

/* ===== 消息区 ===== */
.preview-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  scrollbar-width: thin;
}

.preview-body::-webkit-scrollbar {
  width: 5px;
}

.preview-body::-webkit-scrollbar-thumb {
  border-radius: 3px;
  background: var(--gray-200);
}

.message {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  max-width: 100%;
}

.message.user {
  flex-direction: row-reverse;
}

.message.user .bubble {
  color: var(--gray-0);
  background: var(--main-600);
  border-color: var(--main-600);
}

.message .message-avatar {
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

.message .message-avatar svg {
  width: 14px;
  height: 14px;
}

.message .user-avatar {
  background: var(--gray-400);
}

.message .message-content {
  min-width: 0;
  max-width: 82%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.message .bubble {
  padding: 9px 12px;
  border-radius: 12px;
  border: 1px solid var(--gray-100);
  font-size: 13px;
  line-height: 1.65;
  word-break: break-word;
  color: var(--gray-800);
  background: var(--gray-50);
}

.message .bubble p {
  margin: 0 0 6px;
  white-space: pre-line;
}

.message .bubble p:last-child {
  margin-bottom: 0;
}

.message .bubble ul {
  margin: 6px 0 0;
  padding-left: 18px;
}

.message .bubble li {
  margin-bottom: 2px;
}

.message-time {
  padding-left: 2px;
  font-size: 11px;
  color: var(--gray-400);
}

/* ===== 打字指示器 ===== */
.typing-bubble {
  display: inline-flex;
  align-items: center;
}

.typing-dots {
  display: inline-flex;
  gap: 4px;
}

.typing-dots i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--gray-400);
  animation: typingBounce 1.2s ease-in-out infinite;
}

.typing-dots i:nth-child(2) {
  animation-delay: 0.15s;
}

.typing-dots i:nth-child(3) {
  animation-delay: 0.3s;
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

/* ===== 来源 chips ===== */
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
}

.source-chip:hover,
.source-chip.expanded {
  background: var(--main-50);
}

.source-chip svg {
  width: 12px;
  height: 12px;
}

.source-chip .chip-arrow {
  transition: transform 0.2s ease;
}

.source-chip .chip-arrow.rotated {
  transform: rotate(180deg);
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
}

.source-item svg {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}

.source-item .source-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ===== 预设问题 ===== */
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
}

.suggestion-chip:hover:not(:disabled) {
  border-color: var(--main-200);
  color: var(--main-700);
  background: var(--main-30);
}

.suggestion-chip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ===== 输入区 ===== */
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
}

.preview-input::placeholder {
  color: var(--gray-400);
}

.preview-input:focus {
  border-color: var(--main-300);
}

.preview-input:disabled {
  opacity: 0.6;
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
}

.send-button svg {
  width: 15px;
  height: 15px;
}

.send-button.enabled {
  background: var(--main-600);
  color: var(--gray-0);
}

.send-button.enabled:hover {
  background: var(--main-700);
}

.send-button:disabled {
  cursor: not-allowed;
}

/* ===== 响应式 ===== */
@media (max-width: 768px) {
  .home-agent-preview {
    height: 400px;
    max-height: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .typing-dots i,
  .source-chip .chip-arrow {
    animation: none;
    transition: none;
  }
}
</style>
