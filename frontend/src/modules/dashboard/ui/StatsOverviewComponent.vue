<template>
  <div class="stats-overview-container">
    <DashboardMetricGrid>
      <DashboardMetricCard
        :icon="MessageCircle"
        :value="formatNumber(props.basicStats?.total_conversations)"
        label="累计会话"
        tone="primary"
      >
        <template #meta v-if="props.basicStats?.conversation_trend">
          <span class="metric-trend" :class="props.basicStats.conversation_trend > 0 ? 'up' : 'down'">
            <TrendingUp v-if="props.basicStats.conversation_trend > 0" />
            <TrendingDown v-else />
            {{ Math.abs(props.basicStats.conversation_trend) }}%
          </span>
        </template>
      </DashboardMetricCard>

      <DashboardMetricCard
        :icon="Activity"
        :value="formatNumber(props.basicStats?.active_conversations)"
        label="活跃对话"
        tone="success"
      />
      <DashboardMetricCard
        :icon="Mail"
        :value="formatNumber(props.basicStats?.total_messages)"
        label="总消息数"
        tone="info"
      />
      <DashboardMetricCard
        :icon="Users"
        :value="formatNumber(props.basicStats?.total_users)"
        label="用户数"
        tone="warning"
      />
    </DashboardMetricGrid>
  </div>
</template>

<script setup>
import {
  MessageCircle,
  Activity,
  Mail,
  Users,
  TrendingUp,
  TrendingDown
} from '@lucide/vue'
import { formatNumber } from '@/modules/dashboard/model/dashboard'
import DashboardMetricCard from './DashboardMetricCard.vue'
import DashboardMetricGrid from './DashboardMetricGrid.vue'

const props = defineProps({
  basicStats: {
    type: Object,
    default: () => ({})
  }
})

</script>

<style lang="less" scoped>
.stats-overview-container {
  margin-top: 8px;
}

.dashboard-metric-grid {
  padding: 0 var(--page-padding);
}

.metric-trend {
  display: inline-flex;
  align-items: center;
  gap: 2px;

  svg {
    width: 12px;
    height: 12px;
  }

  &.up {
    color: var(--color-success-700);
  }

  &.down {
    color: var(--color-error-700);
  }
}
</style>
