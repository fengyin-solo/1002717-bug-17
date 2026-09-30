<template>
  <section class="page" data-module="gate">
    <header class="page-head">
      <div>
        <h2>闸口管理</h2>
        <p class="page-desc">车辆进出闸通行：OCR 识别车牌 → 地磅取重 → 放行抬杆，设备抖动自动重试，异常可回退、可重试，冲突以现场确认为准。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">车辆到闸登记</button>
        <button class="btn" type="button" @click="exportRows">导出闸口通行清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in overviewCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>闸口/车道/车牌</span>
        <input v-model="filters.keyword" placeholder="按闸口编号、车道或车牌检索" />
      </label>
      <label class="filter-item">
        <span>进出方向</span>
        <select v-model="filters.direction">
          <option value="">全部</option>
          <option value="进场">进场</option>
          <option value="出场">出场</option>
        </select>
      </label>
      <label class="filter-item">
        <span>闸口状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in flowStatuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p v-if="notice" class="banner" :class="noticeTone">{{ notice }}</p>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>异常提示</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <span :class="{ 'cell-empty': isMissing(row, column), 'cell-abnormal': isAbnormalCell(row, column) }">
              {{ display(row, column) }}
            </span>
          </td>
          <td class="hint-cell">
            <span v-if="row['异常提示']" class="hint-text">{{ row['异常提示'] }}</span>
            <span v-else class="cell-ok">—</span>
          </td>
          <td class="row-actions">
            <template v-for="action in availableActions(row)" :key="action">
              <button
                v-if="needPrompt(action)"
                class="link"
                type="button"
                :disabled="busyId === row.id"
                @click="promptAction(action, row)"
              >
                {{ action }}
              </button>
              <button
                v-else
                class="link"
                type="button"
                :disabled="busyId === row.id"
                @click="runAction(action, row, {})"
              >
                {{ busyId === row.id ? '执行中…' : action }}
              </button>
            </template>
            <span v-if="!availableActions(row).length" class="cell-ok">—</span>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无符合条件的闸口通行记录，可先登记到闸车辆</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条闸口通行记录 · 状态口径与闸口总览一致</span>
    </footer>

    <!-- 车辆到闸登记 -->
    <div v-if="createOpen" class="modal-mask" @click.self="createOpen = false">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>车辆到闸登记</h3>
        <p v-if="createError" class="banner error">{{ createError }}</p>
        <label v-for="f in createFields" :key="f.key" class="form-item">
          <span>{{ f.label }}<i v-if="f.required">*</i></span>
          <input
            v-if="f.type !== 'select'"
            v-model="createForm[f.key]"
            :placeholder="`请输入${f.label}`"
          />
          <select v-else v-model="createForm[f.key]">
            <option value="">请选择</option>
            <option v-for="opt in f.options" :key="opt" :value="opt">{{ opt }}</option>
          </select>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="createOpen = false">取消</button>
          <button class="btn primary" type="submit">登记</button>
        </div>
      </form>
    </div>

    <!-- 动作参数：人工补录车牌 / 现场确认 -->
    <div v-if="prompt.open" class="modal-mask" @click.self="prompt.open = false">
      <form class="modal" @submit.prevent="submitPrompt">
        <h3>{{ prompt.action }} · 记录 #{{ prompt.row?.id }}</h3>
        <p class="banner info">{{ prompt.intro }}</p>
        <label v-if="prompt.kind === 'plate'" class="form-item">
          <span>车牌号码<i>*</i></span>
          <input v-model="prompt.plate" placeholder="如 沪A12345" />
        </label>
        <div v-if="prompt.kind === 'confirm'" class="decision-row">
          <label><input type="radio" value="放行" v-model="prompt.decision" /> 现场确认放行（抬杆）</label>
          <label><input type="radio" value="拦截" v-model="prompt.decision" /> 现场确认拦截（不抬杆）</label>
        </div>
        <label v-if="prompt.kind === 'manual'" class="decision-row">
          <label><input type="radio" value="放行" v-model="prompt.decision" /> 人工放行（抬杆）</label>
          <label><input type="radio" value="拦截" v-model="prompt.decision" /> 人工拦截</label>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="prompt.open = false">取消</button>
          <button class="btn primary" type="submit">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | string[] | null>

const ENDPOINT = '/api/gate'
const columns = ["闸口编号", "闸口类型", "车道编号", "进出方向", "车牌号码", "OCR识别", "地磅称重", "放行抬杆", "闸口状态"]
const flowStatuses = ["待识别", "待称重", "待放行", "抬杆失败", "放行冲突", "拦截待确认", "已放行", "已拦截"]

const rows = ref<Row[]>([])
const total = ref(0)
const busyId = ref<number | null>(null)
const notice = ref('')
const noticeTone = ref('info')
const filters = reactive({ keyword: '', direction: '', status: '' })
const overviewCards = ref<{ label: string; value: number }[]>([
  { label: '在场车数', value: 0 },
  { label: '已抬杆放行', value: 0 },
  { label: '已拦截', value: 0 },
  { label: '异常待处理', value: 0 },
])

const createFields = [
  { key: '闸口编号', label: '闸口编号', required: true },
  { key: '闸口类型', label: '闸口类型', required: true },
  { key: '车道编号', label: '车道编号', required: true },
  { key: '进出方向', label: '进出方向', required: false, type: 'select', options: ['进场', '出场'] },
  { key: '车牌号码', label: '车牌号码', required: false },
]
const createOpen = ref(false)
const createError = ref('')
const createForm = reactive<Record<string, string>>({})

const prompt = reactive({
  open: false,
  kind: 'plate' as 'plate' | 'confirm' | 'manual',
  action: '',
  intro: '',
  row: null as Row | null,
  plate: '',
  decision: '放行',
})

function resetFilters() {
  filters.keyword = ''
  filters.direction = ''
  filters.status = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  for (const f of createFields) createForm[f.key] = ''
  createError.value = ''
  createOpen.value = true
}

async function submitCreate() {
  createError.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...createForm } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      createError.value = payload.message || '登记失败'
      return
    }
    createOpen.value = false
    flash(payload.message || '车辆已到闸登记', true)
    await reload()
  } catch (error) {
    createError.value = error instanceof Error ? error.message : '登记失败'
  }
}

// 哪些动作需要弹窗补参数
function needPrompt(action: string): boolean {
  return action === '人工抬杆' || action === '现场确认'
}

function promptAction(action: string, row: Row) {
  prompt.row = row
  prompt.action = action
  prompt.plate = typeof row['车牌号码'] === 'string' && !String(row['车牌号码']).startsWith('暂无')
    ? String(row['车牌号码'])
    : ''
  prompt.decision = '放行'
  if (action === '现场确认') {
    prompt.kind = 'confirm'
    prompt.intro = '自动放行与人工抬杆结论不一致（或系统拦截），以现场核实结果为准。'
  } else {
    prompt.kind = 'manual'
    prompt.intro = '人工抬杆为现场处置：可选择放行或拦截；车牌识别不到时可在此补录。'
  }
  prompt.open = true
}

async function submitPrompt() {
  if (!prompt.row) return
  const values: Record<string, string> = { decision: prompt.decision }
  if (prompt.plate.trim()) values['车牌号码'] = prompt.plate.trim()
  prompt.open = false
  await runAction(prompt.action, prompt.row, values)
}

function availableActions(row: Row): string[] {
  return (row.actions as string[] | undefined) ?? []
}

function display(row: Row, column: string): string {
  return row[column] == null ? '暂无' : String(row[column])
}

function isMissing(row: Row, column: string): boolean {
  const value = String(row[column] ?? '')
  return value.startsWith('暂无') || (column === '进出方向' && (row.missing as string[] | undefined)?.includes('进出方向') === true)
}

function isAbnormalCell(row: Row, column: string): boolean {
  const value = String(row[column] ?? '')
  if (column === 'OCR识别') return value.includes('识别失败') || value.includes('重试')
  if (column === '地磅称重') return value.includes('取重失败')
  if (column === '放行抬杆') return value.includes('抬杆失败')
  if (column === '闸口状态') return ['抬杆失败', '放行冲突', '拦截待确认', '已拦截'].includes(value)
  return false
}

async function runAction(action: string, row: Row, extra: Record<string, string>) {
  busyId.value = Number(row.id)
  try {
    // 地磅/栏杆持续故障重试时，默认携带“设备已复位”，给现场一个可恢复的入口
    const values: Record<string, string> = { action, ...extra }
    if (action === '地磅取重' && String(row['地磅称重']).includes('取重失败')) values.device_reset = 'true'
    if (action === '重试抬杆' && String(row['放行抬杆']).includes('抬杆失败')) values.device_reset = 'true'
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    flash(payload.message || '动作已执行', Boolean(payload.ok))
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '闸口操作失败', false)
  } finally {
    busyId.value = null
  }
}

function flash(message: string, ok: boolean) {
  notice.value = message
  noticeTone.value = ok ? 'ok' : 'error'
}

async function reload() {
  const query = new URLSearchParams()
  if (filters.keyword) query.set('keyword', filters.keyword)
  if (filters.direction) query.set('direction', filters.direction)
  if (filters.status) query.set('status', filters.status)
  try {
    const [listRes, overviewRes] = await Promise.all([
      request(`${ENDPOINT}?${query.toString()}`),
      request(`${ENDPOINT}/overview`),
    ])
    if (!listRes.ok) throw new Error('闸口列表读取失败')
    const payload = await listRes.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
    if (overviewRes.ok) {
      const overview = await overviewRes.json()
      overviewCards.value = overview.cards ?? overviewCards.value
    }
  } catch (error) {
    flash(error instanceof Error ? error.message : '闸口列表读取失败', false)
  }
}

onMounted(reload)
</script>

<style scoped>
.banner {
  margin: 0 0 10px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
}
.banner.info { background: #eff8ff; color: #175cd3; border: 1px solid #b2ddff; }
.banner.ok { background: #ecfdf3; color: #027a48; border: 1px solid #abefc6; }
.banner.error { background: #fef3f2; color: #b42318; border: 1px solid #fda29b; }
.cell-empty { color: #98a2b3; }
.cell-abnormal { color: #b42318; font-weight: 600; }
.cell-ok { color: #98a2b3; }
.hint-cell { font-size: 12px; color: #b42318; max-width: 320px; }
.hint-text { line-height: 1.5; }
.link:disabled { color: #98a2b3; cursor: not-allowed; }
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(16, 24, 40, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal {
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
  width: 420px;
  max-width: calc(100vw - 32px);
}
.modal h3 { margin: 0 0 12px; font-size: 15px; }
.form-item { display: block; margin-bottom: 10px; font-size: 13px; }
.form-item span { display: block; margin-bottom: 4px; color: #475467; }
.form-item i { color: #d92d20; font-style: normal; margin-left: 2px; }
.form-item input,
.form-item select,
.filter-item select {
  width: 100%;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  box-sizing: border-box;
}
.decision-row { display: flex; flex-direction: column; gap: 8px; font-size: 13px; margin-bottom: 12px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
