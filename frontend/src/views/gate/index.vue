<template>
  <section class="page" data-module="gate">
    <header class="page-head">
      <div>
        <h2>闸口管理</h2>
        <p class="page-desc">车辆到闸按「OCR 识别 → 地磅称重 → 放行抬杆」推进；识别受阻、称重失败、抬杆失败都能就地兜底重试。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记到闸车辆</button>
        <button class="btn" type="button" @click="exportRows">导出闸口清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in summaryCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <!-- 闸口总览：与运营概览页同一份 /api/gate/summary 口径，两个页面不再两样 -->
    <table class="data-table gate-overview">
      <thead>
        <tr>
          <th>闸口编号</th><th>闸口类型</th><th>记录数</th>
          <th>待处理</th><th>异常</th><th>故障车道</th><th>排队车道</th><th>闸口状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="gate in gateSummary" :key="gate['闸口编号']">
          <td>{{ gate['闸口编号'] }}</td>
          <td>{{ gate['闸口类型'] }}</td>
          <td>{{ gate['总记录'] }}</td>
          <td>{{ gate['待处理'] }}</td>
          <td>
            <span :class="gate['异常'] ? 'tag tag-danger' : ''">{{ gate['异常'] || '暂无' }}</span>
          </td>
          <td>{{ gate['故障车道'] || '暂无' }}</td>
          <td>{{ gate['排队车道'] || '暂无' }}</td>
          <td><span :class="gateStatusClass(gate['闸口状态'])">{{ gate['闸口状态'] }}</span></td>
        </tr>
        <tr v-if="!gateSummary.length">
          <td colspan="8" class="empty-state">暂无闸口总览数据</td>
        </tr>
      </tbody>
    </table>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>进出方向</span>
        <select v-model="filters.direction">
          <option value="">全部</option>
          <option value="进场">进场</option>
          <option value="出场">出场</option>
        </select>
      </label>
      <label class="filter-item">
        <span>车道阶段</span>
        <select v-model="filters.stage">
          <option value="">全部</option>
          <option v-for="stage in stageOptions" :key="stage" :value="stage">{{ stage }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>闸口/车道/车牌</span>
        <input v-model="filters.keyword" placeholder="按编号或车牌检索" />
      </label>
      <label class="filter-item check-item">
        <input v-model="abnormalOnly" type="checkbox" @change="reload" />
        <span>只看异常</span>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column.key">
            {{ column.label }}
            <em v-if="column.key === '进出方向_展示'" class="col-hint">（空着也照常列出）</em>
          </th>
          <th style="min-width: 230px">可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-abnormal': isAbnormal(row) }">
          <td v-for="column in columns" :key="column.key">
            <span v-if="isMissingCell(column.key, row)" class="missing-cell">{{ row[column.key] }}</span>
            <span v-else-if="column.key === '闸口状态'" :class="stageClass(row[column.key])">{{ row[column.key] }}</span>
            <span v-else>{{ row[column.key] ?? '暂无' }}</span>
          </td>
          <td class="row-actions">
            <template v-for="act in availableActions(row)" :key="act.key">
              <button
                class="link"
                :class="{ 'link-danger': act.danger, 'link-warn': act.warn }"
                type="button"
                :disabled="busyId === String(row.id)"
                @click="runAction(act, row)"
              >
                {{ busyId === String(row.id) && act.key === busyAction ? `${act.label}中…` : act.label }}
              </button>
            </template>
            <span v-if="!availableActions(row).length" class="muted-text">暂无可用动作</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">
            当前条件下暂无抬杆记录，可放宽筛选或登记到闸车辆
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 行内反馈：失败原因、成功提示都在对应记录下方，重试入口就在旁边 -->
    <ul v-if="feedbacks.length" class="feedback-list">
      <li v-for="fb in feedbacks" :key="fb.id" :class="fb.ok ? 'feedback-ok' : 'feedback-err'">
        <span>记录 {{ fb.id }}：{{ fb.message }}</span>
        <button v-if="fb.retry" class="link" type="button" @click="fb.retry()">立即重试</button>
        <button class="link" type="button" @click="dismissFeedback(fb.id)">知道了</button>
      </li>
    </ul>

    <footer class="page-foot">
      <span>共 {{ total }} 条抬杆记录 · 在场车数 {{ summary.在场车数 ?? '—' }}（随抬杆记录实时重算）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 登记到闸车辆 -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记到闸车辆</h3>
        <label v-for="f in createFields" :key="f.key" class="modal-field">
          <span>{{ f.label }}<em v-if="f.required">*</em></span>
          <select v-if="f.key === '进出方向'" v-model="createForm[f.key]">
            <option value="">暂不确定（稍后补录）</option>
            <option value="进场">进场</option>
            <option value="出场">出场</option>
          </select>
          <input v-else v-model="createForm[f.key]" :placeholder="f.placeholder ?? ''" />
        </label>
        <p v-if="createError" class="error-text">{{ createError }}</p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="createOpen = false">取消</button>
          <button class="btn primary" type="submit">登记</button>
        </div>
      </form>
    </div>

    <!-- 现场补录（方向/车牌）-->
    <div v-if="supplementTarget" class="modal-mask" @click.self="supplementTarget = null">
      <form class="modal" @submit.prevent="submitSupplement">
        <h3>补录现场信息 · 记录 {{ supplementTarget.id }}</h3>
        <p class="muted-text">缺 {{ supplementMissing }}，补录后即可放行。</p>
        <label class="modal-field">
          <span>进出方向</span>
          <select v-model="supplementForm['进出方向']">
            <option value="">请选择</option>
            <option value="进场">进场</option>
            <option value="出场">出场</option>
          </select>
        </label>
        <label class="modal-field">
          <span>车牌（OCR 认不出时现场录入）</span>
          <input v-model="supplementForm['车牌']" placeholder="如 沪A12345" />
        </label>
        <p v-if="supplementError" class="error-text">{{ supplementError }}</p>
        <div class="modal-actions">
          <button class="btn" type="button" @click="supplementTarget = null">取消</button>
          <button class="btn primary" type="submit">保存并继续</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

const ENDPOINT = '/api/gate'

type Row = Record<string, string | number | string[] | null>

interface Action {
  key: string
  label: string
  endpoint?: string
  method?: string
  body?: Record<string, unknown>
  danger?: boolean
  warn?: boolean
}

const columns = [
  { key: '闸口编号', label: '闸口编号' },
  { key: '闸口类型', label: '闸口类型' },
  { key: '车道编号', label: '车道编号' },
  { key: '进出方向_展示', label: '进出方向' },
  { key: 'OCR识别', label: 'OCR识别' },
  { key: '地磅称重', label: '地磅称重' },
  { key: '放行抬杆', label: '放行抬杆' },
  { key: '闸口状态', label: '车道状态' },
]
const stageOptions = ['待识别', '识别受阻', '待称重', '称重受阻', '待放行', '抬杆失败', '信息待补', '已放行']
const failStages = ['识别受阻', '称重受阻', '抬杆失败', '信息待补']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const abnormalOnly = ref(false)
const filters = ref({ keyword: '', direction: '', stage: '' })

interface GateSummary {
  闸口编号: string
  闸口类型: string
  总记录: number
  待处理: number
  异常: number
  故障车道: number
  排队车道: number
  闸口状态: string
}
const gateSummary = ref<GateSummary[]>([])
const summary = ref<Record<string, number | GateSummary[]>>({})
const summaryCards = computed(() => [
  { label: '闸口总数', value: num(summary.value['闸口总数']) },
  { label: '正常开放', value: num(summary.value['正常开放']) },
  { label: '排队等候', value: num(summary.value['排队等候']) },
  { label: '设备故障', value: num(summary.value['设备故障']) },
  { label: '在场车数', value: num(summary.value['在场车数']) },
  { label: '异常记录', value: num(summary.value['异常记录']) },
])
function num(v: unknown): number {
  return typeof v === 'number' ? v : 0
}

const busyId = ref<string | null>(null)
const busyAction = ref('')

interface Feedback {
  id: number
  ok: boolean
  message: string
  retry?: () => void
}
const feedbacks = ref<Feedback[]>([])

function pushFeedback(fb: Feedback) {
  feedbacks.value = [fb, ...feedbacks.value].slice(0, 5)
}
function dismissFeedback(id: number) {
  feedbacks.value = feedbacks.value.filter((f) => f.id !== id)
}

function isAbnormal(row: Row): boolean {
  return failStages.includes(String(row['闸口状态'])) || ((row['缺失字段'] as string[]) ?? []).length > 0
}
function isMissingCell(key: string, row: Row): boolean {
  if (key === '进出方向_展示') return String(row[key] ?? '').startsWith('暂无')
  if (key === 'OCR识别' || key === '地磅称重') return String(row[key] ?? '').startsWith('暂无')
  return false
}
function stageClass(stage: unknown): string {
  const s = String(stage)
  if (s === '已放行') return 'tag tag-ok'
  if (failStages.includes(s)) return 'tag tag-danger'
  return 'tag tag-warn'
}
function gateStatusClass(status: unknown): string {
  const s = String(status)
  if (s === '设备故障') return 'tag tag-danger'
  if (s === '排队等候') return 'tag tag-warn'
  if (s === '正常开放') return 'tag tag-ok'
  return 'tag'
}

/** 每个阶段给出对应的兜底动作；重试入口始终就地可见。 */
function availableActions(row: Row): Action[] {
  const stage = String(row['闸口状态'])
  const id = row.id
  const actions: Action[] = []
  if (stage === '已放行') return actions

  if (stage === '待识别' || stage === '识别受阻') {
    actions.push({ key: 'ocr', label: '重新识别', endpoint: `${ENDPOINT}/${id}/ocr`, method: 'POST' })
    actions.push({ key: 'ocrManual', label: '人工补录车牌', warn: true })
  }
  if (stage === '待称重' || stage === '称重受阻') {
    actions.push({ key: 'weigh', label: stage === '称重受阻' ? '重新取重' : '地磅取重', endpoint: `${ENDPOINT}/${id}/weigh`, method: 'POST' })
  }
  if (stage === '信息待补' || ((row['缺失字段'] as string[]) ?? []).length > 0) {
    actions.push({ key: 'supplement', label: '补录方向/车牌', warn: true })
  }
  if (stage === '待放行') {
    actions.push({ key: 'release', label: '自动放行', endpoint: `${ENDPOINT}/${id}/release`, method: 'POST' })
    actions.push({ key: 'manual', label: '人工抬杆', warn: true })
  }
  if (stage === '抬杆失败') {
    actions.push({ key: 'retryLift', label: '重试抬杆', danger: true, endpoint: `${ENDPOINT}/${id}/release`, method: 'POST' })
    actions.push({ key: 'manual', label: '人工抬杆（现场确认）', warn: true })
  }
  return actions
}

async function runAction(act: Action, row: Row) {
  if (act.key === 'supplement') {
    openSupplement(row)
    return
  }
  if (act.key === 'ocrManual') {
    openSupplement(row)
    return
  }
  if (act.key === 'manual') {
    // 放行与人工抬杆冲突时，以现场确认为准
    await postAction({ ...act, key: 'manual', label: '人工抬杆', endpoint: `${ENDPOINT}/${row.id}/manual-lift` }, row)
    return
  }
  await postAction(act, row)
}

async function postAction(act: Action, row: Row) {
  const id = Number(row.id)
  busyId.value = String(row.id)
  busyAction.value = act.key
  errorMessage.value = ''
  try {
    const response = await request(act.endpoint as string, {
      method: act.method ?? 'POST',
      body: JSON.stringify(act.body ?? {}),
    })
    const payload = (await response.json().catch(() => null)) as { ok?: boolean; message?: string; detail?: string } | null
    if (!response.ok || !payload?.ok) {
      const reason = payload?.detail ? `${payload.message}：${payload.detail}` : (payload?.message ?? '设备未响应，请稍后再试')
      pushFeedback({
        id,
        ok: false,
        message: reason,
        // 称重/抬杆/识别失败都保留重试入口
        retry: () => { dismissFeedback(id); void postAction(act, row) },
      })
    } else {
      pushFeedback({ id, ok: true, message: payload.message ?? '操作已生效' })
    }
  } catch (error) {
    pushFeedback({
      id,
      ok: false,
      message: error instanceof Error ? error.message : '请求未送达',
      retry: () => { dismissFeedback(id); void postAction(act, row) },
    })
  } finally {
    busyId.value = null
    busyAction.value = ''
    await Promise.all([reload(), reloadSummary()])
  }
}

// ---------- 登记 ----------
const createOpen = ref(false)
const createError = ref('')
const createFields = [
  { key: '闸口编号', label: '闸口编号', required: true, placeholder: '如 GATE-01' },
  { key: '闸口类型', label: '闸口类型', required: true, placeholder: '入口闸 / 出口闸' },
  { key: '车道编号', label: '车道编号', required: true, placeholder: '如 L-01' },
  { key: '进出方向', label: '进出方向', required: false },
]
const createForm = ref<Record<string, string>>({})
function openCreate() {
  createForm.value = {}
  createError.value = ''
  createOpen.value = true
}
async function submitCreate() {
  createError.value = ''
  try {
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createForm.value }) })
    const payload = await response.json()
    if (!payload.ok) {
      createError.value = payload.message
      return
    }
    createOpen.value = false
    await Promise.all([reload(), reloadSummary()])
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '登记失败'
  }
}

// ---------- 现场补录 ----------
const supplementTarget = ref<Row | null>(null)
const supplementError = ref('')
const supplementForm = ref<Record<string, string>>({})
const supplementMissing = computed(() => {
  const fields = supplementTarget.value?.['缺失字段']
  return Array.isArray(fields) && fields.length ? fields.join('、') : '信息'
})
function openSupplement(row: Row) {
  supplementTarget.value = row
  supplementError.value = ''
  supplementForm.value = {
    '进出方向': '',
    '车牌': '',
  }
}
async function submitSupplement() {
  if (!supplementTarget.value) return
  supplementError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${supplementTarget.value.id}/supplement`, {
      method: 'POST',
      body: JSON.stringify({ values: supplementForm.value }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      supplementError.value = payload.message
      return
    }
    const id = Number(supplementTarget.value.id)
    supplementTarget.value = null
    pushFeedback({ id, ok: true, message: payload.message })
    await Promise.all([reload(), reloadSummary()])
  } catch (error) {
    supplementError.value = error instanceof Error ? error.message : '补录失败'
  }
}

function resetFilters() {
  filters.value = { keyword: '', direction: '', stage: '' }
  abnormalOnly.value = false
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function reload() {
  errorMessage.value = ''
  // 条件留空时不带参数：空方向绝不把列表筛成空白
  const params = new URLSearchParams()
  if (filters.value.keyword.trim()) params.set('keyword', filters.value.keyword.trim())
  if (filters.value.direction) params.set('direction', filters.value.direction)
  if (filters.value.stage) params.set('stage', filters.value.stage)
  if (abnormalOnly.value) params.set('abnormal', 'true')
  const query = params.toString()
  try {
    const response = await request(`${ENDPOINT}${query ? `?${query}` : ''}`)
    if (!response.ok) throw new Error('抬杆记录读取失败')
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '闸口列表读取失败'
  }
}

async function reloadSummary() {
  try {
    const response = await request(`${ENDPOINT}/summary`)
    if (!response.ok) return
    const payload = await response.json()
    summary.value = payload
    gateSummary.value = payload['闸口明细'] ?? []
  } catch {
    // 总览拉取失败不阻塞作业列表
  }
}

onMounted(() => {
  void reload()
  void reloadSummary()
})
</script>

<style scoped>
.gate-overview { margin-bottom: 14px; }
.col-hint { color: var(--muted); font-style: normal; font-size: 12px; font-weight: normal; }
.check-item { display: flex; align-items: center; gap: 4px; }
.check-item input { margin: 0; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; background: #eef2f7; color: #475569; }
.tag-ok { background: #e7f6ec; color: #1a7f37; }
.tag-warn { background: #fdf3e0; color: #b25e09; }
.tag-danger { background: #fdeaea; color: #b42318; }
.missing-cell { color: #b25e09; }
.muted-text { color: var(--muted); font-size: 12px; }
.row-abnormal { background: #fffaf5; }
.link-warn { color: #b25e09; }
.link-danger { color: #b42318; font-weight: 600; }
.feedback-list { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.feedback-list li { display: flex; gap: 12px; align-items: center; padding: 6px 10px; border-radius: 6px; font-size: 13px; }
.feedback-ok { background: #e7f6ec; color: #1a7f37; }
.feedback-err { background: #fdeaea; color: #b42318; }
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 380px; }
.modal h3 { margin: 0 0 12px; font-size: 16px; }
.modal-field { display: block; margin-bottom: 10px; font-size: 13px; }
.modal-field span { display: block; color: var(--muted); margin-bottom: 4px; }
.modal-field em { color: #b42318; font-style: normal; }
.modal-field input, .modal-field select { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }
</style>
