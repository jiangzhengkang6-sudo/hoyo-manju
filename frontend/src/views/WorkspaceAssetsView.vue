<script setup lang="ts">
/**
 * 项目级资产库（PRD §5.1-6）。
 * 跨项目汇总角色与画风，支持按类型（全部 / 角色 / 画风）、来源项目与名称过滤。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import {
  characterStatusLabel,
  characterStatusTone,
  isColorLike,
  normalizeAssets,
  normalizePalette,
  workspaceApi,
  type WorkspaceAssetKind,
  type WorkspaceAssets,
  type WorkspaceStyleAsset,
} from '@/api/workspace'
import { ApiError, toApiError } from '@/api/errors'
import { relativeTime, truncate } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import MediaThumb from '@/components/MediaThumb.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'

/** 分段控件选项；`value` 直接作为 `kind` 查询参数（空串表示不传）。 */
const KIND_TABS: { value: WorkspaceAssetKind | ''; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'character', label: '角色' },
  { value: 'style', label: '画风' },
]

const kind = ref<WorkspaceAssetKind | ''>('')
const projectId = ref('')
/** 搜索框的即时值；真正的请求走防抖后的 `query`。 */
const searchInput = ref('')
const query = ref('')

const assets = ref<WorkspaceAssets>({ characters: [], styles: [], projects: [], total: 0 })
const loading = ref(false)
const loadError = ref<ApiError | null>(null)
/** 首次加载完成前不展示空态，避免闪一下「没有匹配」。 */
const loaded = ref(false)

let debounceTimer: number | undefined

/** 防抖 300ms：输入过程中不发请求，停下后一次性查询。 */
watch(searchInput, (value) => {
  if (debounceTimer !== undefined) window.clearTimeout(debounceTimer)
  debounceTimer = window.setTimeout(() => {
    query.value = value.trim()
  }, 300)
})

/** 类型 / 项目切换立即生效（下拉与分段控件无输入抖动）。 */
watch([kind, projectId], () => {
  void load()
})

watch(query, () => {
  void load()
})

async function load(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const raw = await workspaceApi.assets({
      kind: kind.value || undefined,
      project_id: projectId.value || undefined,
      q: query.value || undefined,
    })
    assets.value = normalizeAssets(raw)
  } catch (err) {
    loadError.value = toApiError(err)
  } finally {
    loading.value = false
    loaded.value = true
  }
}

function resetFilters(): void {
  searchInput.value = ''
  query.value = ''
  projectId.value = ''
  kind.value = ''
}

const characters = computed(() => assets.value.characters)
const styles = computed(() => assets.value.styles)
const projects = computed(() => assets.value.projects)

/** 是否处于过滤状态，用于区分「库为空」与「无匹配」。 */
const filtering = computed(() => Boolean(query.value || projectId.value || kind.value))

/** 当前按类型过滤后应该展示的条数，用于空态判定。 */
const visibleCount = computed(() => {
  if (kind.value === 'character') return characters.value.length
  if (kind.value === 'style') return styles.value.length
  return characters.value.length + styles.value.length
})

const showCharacters = computed(() => kind.value !== 'style')
const showStyles = computed(() => kind.value !== 'character')

function paletteOf(style: WorkspaceStyleAsset): string[] {
  return normalizePalette(style.palette)
}

/** 色板里可直接当颜色渲染的值。 */
function paletteColors(style: WorkspaceStyleAsset): string[] {
  return paletteOf(style).filter(isColorLike)
}

/** 非颜色值（如 `冷色调` 这种描述），用纯文本呈现。 */
function paletteTexts(style: WorkspaceStyleAsset): string[] {
  return paletteOf(style).filter((v) => !isColorLike(v))
}

onMounted(load)

onBeforeUnmount(() => {
  if (debounceTimer !== undefined) window.clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">资产库</h1>
        <p class="muted small">
          跨项目汇总的角色与画风资产。角色参考图来自 `primary_media_id`，画风色板按后端返回值防御式渲染。
        </p>
      </div>
      <div class="row-wrap">
        <RouterLink to="/workspace" class="btn btn-ghost">返回工作区</RouterLink>
        <button type="button" class="btn" :disabled="loading" @click="load">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </header>

    <section class="card card-pad toolbar">
      <div class="seg" role="tablist" aria-label="资产类型">
        <button
          v-for="tab in KIND_TABS"
          :key="tab.value || 'all'"
          type="button"
          class="seg-item"
          role="tab"
          :class="{ 'seg-active': kind === tab.value }"
          :aria-selected="kind === tab.value"
          @click="kind = tab.value"
        >
          {{ tab.label }}
        </button>
      </div>

      <div class="toolbar-filters">
        <div class="field tb-field">
          <label class="label" for="wa-project">来源项目</label>
          <select id="wa-project" v-model="projectId" class="select">
            <option value="">全部项目</option>
            <option v-for="project in projects" :key="project.id" :value="project.id">
              {{ project.name }}
            </option>
          </select>
        </div>

        <div class="field tb-field">
          <label class="label" for="wa-search">名称搜索</label>
          <input
            id="wa-search"
            v-model="searchInput"
            class="input"
            type="search"
            placeholder="输入角色名或画风名…"
          />
          <span class="hint">输入停止 300ms 后自动查询</span>
        </div>

        <button
          type="button"
          class="btn btn-ghost tb-reset"
          :disabled="!filtering"
          @click="resetFilters"
        >
          重置筛选
        </button>
      </div>

      <div class="row-wrap tb-summary">
        <span class="badge">共 {{ assets.total }} 条</span>
        <span class="tiny dim">角色 {{ characters.length }} · 画风 {{ styles.length }}</span>
        <span v-if="loading" class="tiny dim">查询中…</span>
      </div>
    </section>

    <SkeletonBlock v-if="loading && !loaded" variant="cards" :rows="6" />

    <ErrorState
      v-else-if="loadError"
      :error="loadError"
      context="加载资产库"
      @retry="load"
    />

    <EmptyState
      v-else-if="!visibleCount"
      icon="◇"
      :title="filtering ? '没有匹配的资产' : '资产库还是空的'"
      :description="
        filtering
          ? '换个关键词、项目或类型再试试。'
          : '在项目里创建角色与画风后，这里会跨项目汇总展示。'
      "
    >
      <button v-if="filtering" type="button" class="btn btn-primary" @click="resetFilters">
        重置筛选
      </button>
      <RouterLink v-else to="/" class="btn btn-primary">前往项目列表</RouterLink>
    </EmptyState>

    <template v-else>
      <!-- 角色 -->
      <section v-if="showCharacters && characters.length" class="col">
        <div class="row-between sec-head">
          <span class="section-title">角色</span>
          <span class="mono tiny dim">{{ characters.length }}</span>
        </div>

        <div class="grid-cards">
          <article
            v-for="item in characters"
            :key="item.id"
            class="card character-card"
          >
            <div class="cc-thumb">
              <MediaThumb
                :media-id="item.primary_media_id"
                :alt="item.name"
                :portrait="false"
              />
            </div>

            <div class="cc-body">
              <div class="cc-head">
                <span class="cc-name ellipsis" :title="item.name">{{ item.name }}</span>
                <span class="badge" :class="characterStatusTone(item.status)">
                  {{ characterStatusLabel(item.status) }}
                </span>
              </div>

              <div class="cc-project tiny dim">
                <span aria-hidden="true">▤</span>
                <span class="ellipsis" :title="item.project_name ?? item.project_id ?? '未知项目'">
                  {{ item.project_name ?? '未知项目' }}
                </span>
              </div>

              <p v-if="item.identity" class="cc-identity small" :title="item.identity">
                {{ item.identity }}
              </p>
              <p v-else class="cc-identity small dim-2">未填写身份</p>

              <p v-if="item.appearance" class="cc-appearance tiny dim">
                {{ truncate(item.appearance, 52) }}
              </p>

              <div class="cc-foot mono tiny dim-2">
                <span :title="item.id">{{ truncate(item.id, 18) }}</span>
                <span>{{ relativeTime(item.created_at) }}</span>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- 画风 -->
      <section v-if="showStyles && styles.length" class="col">
        <div class="row-between sec-head">
          <span class="section-title">画风</span>
          <span class="mono tiny dim">{{ styles.length }}</span>
        </div>

        <div class="grid-cards">
          <article v-for="style in styles" :key="style.id" class="card style-card">
            <div class="sc-head">
              <span class="sc-name ellipsis" :title="style.name ?? style.id">
                {{ style.name ?? '未命名画风' }}
              </span>
              <span v-if="style.is_default" class="badge badge-accent">项目默认</span>
            </div>

            <div class="sc-project tiny dim">
              <span aria-hidden="true">▤</span>
              <span class="ellipsis" :title="style.project_name ?? style.project_id ?? '未知项目'">
                {{ style.project_name ?? '未知项目' }}
              </span>
              <span v-if="style.version != null" class="mono">v{{ style.version }}</span>
            </div>

            <p v-if="style.description" class="sc-desc small">{{ style.description }}</p>

            <!-- 色板：hex 类值画色块，其余按纯文本 -->
            <div v-if="paletteOf(style).length" class="sc-palette">
              <div v-if="paletteColors(style).length" class="swatches">
                <span
                  v-for="color in paletteColors(style)"
                  :key="color"
                  class="swatch"
                  :style="{ background: color }"
                  :title="color"
                />
              </div>
              <div v-if="paletteTexts(style).length" class="row-wrap palette-text">
                <span v-for="text in paletteTexts(style)" :key="text" class="chip">{{ text }}</span>
              </div>
            </div>
            <p v-else class="tiny dim-2">未提供色板</p>

            <dl class="sc-meta">
              <div v-if="style.camera_style" class="sc-meta-row">
                <dt class="tiny dim">镜头</dt>
                <dd class="tiny">{{ style.camera_style }}</dd>
              </div>
              <div v-if="style.lighting_style" class="sc-meta-row">
                <dt class="tiny dim">光线</dt>
                <dd class="tiny">{{ style.lighting_style }}</dd>
              </div>
            </dl>

            <div class="sc-foot mono tiny dim-2">
              <span>{{ relativeTime(style.created_at) }}</span>
            </div>
          </article>
        </div>
      </section>

      <!-- 只筛出了画风但当前类型下为空时的兜底（各分区已单独判空） -->
      <p v-if="showCharacters && !characters.length && showStyles && styles.length" class="tiny dim">
        当前筛选下没有匹配的角色。
      </p>
      <p v-if="showStyles && !styles.length && showCharacters && characters.length" class="tiny dim">
        当前筛选下没有匹配的画风。
      </p>
    </template>
  </div>
</template>

<style scoped>
.page {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: var(--gap-lg);
  max-width: 1400px;
}
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.page-head p {
  margin-top: 4px;
  max-width: 680px;
}
.sec-head {
  margin-bottom: 2px;
}

/* 工具栏 */
.toolbar {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.toolbar-filters {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
}
.tb-field {
  min-width: 220px;
}
.tb-reset {
  margin-bottom: 1px;
}
.tb-summary {
  padding-top: 10px;
  border-top: 1px solid var(--line);
}

/* 分段控件 */
.seg {
  display: inline-flex;
  padding: 3px;
  gap: 3px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: var(--bg-1);
  width: fit-content;
}
.seg-item {
  height: 28px;
  padding: 0 14px;
  border: 1px solid transparent;
  border-radius: var(--radius-xs);
  background: transparent;
  color: var(--text-1);
  font-size: 12.5px;
  font-weight: 560;
  cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.seg-item:hover {
  color: var(--text-0);
  background: var(--neutral-soft);
}
.seg-active {
  background: var(--accent-soft);
  border-color: var(--line-accent);
  color: var(--text-0);
}

/* 角色卡片 */
.character-card {
  padding: 12px;
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.cc-thumb {
  width: 108px;
  flex: 0 0 auto;
}
.cc-body {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
  flex: 1 1 auto;
}
.cc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.cc-name {
  font-size: 15px;
  font-weight: 650;
  min-width: 0;
}
.cc-project {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}
.cc-identity {
  margin: 0;
  line-height: 1.5;
}
.cc-appearance {
  margin: 0;
  line-height: 1.5;
}
.cc-foot {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}

/* 画风卡片 */
.style-card {
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 9px;
}
.sc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.sc-name {
  font-size: 15px;
  font-weight: 650;
  min-width: 0;
}
.sc-project {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.sc-desc {
  margin: 0;
  line-height: 1.55;
  color: var(--text-1);
}
.sc-palette {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.swatches {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.swatch {
  width: 26px;
  height: 26px;
  border-radius: var(--radius-xs);
  border: 1px solid var(--line-strong);
  display: block;
}
.palette-text {
  gap: 6px;
}
.sc-meta {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.sc-meta-row {
  display: flex;
  gap: 8px;
  margin: 0;
}
.sc-meta-row dt {
  flex: 0 0 auto;
}
.sc-meta-row dd {
  margin: 0;
  color: var(--text-1);
  min-width: 0;
}
.sc-foot {
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}
</style>
