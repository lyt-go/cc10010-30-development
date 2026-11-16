<template>
  <section class="page" data-module="sqltpl">
    <header class="page-head">
      <div>
        <h2>SQL模板管理</h2>
        <p class="page-desc">管理可复用的ETL SQL模板，支持参数化替换和版本管理。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记模板定义</button>
        <button class="btn" type="button" @click="openVersionForm">创建模板版本</button>
        <button class="btn" type="button" @click="exportRows">导出SQL模板清单</button>
        <button class="btn" type="button" @click="downloadBundle">打包导出发布版本</button>
        <button class="btn" type="button" @click="bundleInput?.click()">导入版本包</button>
        <input ref="bundleInput" type="file" accept="application/json,.json" hidden @change="importBundle" />
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
          <th>当前发布版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="row in rows" :key="String(row.id)">
          <tr>
            <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
            <td>{{ row['当前发布版本'] ?? '未发布' }}</td>
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
              <button class="link" type="button" @click="toggleVersions(row)">
                {{ String(expandedId) === String(row.id) ? '收起版本' : '版本与发布' }}
              </button>
            </td>
          </tr>
          <tr v-if="String(expandedId) === String(row.id)">
            <td :colspan="columns.length + 2" class="version-cell">
              <div class="version-panel">
                <div class="version-head">
                  <strong>{{ row['模板编号'] }} 的版本</strong>
                  <span class="muted">
                    发布版本数 {{ row['已发布版本数'] ?? 0 }} · 草稿 {{ row['草稿版本数'] ?? 0 }}
                  </span>
                </div>
                <p v-if="versionError" class="error-text">{{ versionError }}</p>
                <p v-if="versionMessage" class="ok-text">{{ versionMessage }}</p>
                <table class="data-table nested">
                  <thead>
                    <tr>
                      <th>版本</th>
                      <th>状态</th>
                      <th>参数占位符</th>
                      <th>参数默认值</th>
                      <th>快照摘要</th>
                      <th>发布时间</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="version in versions" :key="String(version.id)">
                      <td>{{ version['模板版本'] }}</td>
                      <td>{{ version.status }}</td>
                      <td>{{ (version['参数占位符'] as string[]).join(', ') || '—' }}</td>
                      <td>{{ formatDefaults(version['参数默认值']) }}</td>
                      <td class="mono">{{ shortDigest(version['快照摘要']) }}</td>
                      <td>{{ version['发布时间'] ?? '—' }}</td>
                      <td class="row-actions">
                        <button class="link" type="button" @click="downloadVersion(row, version)">下载SQL</button>
                        <button
                          v-if="version.status === '草稿'"
                          class="link"
                          type="button"
                          @click="publishVersion(row, version)"
                        >
                          发布
                        </button>
                        <button class="link" type="button" @click="showImpact(row, version)">影响面</button>
                      </td>
                    </tr>
                    <tr v-if="!versions.length">
                      <td colspan="7" class="empty-state">该模板还没有版本，可上传 SQL 文件或创建版本</td>
                    </tr>
                  </tbody>
                </table>
                <div class="version-foot">
                  <button class="btn" type="button" @click="openVersionForm(row)">创建新版本</button>
                  <button class="btn" type="button" @click="uploadInput?.click()">上传SQL文件</button>
                  <input
                    ref="uploadInput"
                    type="file"
                    accept=".sql,text/plain"
                    hidden
                    @change="(event) => uploadVersionFile(row, event)"
                  />
                  <a
                    class="btn"
                    :href="`${ENDPOINT}/export/bundle`"
                    target="_blank"
                    rel="noreferrer"
                  >打包全部版本</a>
                </div>
                <div v-if="impact" class="impact-box">
                  <strong>发布影响面：{{ impact['待发布版本'] }} ← 当前 {{ impact['当前发布版本'] ?? '无' }}</strong>
                  <p class="muted">旧版本绑定作业 {{ impact['旧版本绑定作业数'] }} 个；发布只影响新绑定，已绑定作业继续使用原版本快照。</p>
                  <ul v-if="(impact['参数冲突'] as string[]).length" class="impact-list">
                    <li v-for="item in impact['参数冲突'] as string[]" :key="item">{{ item }}</li>
                  </ul>
                  <ul v-else class="impact-list">
                    <li>参数契约无变化</li>
                  </ul>
                </div>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无SQL模板数据，可先登记模板定义</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条SQL模板记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/sqltpl'
const columns = ["模板编号", "模板名称", "模板类型", "SQL片段", "参数占位符", "预期输出列", "参数默认值", "模板版本"]
const actions = ["提交模板评审", "废弃模板"]
const statuses = ["草稿", "模板评审中", "已发布", "已废弃"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const expandedId = ref<string | number | null>(null)
const versions = ref<Row[]>([])
const versionError = ref('')
const versionMessage = ref('')
const impact = ref<Row | null>(null)
const bundleInput = ref<HTMLInputElement | null>(null)
const uploadInput = ref<HTMLInputElement | null>(null)

const statCards = computed(() => [
  { label: '模板定义总数', value: total.value },
  { label: '已发布模板', value: rows.value.filter((row) => row['当前发布版本']).length },
  { label: '评审中模板', value: rows.value.filter((row) => row.status === '模板评审中').length },
])

function formatCell(row: Row, column: string) {
  const value = row[column]
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') return formatDefaults(value)
  return String(value)
}

function formatDefaults(value: unknown) {
  if (!value || typeof value !== 'object') return '—'
  const entries = Object.entries(value as Record<string, unknown>)
  if (!entries.length) return '—'
  return entries.map(([key, item]) => `${key}=${item ?? ''}`).join('，')
}

function shortDigest(value: unknown) {
  return value ? String(value).slice(0, 12) : '—'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function downloadBundle() {
  window.open(`${ENDPOINT}/export/bundle`, '_blank')
}

function openCreate() {
  const templateNo = window.prompt('模板编号，例如 SQLT-0101')
  if (!templateNo) return
  const templateName = window.prompt('模板名称') ?? ''
  const templateType = window.prompt('模板类型，例如 聚合 / 清洗 / 关联') ?? ''
  void createTemplate(templateNo, templateName, templateType)
}

async function createTemplate(templateNo: string, templateName: string, templateType: string) {
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { 模板编号: templateNo, 模板名称: templateName, 模板类型: templateType } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '模板登记失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '模板登记失败'
  }
}

function openVersionForm(row?: Row) {
  const template = row ?? rows.value[0]
  if (!template) {
    errorMessage.value = '请先登记模板定义'
    return
  }
  const version = window.prompt('新版本号，例如 v2')
  if (!version) return
  const sql = window.prompt('SQL 片段（使用 {{ 参数名 }} 占位符）')
  if (!sql) return
  const placeholderText = window.prompt('参数占位符，逗号分隔，例如 dt, region_id') ?? ''
  const defaultsText = window.prompt('参数默认值，格式 参数=值，逗号分隔，例如 dt=2026-11-16') ?? ''
  void createVersion(template, version, sql, placeholderText, defaultsText)
}

async function createVersion(template: Row, version: string, sql: string, placeholderText: string, defaultsText: string) {
  versionError.value = ''
  versionMessage.value = ''
  errorMessage.value = ''
  const placeholders = placeholderText.split(/[,，\s]+/).filter(Boolean)
  const defaults: Record<string, string> = {}
  defaultsText.split(/[,，;；]+/).filter(Boolean).forEach((chunk) => {
    const [key, value] = chunk.split('=')
    if (key.trim()) defaults[key.trim()] = (value ?? '').trim()
  })
  try {
    const response = await request(`${ENDPOINT}/${template.id}/versions`, {
      method: 'POST',
      body: JSON.stringify({
        values: { 模板版本: version, SQL片段: sql, 参数占位符: placeholders, 参数默认值: defaults },
      }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '版本创建失败')
    versionMessage.value = payload.message
    expandedId.value = template.id
    await reload()
    await loadVersions(template)
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : '版本创建失败'
  }
}

async function toggleVersions(row: Row) {
  if (String(expandedId.value) === String(row.id)) {
    expandedId.value = null
    versions.value = []
    impact.value = null
    return
  }
  expandedId.value = row.id
  await loadVersions(row)
}

async function loadVersions(row: Row) {
  versionError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/versions?size=100`)
    if (!response.ok) throw new Error('版本列表读取失败')
    const payload = await response.json()
    versions.value = payload.items ?? []
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : '版本列表读取失败'
  }
}

async function publishVersion(row: Row, version: Row) {
  versionError.value = ''
  versionMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/versions/${version.id}/publish`, {
      method: 'POST',
      body: JSON.stringify({ values: { 快照摘要: version['快照摘要'] } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '版本发布失败')
    versionMessage.value = payload.message
    await reload()
    await loadVersions(row)
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : '版本发布失败'
  }
}

function downloadVersion(row: Row, version: Row) {
  window.open(`${ENDPOINT}/${row.id}/versions/${version.id}/file`, '_blank')
}

async function showImpact(row: Row, version: Row) {
  versionError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/versions/${version.id}/impact`)
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.message || '影响面读取失败')
    impact.value = payload
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : '影响面读取失败'
  }
}

async function uploadVersionFile(row: Row, event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  versionError.value = ''
  versionMessage.value = ''
  const version = window.prompt('新版本号（留空自动生成）') ?? ''
  const placeholders = window.prompt('参数占位符，逗号分隔；留空继承当前发布版本') ?? ''
  const defaults = window.prompt('参数默认值，格式 参数=值；留空继承当前发布版本') ?? ''
  const form = new FormData()
  form.append('file', file)
  if (version.trim()) form.append('模板版本', version.trim())
  if (placeholders.trim()) form.append('参数占位符', placeholders.trim())
  if (defaults.trim()) form.append('参数默认值', defaults.trim())
  try {
    const response = await fetch(`${ENDPOINT}/${row.id}/versions/upload`, { method: 'POST', body: form })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '文件上传失败')
    versionMessage.value = payload.message
    expandedId.value = row.id
    await reload()
    await loadVersions(row)
  } catch (error) {
    versionError.value = error instanceof Error ? error.message : '文件上传失败'
  }
}

async function importBundle(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  errorMessage.value = ''
  const form = new FormData()
  form.append('file', file)
  try {
    const response = await fetch(`${ENDPOINT}/import`, { method: 'POST', body: form })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '版本包导入失败')
    errorMessage.value = `导入完成：新建 ${payload.imported_count} 个版本，复用 ${payload.reused_count} 个版本`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本包导入失败'
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
      throw new Error(payload.message || 'SQL模板动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SQL模板操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('模板定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SQL模板列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.version-cell { background: #f8fafc; }
.version-panel { display: flex; flex-direction: column; gap: 8px; }
.version-head { display: flex; justify-content: space-between; align-items: center; }
.data-table.nested { background: #fff; }
.version-foot { display: flex; gap: 8px; flex-wrap: wrap; }
.impact-box { border: 1px dashed var(--border); border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.impact-list { margin: 6px 0 0; padding-left: 18px; color: #b42318; }
.muted { color: var(--muted); font-size: 12px; }
.mono { font-family: 'SFMono-Regular', Consolas, monospace; font-size: 12px; }
.ok-text { color: #067647; }
</style>
