<script setup lang="ts">
/** 项目详情：概览 + 剧集列表 + 成本 + 审计（API.md §2、§3、§15）。 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useProjectStore } from '@/stores/project'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { episodesApi, stageLabel } from '@/api/episodes'
import { formatCents, stageCostLabel } from '@/api/cost'
import { formatDateTime, relativeTime } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import StatTile from '@/components/StatTile.vue'
import type { Episode } from '@/types'

const props = defineProps<{ projectId: string }>()

const router = useRouter()
const store = useProjectStore()
const config = useConfigStore()
const toast = useToastStore()

const {
  current,
  episodes,
  cost,
  audit,
  detailLoading,
  detailError,
  listError,
} = storeToRefs(store)

const showCreate = ref(false)
const creating = ref(false)
const formError = ref('')
const busyEpisodeId = ref<string | null>(null)

const form = reactive({ title: '', episode_no: undefined as number | undefined, target_duration_ms: 60000 })

const projectError = computed(() => detailError.value ?? (current.value ? null : listError.value))

const costBuckets = computed(() => {
  const byStage = cost.value?.by_stage ?? []
  const max = Math.max(1, ...byStage.map((b) => b.cents ?? 0))
  return byStage.map((b) => ({
    label: stageCostLabel(b.stage),
    cents: b.cents ?? 0,
    width: ((b.cents ?? 0) / max) * 100,
  }))
})

const kindBuckets = computed(() => {
  const byKind = cost.value?.by_kind ?? []
  const max = Math.max(1, ...byKind.map((b) => b.cents ?? 0))
  return byKind.map((b) => ({
    label: b.kind ?? '未分类',
    cents: b.cents ?? 0,
    width: ((b.cents ?? 0) / max) * 100,
  }))
})

const episodeProgress = computed(() => {
  const total = episodes.value.length
  const done = episodes.value.filter((e) => e.stage === 'EXPORTED').length
  return { total, done }
})

async function load(): Promise<void> {
  await store.loadDetail(props.projectId)
}

onMounted(async () => {
  await config.load()
  await load()
})

watch(
  () => props.projectId,
  () => void load(),
)

function openCreate(): void {
  form.title = ''
  const last = episodes.value[episodes.value.length - 1]
  form.episode_no = (last?.episode_no ?? 0) + 1
  form.target_duration_ms = Math.min(60000, config.episodeMaxMs)
  formError.value = ''
  showCreate.value = true
}

async function submitCreate(): Promise<void> {
  if (!form.title.trim()) {
    formError.value = '剧集标题不能为空'
    return
  }
  if (!form.target_duration_ms || form.target_duration_ms > config.episodeMaxMs) {
    formError.value = `目标时长需在 1 ~ ${(config.episodeMaxMs / 1000).toFixed(0)} 秒之间`
    return
  }
  creating.value = true
  formError.value = ''
  try {
    const created = await store.createEpisode(props.projectId, {
      title: form.title.trim(),
      episode_no: form.episode_no,
      target_duration_ms: form.target_duration_ms,
    })
    showCreate.value = false
    toast.success('剧集已创建', created.title)
    void router.push({ name: 'episode-story', params: { episodeId: created.id } })
  } catch {
    /* api 层已提示 */
  } finally {
    creating.value = false
  }
}

function openEpisode(episode: Episode): void {
  void router.push({ name: 'episode-story', params: { episodeId: episode.id } })
}

async function copyEpisode(episode: Episode): Promise<void> {
  busyEpisodeId.value = episode.id
  try {
    const created = await episodesApi.copy(episode.id)
    toast.success('剧集已复制', created.title)
    await store.loadEpisodes(props.projectId)
  } catch {
    /* api 层已提示 */
  } finally {
    busyEpisodeId.value = null
  }
}

async function toggleArchiveEpisode(episode: Episode): Promise<void> {
  busyEpisodeId.value = episode.id
  try {
    if (episode.archived) await episodesApi.restore(episode.id)
    else await episodesApi.archive(episode.id)
    await store.loadEpisodes(props.projectId)
  } catch {
    /* api 层已提示 */
  } finally {
    busyEpisodeId.value = null
  }
}

/** 彻底删除剧集：内容全部清空、不可恢复（只想隐藏请用「归档」）。 */
async function deleteEpisode(episode: Episode): Promise<void> {
  const ok = window.confirm(
    `确定彻底删除剧集「${episode.title}」？\n\n` +
      '剧本、分镜、角色卡、音视频与导出记录都会一并删除，且无法恢复。\n' +
      '如果只是想从列表里隐藏，请改点「归档」（归档可恢复）。',
  )
  if (!ok) return
  busyEpisodeId.value = episode.id
  try {
    await episodesApi.remove(episode.id)
    toast.success('剧集已删除', episode.title)
    await store.loadEpisodes(props.projectId)
  } catch {
    /* api 层已提示（例如还有进行中的任务会返回 409） */
  } finally {
    busyEpisodeId.value = null
  }
}

async function toggleArchiveProject(): Promise<void> {
  if (!current.value) return
  try {
    if (current.value.archived) await store.restoreProject(props.projectId)
    else await store.archiveProject(props.projectId)
    await load()
  } catch {
    /* api 层已提示 */
  }
}
</script>

<template>
  <div class="page">
    <nav class="crumbs small dim">
      <RouterLink to="/">项目</RouterLink>
      <span>/</span>
      <span>{{ current?.name ?? projectId }}</span>
    </nav>

    <SkeletonBlock v-if="detailLoading && !current" :rows="3" :height="18" />

    <ErrorState
      v-else-if="projectError && !current"
      :error="projectError"
      context="加载项目"
      @retry="load"
    />

    <template v-else-if="current">
      <header class="page-head">
        <div>
          <div class="row-wrap">
            <h1 class="title-xl">{{ current.name }}</h1>
            <span v-if="current.archived" class="badge">已归档</span>
          </div>
          <div class="row-wrap" style="margin-top: 8px">
            <span v-if="current.genre" class="chip">{{ current.genre }}</span>
            <span class="chip mono">{{ current.aspect_ratio || '—' }}</span>
            <span class="chip mono">{{ current.default_language || '—' }}</span>
            <span v-if="current.style" class="chip">{{ current.style }}</span>
          </div>
        </div>
        <div class="row-wrap">
          <RouterLink
            class="btn btn-ghost"
            :to="{ name: 'project-assets', params: { projectId } }"
          >
            角色与画风资产库
          </RouterLink>
          <button type="button" class="btn btn-ghost" @click="load">刷新</button>
          <button type="button" class="btn btn-ghost" @click="toggleArchiveProject">
            {{ current.archived ? '恢复项目' : '归档项目' }}
          </button>
          <button type="button" class="btn btn-primary" @click="openCreate">新建剧集</button>
        </div>
      </header>

      <div class="stat-row">
        <StatTile label="剧集总数" :value="episodeProgress.total" tone="accent" />
        <StatTile label="已导出" :value="episodeProgress.done" tone="ok" />
        <StatTile label="项目成本" :value="formatCents(cost?.total_cents)" />
        <StatTile
          label="剩余配额"
          :value="cost?.remaining_quota_cents != null ? formatCents(cost.remaining_quota_cents) : '—'"
        />
      </div>

      <section class="card">
        <div class="card-head">
          <h2 class="title-md">剧集</h2>
          <span class="tiny dim mono">{{ episodes.length }} 个</span>
        </div>
        <EmptyState
          v-if="!episodes.length"
          compact
          icon="▦"
          title="还没有剧集"
          description="创建第一集，进入故事输入阶段。"
        >
          <button type="button" class="btn btn-primary btn-sm" @click="openCreate">新建剧集</button>
        </EmptyState>
        <table v-else class="table">
          <thead>
            <tr>
              <th style="width: 64px">集号</th>
              <th>标题</th>
              <th style="width: 110px">阶段</th>
              <th style="width: 110px">目标时长</th>
              <th style="width: 120px">更新时间</th>
              <th style="width: 280px" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="episode in episodes" :key="episode.id">
              <td class="mono dim">{{ episode.episode_no ?? '—' }}</td>
              <td>
                <div class="row" style="min-width: 0">
                  <span class="ellipsis">{{ episode.title }}</span>
                  <span v-if="episode.archived" class="badge">已归档</span>
                </div>
              </td>
              <td>
                <span
                  class="badge"
                  :class="episode.stage === 'EXPORTED' ? 'badge-ok' : episode.stage === 'BLOCKED' ? 'badge-danger' : 'badge-info'"
                >
                  {{ stageLabel(episode.stage) }}
                </span>
              </td>
              <td class="mono dim">
                {{ episode.target_duration_ms ? `${(episode.target_duration_ms / 1000).toFixed(0)}s` : '—' }}
              </td>
              <td class="mono dim">{{ relativeTime(episode.updated_at) }}</td>
              <td>
                <div class="row" style="justify-content: flex-end">
                  <button type="button" class="btn btn-sm btn-primary" @click="openEpisode(episode)">
                    打开工作台
                  </button>
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    :disabled="busyEpisodeId === episode.id"
                    @click="copyEpisode(episode)"
                  >
                    复制
                  </button>
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    :disabled="busyEpisodeId === episode.id"
                    @click="toggleArchiveEpisode(episode)"
                  >
                    {{ episode.archived ? '恢复' : '归档' }}
                  </button>
                  <button
                    type="button"
                    class="btn btn-sm btn-danger"
                    :disabled="busyEpisodeId === episode.id"
                    title="彻底删除（不可恢复）。只想隐藏请用「归档」"
                    @click="deleteEpisode(episode)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <div class="two-col">
        <section class="card">
          <div class="card-head">
            <h2 class="title-md">成本明细</h2>
            <span class="mono tiny dim">{{ formatCents(cost?.total_cents) }}</span>
          </div>
          <div class="card-body">
            <EmptyState v-if="!cost" compact icon="¥" title="暂无成本数据" description="生成内容后，这里会按阶段与类型汇总。" />
            <template v-else>
              <div class="bucket-group">
                <span class="section-title">按阶段</span>
                <div v-for="bucket in costBuckets" :key="bucket.label" class="bucket">
                  <span class="bucket-label tiny">{{ bucket.label }}</span>
                  <div class="progress">
                    <div class="progress-bar" :style="{ width: `${bucket.width}%` }" />
                  </div>
                  <span class="mono tiny bucket-cents">{{ formatCents(bucket.cents) }}</span>
                </div>
                <p v-if="!costBuckets.length" class="tiny dim">后端未返回 by_stage。</p>
              </div>
              <div class="bucket-group">
                <span class="section-title">按类型</span>
                <div v-for="bucket in kindBuckets" :key="bucket.label" class="bucket">
                  <span class="bucket-label tiny">{{ bucket.label }}</span>
                  <div class="progress">
                    <div class="progress-bar" :style="{ width: `${bucket.width}%` }" />
                  </div>
                  <span class="mono tiny bucket-cents">{{ formatCents(bucket.cents) }}</span>
                </div>
                <p v-if="!kindBuckets.length" class="tiny dim">后端未返回 by_kind。</p>
              </div>
            </template>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">审计日志</h2>
            <span class="mono tiny dim">{{ audit.length }} 条</span>
          </div>
          <div class="card-body">
            <EmptyState
              v-if="!audit.length"
              compact
              icon="≡"
              title="暂无审计记录"
              description="删除 / 导出 / 配额 / 供应商变更会记录在此。"
            />
            <ul v-else class="audit-list">
              <li v-for="(entry, i) in audit" :key="entry.id ?? i" class="audit-item">
                <div class="row-between">
                  <span class="badge badge-info">{{ entry.action ?? '事件' }}</span>
                  <span class="mono tiny dim">{{ formatDateTime(entry.created_at) }}</span>
                </div>
                <p class="tiny muted pre-wrap">{{ entry.detail ?? entry.target ?? '' }}</p>
                <span v-if="entry.actor" class="tiny dim-2">操作者 {{ entry.actor }}</span>
              </li>
            </ul>
          </div>
        </section>
      </div>
    </template>

    <ModalDialog :open="showCreate" title="新建剧集" :width="520" @close="showCreate = false">
      <div class="field">
        <label class="label" for="e-title">剧集标题<span class="req">*</span></label>
        <input id="e-title" v-model="form.title" class="input" placeholder="例如：第一话 · 雨夜追迹" />
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="e-no">集号</label>
          <input id="e-no" v-model.number="form.episode_no" class="input mono" type="number" min="1" />
        </div>
        <div class="field">
          <label class="label" for="e-dur">目标时长（毫秒）<span class="req">*</span></label>
          <input
            id="e-dur"
            v-model.number="form.target_duration_ms"
            class="input mono"
            type="number"
            :max="config.episodeMaxMs"
            min="1000"
            step="1000"
          />
          <span class="hint">上限 {{ (config.episodeMaxMs / 1000).toFixed(0) }}s（后台配置）</span>
        </div>
      </div>
      <p v-if="formError" class="field-error">{{ formError }}</p>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showCreate = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="creating" @click="submitCreate">
          {{ creating ? '创建中…' : '创建并进入工作台' }}
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
.crumbs {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}
.crumbs a:hover {
  color: var(--accent);
}
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--gap);
}
.bucket-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: 16px;
}
.bucket-group:last-child {
  margin-bottom: 0;
}
.bucket {
  display: grid;
  grid-template-columns: 92px 1fr 76px;
  align-items: center;
  gap: var(--space-3);
}
.bucket-label {
  color: var(--text-1);
}
.bucket-cents {
  text-align: right;
  color: var(--text-1);
}
.audit-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  max-height: 320px;
  overflow: auto;
}
.audit-item {
  border-left: 2px solid var(--line);
  padding-left: 10px;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}
</style>
