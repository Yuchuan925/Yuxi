<template>
  <article class="result-item">
    <div class="result-header">
      <span class="result-index">#{{ index + 1 }}</span>
      <span v-if="Number.isFinite(chunk.score)" class="result-score">
        {{ scoreLabel }}: {{ chunk.score.toFixed(4) }}
      </span>
      <span v-if="Number.isFinite(chunk.rerank_score)" class="result-rerank-score">
        重排序分数: {{ chunk.rerank_score.toFixed(4) }}
      </span>
    </div>
    <div v-if="chunk.highlights?.length" class="result-highlights" aria-label="命中片段">
      <p v-for="(fragment, fragmentIndex) in chunk.highlights" :key="fragmentIndex">
        <template v-for="(segment, segmentIndex) in fragment" :key="segmentIndex">
          <mark v-if="segment.matched">{{ segment.text }}</mark>
          <span v-else>{{ segment.text }}</span>
        </template>
      </p>
    </div>
    <details v-if="chunk.highlights?.length" class="result-content">
      <summary>查看完整片段</summary>
      <p>{{ chunk.content }}</p>
    </details>
    <div v-else class="result-content">{{ chunk.content }}</div>
    <div class="result-metadata">
      <span v-if="chunk.metadata?.source">来源: {{ chunk.metadata.source }}</span>
      <span v-if="chunk.metadata?.file_id">文件ID: {{ chunk.metadata.file_id }}</span>
      <span v-if="chunk.metadata?.chunk_index !== undefined">
        块索引: {{ chunk.metadata.chunk_index }}
      </span>
    </div>
  </article>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  chunk: { type: Object, required: true },
  index: { type: Number, required: true }
})

const scoreLabel = computed(
  () =>
    ({
      cosine: '向量相似度',
      bm25: '关键词相关度',
      hybrid: '混合检索分数',
      graph: '图检索分数',
      fusion: '融合排名分数'
    })[props.chunk.score_type] || '检索分数'
)
</script>

<style scoped lang="less">
.result-item {
  padding: 12px;
  margin-bottom: 12px;
  border: 1px solid var(--gray-200);
  border-radius: 8px;
  background: var(--gray-0);
  color: var(--color-text);
}

.result-header,
.result-metadata {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  font-size: 12px;
}

.result-header {
  padding-bottom: 8px;
  border-bottom: 1px solid var(--gray-150);
}

.result-index {
  color: var(--main-color);
  font-weight: 600;
}

.result-score,
.result-rerank-score {
  color: var(--color-text-secondary);
}

.result-content,
.result-highlights {
  padding: 8px 0;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;

  p {
    margin: 0 0 8px;
  }
}

mark {
  background: var(--color-warning-50);
  color: var(--color-warning-900);
}

summary {
  cursor: pointer;
  color: var(--main-color);
}

.result-metadata {
  padding-top: 8px;
  border-top: 1px solid var(--gray-150);
  color: var(--color-text-secondary);
}
</style>
