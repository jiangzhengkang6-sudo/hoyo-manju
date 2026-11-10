<script setup lang="ts">
/**
 * 视频生成与确认（API.md §7、§9）。
 * 先估算成本（estimate）→ 用户确认 → 携带 quote_id 与幂等键生成（generate）
 * → 逐镜确认片段 / 跳过 / 重试 → 门控确认。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { reportError } from '@/api/client'
import { cancelJob as cancelJobRequest, jobsApi } from '@/api/jobs'
import { mediaApi } from '@/api/media'
import { shotsApi } from '@/api/shots'
import { quoteRemainingMs, videoApi } from '@/api/video'
import { CAMERA_MOVEMENTS } from '@/api/story'
import { formatCents } from '@/api/cost'
import { providerStatusLabel, formatDuration, formatSeconds } from '@/utils/format'
import { ApiError } from '@/api/errors'
import GateCard from '@/components/GateCard.vue'
import StageHead from '@/components/StageHead.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import MediaThumb from '@/components/MediaThumb.vue'
import QuotaMeter from '@/components/QuotaMeter.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import type { Job, Shot, ShotVideoState, VideoParamsView, VideoQuote } from '@/types'

const store = useEpisodeStore()
const config = useConfigStore()
const toast = useToastStore()
const { shots, workflow, pendingGate, jobSnapshots } = storeToRefs(store)
const { quota } = storeToRefs(config)

const selectedNos = ref<number[]>([])
const quote = ref<VideoQuote | null>(null)
const estimating = ref(false)
const generating = ref(false)
const busy = ref(false)
const gateBusy = ref(false)
const notice = ref('')
const jobIds = ref<string[]>([])
const now = ref(Date.now())
const skipOpen = ref(false)
const skipTarget = ref<Shot | null>(null)
const skipForm = ref<{ fallback: 'static_image' | 'blank'; reason: string }>({
  fallback: 'static_image',
  reason: '',
})
const expandedNo = ref<number | null>(null)
const videoStates = ref<Record<string, ShotVideoState>>({})
const videoLoading = ref(false)
/** 正在上传的镜号（0 表示没有） */
const uploadingNo = ref(0)
/** 共享的文件选择器：点某一镜的「上传」时记下目标镜头 */
const uploadTarget = ref<Shot | null>(null)
const uploadInput = ref<HTMLInputElement | null>(null)

let timer: number | null = null

const jobs = computed<Job[]>(() =>
  jobIds.value.map((id) => jobSnapshots.value[id]).filter((j): j is Job => Boolean(j)),
)

const quoteLeftMs = computed(() => quoteRemainingMs(quote.value, now.value))
const quoteExpired = computed(() => quoteLeftMs.value !== null && quoteLeftMs.value <= 0)

/** 估价允许候选图，但出片要求「已确认画面」——这里点出还差哪几镜。 */
const unconfirmedInQuote = computed<number[]>(() =>
  (quote.value?.shot_confirmation ?? [])
    .filter((item) => !item.confirmed_media_id)
    .map((item) => item.shot_no),
)

const quotaInsufficient = computed(() => {
  if (!quote.value) return false
  const after = quote.value.quota_after_cents
  return typeof after === 'number' && after < 0
})

const pendingGateTitle = computed(() =>
  pendingGate.value === 'gate_video_cost_confirmation' ? '视频成本确认' : '视频片段确认',
)

const approvedCount = computed(() => shots.value.filter((s) => Boolean(s.video_media_id)).length)
const skippedCount = computed(() => shots.value.filter((s) => s.video_skipped).length)

onMounted(async () => {
  await config.load()
  await config.loadQuota()
  await loadParams()
  timer = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
})

/* ------------------------------------------------------------------ *
 * FR-040 视频参数（画幅比例 + 逐镜参数）
 * ------------------------------------------------------------------ */

const params = ref<VideoParamsView | null>(null)
const paramsBusy = ref(false)
const paramsOpen = ref(false)
/** 逐镜参数草稿，key 是镜号；保存时整体提交。 */
const paramDraft = ref<Record<number, { duration_ms: number; motion_strength: number; camera_movement: string }>>({})

async function loadParams(): Promise<void> {
  const id = store.episodeId
  if (!id) return
  try {
    const data = await videoApi.params(id)
    params.value = data
    paramDraft.value = Object.fromEntries(
      (data.shots ?? []).map((s) => [
        s.shot_no,
        {
          duration_ms: s.duration_ms,
          motion_strength: s.motion_strength,
          camera_movement: s.camera_movement,
        },
      ]),
    )
  } catch {
    /* api 层已提示；参数面板隐藏即可，不影响生成主流程 */
    params.value = null
  }
}

/** 画幅比例落在项目上，改动即影响全剧集的图像/视频/合成尺寸。 */
async function saveAspectRatio(value: string): Promise<void> {
  if (!store.episodeId || params.value?.aspect_ratio === value) return
  paramsBusy.value = true
  try {
    await videoApi.updateParams(store.episodeId, { aspect_ratio: value })
    await loadParams()
    const size = params.value ? `${params.value.width}×${params.value.height}` : ''
    toast.success('画幅比例已更新', `输出尺寸 ${size}（影响全剧集）`)
    await store.loadShots()
  } catch {
    /* api 层已提示 */
  } finally {
    paramsBusy.value = false
  }
}

async function savePerShotParams(): Promise<void> {
  const id = store.episodeId
  if (!id) return
  const rows = Object.entries(paramDraft.value)
  if (!rows.length) {
    toast.warn('没有可保存的镜头参数')
    return
  }
  if (!window.confirm(`将把时长/运动强度/运镜应用到全部 ${rows.length} 个镜头，继续？`)) return
  paramsBusy.value = true
  try {
    // 后端按「一组参数应用到 shot_nos」工作，逐镜差异用每镜一次请求表达
    for (const [shotNo, value] of rows) {
      await videoApi.updateParams(id, {
        shot_nos: [Number(shotNo)],
        duration_ms: value.duration_ms,
        motion_strength: value.motion_strength,
        camera_movement: value.camera_movement,
      })
    }
    await loadParams()
    toast.success('镜头参数已保存', `共 ${rows.length} 个镜头`)
  } catch {
    /* api 层已提示 */
  } finally {
    paramsBusy.value = false
  }
}

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
})

function toggleSelect(shotNo: number): void {
  selectedNos.value = selectedNos.value.includes(shotNo)
    ? selectedNos.value.filter((n) => n !== shotNo)
    : [...selectedNos.value, shotNo].sort((a, b) => a - b)
}

function selectAll(): void {
  selectedNos.value = shots.value.map((s) => s.shot_no)
}

function selectPending(): void {
  selectedNos.value = shots.value.filter((s) => !s.video_media_id && !s.video_skipped).map((s) => s.shot_no)
}

async function estimate(): Promise<void> {
  // 只估勾选的镜头：不再回退到「全部待生成」，否则一个没画面的镜头就会拦住整次估价。
  const nos = selectedNos.value
  if (!nos.length) {
    toast.warn('请先勾选镜头', '勾选要生成视频的镜头；未勾选的镜头不参与估价')
    return
  }
  estimating.value = true
  try {
    quote.value = await videoApi.estimate(store.episodeId ?? '', { shot_nos: nos })
    now.value = Date.now()
    toast.info('成本估算完成', `预计 ${formatCents(quote.value.estimate_cents)}`)
  } catch {
    /* api 层已提示 */
  } finally {
    estimating.value = false
  }
}

async function generate(): Promise<void> {
  if (!quote.value) {
    toast.warn('请先估算成本', '未确认成本的生成请求会被后端拒绝（CONFLICT）')
    return
  }
  if (quoteExpired.value) {
    toast.warn('报价已过期', '请重新估算成本')
    return
  }
  generating.value = true
  try {
    const nos = quote.value.per_shot?.map((p) => p.shot_no) ?? selectedNos.value
    const job = await videoApi.generate(store.episodeId ?? '', {
      shot_nos: nos,
      quote_id: quote.value.quote_id,
    })
    jobIds.value = [job.id, ...jobIds.value]
    store.trackJob(job)
    quote.value = null
    await config.loadQuota()
    toast.success('视频生成任务已提交', `${nos.length} 个镜头`)
  } catch (err) {
    if (err instanceof ApiError && err.code === 'CONFLICT') {
      notice.value = '后端拒绝了本次生成：成本未确认或报价已失效（CONFLICT），请重新估算。'
    }
    reportError(err, '生成视频')
  } finally {
    generating.value = false
  }
}

async function loadVideoState(shot: Shot): Promise<void> {
  if (expandedNo.value === shot.shot_no) {
    expandedNo.value = null
    return
  }
  expandedNo.value = shot.shot_no
  videoLoading.value = true
  try {
    videoStates.value = {
      ...videoStates.value,
      [shot.id]: await shotsApi.video(shot.id),
    }
  } catch {
    /* 片段状态不可用时保持空态 */
  } finally {
    videoLoading.value = false
  }
}

function clipsOf(shot: Shot) {
  const state = videoStates.value[shot.id]
  return state?.history ?? state?.clips ?? []
}

async function confirmClip(shot: Shot, mediaId: string): Promise<void> {
  busy.value = true
  try {
    const job = await videoApi.confirm(shot.id, mediaId)
    jobIds.value = [job.id, ...jobIds.value]
    store.trackJob(job)
    toast.success('片段已确认', `#${shot.shot_no}`)
    await store.loadShots()
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

function openSkip(shot: Shot): void {
  skipTarget.value = shot
  skipForm.value = { fallback: 'static_image', reason: '' }
  skipOpen.value = true
}

/* ---------------- 按任务号补拉 ---------------- */

const fetchOpen = ref(false)
const fetchBusy = ref(false)
const fetchTaskId = ref('')
const fetchShotNo = ref<number>(1)

/** 打开补拉弹窗：默认落到第一个还没视频的镜头。 */
function openFetchProvider(): void {
  const todo = shots.value.find((s) => !s.video_media_id && !s.video_skipped)
  fetchShotNo.value = (todo ?? shots.value[0])?.shot_no ?? 1
  fetchTaskId.value = ''
  fetchOpen.value = true
}

async function submitFetchProvider(): Promise<void> {
  const taskId = fetchTaskId.value.trim()
  const shot = shots.value.find((s) => s.shot_no === fetchShotNo.value)
  if (!taskId) {
    toast.warn('请填写任务编号', '形如 tsk_vid_01M6QZ…，在供应商控制台的任务日志里复制')
    return
  }
  if (!shot) {
    toast.warn('请选择目标镜头')
    return
  }
  fetchBusy.value = true
  try {
    const result = await videoApi.fetchProvider(shot.id, taskId)
    await store.loadShots()
    fetchOpen.value = false
    toast.success(
      `第 ${result.shot_no} 镜已取回片段`,
      result.warnings?.[0] ||
        (result.billed_cents > 0
          ? `供应商已计费 ${formatCents(result.billed_cents)}，已补记入账`
          : '未产生新的生成费用'),
    )
  } catch {
    /* api 层已提示（含「链接已过期」） */
  } finally {
    fetchBusy.value = false
  }
}

/**
 * 上传自己的视频顶替这一镜。
 *
 * 用途：供应商那边其实生成成功、本地因为网络/超时没拉回来（钱照扣），
 * 用户把片子从控制台下载后传回来，直接当该镜片段用，不再花一次生成费。
 */
function pickVideo(shot: Shot): void {
  if (busy.value || uploadingNo.value) return
  uploadTarget.value = shot
  uploadInput.value?.click()
}

async function onVideoPicked(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  const shot = uploadTarget.value
  input.value = '' // 允许连续选同一个文件
  uploadTarget.value = null
  if (!file || !shot || !store.episodeId) return
  if (!file.type.startsWith('video/')) {
    toast.warn('这里只接受视频文件', '支持 mp4 / mov / webm')
    return
  }
  uploadingNo.value = shot.shot_no
  try {
    const media = await mediaApi.upload(file, 'video', {
      episode_id: store.episodeId,
      rights_declared: true,
      source_note: `第 ${shot.shot_no} 镜镜头片段（本地导入）`,
    })
    const result = await videoApi.manual(shot.id, media.id)
    await store.loadShots()
    const note = result.warnings?.[0]
    toast.success(
      `第 ${shot.shot_no} 镜已换成上传的片段`,
      note || (result.duration_ms ? `时长 ${formatSeconds(result.duration_ms / 1000)}` : '合成时按剧本时长裁剪'),
    )
  } catch {
    /* api 层已提示 */
  } finally {
    uploadingNo.value = 0
  }
}

async function submitSkip(): Promise<void> {  const shot = skipTarget.value
  if (!shot) return
  busy.value = true
  try {
    const job = await videoApi.skip(shot.id, skipForm.value.fallback, skipForm.value.reason || undefined)
    jobIds.value = [job.id, ...jobIds.value]
    store.trackJob(job)
    skipOpen.value = false
    toast.info('已跳过该镜视频', skipForm.value.fallback === 'static_image' ? '使用静帧兜底' : '使用空白兜底')
    await store.loadShots()
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function retryVideo(shot: Shot): Promise<void> {
  busy.value = true
  try {
    const job = await videoApi.retry(shot.id)
    jobIds.value = [job.id, ...jobIds.value]
    store.trackJob(job)
    toast.info('已提交视频重试任务', `#${shot.shot_no}`)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function retryJob(id: string): Promise<void> {
  try {
    const job = await jobsApi.retry(id)
    jobIds.value = [job.id, ...jobIds.value]
    store.trackJob(job)
  } catch {
    /* api 层已提示 */
  }
}

async function cancelJob(id: string): Promise<void> {
  try {
    const job = await cancelJobRequest(id)
    if (job) store.trackJob(job)
  } catch {
    /* api 层已提示 */
  }
}

/* ---------------- 门控 ---------------- */

async function approve(): Promise<void> {
  gateBusy.value = true
  try {
    await store.resumeGate('approve')
    toast.success('门控已确认')
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
    <StageHead
      index="04"
      title="视频生成"
      description="按已确认的分镜图生成片段：先看报价，再提交任务；单条可随时停止。"
    />
    <GateCard
      :gate="pendingGate"
      :workflow="workflow"
      :busy="gateBusy"
      :notice="notice"
      :approve-text="pendingGate === 'gate_video_cost_confirmation' ? '确认成本，开始生成视频' : '确认全部片段，进入音频阶段'"
      :description="`当前门控：${pendingGateTitle}。`"
      @approve="approve"
      @reject="reject"
      @edit="edit"
      @refresh="store.refreshLive()"
    >
      <div class="row-wrap tiny dim">
        <span>已确认片段 <span class="mono">{{ approvedCount }}/{{ shots.length }}</span></span>
        <span>已跳过 <span class="mono">{{ skippedCount }}</span></span>
        <span>待生成 <span class="mono">{{ shots.length - approvedCount - skippedCount }}</span></span>
      </div>
    </GateCard>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">成本估算与生成</h2>
          <div class="row-wrap">
            <button type="button" class="btn btn-sm btn-ghost" @click="selectAll">全选</button>
            <button type="button" class="btn btn-sm btn-ghost" @click="selectPending">选未生成</button>
            <button type="button" class="btn btn-sm btn-ghost" @click="selectedNos = []">清空</button>
            <button type="button" class="btn btn-sm" :disabled="estimating" @click="estimate">
              {{ estimating ? '估算中…' : '估算成本' }}
            </button>
          </div>
        </div>
        <div class="card-body col" style="gap: var(--space-4)">
          <div class="row-wrap tiny dim">
            <span>已选 <span class="mono">{{ selectedNos.length }}</span> 镜</span>
            <span>只对勾选的镜头估价 · 有画面（已确认或已选中候选图）即可</span>
          </div>

          <EmptyState
            v-if="!quote"
            compact
            icon="¥"
            title="尚未估算成本"
            description="选择镜头后点击「估算成本」，报价确认后才可提交生成（后端强制校验）。"
          />

          <div v-else class="quote">
            <div class="quote-head">
              <div>
                <div class="section-title">报价单</div>
                <div class="mono tiny dim">{{ quote.quote_id }}</div>
              </div>
              <span
                class="badge"
                :class="quoteExpired ? 'badge-danger' : 'badge-ok'"
              >
                {{ quoteExpired ? '已过期' : `剩余 ${formatSeconds((quoteLeftMs ?? 0) / 1000)}` }}
              </span>
            </div>

            <dl class="kv">
              <dt>供应商</dt>
              <dd class="mono">{{ quote.provider || '—' }}</dd>
              <dt>模型</dt>
              <dd class="mono">{{ quote.model || '—' }}</dd>
              <dt>预计成本</dt>
              <dd class="mono quote-cost">{{ formatCents(quote.estimate_cents) }}</dd>
              <dt>预估耗时</dt>
              <dd class="mono">{{ quote.estimated_wait_seconds ?? '—' }} 秒</dd>
              <dt v-if="quote.quota_after_cents !== undefined">生成后余额</dt>
              <dd class="mono" :class="{ over: quotaInsufficient }">
                {{ formatCents(quote.quota_after_cents) }}
              </dd>
              <dt v-if="quote.expires_at">报价有效期至</dt>
              <dd class="mono">{{ quote.expires_at }}</dd>
            </dl>

            <div v-if="quote.per_shot?.length" class="per-shot">
              <span class="section-title">分镜明细</span>
              <ul>
                <li v-for="item in quote.per_shot" :key="item.shot_no">
                  <span class="mono">#{{ item.shot_no }}</span>
                  <span class="mono tiny dim">{{ formatDuration(item.duration_ms) }}</span>
                  <span class="mono tiny grow">{{ formatCents(item.estimate_cents) }}</span>
                </li>
              </ul>
            </div>

            <p v-if="quotaInsufficient" class="over tiny">
              生成后余额为负，配额不足，请先调整配额或减少镜头数量。
            </p>
            <p v-if="unconfirmedInQuote.length" class="over tiny">
              以下镜头还没有「确认画面」，生成视频前需先在分镜页点「确认画面」：
              <span class="mono">#{{ unconfirmedInQuote.join('、#') }}</span>
            </p>

            <div class="row-wrap">
              <button
                type="button"
                class="btn btn-primary"
                :disabled="generating || quoteExpired || quotaInsufficient || unconfirmedInQuote.length > 0"
                @click="generate"
              >
                {{ generating ? '提交中…' : '确认并生成视频' }}
              </button>
              <button type="button" class="btn btn-ghost" :disabled="estimating" @click="estimate">
                重新估算
              </button>
            </div>
          </div>
        </div>
      </section>

      <div class="col" style="gap: var(--gap)">
        <!-- FR-040：画幅比例与逐镜视频参数 -->
        <section class="card">
          <div class="card-head">
            <h2 class="title-md">视频参数</h2>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              :disabled="paramsBusy"
              @click="paramsOpen = !paramsOpen"
            >
              {{ paramsOpen ? '收起' : '展开' }}
            </button>
          </div>
          <div class="card-body col" style="gap: var(--space-3)">
            <EmptyState
              v-if="!params"
              compact
              icon="⚙"
              title="参数不可用"
              description="读取视频参数失败，不影响生成流程。"
            />
            <template v-else>
              <div class="field">
                <label class="label" for="aspect-ratio">画幅比例（项目级，影响图像/视频/合成）</label>
                <select
                  id="aspect-ratio"
                  class="select"
                  :value="params.aspect_ratio ?? ''"
                  :disabled="paramsBusy"
                  @change="saveAspectRatio(($event.target as HTMLSelectElement).value)"
                >
                  <option
                    v-for="preset in params.aspect_presets ?? []"
                    :key="preset.value"
                    :value="preset.value"
                  >
                    {{ preset.value }} · {{ preset.width }}×{{ preset.height }}
                  </option>
                </select>
                <span class="hint mono">
                  当前输出 {{ params.width }}×{{ params.height }}
                </span>
              </div>

              <template v-if="paramsOpen">
                <div class="params-table">
                  <div class="params-head">
                    <span>镜号</span><span>时长(ms)</span><span>运动强度</span><span>运镜</span>
                  </div>
                  <div v-for="(row, no) in paramDraft" :key="no" class="params-row">
                    <span class="mono">{{ no }}</span>
                    <input
                      v-model.number="row.duration_ms"
                      class="input mono"
                      type="number"
                      :min="params.duration_ms_min ?? 1000"
                      :max="params.duration_ms_max ?? 15000"
                    />
                    <input
                      v-model.number="row.motion_strength"
                      class="input mono"
                      type="number"
                      step="0.1"
                      :min="params.motion_strength_min ?? 0"
                      :max="params.motion_strength_max ?? 1"
                    />
                    <select v-model="row.camera_movement" class="select">
                      <option v-for="m in CAMERA_MOVEMENTS" :key="m" :value="m">{{ m }}</option>
                    </select>
                  </div>
                </div>
                <button
                  type="button"
                  class="btn btn-sm"
                  :disabled="paramsBusy"
                  @click="savePerShotParams"
                >
                  {{ paramsBusy ? '保存中…' : '保存镜头参数' }}
                </button>
                <span class="hint">
                  参数写入分镜后，下次生成视频时按此执行；已生成的视频需重新生成才会生效。
                </span>
              </template>
            </template>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">配额</h2>
            <button type="button" class="btn btn-sm btn-ghost" @click="config.loadQuota()">刷新</button>
          </div>
          <div class="card-body">
            <QuotaMeter :quota="quota" :loading="false" />
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">生成任务</h2>
            <span class="mono tiny dim">{{ jobs.length }} 个</span>
          </div>
          <div class="card-body col">
            <EmptyState v-if="!jobs.length" compact icon="⚙" title="暂无任务" description="提交生成后会出现在这里。" />
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
    </div>

    <section class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">片段确认</h2>
          <span class="mono tiny dim">{{ shots.length }} 镜</span>
        </div>
        <div class="row">
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            title="任务在供应商侧已生成、本地没拉回来时，填任务编号把片子取回来（不再计费）"
            @click="openFetchProvider"
          >
            按任务号拉回
          </button>
          <button type="button" class="btn btn-sm btn-ghost" @click="store.loadShots()">刷新状态</button>
        </div>
      </div>
      <div class="card-body">
        <!-- 共享的文件选择器：每镜的「上传片段」都触发它，选完再按目标镜头归位 -->
        <input ref="uploadInput" type="file" accept="video/*" hidden @change="onVideoPicked" />
        <ErrorState
          v-if="store.error && !shots.length"
          :error="store.error"
          context="加载分镜"
          compact
          @retry="store.loadShots()"
        />
        <EmptyState
          v-else-if="!shots.length"
          icon="▦"
          title="还没有分镜"
          description="请先在「分镜画面」阶段生成并确认分镜图。"
        />
        <ul v-else class="clip-list">
          <li v-for="shot in shots" :key="shot.id" class="clip-item">
            <label class="clip-check" @click.stop>
              <input
                type="checkbox"
                :checked="selectedNos.includes(shot.shot_no)"
                @change="toggleSelect(shot.shot_no)"
              />
            </label>
            <div class="clip-thumb" @click="loadVideoState(shot)">
              <MediaThumb
                :media-id="shot.video_media_id ?? shot.confirmed_media_id"
                kind="video"
              />
            </div>
            <div class="clip-main">
              <div class="row-wrap">
                <span class="mono small">#{{ String(shot.shot_no).padStart(3, '0') }}</span>
                <span
                  class="badge"
                  :class="
                    shot.video_status === 'CONFIRMED'
                      ? 'badge-ok'
                      : shot.video_skipped
                        ? 'badge-warn'
                        : 'badge-info'
                  "
                >
                  {{
                    shot.video_skipped
                      ? '已跳过'
                      : shot.video_status === 'CONFIRMED'
                        ? '片段已确认'
                        : shot.video_status === 'CANDIDATES_READY'
                          ? '待确认'
                          : (shot.video_status ?? '未生成')
                  }}
                </span>
                <span v-if="shot.video_fallback" class="badge">兜底 {{ shot.video_fallback }}</span>
                <span class="mono tiny dim">{{ formatDuration(shot.duration_ms) }}</span>
              </div>
              <p class="tiny dim ellipsis">{{ shot.action || '无画面描述' }}</p>
            </div>
            <div class="clip-actions">
              <button
                v-if="shot.video_media_id"
                type="button"
                class="btn btn-sm btn-primary"
                :disabled="busy"
                @click="confirmClip(shot, shot.video_media_id)"
              >
                {{ shot.video_status === 'CONFIRMED' ? '再次确认' : '确认此片段' }}
              </button>
              <button type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="retryVideo(shot)">
                重试
              </button>
              <button
                type="button"
                class="btn btn-sm btn-ghost"
                :disabled="busy || uploadingNo === shot.shot_no"
                :title="
                  '用自己已有的视频替换这一镜（例如供应商已生成成功、本地没拉回来的片子）。' +
                  '上传即表示你拥有该素材的使用授权。'
                "
                @click="pickVideo(shot)"
              >
                {{ uploadingNo === shot.shot_no ? '上传中…' : '上传片段' }}
              </button>
              <button type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="openSkip(shot)">
                跳过
              </button>
              <button type="button" class="btn btn-sm btn-ghost" @click="loadVideoState(shot)">
                {{ expandedNo === shot.shot_no ? '收起' : '历史' }}
              </button>
            </div>

            <div v-if="expandedNo === shot.shot_no" class="clip-history">
              <div v-if="videoLoading" class="tiny dim">加载片段历史…</div>
              <template v-else>
                <div v-if="!clipsOf(shot).length" class="tiny dim">暂无片段历史（/shots/{id}/video 未返回记录）。</div>
                <div v-else class="clip-history-grid">
                  <figure v-for="(clip, i) in clipsOf(shot)" :key="clip.media_id ?? i" class="clip-history-item">
                    <MediaThumb :media-id="clip.media_id" :url="clip.url" kind="video" />
                    <figcaption>
                      <div class="row-between">
                        <span class="badge" :class="clip.status === 'SUCCEEDED' ? 'badge-ok' : clip.status === 'FAILED' ? 'badge-danger' : 'badge-info'">
                          {{ providerStatusLabel(clip.status) }}
                        </span>
                        <span class="mono tiny dim">{{ formatDuration(clip.duration_ms) }}</span>
                      </div>
                      <p v-if="clip.error" class="tiny over">{{ clip.error }}</p>
                      <button
                        v-if="clip.media_id"
                        type="button"
                        class="btn btn-sm"
                        :disabled="busy"
                        @click="confirmClip(shot, clip.media_id)"
                      >
                        确认此片段
                      </button>
                    </figcaption>
                  </figure>
                </div>
              </template>
            </div>
          </li>
        </ul>
      </div>
    </section>

    <ModalDialog :open="skipOpen" title="跳过该镜视频" :width="480" @close="skipOpen = false">
      <p class="small muted">
        跳过后该镜将使用兜底素材参与合成，合成阶段不会因缺少视频而失败。
      </p>
      <div class="field">
        <label class="label" for="sk-fb">兜底方式</label>
        <select id="sk-fb" v-model="skipForm.fallback" class="select">
          <option value="static_image">使用已确认静帧（static_image）</option>
          <option value="blank">使用空白画面（blank）</option>
        </select>
      </div>
      <div class="field">
        <label class="label" for="sk-reason">原因（可选）</label>
        <textarea id="sk-reason" v-model="skipForm.reason" class="textarea" rows="2" />
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="skipOpen = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitSkip">
          {{ busy ? '提交中…' : '确认跳过' }}
        </button>
      </template>
    </ModalDialog>

    <ModalDialog :open="fetchOpen" title="按任务号拉回片段" :width="520" @close="fetchOpen = false">
      <p class="small muted">
        供应商那边已经生成成功、本地没拉回来的片子，可以凭任务编号取回来。
        <strong>不会提交新任务、不产生新的生成费用</strong>；供应商的成片只保留 24 小时，超过就只能重新生成。
      </p>
      <div class="field">
        <label class="label" for="fp-shot">挂到哪一镜</label>
        <select id="fp-shot" v-model.number="fetchShotNo" class="select">
          <option v-for="shot in shots" :key="shot.id" :value="shot.shot_no">
            第 {{ shot.shot_no }} 镜 · {{ formatDuration(shot.duration_ms) }}
            {{ shot.video_status === 'CONFIRMED' ? '（已有片段，会被替换）' : '' }}
          </option>
        </select>
      </div>
      <div class="field">
        <label class="label" for="fp-task">供应商任务编号</label>
        <input
          id="fp-task"
          v-model="fetchTaskId"
          class="input mono"
          placeholder="tsk_vid_01M6QZ..."
          autocomplete="off"
        />
        <span class="hint">在供应商控制台的任务日志里复制；形如 tsk_vid_ 开头的一串字符。</span>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="fetchOpen = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="fetchBusy" @click="submitFetchProvider">
          {{ fetchBusy ? '取回中…' : '取回并挂上' }}
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
.quote {
  border: 1px solid var(--line-accent);
  border-radius: var(--radius);
  background: linear-gradient(180deg, rgba(224, 138, 60, 0.06), transparent 42%), var(--panel-2);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.quote-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.quote-cost {
  font-size: var(--fs-16);
  font-weight: 650;
  color: var(--accent);
}
.over {
  color: var(--danger);
}
.per-shot ul {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  max-height: 180px;
  overflow: auto;
}
.per-shot li {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-1) var(--space-2);
  border-radius: var(--radius-xs);
  background: rgba(243, 237, 227, 0.03);
}
.clip-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.clip-item {
  display: grid;
  grid-template-columns: 26px 96px minmax(0, 1fr) auto;
  gap: var(--space-3);
  align-items: center;
  padding: var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel-2);
  transition: border-color var(--dur) var(--ease);
}
.clip-item:hover {
  border-color: var(--line-strong);
}
.clip-check {
  display: grid;
  place-items: center;
}
.clip-thumb {
  width: 112px;
  /* 别被行高拉长：否则 16:9 的视频会被 cover 成一条竖缝 */
  align-self: start;
  cursor: pointer;
}
.clip-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.clip-actions {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
  justify-content: flex-end;
}
.clip-history {
  grid-column: 1 / -1;
  border-top: 1px dashed var(--line);
  padding-top: 10px;
  margin-top: 4px;
}
.clip-history-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: var(--space-3);
}
.clip-history-item {
  margin: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--bg-1);
}
.clip-history-item figcaption {
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

/* FR-040 逐镜视频参数表 */
.params-table {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  max-height: 300px;
  overflow: auto;
}
.params-head,
.params-row {
  display: grid;
  grid-template-columns: 42px 84px 84px 1fr;
  gap: var(--space-2);
  align-items: center;
}
.params-head {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg-1);
  padding-bottom: 4px;
  font-size: var(--fs-11);
  color: var(--text-2);
}
.params-row .input,
.params-row .select {
  height: 27px;
  padding: 0 6px;
  font-size: var(--fs-12);
}
</style>
