<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。闸口状态与闸口管理页取同一份口径。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>

    <!-- 闸口总览：直接取 /api/gate/summary，与闸口管理页完全一致，在场车数随抬杆记录重算 -->
    <h3 class="section-title">闸口总览</h3>
    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">在场车数</span>
        <strong class="stat-value">{{ gateSummary['在场车数'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">正常开放</span>
        <strong class="stat-value">{{ gateSummary['正常开放'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">排队等候</span>
        <strong class="stat-value">{{ gateSummary['排队等候'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">设备故障</span>
        <strong class="stat-value gate-fault">{{ gateSummary['设备故障'] ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">异常记录</span>
        <strong class="stat-value gate-fault">{{ gateSummary['异常记录'] ?? 0 }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr>
          <th>闸口编号</th><th>闸口类型</th><th>记录数</th>
          <th>待处理</th><th>异常</th><th>故障车道</th><th>排队车道</th><th>闸口状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="gate in gateDetails" :key="gate['闸口编号']">
          <td>{{ gate['闸口编号'] }}</td>
          <td>{{ gate['闸口类型'] }}</td>
          <td>{{ gate['总记录'] }}</td>
          <td>{{ gate['待处理'] }}</td>
          <td>{{ gate['异常'] }}</td>
          <td>{{ gate['故障车道'] }}</td>
          <td>{{ gate['排队车道'] }}</td>
          <td>{{ gate['闸口状态'] }}</td>
        </tr>
        <tr v-if="!gateDetails.length">
          <td colspan="8" class="empty-state">暂无闸口总览数据</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">各业务模块</h3>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

type GateSummary = {
  闸口总数: number
  正常开放: number
  排队等候: number
  设备故障: number
  在场车数: number
  待处理: number
  异常记录: number
  闸口明细: {
    闸口编号: string
    闸口类型: string
    总记录: number
    待处理: number
    异常: number
    故障车道: number
    排队车道: number
    闸口状态: string
  }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])
const gateSummary = ref<Partial<GateSummary>>({})
const gateDetails = ref<GateSummary['闸口明细']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{ label: '业务模块', value: 0 }, { label: '今日新增', value: 0 }]
    moduleRows.value = []
  }
  // 闸口状态、在场车数与闸口管理页共用同一个接口、同一套聚合规则
  try {
    const response = await request('/api/gate/summary')
    if (response.ok) {
      const payload = (await response.json()) as GateSummary
      gateSummary.value = payload
      gateDetails.value = payload.闸口明细 ?? []
    }
  } catch {
    // 总览拉取失败时保留 0 值，不影响其余模块看板
  }
})
</script>

<style scoped>
.section-title { font-size: 15px; margin: 18px 0 8px; }
.gate-fault { color: #b42318; }
</style>
