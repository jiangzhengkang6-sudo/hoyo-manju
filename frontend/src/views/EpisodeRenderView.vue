<script setup lang="ts">
/**
 * 预览与导出（API.md §12、§9）。
 * 时间线校验 → 低码率预览 → 正式导出 → 素材包 ZIP。
 */
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { downloadFile, reportError } from '@/api/client'
import { jobsApi } from '@/api/jobs'
import {
  EXPORT_KIND_LABEL,
  EXPORT_STATUS_LABEL,
  fetchExports,
  renderApi,
} from '@/api/render'
import { fetchApprovals, settingsApi } from '@/api/settings'
import { formatDateTime, formatDuration, formatSeconds, percent, relativeTime } from '@/utils/format'
import { ApiError, toApiError } from '@/api/errors'
import GateCard from '@/components/GateCard.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import MediaThumb from '@/components/MediaThumb.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import type {
  Approval,
  ExportGate,
  ExportRecord,
  Job,
  SafetyAppeal,
  SafetySummary,
  Timeline,
  TimelineValidation,
} from '@/types'

const store = useEpisodeStore()
const config = useConfigStore()
const toast = useToastStore()
const { workflow, pendingGate, jobSnapshots, shots } = storeToRefs(store)

const timeline = ref<Timeline | null>(null)
const validation = ref<TimelineValidation | null>(null)
const exports = ref<ExportRecord[]>([])
const approvals = ref<Approval[]>([])
const safety = ref<SafetySummary | null>(null)

const loading = ref(false)
const validating = ref(false)
const busy = ref(false)
const gateBusy = ref(false)
const notice = ref('')
const jobIds = ref<string[]>([])
const exportPreset = ref('')
const watermark = ref(false)
const loadError = ref<ApiError | null>(null)

const jobs = computed<Job[]>(() =>
  jobIds.value.map((id) => jobSnapshots.value[id]).filter((j): j is Job => Boolean(j)),
)

const previews = computed(() => exports.value.filter((e) => e.kind === 'preview'))
const packages = computed(() => exports.value.filter((e) => e.kind === 'package'))
const renders = computed(() => exports.value.filter((e) => e.kind !== 'preview' && e.kind !== 'package'))

const clips = computed(() => timeline.value?.clips ?? timeline.value?.shots ?? [])
const totalDurationMs = computed(
  () => timeline.value?.total_duration_ms ?? store.totalDurationMs ?? 0,
)

const latestPreview = computed(() => previews.value.find((e) => e.media_id) ?? null)

const episodeId = computed(() => store.episodeId ?? '')

async function loadTimeline(): Promise<void> {
  if (!episodeId.value) return
  loading.value = true
  loadError.value = null
  try {
    timeline.value = await renderApi.timeline(episodeId.value)
  } catch (err) {
    timeline.value = null
    loadError.value = toApiError(err)
  } finally {
    loading.value = false
  }
}

async function loadExports(): Promise<void> {
  if (!episodeId.value) return
  exports.value = await fetchExports(episodeId.value).catch(() => [])
}

async function loadApprovals(): Promise<void> {
  if (!episodeId.value) return
  approvals.value = await fetchApprovals(episodeId.value).catch(() => [])
  safety.value = await settingsApi.safety(episodeId.value).catch(() => null)
}

async function loadAll(): Promise<void> {
  await Promise.all([loadTimeline(), loadExports(), loadApprovals(), loadGate()])
}

/* ---------------- §12-8 内容拦截：闸门 + 申诉 ---------------- */

const gate = ref<ExportGate | null>(null)
const appeals = ref<SafetyAppeal[]>([])
const appealReason = ref('')
const appealDetail = ref('')
const appealTarget = ref('')
const appealBusy = ref(false)

/** 被拦截且尚未获人工批准的素材 —— 这些才是真正挡住导出的。 */
const unresolved = computed(() => gate.value?.unresolved ?? [])
const canExport = computed(() => gate.value?.can_export ?? true)
/** 每条未解决素材的申诉状态，用来决定显示「去申诉」还是「等复核」。 */
function appealFor(mediaId: string): SafetyAppeal | undefined {
  return appeals.value.find((a) => a.target_id === mediaId)
}

function appealStatusLabel(status?: string): string {
  if (status === 'APPROVED') return '已批准'
  if (status === 'REJECTED') return '已驳回'
  return '待复核'
}

async function loadGate(): Promise<void> {
  if (!episodeId.value) return
  try {
    gate.value = await renderApi.exportGate(episodeId.value)
  } catch {
    gate.value = null
  }
  try {
    appeals.value = (await renderApi.appeals(episodeId.value)).items ?? []
  } catch {
    appeals.value = []
  }
}

async function submitAppeal(): Promise<void> {
  if (!episodeId.value) return
  if (!appealReason.value.trim()) {
    toast.warn('请填写申诉理由')
    return
  }
  appealBusy.value = true
  try {
    await renderApi.createAppeal(episodeId.value, {
      target_type: 'asset',
      target_id: appealTarget.value,
      reason: appealReason.value.trim(),
      detail: appealDetail.value.trim(),
    })
    toast.success('申诉已提交，等待人工复核')
    appealReason.value = ''
    appealDetail.value = ''
    appealTarget.value = ''
    await loadGate()
  } catch (err) {
    reportError(err)
  } finally {
    appealBusy.value = false
  }
}

function startAppeal(mediaId: string): void {
  appealTarget.value = mediaId
  appealReason.value = ''
  appealDetail.value = ''
}

onMounted(async () => {
  await config.load()
  exportPreset.value = config.presets[0]?.id ?? ''
  await loadAll()
})

async function validateTimeline(): Promise<void> {
  if (!episodeId.value) return
  validating.value = true
  try {
    validation.value = await renderApi.validateTimeline(episodeId.value)
    if (validation.value.valid) toast.success('时间线校验通过')
    else toast.warn('时间线存在问题', `${validation.value.errors.length} 个错误`)
  } catch {
    /* api 层已提示 */
  } finally {
    validating.value = false
  }
}

function track(job: Job): void {
  jobIds.value = [job.id, ...jobIds.value]
  store.trackJob(job)
}

async function renderPreview(): Promise<void> {
  if (!episodeId.value) return
  busy.value = true
  try {
    track(await renderApi.preview(episodeId.value, { watermark: watermark.value }))
    toast.info('预览合成任务已提交', `码率 ${config.previewBitrateKbps}kbps`)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function renderExport(): Promise<void> {
  if (!episodeId.value) return
  if (!exportPreset.value) {
    toast.warn('请选择导出预设')
    return
  }
  busy.value = true
  try {
    track(
      await renderApi.export(episodeId.value, {
        preset: exportPreset.value,
        watermark: watermark.value,
      }),
    )
    toast.info('导出任务已提交', exportPreset.value)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function exportPackage(): Promise<void> {
  if (!episodeId.value) return
  busy.value = true
  try {
    track(await renderApi.package(episodeId.value))
    toast.info('素材包打包任务已提交', '不依赖渲染成功')
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function download(record: ExportRecord): Promise<void> {
  try {
    await downloadFile(
      `/exports/${record.id}/download`,
      `episode_${episodeId.value}_${record.kind}.${record.kind === 'package' ? 'zip' : 'mp4'}`,
    )
    toast.success('已开始下载')
  } catch {
    /* api 层已提示 */
  }
}

async function retryJob(id: string): Promise<void> {
  try {
    track(await jobsApi.retry(id))
  } catch {
    /* api 层已提示 */
  }
}

async function cancelJob(id: string): Promise<void> {
  try {
    const res = await jobsApi.cancel(id)
    if (res.job) store.trackJob(res.job)
  } catch {
    /* api 层已提示 */
  }
}

/* ---------------- 门控 ---------------- */

async function approve(): Promise<void> {
  gateBusy.value = true
  try {
    await store.resumeGate('approve')
    toast.success('导出已确认')
    await loadExports()
  } catch (err) {
    handleGateError(err)
  } finally {
    gateBusy.value = false
  }
}

async function reject(reason: string): Promise<void> {
  gateBusy.value = true
  try {
    await store.resumeGate('reject', { reason })
  } catch (err) {
    handleGateError(err)
  } finally {
    gateBusy.value = false
  }
}

async function edit(instruction: string): Promise<void> {
  gateBusy.value = true
  try {
    await store.resumeGate('edit', { instruction })
  } catch (err) {
    handleGateError(err)
  } finally {
    gateBusy.value = false
  }
}

function handleGateError(err: unknown): void {
  if (err instanceof ApiError && err.code === 'CHECKPOINT_STALE') {
    notice.value = '检查点已过期（CHECKPOINT_STALE），请刷新状态后重试。'
  }
  reportError(err, '门控决策')
}
</script>

<template>
  <div class="stage">
    <GateCard
      :gate="pendingGate"
      :workflow="workflow"
      :busy="gateBusy"
      :notice="notice"
      approve-text="确认导出，完成剧集"
      description="确认预览与导出结果后，剧集进入 EXPORTED 阶段。"
      @approve="approve"
      @reject="reject"
      @edit="edit"
      @refresh="store.refreshLive()"
    >
      <div class="row-wrap tiny dim">
        <span>镜头 <span class="mono">{{ shots.length }}</span></span>
        <span>总时长 <span class="mono">{{ formatDuration(totalDurationMs) }}</span></span>
        <span>导出记录 <span class="mono">{{ exports.length }}</span></span>
        <span v-if="validation" :class="validation.valid ? '' : 'over'">
          校验 {{ validation.valid ? '通过' : `${validation.errors.length} 错误` }}
        </span>
      </div>
    </GateCard>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">时间线</h2>
          <div class="row">
            <button type="button" class="btn btn-sm btn-ghost" :disabled="loading" @click="loadTimeline">
              刷新
            </button>
            <button type="button" class="btn btn-sm" :disabled="validating" @click="validateTimeline">
              {{ validating ? '校验中…' : '校验时间线' }}
            </button>
          </div>
        </div>
        <div class="card-body col" style="gap: 14px">
          <SkeletonBlock v-if="loading && !timeline" :rows="4" />
          <ErrorState
            v-else-if="loadError && !timeline"
            :error="loadError"
            context="加载时间线"
            compact
            @retry="loadTimeline"
          />
          <EmptyState
            v-else-if="!timeline"
            icon="≡"
            title="时间线不可用"
            description="完成分镜、视频与音频阶段后，时间线会自动生成。"
          />

          <template v-else>
            <div class="timeline-bar">
              <div
                v-for="clip in clips"
                :key="clip.shot_no"
                class="timeline-clip"
                :class="{ skipped: clip.skipped, missing: !clip.video_media_id }"
                :style="{ width: `${Math.max(2, percent(clip.duration_ms, totalDurationMs))}%` }"
                :title="`#${clip.shot_no} · ${formatDuration(clip.duration_ms)}`"
              >
                <span class="mono">{{ clip.shot_no }}</span>
              </div>
              <div v-if="!clips.length" class="timeline-empty tiny dim">时间线未返回片段</div>
            </div>

            <div class="row-wrap tiny dim">
              <span>片段 <span class="mono">{{ clips.length }}</span></span>
              <span>总时长 <span class="mono">{{ formatDuration(totalDurationMs) }}</span></span>
              <span>音轨 <span class="mono">{{ timeline.audio_tracks?.length ?? 0 }}</span></span>
              <span>字幕 <span class="mono">{{ timeline.subtitles?.length ?? 0 }}</span></span>
            </div>

            <div v-if="validation" class="validation" :class="validation.valid ? 'ok' : 'bad'">
              <div class="row-between">
                <span class="badge" :class="validation.valid ? 'badge-ok' : 'badge-danger'">
                  {{ validation.valid ? '时间线可合成' : '时间线存在错误' }}
                </span>
                <span class="mono tiny dim">
                  {{ validation.errors.length }} 错误 · {{ validation.warnings.length }} 警告
                </span>
              </div>
              <ul v-if="validation.errors.length" class="validation-list">
                <li v-for="(e, i) in validation.errors" :key="i">
                  <span class="mono tiny">{{ e.code }}</span>
                  <span v-if="e.shot_no != null" class="badge mono">#{{ e.shot_no }}</span>
                  <span v-if="e.track" class="badge">{{ e.track }}</span>
                  <span class="tiny grow">{{ e.message }}</span>
                </li>
              </ul>
              <ul v-if="validation.warnings.length" class="validation-list warn">
                <li v-for="(w, i) in validation.warnings" :key="i">
                  <span class="mono tiny">{{ w.code }}</span>
                  <span class="tiny grow">{{ w.message }}</span>
                </li>
              </ul>
            </div>

            <table class="table">
              <thead>
                <tr>
                  <th style="width: 60px">镜号</th>
                  <th style="width: 110px">开始</th>
                  <th style="width: 100px">时长</th>
                  <th>视频</th>
                  <th style="width: 90px">状态</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="clip in clips" :key="clip.shot_no">
                  <td class="mono">#{{ clip.shot_no }}</td>
                  <td class="mono dim">{{ formatSeconds((clip.start_ms ?? 0) / 1000) }}</td>
                  <td class="mono">{{ formatDuration(clip.duration_ms) }}</td>
                  <td class="tiny dim mono">{{ clip.video_media_id ?? '—' }}</td>
                  <td>
                    <span v-if="clip.skipped" class="badge badge-warn">跳过 · {{ clip.fallback ?? '—' }}</span>
                    <span v-else-if="clip.video_media_id" class="badge badge-ok">就绪</span>
                    <span v-else class="badge badge-danger">缺片段</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </template>
        </div>
      </section>

      <div class="col" style="gap: var(--gap)">
        <section class="card">
          <div class="card-head">
            <h2 class="title-md">预览与导出</h2>
            <span class="mono tiny dim">预览码率 {{ config.previewBitrateKbps }}kbps</span>
          </div>
          <div class="card-body col" style="gap: 12px">
            <label class="checkbox">
              <input v-model="watermark" type="checkbox" />
              <span class="small">添加 AIGC 水印</span>
            </label>

            <div class="field">
              <label class="label" for="r-preset">导出预设</label>
              <select id="r-preset" v-model="exportPreset" class="select">
                <option v-for="preset in config.presets" :key="preset.id" :value="preset.id">
                  {{ preset.label }}<template v-if="preset.meta"> · {{ preset.meta }}</template>
                </option>
              </select>
              <span class="hint">预设来自 /config/limits.export_presets</span>
            </div>

            <div class="row-wrap">
              <button
                type="button"
                class="btn"
                :disabled="busy || !canExport"
                :title="canExport ? '' : '内容被拦截，禁止导出'"
                @click="renderPreview"
              >
                生成预览
              </button>
              <button
                type="button"
                class="btn btn-primary"
                :disabled="busy || !canExport"
                :title="canExport ? '' : '内容被拦截，禁止导出'"
                @click="renderExport"
              >
                正式导出
              </button>
              <button
                type="button"
                class="btn btn-ghost"
                :disabled="busy || !canExport"
                :title="canExport ? '' : '内容被拦截，禁止导出'"
                @click="exportPackage"
              >
                打包素材 ZIP
              </button>
            </div>
            <p v-if="!canExport" class="hint">
              本集存在未通过内容安全检查的素材（{{ unresolved.length }} 项），已禁止确认与导出。
              请修改输入或在上方「安全检查」里提交申诉。
            </p>

            <div v-if="latestPreview" class="preview-player">
              <span class="section-title">最新预览</span>
              <MediaThumb :media-id="latestPreview.media_id" :url="latestPreview.url" kind="video" />
            </div>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">确认记录</h2>
            <span class="mono tiny dim">{{ approvals.length }} 条</span>
          </div>
          <div class="card-body">
            <EmptyState
              v-if="!approvals.length"
              compact
              icon="✓"
              title="暂无确认记录"
              description="每次门控决策与画面确认都会写入 approvals。"
            />
            <ul v-else class="approval-list">
              <li v-for="(a, i) in approvals" :key="a.id ?? i" class="approval-item">
                <div class="row-wrap">
                  <span class="badge badge-info">{{ a.gate ?? a.action ?? '确认' }}</span>
                  <span v-if="a.action" class="badge">{{ a.action }}</span>
                  <span v-if="a.asset_version != null" class="mono tiny dim">v{{ a.asset_version }}</span>
                </div>
                <span class="mono tiny dim-2">{{ a.media_id ?? '—' }}</span>
                <span class="tiny dim">
                  {{ a.operator ?? a.actor ?? '系统' }} · {{ formatDateTime(a.created_at) }}
                </span>
              </li>
            </ul>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">安全检查</h2>
            <span
              v-if="gate"
              class="badge"
              :class="canExport ? 'badge-ok' : 'badge-danger'"
            >
              {{ canExport ? '可导出' : '已禁止导出' }}
            </span>
          </div>
          <div class="card-body col" style="gap: 12px">
            <!-- §12-8：被拦截时必须说明「为什么挡、怎么解」，而不是只给一个 422 -->
            <div v-if="!canExport" class="gate-block">
              <strong>有 {{ unresolved.length }} 项内容未通过安全检查，已禁止确认与导出</strong>
              <p class="tiny dim">
                可以按下方提示修改输入后重新生成，或对具体素材提交申诉等待人工复核。
                申诉通过后该素材会自动解除阻断。
              </p>
              <ul class="gate-list">
                <li v-for="mediaId in unresolved" :key="mediaId">
                  <span class="mono tiny">{{ mediaId }}</span>
                  <span
                    v-if="appealFor(mediaId)"
                    class="badge"
                    :class="appealFor(mediaId)?.status === 'APPROVED' ? 'badge-ok' : 'badge-warn'"
                  >
                    {{ appealStatusLabel(appealFor(mediaId)?.status) }}
                  </span>
                  <span v-else class="badge badge-danger">未申诉</span>
                  <button
                    v-if="!appealFor(mediaId)"
                    type="button"
                    class="btn btn-sm btn-ghost"
                    @click="startAppeal(mediaId)"
                  >
                    申诉
                  </button>
                </li>
              </ul>

              <div v-if="appealTarget" class="appeal-form col" style="gap: 8px">
                <div class="tiny dim">
                  申诉素材 <span class="mono">{{ appealTarget }}</span>
                </div>
                <input
                  v-model="appealReason"
                  class="input"
                  placeholder="申诉理由（必填，例如：这是虚构剧情，不含真实暴力内容）"
                />
                <textarea
                  v-model="appealDetail"
                  class="textarea"
                  rows="2"
                  placeholder="补充说明（选填）：涉及的镜头、创作意图等"
                />
                <div class="row-wrap">
                  <button
                    type="button"
                    class="btn btn-sm btn-primary"
                    :disabled="appealBusy || !appealReason.trim()"
                    @click="submitAppeal"
                  >
                    {{ appealBusy ? '提交中…' : '提交申诉' }}
                  </button>
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    :disabled="appealBusy"
                    @click="appealTarget = ''"
                  >
                    取消
                  </button>
                </div>
              </div>

              <p v-if="gate?.pending?.length" class="tiny dim">
                已有 {{ gate.pending.length }} 项申诉在复核中，结果出来后本页会自动更新。
              </p>
            </div>

            <EmptyState v-if="!safety" compact icon="⚑" title="暂无安全检查汇总" />
            <dl v-else class="kv">
              <dt>拦截</dt>
              <dd class="mono over">{{ safety.blocked ?? 0 }}</dd>
              <dt>待复核</dt>
              <dd class="mono warn-text">{{ safety.review ?? 0 }}</dd>
              <dt>条目</dt>
              <dd class="mono">{{ safety.items?.length ?? 0 }}</dd>
              <dt>申诉</dt>
              <dd class="mono">{{ appeals.length }}</dd>
            </dl>

            <ul v-if="appeals.length" class="appeal-list">
              <li v-for="a in appeals" :key="a.id">
                <span
                  class="badge"
                  :class="a.status === 'APPROVED' ? 'badge-ok' : a.status === 'REJECTED' ? 'badge-danger' : 'badge-warn'"
                >
                  {{ appealStatusLabel(a.status) }}
                </span>
                <span class="tiny muted ellipsis">{{ a.reason }}</span>
                <span v-if="a.resolution_note" class="tiny dim">→ {{ a.resolution_note }}</span>
              </li>
            </ul>
          </div>
        </section>
      </div>
    </div>

    <section class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">导出记录</h2>
          <span class="mono tiny dim">
            预览 {{ previews.length }} · 导出 {{ renders.length }} · 素材包 {{ packages.length }}
          </span>
        </div>
        <button type="button" class="btn btn-sm btn-ghost" @click="loadExports">刷新</button>
      </div>
      <div class="card-body">
        <EmptyState
          v-if="!exports.length"
          icon="⤓"
          title="还没有导出记录"
          description="生成预览或正式导出后，记录会出现在这里。"
        />
        <table v-else class="table">
          <thead>
            <tr>
              <th style="width: 90px">类型</th>
              <th style="width: 100px">状态</th>
              <th style="width: 100px">预设</th>
              <th>媒体</th>
              <th style="width: 130px">时间</th>
              <th style="width: 140px" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in exports" :key="record.id">
              <td><span class="badge badge-accent">{{ EXPORT_KIND_LABEL[record.kind] ?? record.kind }}</span></td>
              <td>
                <span
                  class="badge"
                  :class="record.status === 'SUCCEEDED' ? 'badge-ok' : record.status === 'FAILED' ? 'badge-danger' : 'badge-info'"
                >
                  {{ EXPORT_STATUS_LABEL[record.status] ?? record.status }}
                </span>
              </td>
              <td class="mono tiny dim">{{ record.preset ?? '—' }}</td>
              <td class="tiny dim mono ellipsis">{{ record.media_id ?? '—' }}</td>
              <td class="mono tiny dim">{{ relativeTime(record.created_at) }}</td>
              <td>
                <div class="row" style="justify-content: flex-end">
                  <button
                    v-if="record.status === 'SUCCEEDED'"
                    type="button"
                    class="btn btn-sm btn-primary"
                    @click="download(record)"
                  >
                    下载
                  </button>
                  <span v-if="record.error" class="tiny over ellipsis" :title="record.error">{{ record.error }}</span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2 class="title-md">合成任务</h2>
        <span class="mono tiny dim">{{ jobs.length }} 个</span>
      </div>
      <div class="card-body col">
        <EmptyState v-if="!jobs.length" compact icon="⚙" title="暂无任务" description="提交预览 / 导出 / 打包后会出现在这里。" />
        <JobInline
          v-for="job in jobs"
          :key="job.id"
          :job="job"
          :busy="busy"
          @retry="retryJob"
          @cancel="cancelJob"
        />
      </div>
    </section>
  </div>
</template>

<style scoped>
.stage {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
.two-col {
  display: grid;
  grid-template-columns: 1.45fr 1fr;
  gap: var(--gap);
  align-items: start;
}
@media (max-width: 1120px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
.timeline-bar {
  display: flex;
  gap: 2px;
  height: 44px;
  padding: 4px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-1);
  overflow: hidden;
}
.timeline-clip {
  flex: 0 0 auto;
  min-width: 10px;
  border-radius: 4px;
  background: linear-gradient(180deg, rgba(56, 189, 248, 0.55), rgba(167, 139, 250, 0.55));
  display: grid;
  place-items: center;
  font-size: 10px;
  color: #06121c;
  overflow: hidden;
  transition: filter var(--dur) var(--ease);
}
.timeline-clip:hover {
  filter: brightness(1.2);
}
.timeline-clip.skipped {
  background: repeating-linear-gradient(
    45deg,
    rgba(251, 191, 36, 0.5),
    rgba(251, 191, 36, 0.5) 5px,
    rgba(251, 191, 36, 0.24) 5px,
    rgba(251, 191, 36, 0.24) 10px
  );
}
.timeline-clip.missing {
  background: rgba(248, 113, 113, 0.35);
}
.timeline-empty {
  display: grid;
  place-items: center;
  width: 100%;
}
.validation {
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--line);
}
.validation.ok {
  border-color: rgba(52, 211, 153, 0.35);
  background: var(--ok-soft);
}
.validation.bad {
  border-color: rgba(248, 113, 113, 0.35);
  background: var(--danger-soft);
}
.validation-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 180px;
  overflow: auto;
}
.validation-list li {
  display: flex;
  align-items: center;
  gap: 8px;
}
.validation-list .mono {
  color: var(--warn);
  flex: 0 0 auto;
}
.validation-list.warn .mono {
  color: var(--text-2);
}
.preview-player {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.approval-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
}
.approval-item {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
}
.over {
  color: var(--danger);
}
.warn-text {
  color: var(--warn);
}

/* §12-8：内容拦截闸门与申诉 */
.gate-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--danger);
  border-radius: 8px;
  background: color-mix(in srgb, var(--danger) 8%, transparent);
}
.gate-list {
  margin: 0;
  padding-left: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.gate-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.appeal-form {
  padding-top: 4px;
  border-top: 1px dashed var(--line-strong);
}
.appeal-list {
  margin: 0;
  padding-left: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.appeal-list li {
  display: flex;
  align-items: center;
  gap: 8px;
}
</style>
