<template>
  <section class="page" data-module="etljob">
    <header class="page-head">
      <div>
        <h2>ETL作业管理</h2>
        <p class="page-desc">管理ETL作业的定义，包括源端目标端映射、执行逻辑和调度配置。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记作业定义</button>
        <button class="btn" type="button" @click="exportRows">导出ETL作业清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in statCards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>模板版本</th>
          <th>绑定状态</th>
          <th>模板发布状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
          <tr>
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>{{ row['模板编号'] ? `${row['模板编号']} · ${row['模板版本'] ?? '—'}` : '未绑定' }}</td>
            <td>{{ row['绑定状态'] ?? '未绑定' }}</td>
            <td>{{ row['模板发布状态'] ?? '未绑定模板' }}</td>
            <td class="row-actions">
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
              <button class="link" type="button" @click="openBinding(row)">绑定模板版本</button>
              <button class="link" type="button" @click="viewBinding(row)">查看绑定</button>
            </td>
          </tr>
          <tr v-if="String(activeId) === String(row.id)">
            <td :colspan="columns.length + 4" class="binding-cell">
              <div class="binding-panel">
                <div class="binding-head">
                  <strong>{{ row['作业编号'] }} · {{ row['作业名称'] }}</strong>
                  <button class="link" type="button" @click="closePanel">关闭</button>
                </div>
                <p v-if="panelError" class="error-text">{{ panelError }}</p>
                <p v-if="panelMessage" class="ok-text">{{ panelMessage }}</p>

                <div v-if="panelMode === 'bind'" class="binding-form">
                  <label class="filter-item">
                    <span>SQL模板</span>
                    <select v-model.number="selectedTemplateId" @change="loadBindableVersions">
                      <option :value="0" disabled>选择模板</option>
                      <option v-for="template in templates" :key="String(template.id)" :value="template.id">
                        {{ template['模板编号'] }} · {{ template['模板名称'] }}（发布 {{ template['当前发布版本'] ?? '无' }}）
                      </option>
                    </select>
                  </label>
                  <label class="filter-item">
                    <span>模板版本（仅已发布/已替代）</span>
                    <select v-model.number="selectedVersionId">
                      <option :value="0" disabled>选择版本</option>
                      <option v-for="version in bindableVersions" :key="String(version.id)" :value="version.id">
                        {{ version['模板版本'] }} · {{ version.status }} · {{ shortDigest(version['快照摘要']) }}
                      </option>
                    </select>
                  </label>
                  <div v-if="selectedVersion" class="param-box">
                    <p class="muted">
                      快照参数：{{ (selectedVersion['参数占位符'] as string[]).join(', ') || '无' }}
                      ；未填写的参数使用版本默认值。
                    </p>
                    <label v-for="name in selectedVersion['参数占位符'] as string[]" :key="name" class="filter-item">
                      <span>{{ name }}</span>
                      <input v-model="parameterInputs[name]" :placeholder="`默认 ${defaultOf(selectedVersion, name)}`" />
                    </label>
                  </div>
                  <label class="filter-item">
                    <span>对侧作业编号（可选，需已绑定同一版本）</span>
                    <input v-model="counterpartJobNo" placeholder="例如 ETLJ-0001" />
                  </label>
                  <div class="binding-actions">
                    <button class="btn primary" type="button" @click="submitBinding(row)">绑定并生成参数</button>
                    <span class="muted">同一版本同一参数重复提交只保留一个绑定结果</span>
                  </div>
                </div>

                <div v-else-if="panelMode === 'detail' && bindingDetail" class="binding-detail">
                  <table class="data-table nested">
                    <tbody>
                      <tr><th>模板</th><td>{{ bindingDetail.binding['模板编号'] }}</td></tr>
                      <tr><th>绑定版本</th><td>{{ bindingDetail.binding['模板版本'] }}（{{ bindingDetail.binding['版本状态'] }}）</td></tr>
                      <tr><th>当前发布版本</th><td>{{ bindingDetail.binding['模板当前发布版本'] ?? '—' }} · {{ bindingDetail.binding['版本是否最新'] ? '已是最新' : '发现新版本' }}</td></tr>
                      <tr><th>快照摘要</th><td class="mono">{{ bindingDetail.binding['快照摘要'] }}</td></tr>
                      <tr><th>参数绑定</th><td>{{ formatDefaults(bindingDetail.binding['参数绑定']) }}</td></tr>
                      <tr><th>参数绑定摘要</th><td class="mono">{{ shortDigest(bindingDetail.binding['参数绑定摘要']) }}</td></tr>
                      <tr><th>渲染SQL</th><td><pre class="sql-box">{{ bindingDetail.binding['渲染SQL'] }}</pre></td></tr>
                      <tr><th>绑定时间</th><td>{{ bindingDetail.binding['绑定时间'] }}</td></tr>
                      <tr v-if="bindingDetail.binding['对侧作业编号']"><th>对侧作业</th><td>{{ bindingDetail.binding['对侧作业编号'] }}（沿用同一版本）</td></tr>
                    </tbody>
                  </table>
                  <details v-if="(bindingDetail.history as Row[]).length > 1">
                    <summary>绑定历史（{{ (bindingDetail.history as Row[]).length }}）</summary>
                    <ul class="history-list">
                      <li v-for="item in bindingDetail.history as Row[]" :key="String(item['绑定ID'])">
                        {{ item['模板版本'] }} · {{ shortDigest(item['快照摘要']) }} · {{ item['绑定时间'] }}
                      </li>
                    </ul>
                  </details>
                </div>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无ETL作业数据，可先登记作业定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条ETL作业记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/etljob'
const columns = ["作业编号", "作业名称", "源端数据表", "目标数据表", "执行SQL", "调度表达式", "超时分钟", "失败重试次数"]
const actions = ["提交作业评审", "发布作业", "下线作业"]
const statuses = ["草稿", "作业评审中", "已发布", "已下线"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const activeId = ref<string | number | null>(null)
const panelMode = ref<'bind' | 'detail'>('bind')
const panelError = ref('')
const panelMessage = ref('')
const templates = ref<Row[]>([])
const bindableVersions = ref<Row[]>([])
const selectedTemplateId = ref(0)
const selectedVersionId = ref(0)
const parameterInputs = reactive<Record<string, string>>({})
const counterpartJobNo = ref('')
const bindingDetail = ref<Row | null>(null)

const statCards = computed(() => [
  { label: '作业定义总数', value: total.value },
  { label: '已绑定模板版本', value: rows.value.filter((row) => row['绑定ID']).length },
  { label: '模板有新版本', value: rows.value.filter((row) => row['模板发布状态'] === '发现新版本').length },
])

const selectedVersion = computed(() => bindableVersions.value.find((item) => item.id === selectedVersionId.value) ?? null)

function shortDigest(value: unknown) {
  return value ? String(value).slice(0, 12) : '—'
}

function formatDefaults(value: unknown) {
  if (!value || typeof value !== 'object') return '—'
  const entries = Object.entries(value as Record<string, unknown>)
  if (!entries.length) return '—'
  return entries.map(([key, item]) => `${key}=${item ?? ''}`).join('，')
}

function defaultOf(version: Row, name: string) {
  const defaults = (version['参数默认值'] ?? {}) as Record<string, unknown>
  return name in defaults ? String(defaults[name]) : '无默认值'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  const jobNo = window.prompt('作业编号，例如 ETLJ-0101')
  if (!jobNo) return
  const jobName = window.prompt('作业名称') ?? ''
  const sourceTable = window.prompt('源端数据表') ?? ''
  void createJob(jobNo, jobName, sourceTable)
}

async function createJob(jobNo: string, jobName: string, sourceTable: string) {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { 作业编号: jobNo, 作业名称: jobName, 源端数据表: sourceTable } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '作业登记失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '作业登记失败'
  }
}

function closePanel() {
  activeId.value = null
  bindingDetail.value = null
  panelError.value = ''
  panelMessage.value = ''
}

async function openBinding(row: Row) {
  activeId.value = row.id
  panelMode.value = 'bind'
  panelError.value = ''
  panelMessage.value = ''
  bindingDetail.value = null
  selectedTemplateId.value = 0
  selectedVersionId.value = 0
  bindableVersions.value = []
  counterpartJobNo.value = ''
  Object.keys(parameterInputs).forEach((key) => delete parameterInputs[key])
  try {
    if (!templates.value.length) {
      const response = await request('/api/sqltpl?size=200')
      if (!response.ok) throw new Error('模板列表读取失败')
      const payload = await response.json()
      templates.value = payload.items ?? []
    }
  } catch (error) {
    panelError.value = error instanceof Error ? error.message : '模板列表读取失败'
  }
}

async function loadBindableVersions() {
  panelError.value = ''
  selectedVersionId.value = 0
  bindableVersions.value = []
  Object.keys(parameterInputs).forEach((key) => delete parameterInputs[key])
  if (!selectedTemplateId.value) return
  try {
    const response = await request(`${ENDPOINT}/templates/${selectedTemplateId.value}/versions?size=100`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '可用版本读取失败')
    bindableVersions.value = payload.items ?? []
    if (bindableVersions.value.length) {
      selectedVersionId.value = bindableVersions.value[0].id
    }
  } catch (error) {
    panelError.value = error instanceof Error ? error.message : '可用版本读取失败'
  }
}

async function submitBinding(row: Row) {
  panelError.value = ''
  panelMessage.value = ''
  if (!selectedVersionId.value) {
    panelError.value = '请选择已发布的模板版本'
    return
  }
  const parameters: Record<string, string> = {}
  Object.entries(parameterInputs).forEach(([key, value]) => {
    if (String(value).trim() !== '') parameters[key] = value
  })
  try {
    const response = await request(`${ENDPOINT}/${row.id}/bind-template`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          模板ID: selectedTemplateId.value,
          模板版本ID: selectedVersionId.value,
          参数绑定: parameters,
          对侧作业编号: counterpartJobNo.value.trim() || undefined,
        },
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '模板版本绑定失败')
    panelMessage.value = `${payload.message}${payload.entry?.idempotent ? '（幂等复用）' : ''}`
    await reload()
    await viewBinding(row, false)
    panelMode.value = 'detail'
  } catch (error) {
    panelError.value = error instanceof Error ? error.message : '模板版本绑定失败'
  }
}

async function viewBinding(row: Row, clearMessage = true) {
  activeId.value = row.id
  panelMode.value = 'detail'
  panelError.value = ''
  if (clearMessage) panelMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/binding`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '绑定信息读取失败')
    bindingDetail.value = payload
  } catch (error) {
    bindingDetail.value = null
    panelError.value = error instanceof Error ? error.message : '绑定信息读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || 'ETL作业动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'ETL作业操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('作业定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'ETL作业列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.binding-cell { background: #f8fafc; }
.binding-panel { display: flex; flex-direction: column; gap: 8px; }
.binding-head { display: flex; justify-content: space-between; align-items: center; }
.binding-form { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.binding-form select, .binding-form input { min-width: 180px; }
.param-box { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; width: 100%; }
.binding-actions { display: flex; gap: 10px; align-items: center; width: 100%; }
.binding-detail { display: flex; flex-direction: column; gap: 8px; }
.data-table.nested { background: #fff; }
.sql-box { margin: 0; white-space: pre-wrap; font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.history-list { margin: 6px 0 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
.muted { color: var(--muted); font-size: 12px; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.ok-text { color: #067647; }
</style>
