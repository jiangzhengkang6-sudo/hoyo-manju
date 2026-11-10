<script setup lang="ts">
/**
 * TTS / BGM / 字幕（API.md §8）。
 * 音色库 → 角色绑定音色 → 台词配音 → BGM 挂载 → 字幕生成与导出。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { downloadFile, reportError } from '@/api/client'
import { cancelJob as cancelJobRequest, jobsApi } from '@/api/jobs'
import {
  TRACK_KIND_LABEL,
  audioApi,
  fetchSubtitles,
  fetchTracks,
  fetchTtsLines,
  fetchVoices,
} from '@/api/audio'
import { charactersApi, fetchCharacters } from '@/api/characters'
import { formatCents } from '@/api/cost'
import { mediaApi } from '@/api/media'
import { projectsApi } from '@/api/projects'
import { formatDuration, formatSeconds } from '@/utils/format'
import GateCard from '@/components/GateCard.vue'
import StageHead from '@/components/StageHead.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ErrorState from '@/components/ErrorState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { ApiError, toApiError } from '@/api/errors'
import type {
  AudioTrack,
  BgmEstimate,
  Character,
  Job,
  MediaAsset,
  Project,
  SubtitleCue,
  TrackKind,
  TtsEstimate,
  TtsLine,
  TtsLineStatus,
  Voice,
} from '@/types'

const store = useEpisodeStore()
const config = useConfigStore()
const toast = useToastStore()
const { episode, shots, workflow, pendingGate, jobSnapshots } = storeToRefs(store)

const voices = ref<Voice[]>([])
const characters = ref<Character[]>([])
const ttsLines = ref<TtsLine[]>([])
const tracks = ref<AudioTrack[]>([])
const cues = ref<SubtitleCue[]>([])
const audioMedia = ref<MediaAsset[]>([])

const loading = ref(false)
const error = ref<ApiError | null>(null)
const busy = ref(false)
const gateBusy = ref(false)
const notice = ref('')
const jobIds = ref<string[]>([])

const showVoiceForm = ref(false)
const showTrackForm = ref(false)
const voiceFile = ref<File | null>(null)
const voicePreviewId = ref<string | null>(null)

/* FR-052：BGM 上传 —— 直接在本页传音频，不必先去素材库 */
const bgmFile = ref<File | null>(null)

/* 氛围音 / BGM 生成（分层方案：台词逐句，垫乐单独一轨） */
const bgmPrompt = ref('雨夜巷口，雨点打在铁皮棚上，远处闷雷，低沉的弦乐缓慢铺底，没有人声。')
const bgmSeconds = ref(30)
const bgmVolumeDb = ref(-14)
const bgmEstimate = ref<BgmEstimate | null>(null)
const bgmBusy = ref(false)
/** 空 = 整集垫乐；有值 = 从该镜开始 */
const bgmShotNo = ref<number | null>(null)
/** 与起始镜一起构成区间：null 或等于起始镜 = 只覆盖一个镜头 */
const bgmShotTo = ref<number | null>(null)
/** 用户手改过描述后，就不再用镜头内容自动覆盖 */
const bgmPromptTouched = ref(false)
const bgmSuggestBusy = ref(false)
/** 用户手动选过作用范围后，不再替他改回默认 */
const bgmScopeTouched = ref(false)
const bgmShots = computed(() =>
  shots.value.filter((shot) => shot.required !== false).slice().sort((a, b) => a.shot_no - b.shot_no),
)
/** 结束镜只能选起始镜之后：一段垫乐跨连续几个镜头共用 */
const bgmShotToOptions = computed(() =>
  bgmShots.value.filter((shot) => bgmShotNo.value !== null && shot.shot_no >= bgmShotNo.value),
)
const bgmRange = computed(() => {
  if (bgmShotNo.value === null) return null
  const from = bgmShotNo.value
  const to = Math.max(from, bgmShotTo.value ?? from)
  return { from, to }
})
const bgmScopeLabel = computed(() => {
  const range = bgmRange.value
  if (!range) return '整集'
  return range.from === range.to ? `第 ${range.from} 镜` : `第 ${range.from}–${range.to} 镜`
})
const bgmUploading = ref(false)

/**
 * 上传音频并自动选中，省掉「先去素材库传、再回来选」的来回。
 * 上传即声明权利（`rights_declared=true`），与素材库口径一致。
 */
async function uploadBgm(): Promise<void> {
  const file = bgmFile.value
  const id = episodeId.value
  if (!file || !id) return
  bgmUploading.value = true
  try {
    const media = await mediaApi.upload(file, 'audio', {
      episode_id: id,
      rights_declared: true,
      source_note: '本页上传的背景音乐',
    })
    if (!audioMedia.value.some((m) => m.id === media.id)) {
      audioMedia.value = [media, ...audioMedia.value]
    }
    trackForm.media_id = media.id
    bgmFile.value = null
    toast.success('音频已上传', '已自动选为本条音轨的素材')
  } catch {
    /* api 层已提示 */
  } finally {
    bgmUploading.value = false
  }
}

/* FR-053：字幕人工校正 */
const cueDraft = ref<Record<string, { text: string; start_ms: number; end_ms: number }>>({})
const cueBusy = ref<string | null>(null)

function cueOf(cue: SubtitleCue) {
  return cueDraft.value[cue.id] ?? {
    text: cue.text ?? '',
    start_ms: cue.start_ms ?? 0,
    end_ms: cue.end_ms ?? 0,
  }
}

function ensureCueDraft(cue: SubtitleCue): void {
  if (!cueDraft.value[cue.id]) {
    cueDraft.value = {
      ...cueDraft.value,
      [cue.id]: { text: cue.text ?? '', start_ms: cue.start_ms ?? 0, end_ms: cue.end_ms ?? 0 },
    }
  }
}

/** 正在微调时间的字幕条（`null` = 都在只读态） */
const cueTimeEditing = ref<string | null>(null)

/** 字幕时间按秒展示：`0:02.7` 比裸毫秒 `2656` 好读得多。 */
function cueTime(cue: SubtitleCue, edge: 'start' | 'end'): number {
  const draft = cueOf(cue)
  return Number(edge === 'start' ? draft.start_ms : draft.end_ms) / 1000
}

function cueDuration(cue: SubtitleCue): number {
  return Math.max(cueTime(cue, 'end') - cueTime(cue, 'start'), 0)
}

/** 字幕时间保留 0.1 秒：`0:02.7`。`formatSeconds` 只到整秒，字幕对不齐。 */
function formatCueTime(seconds: number): string {
  const total = Math.max(0, seconds)
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  return `${minutes}:${rest.toFixed(1).padStart(4, '0')}`
}

function toggleCueTime(cue: SubtitleCue): void {
  ensureCueDraft(cue)
  cueTimeEditing.value = cueTimeEditing.value === cue.id ? null : cue.id
}

/** 输入按秒，落库仍按毫秒（后端口径不变）。 */
function setCueTime(cue: SubtitleCue, edge: 'start' | 'end', raw: string): void {
  const seconds = Number(raw)
  if (!Number.isFinite(seconds) || seconds < 0) return
  ensureCueDraft(cue)
  const draft = cueDraft.value[cue.id]
  if (!draft) return
  if (edge === 'start') draft.start_ms = Math.round(seconds * 1000)
  else draft.end_ms = Math.round(seconds * 1000)
}

async function saveCue(cue: SubtitleCue): Promise<void> {
  const id = episodeId.value
  const draftCue = cueDraft.value[cue.id]
  if (!id || !draftCue) return
  if (draftCue.end_ms <= draftCue.start_ms) {
    toast.warn('结束时间需大于开始时间', '字幕时间轴不能倒置')
    return
  }
  cueBusy.value = cue.id
  try {
    const updated = await audioApi.subtitleUpdate(id, cue.id, draftCue)
    const next = { ...cueDraft.value }
    delete next[cue.id]
    cueDraft.value = next
    cues.value = cues.value.map((c) => (c.id === cue.id ? { ...c, ...updated } : c))
    toast.success('字幕已保存', `#${cue.index ?? cue.id}`)
  } catch {
    /* api 层已提示 */
  } finally {
    cueBusy.value = null
  }
}

const cuesDirty = computed(() => Object.keys(cueDraft.value).length > 0)

async function saveAllCues(): Promise<void> {
  const id = episodeId.value
  if (!id) return
  const pending = Object.entries(cueDraft.value)
  if (!pending.length) return
  busy.value = true
  let ok = 0
  let failed = 0
  try {
    for (const [cueId, draftCue] of pending) {
      if (draftCue.end_ms <= draftCue.start_ms) {
        failed += 1
        continue
      }
      try {
        const updated = await audioApi.subtitleUpdate(id, cueId, draftCue)
        cues.value = cues.value.map((c) => (c.id === cueId ? { ...c, ...updated } : c))
        ok += 1
      } catch {
        failed += 1
      }
    }
    // 只清掉成功的，失败项保留给用户重试
    const next = { ...cueDraft.value }
    for (const c of cues.value) delete next[c.id]
    cueDraft.value = next
    if (failed === 0) {
      toast.success('字幕已全部保存', `${ok} 条`)
    } else {
      toast.warn('部分字幕保存失败', `成功 ${ok} 条，失败 ${failed} 条（仍在草稿中）`)
    }
  } finally {
    busy.value = false
  }
}

function discardCueDraft(): void {
  cueDraft.value = {}
  toast.info('已放弃字幕改动')
}

const voiceForm = reactive({
  name: '',
  provider: 'mock',
  language: 'zh-CN',
  gender: '',
})

const trackForm = reactive({
  media_id: '',
  kind: 'bgm' as TrackKind,
  start_ms: 0,
  end_ms: 0,
  volume_db: -6,
  fade_in_ms: 500,
  fade_out_ms: 800,
  rights_declared: false,
})

const projectId = computed(() => episode.value?.project_id ?? '')
const project = ref<Project | null>(null)
const episodeId = computed(() => store.episodeId ?? '')

const jobs = computed<Job[]>(() =>
  jobIds.value.map((id) => jobSnapshots.value[id]).filter((j): j is Job => Boolean(j)),
)

const ttsStale = computed(() => ttsLines.value.filter((l) => l.stale).length)

const voiceById = computed<Record<string, Voice>>(() => {
  const map: Record<string, Voice> = {}
  for (const v of voices.value) map[v.id] = v
  return map
})

async function loadAll(): Promise<void> {
  if (!episodeId.value) return
  loading.value = true
  error.value = null
  try {
    const [p, v, c, t, tr, cu, media] = await Promise.all([
      projectId.value ? projectsApi.get(projectId.value).catch(() => null) : Promise.resolve(null),
      projectId.value ? fetchVoices(projectId.value).catch(() => []) : Promise.resolve([]),
      projectId.value
        ? fetchCharacters(projectId.value, { episodeId: episodeId.value }).catch(() => [])
        : Promise.resolve([]),
      fetchTtsLines(episodeId.value).catch(() => []),
      fetchTracks(episodeId.value).catch(() => []),
      fetchSubtitles(episodeId.value).catch(() => []),
      mediaApi.episodeMedia(episodeId.value, 'audio').then((r) => (Array.isArray(r) ? r : r.items)).catch(() => []),
    ])
    project.value = p
    voices.value = v
    characters.value = c
    ttsLines.value = t
    tracks.value = tr
    cues.value = cu
    audioMedia.value = media
    // FR-050：配音前置条件随分镜/角色变化，每次加载都重新取
    await loadTtsEstimate()
    // §12-4：逐句状态决定「上传音频 / 跳过」入口出现在哪几行
    await loadTtsLines()
    // 氛围音按秒计费，估价随「时长」和「余额」变化
    await loadBgmEstimate()
    await ensureDefaultBgmScope()
  } catch (err) {
    error.value = toApiError(err)
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await config.load()
  await loadAll()
})

watch(projectId, () => void loadAll())

/**
 * 配音任务成功时刷新逐句状态与音轨。
 *
 * 只靠 SSE 的 `media.created` 不够：它只更新概览/工作流，不会重取 `/tts/lines`，
 * 结果就是音频已经生成好了，页面还写着「还有 N 句台词没有音频」。
 */
watch(
  () => jobs.value.map((job) => `${job.id}:${job.status}`).join('|'),
  async (next, prev) => {
    if (!prev) return
    const before = new Set(prev.split('|'))
    const succeeded = next
      .split('|')
      .some((item) => !before.has(item) && item.endsWith(':SUCCEEDED'))
    if (!succeeded) return
    await Promise.all([loadTtsLines(), loadTtsEstimate(), loadAll()])
    store.refreshLive()
  },
)

function track(job: Job): void {
  jobIds.value = [job.id, ...jobIds.value]
  store.trackJob(job)
}

/* ---------------- 音色 ---------------- */

async function submitVoice(): Promise<void> {
  if (!projectId.value) return
  if (!voiceForm.name.trim()) {
    toast.warn('音色名称不能为空')
    return
  }
  busy.value = true
  try {
    let mediaId: string | undefined
    if (voiceFile.value) {
      const media = await mediaApi.upload(voiceFile.value, 'audio', {
        rights_declared: true,
        source_note: '音色样本',
      })
      mediaId = media.id
    }
    const created = await audioApi.createVoice(projectId.value, {
      name: voiceForm.name.trim(),
      provider: voiceForm.provider.trim() || 'mock',
      language: voiceForm.language.trim() || config.defaultLanguage,
      gender: voiceForm.gender.trim() || undefined,
      media_id: mediaId,
    })
    voices.value = [...voices.value, created]
    showVoiceForm.value = false
    voiceFile.value = null
    toast.success('音色已新增', created.name ?? created.id)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function bindVoice(character: Character, voiceId: string): Promise<void> {
  if (!voiceId) return
  busy.value = true
  try {
    const updated = await charactersApi.bindVoice(character.id, voiceId)
    characters.value = characters.value.map((c) => (c.id === updated.id ? updated : c))
    toast.success('音色已绑定', `${character.name} → ${voiceById.value[voiceId]?.name ?? voiceId}`)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

/** 项目「统一语速」：决定之后新建/同步角色的初始语速。 */
async function saveDefaultSpeed(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const value = Number(input.value)
  const current = project.value?.default_voice_speed ?? 1
  if (!projectId.value) return
  if (!Number.isFinite(value) || value < 0.5 || value > 2) {
    toast.warn('语速范围是 0.5 ~ 2.0 倍')
    input.value = current.toFixed(2)
    return
  }
  try {
    project.value = await projectsApi.patch(projectId.value, { default_voice_speed: value })
    toast.success('统一语速已保存', `之后新建的角色初始语速 ${value.toFixed(2)}×`)
  } catch {
    input.value = current.toFixed(2)
  }
}

/** 把统一语速刷到全部现有角色（只改设定，不重新合成）。 */
async function applySpeedToAll(): Promise<void> {
  if (!projectId.value) return
  const speed = Number(project.value?.default_voice_speed ?? 1)
  busy.value = true
  try {
    const res = await projectsApi.voiceSpeed(projectId.value, speed)
    toast.success(
      '已应用到全部角色',
      `${res.count} 个角色 → ${speed.toFixed(2)}×；` +
        (res.retimed ? `${res.retimed} 条已有配音已就地变速（0 费用）` : '暂无已有配音'),
    )
    await Promise.all([loadTtsLines(), loadAll()])
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

/** FR-051：调整角色语速。供应商不认 speed，后端会用 FFmpeg atempo 本地变速。 */
async function changeVoiceSpeed(character: Character, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const value = Number(input.value)
  const current = character.voice_speed ?? 1
  if (!character.voice_id) {
    toast.warn('请先给这个角色绑定音色，再调语速')
    input.value = String(current)
    return
  }
  if (!Number.isFinite(value) || value < 0.5 || value > 2) {
    toast.warn('语速范围是 0.5 ~ 2.0 倍')
    input.value = String(current)
    return
  }
  busy.value = true
  try {
    const updated = await charactersApi.bindVoice(character.id, character.voice_id, value)
    characters.value = characters.value.map((item) => (item.id === updated.id ? updated : item))
    toast.success('语速已更新', `${character.name} → ${value.toFixed(2)}×，已生成的这句会标记为待重跑`)
    // 后端会把该角色已有的台词音轨标 stale，重新取一次逐句状态
    await loadTtsLines()
  } catch {
    input.value = String(current)
  } finally {
    busy.value = false
  }
}

/* ---------------- TTS ---------------- */

/**
 * FR-050：TTS 前置条件（有台词的角色必须已绑定音色）。
 * 不满足时后端会 409 拒绝，所以这里提前取预估、把按钮禁用并说明原因 ——
 * 让用户点下去才报错是很差的体验。
 */
const ttsEstimate = ref<TtsEstimate | null>(null)
const ttsEstimateError = ref<string | null>(null)

const missingVoices = computed(() => ttsEstimate.value?.blocking_problems ?? [])
const ttsBlocked = computed(() => missingVoices.value.length > 0)

async function loadTtsEstimate(): Promise<void> {
  if (!episodeId.value) return
  try {
    ttsEstimate.value = await audioApi.ttsEstimate(episodeId.value)
    ttsEstimateError.value = null
  } catch (err) {
    ttsEstimate.value = null
    ttsEstimateError.value = err instanceof Error ? err.message : '无法获取配音预估'
  }
}

/* ---------------- §12-4 TTS 失败降级：上传音频 / 跳过配音 ---------------- */

const ttsLineStatus = ref<TtsLineStatus[]>([])
const ttsCounts = ref<Record<string, number>>({})
const degradeBusy = ref(false)

/** 还没生成音频的台词（这些才需要兜底入口）。 */
const missingLines = computed(() => ttsLineStatus.value.filter((l) => l.status === 'missing'))
/** 已有音频但已过期的台词。 */
const staleLines = computed(() => ttsLineStatus.value.filter((l) => l.status === 'stale'))
/** 参数变了、音频还是旧配方出来的（不重生成就会和别人的声音不一样）。 */
const driftLines = computed(() => ttsLineStatus.value.filter((l) => l.voice_drift))
/** 指纹上线前生成的历史音频：不知道当年用的哪套配方，只做提示、不判过期。 */
const legacyLines = computed(() => ttsLineStatus.value.filter((l) => l.voice_legacy))
/** 已经有音频的台词（第二张表只列它们，避免和上面的「缺音频」表重复）。 */
const generatedLines = computed(() =>
  ttsLineStatus.value.filter((line) => line.status !== 'missing'),
)

async function loadTtsLines(): Promise<void> {
  if (!episodeId.value) return
  try {
    const body = await audioApi.ttsLines(episodeId.value)
    ttsLineStatus.value = body.items ?? []
    ttsCounts.value = body.counts ?? {}
  } catch {
    ttsLineStatus.value = []
    ttsCounts.value = {}
  }
}

/** 上传音频顶替某句台词：先传素材，再把它挂到该句上。 */
async function uploadManualAudio(line: TtsLineStatus, file: File | null): Promise<void> {
  if (!file || !episodeId.value || line.shot_no == null) return
  degradeBusy.value = true
  try {
    const asset = await mediaApi.upload(file, 'audio', {
      episode_id: episodeId.value,
      rights_declared: true,
    })
    await audioApi.ttsManual(episodeId.value, {
      shot_no: line.shot_no,
      line_index: lineIndexInShot(line),
      media_id: asset.id,
    })
    toast.success(`已用上传的音频顶替第 ${line.shot_no} 镜的配音`)
    await Promise.all([loadTtsLines(), loadTtsEstimate(), loadAll()])
  } catch (err) {
    reportError(err)
  } finally {
    degradeBusy.value = false
  }
}

/** 该句在所属镜头内的序号（后端按序号定位，不能传全局下标）。 */
/** 已有音频的即时调速：本地变速，不调用模型，因此不产生费用。 */
async function retimeLine(line: TtsLineStatus, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const value = Number(input.value)
  const current = line.speed ?? 1
  if (!episodeId.value || !line.track_id) return
  if (!Number.isFinite(value) || value < 0.5 || value > 2) {
    toast.warn('语速范围是 0.5 ~ 2.0 倍')
    input.value = String(current)
    return
  }
  if (Math.abs(value - current) < 0.001) return
  busy.value = true
  try {
    await audioApi.ttsSpeed(episodeId.value, line.track_id, value)
    toast.success('语速已重调', `${value.toFixed(2)}× · 本地变速，未调用模型、无费用`)
    await Promise.all([loadTtsLines(), loadAll()])
  } catch {
    input.value = String(current)
  } finally {
    busy.value = false
  }
}

/** 性别 / 供应商都说人话：后端存的是 female、qwen 这类枚举。 */
const GENDER_LABELS: Record<string, string> = { female: '女声', male: '男声', neutral: '中性' }
const PROVIDER_LABELS: Record<string, string> = {
  qwen: '通义千问',
  mock: '本地模拟',
  openai: 'OpenAI',
  azure: 'Azure',
  minimax: 'MiniMax',
  volcano: '火山引擎',
  fish: 'Fish Audio',
}

function genderLabel(gender?: string | null): string {
  if (!gender) return ''
  return GENDER_LABELS[gender.toLowerCase()] ?? ''
}

function providerLabel(provider?: string | null): string {
  if (!provider) return ''
  return PROVIDER_LABELS[provider.toLowerCase()] ?? '其他供应商'
}

/** 音色列展示中文名，别把 `voc_xxx` 这种内部 id 直接倒给用户。 */
function lineVoiceName(line: TtsLineStatus): string {
  if (!line.voice_id) return '—'
  return voiceById.value[line.voice_id]?.name ?? '未知音色'
}

/** 状态一律中文；后端返回的是 ready/missing/skipped 这类枚举。 */
function lineStatusLabel(line: TtsLineStatus): string {
  // 「旧音色」比「已过期」更具体：不是任务失败，是配方变了、这条还是旧声音
  if (line.voice_drift) return '旧音色'
  if (line.stale) return '已过期'
  if (line.source === 'skipped') return '已跳过'
  if (line.source === 'manual') return '用户上传'
  const labels: Record<string, string> = {
    ready: '已就绪',
    missing: '待生成',
    stale: '已过期',
    skipped: '已跳过',
  }
  return labels[line.status] ?? '已就绪'
}

function lineIndexInShot(line: TtsLineStatus): number {
  // 后端现在直接返回 line_index；旧数据才退回「按文本找第几条」
  if (typeof line.line_index === 'number') return line.line_index
  const same = ttsLineStatus.value.filter((l) => l.shot_no === line.shot_no)
  return Math.max(0, same.findIndex((l) => l.text === line.text))
}

async function skipLines(shotNos: number[], reason = '', lineIndex?: number): Promise<void> {
  if (!episodeId.value || !shotNos.length) return
  degradeBusy.value = true
  try {
    await audioApi.ttsSkip(episodeId.value, { shot_nos: shotNos, reason, line_index: lineIndex })
    toast.info(`已跳过 ${shotNos.length} 个镜头的配音，可继续预览`)
    await Promise.all([loadTtsLines(), loadAll()])
  } catch (err) {
    reportError(err)
  } finally {
    degradeBusy.value = false
  }
}

async function onManualPick(line: TtsLineStatus, event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  await uploadManualAudio(line, input.files?.[0] ?? null)
  // 清空，否则同一个文件再选一次不会触发 change
  input.value = ''
}

/** 阻断项里的镜号列表，拼成「第 1、2 镜」。 */
function shotNosLabel(shotNos: number[]): string {
  if (!shotNos.length) return ''
  return `第 ${shotNos.join('、')} 镜`
}

async function generateTts(scope: 'all' | 'confirmed', characterId?: string): Promise<void> {
  if (!episodeId.value) return
  if (ttsBlocked.value) {
    toast.warn('请先为有台词的角色绑定音色')
    return
  }
  busy.value = true
  try {
    const body: { shot_nos?: number[]; character_id?: string } = {}
    if (scope === 'confirmed') {
      body.shot_nos = shots.value.filter((s) => s.confirmed_media_id).map((s) => s.shot_no)
      if (!body.shot_nos.length) {
        toast.warn('没有已确认画面的镜头')
        return
      }
    }
    if (characterId) body.character_id = characterId
    track(await audioApi.ttsGenerate(episodeId.value, body))
    toast.info('配音任务已提交')
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

/* ---------------- BGM ---------------- */

async function loadBgmEstimate(): Promise<void> {
  if (!episodeId.value) return
  const range = bgmRange.value
  try {
    bgmEstimate.value = await audioApi.bgmEstimate(
      episodeId.value,
      range
        ? { shot_from: range.from, shot_to: range.to }
        : { seconds: bgmSeconds.value },
    )
  } catch {
    bgmEstimate.value = null
  }
}

/**
 * 按所选镜头的内容重写氛围描述。
 *
 * 不同镜头要不同的垫乐，但每次都手写太累；分镜里已经有场景、音效和情绪，
 * 后端直接拼一句草稿（不调模型、不扣费）。用户手改过就默认不覆盖。
 */
async function refreshBgmPrompt(force = false): Promise<void> {
  const id = episodeId.value
  const range = bgmRange.value
  if (!id || !range) return
  if (bgmPromptTouched.value && !force) return
  bgmSuggestBusy.value = true
  try {
    const suggestion = await audioApi.bgmSuggest(id, range.from, range.to)
    bgmPrompt.value = suggestion.prompt
    bgmPromptTouched.value = false
  } catch {
    /* 建议拿不到不影响手动写 */
  } finally {
    bgmSuggestBusy.value = false
  }
}

/**
 * 生成一轨氛围音 / BGM。
 *
 * 只提交一次请求：模型那侧一次就能出「环境声 + 音乐」，我们把它当垫乐挂到
 * 时间线（音量默认 -14dB，淡入淡出），台词轨完全不受影响。
 */
async function generateBgm(): Promise<void> {
  if (!episodeId.value) return
  const prompt = bgmPrompt.value.trim()
  if (prompt.length < 4) {
    toast.warn('先写一句氛围描述', '例如：雨夜巷口，雨声与低沉的弦乐')
    return
  }
  const range = bgmRange.value
  bgmBusy.value = true
  try {
    const job = await audioApi.bgmGenerate(episodeId.value, {
      prompt,
      seconds: bgmSeconds.value,
      ...(range ? { shot_from: range.from, shot_to: range.to } : {}),
      volume_db: bgmVolumeDb.value,
    })
    track(job)
    toast.info(
      range ? `${bgmScopeLabel.value}氛围音生成中` : '整集氛围音生成中',
      `${bgmEstimate.value ? formatCents(bgmEstimate.value.estimate_cents) : '—'} · 完成后自动挂到时间线`,
    )
  } catch {
    /* api 层已提示 */
  } finally {
    bgmBusy.value = false
  }
}

function onBgmScopePick(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value
  bgmScopeTouched.value = true
  bgmShotNo.value = raw ? Number(raw) : null
  // 换了起点就把终点拉回起点，避免出现「起点在 5、终点在 2」的空区间
  bgmShotTo.value = bgmShotNo.value
  if (bgmShotNo.value) {
    const shot = shots.value.find((item) => item.shot_no === bgmShotNo.value)
    if (shot?.duration_ms) {
      bgmSeconds.value = Math.max(3, Math.ceil(shot.duration_ms / 1000))
    }
  }
  void afterScopeChange()
}

function onBgmToPick(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value
  bgmShotTo.value = raw ? Number(raw) : bgmShotNo.value
  void afterScopeChange()
}

async function afterScopeChange(): Promise<void> {
  await loadBgmEstimate()
  await refreshBgmPrompt()
}

/** 首次拿到分镜时，把作用范围落在第 1 镜：让「描述跟着镜头走」一眼可见。 */
const bgmDefaultApplied = ref(false)
async function ensureDefaultBgmScope(): Promise<void> {
  if (bgmDefaultApplied.value || bgmScopeTouched.value) return
  if (bgmShotNo.value !== null) {
    bgmDefaultApplied.value = true
    return
  }
  if (!bgmShots.value.length) return
  bgmDefaultApplied.value = true
  bgmShotNo.value = bgmShots.value[0].shot_no
  bgmShotTo.value = bgmShotNo.value
  await afterScopeChange()
}

// 分镜由 store 统一加载，可能晚于本页首次渲染
watch(bgmShots, () => void ensureDefaultBgmScope())

/**
 * 环境音轨覆盖了哪几个镜头。
 *
 * 后端把范围记在素材 metadata 里（`shot_from`/`shot_to`），单镜时两者相等；
 * 拿不到就返回空串，列表照常显示时间区间。
 */
function trackShotScope(track: AudioTrack): string {
  const media = track.media as { metadata_json?: Record<string, unknown> } | undefined
  const from = Number(media?.metadata_json?.shot_from ?? 0)
  const to = Number(media?.metadata_json?.shot_to ?? 0)
  if (!from) return ''
  return from === to ? `第 ${from} 镜` : `第 ${from}–${to} 镜`
}

watch(bgmSeconds, () => void loadBgmEstimate())

function openTrackForm(): void {
  trackForm.media_id = audioMedia.value[0]?.id ?? ''
  trackForm.kind = 'bgm'
  trackForm.start_ms = 0
  trackForm.end_ms = store.totalDurationMs
  trackForm.volume_db = -6
  trackForm.fade_in_ms = 500
  trackForm.fade_out_ms = 800
  trackForm.rights_declared = false
  showTrackForm.value = true
}

async function submitTrack(): Promise<void> {
  if (!episodeId.value) return
  if (!trackForm.media_id) {
    toast.warn('请选择音频素材', '可先在 /media/upload 上传音频')
    return
  }
  if (trackForm.end_ms <= trackForm.start_ms) {
    toast.warn('结束时间需大于开始时间')
    return
  }
  busy.value = true
  try {
    const created = await audioApi.bgmCreate(episodeId.value, {
      media_id: trackForm.media_id,
      kind: trackForm.kind,
      start_ms: trackForm.start_ms,
      end_ms: trackForm.end_ms,
      volume_db: trackForm.volume_db,
      fade_in_ms: trackForm.fade_in_ms,
      fade_out_ms: trackForm.fade_out_ms,
      rights_declared: trackForm.rights_declared,
    })
    tracks.value = [...tracks.value, created]
    showTrackForm.value = false
    toast.success('音轨已挂载')
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function removeTrack(trackItem: AudioTrack): Promise<void> {
  if (!episodeId.value || !trackItem.id) return
  busy.value = true
  try {
    await audioApi.bgmDelete(episodeId.value, trackItem.id)
    tracks.value = tracks.value.filter((t) => t.id !== trackItem.id)
    toast.success('音轨已移除')
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

/* ---------------- 字幕 ---------------- */

async function generateSubtitles(): Promise<void> {
  if (!episodeId.value) return
  busy.value = true
  try {
    track(await audioApi.subtitlesGenerate(episodeId.value))
    toast.info('字幕生成任务已提交')
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function exportSubtitle(format: 'srt' | 'vtt'): Promise<void> {
  if (!episodeId.value) return
  try {
    await downloadFile(`/episodes/${episodeId.value}/subtitles/export`, `episode_${episodeId.value}.${format}`, {
      format,
    })
    toast.success(`已下载 ${format.toUpperCase()} 字幕`)
  } catch {
    /* api 层已提示 */
  }
}

/* ---------------- 任务 ---------------- */

async function retryJob(id: string): Promise<void> {
  try {
    track(await jobsApi.retry(id))
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

/* ---------------- 门控（音频阶段之后可能仍有视频门控） ---------------- */

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
  toast.error('门控决策失败', err instanceof Error ? err.message : String(err))
}
</script>

<template>
  <div class="stage">
    <StageHead
      index="05"
      title="音频字幕"
      description="台词配音、背景音乐与字幕时间轴，都在这一步对齐。"
    />
    <GateCard
      :gate="pendingGate"
      :workflow="workflow"
      :busy="gateBusy"
      :notice="notice"
      approve-text="确认音频与字幕，进入合成"
      description="确认配音、BGM 与字幕后，工作流会进入自动合成阶段。"
      @approve="approve"
      @reject="reject"
      @edit="edit"
      @refresh="store.refreshLive()"
    >
      <div class="row-wrap tiny dim">
        <span>台词音频 <span class="mono">{{ ttsLines.length }}</span></span>
        <span v-if="ttsStale" class="over">过期 <span class="mono">{{ ttsStale }}</span></span>
        <span>音轨 <span class="mono">{{ tracks.length }}</span></span>
        <span>字幕 <span class="mono">{{ cues.length }}</span></span>
      </div>
    </GateCard>

    <ErrorState v-if="error" :error="error" context="加载音频数据" @retry="loadAll" />

    <div class="two-col two-col--even">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">音色库</h2>
          <div class="row">
            <button type="button" class="btn btn-sm btn-ghost" @click="loadAll">刷新</button>
            <button type="button" class="btn btn-sm btn-primary" @click="showVoiceForm = true">
              新增音色
            </button>
          </div>
        </div>
        <div class="card-body">
          <SkeletonBlock v-if="loading && !voices.length" :rows="3" />
          <EmptyState
            v-else-if="!voices.length"
            compact
            icon="♪"
            title="还没有可用音色"
            description="可上传音色样本，或使用供应商内置音色。"
          >
            <button type="button" class="btn btn-sm btn-primary" @click="showVoiceForm = true">新增音色</button>
          </EmptyState>
          <ul v-else class="voice-list">
            <li v-for="voice in voices" :key="voice.id" class="voice-item">
              <div class="voice-main">
                <div class="row-wrap">
                  <strong class="small">{{ voice.name ?? voice.id }}</strong>
                  <span v-if="voice.builtin" class="badge">内置</span>
                  <span v-if="voice.gender" class="badge">{{ genderLabel(voice.gender) }}</span>
                  <span v-if="voice.language" class="badge">{{ voice.language }}</span>
                </div>
                <span class="tiny dim">{{ providerLabel(voice.provider) }} · 可直接试听或绑定给角色</span>
              </div>
              <div class="row">
                <button
                  type="button"
                  class="btn btn-sm btn-ghost"
                  @click="voicePreviewId = voicePreviewId === voice.id ? null : voice.id"
                >
                  {{ voicePreviewId === voice.id ? '收起' : '试听' }}
                </button>
              </div>
              <div v-if="voicePreviewId === voice.id" class="voice-preview">
                <audio :src="audioApi.voicePreviewUrl(voice.id)" controls preload="none" />
              </div>
            </li>
          </ul>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <div class="row">
            <h2 class="title-md">角色音色绑定</h2>
            <span class="mono tiny dim">{{ characters.length }} 个角色</span>
          </div>
          <div class="row-wrap speed-bar">
            <label class="speed-field" title="新建/剧本同步角色时的初始语速">
              <span class="tiny dim">统一语速</span>
              <input
                class="input mono speed-input"
                type="number"
                min="0.5"
                max="2"
                step="0.05"
                :value="(project?.default_voice_speed ?? 1).toFixed(2)"
                :disabled="busy"
                @change="saveDefaultSpeed($event)"
              />
              <span class="tiny dim">×</span>
            </label>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              :disabled="busy || !characters.length"
              title="把统一语速写进每个角色；已有配音会标记为待重跑"
              @click="applySpeedToAll"
            >
              应用到全部角色
            </button>
          </div>
        </div>
        <div class="card-body">
          <p class="hint">
            「统一语速」决定之后新建角色的初始语速；已有音频可在下方台词表逐条即时调整。
          </p>
          <EmptyState
            v-if="!characters.length"
            compact
            icon="☺"
            title="项目还没有角色"
            description="先到「角色与画风」创建角色卡。"
          />
          <ul v-else class="bind-list">
            <li v-for="character in characters" :key="character.id" class="bind-item">
              <div class="grow">
                <div class="row-wrap">
                  <strong class="small">{{ character.name }}</strong>
                  <span v-if="character.voice_id" class="badge badge-ok">
                    {{ voiceById[character.voice_id]?.name ?? '未知音色' }}
                  </span>
                  <span v-else class="badge badge-warn">未绑定</span>
                </div>
              </div>
              <div class="bind-controls">
                <select
                  class="select bind-select"
                  :value="character.voice_id ?? ''"
                  @change="bindVoice(character, ($event.target as HTMLSelectElement).value)"
                >
                  <option value="">选择音色…</option>
                  <option v-for="voice in voices" :key="voice.id" :value="voice.id">
                    {{ voice.name ?? voice.id }}
                  </option>
                </select>
                <label class="speed-field" :class="{ disabled: !character.voice_id }">
                  <span class="tiny dim">语速</span>
                  <input
                    class="input mono speed-input"
                    type="number"
                    min="0.5"
                    max="2"
                    step="0.05"
                    :value="character.voice_speed ?? 1"
                    :disabled="busy || !character.voice_id"
                    :title="character.voice_id ? '0.5 ~ 2.0 倍；改完需重新生成配音' : '请先绑定音色'"
                    @change="changeVoiceSpeed(character, $event)"
                  />
                  <span class="tiny dim">×</span>
                </label>
              </div>
            </li>
          </ul>
        </div>
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">台词配音（TTS）</h2>
          <span class="mono tiny dim">
            {{ ttsCounts.total ?? ttsLines.length }} 句 · 已生成 {{ ttsCounts.ready ?? 0 }}
          </span>
          <span
            v-if="(ttsCounts.missing ?? 0) > 0"
            class="mono tiny dim"
            title="尚未合成音频的台词条数与预估成本"
          >
            待合成 {{ ttsCounts.missing }} 句 ·
            <template v-if="ttsEstimate">预估 {{ formatCents(ttsEstimate.estimate_cents) }}</template>
          </span>
        </div>
        <div class="row-wrap">
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="busy || ttsBlocked"
            :title="ttsBlocked ? '请先为有台词的角色绑定音色' : ''"
            @click="generateTts('all')"
          >
            生成全部台词
          </button>
          <button
            type="button"
            class="btn btn-sm btn-primary"
            :disabled="busy || ttsBlocked"
            :title="ttsBlocked ? '请先为有台词的角色绑定音色' : ''"
            @click="generateTts('confirmed')"
          >
            仅生成已确认画面
          </button>
        </div>
      </div>
      <div class="card-body col" style="gap: var(--space-4)">
        <!-- FR-050：前置条件不满足时明确告知「差什么、去哪补」，而不是让提交失败 -->
        <div v-if="ttsBlocked" class="voice-block">
          <div class="voice-block-head">
            <strong>有台词的角色还没绑定音色，无法生成配音</strong>
          </div>
          <ul class="voice-block-list">
            <li v-for="problem in missingVoices" :key="problem.character">
              <span class="badge badge-warn">{{ problem.character }}</span>
              <span class="dim tiny">{{ shotNosLabel(problem.shot_nos) }}</span>
              <span class="tiny">{{ problem.message }}</span>
            </li>
          </ul>
          <RouterLink
            class="btn btn-sm btn-primary"
            :to="{ name: 'episode-characters', params: { episodeId } }"
          >
            去「角色」页绑定音色
          </RouterLink>
        </div>
        <div v-else-if="ttsEstimateError" class="hint">
          无法获取配音预估：{{ ttsEstimateError }}
        </div>
        <!-- §12-4：TTS 失败时的兜底入口。只对真正缺音频的句子出现，避免误导 -->
        <div v-if="missingLines.length" class="degrade-block">
          <div class="degrade-head">
            <strong>还有 {{ missingLines.length }} 句台词没有音频</strong>
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              :disabled="degradeBusy"
              @click="skipLines(missingLines.map((l) => l.shot_no).filter((n): n is number => n != null), '用户选择跳过配音')"
            >
              全部跳过，先出片
            </button>
          </div>
          <p class="tiny dim">
            TTS 失败不影响出片：可以给每句上传自己的音频，或直接跳过该句继续预览与导出。
          </p>
          <table class="table">
            <thead>
              <tr>
                <th style="width: 60px">镜号</th>
                <th style="width: 100px">角色</th>
                <th>台词</th>
                <th style="width: 210px">兜底处理</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(line, i) in missingLines" :key="`${line.shot_no}-${i}`">
                <td class="mono">{{ line.shot_no != null ? `#${line.shot_no}` : '—' }}</td>
                <td class="small">{{ line.character || '—' }}</td>
                <td class="tiny muted">{{ line.text || '—' }}</td>
                <td>
                  <div class="row-wrap">
                    <label class="btn btn-sm btn-ghost" :class="{ disabled: degradeBusy }">
                      上传音频
                      <input
                        type="file"
                        accept="audio/*"
                        class="hidden-file"
                        :disabled="degradeBusy"
                        @change="onManualPick(line, $event)"
                      />
                    </label>
                    <button
                      type="button"
                      class="btn btn-sm btn-ghost"
                      :disabled="degradeBusy || line.shot_no == null"
                      @click="skipLines([line.shot_no as number], '用户选择跳过该句配音', lineIndexInShot(line))"
                    >
                      跳过
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-if="staleLines.length || legacyLines.length" class="hint">
          <template v-if="driftLines.length">
            有 {{ driftLines.length }} 句台词的音色参数已经改过，现有音频还是按旧参数生成的
            —— 和新生成的几句听起来会是两个声音，建议一起重新生成。
          </template>
          <template v-if="legacyLines.length">
            <span v-if="driftLines.length">；</span>
            另有 {{ legacyLines.length }} 句是早期生成的音频，没有记录当时的音色参数，可能和现在的音色不一致。
          </template>
        </div>
        <EmptyState
          v-if="!ttsLines.length"
          compact
          icon="♪"
          title="还没有台词音频"
          description="绑定音色后点击生成，配音会与镜头台词对齐。"
        />
        <table v-else class="table">
          <thead>
            <tr>
              <th style="width: 60px">镜号</th>
              <th style="width: 110px">角色</th>
              <th>台词</th>
              <th style="width: 130px">音色</th>
              <th style="width: 104px" title="本地变速，不重新调用模型">语速</th>
              <th style="width: 112px">状态</th>
              <th style="width: 110px">试听</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(line, i) in generatedLines" :key="line.track_id ?? line.media_id ?? i">
              <td class="mono">{{ line.shot_no != null ? `#${line.shot_no}` : '—' }}</td>
              <td class="small">{{ line.character ?? '—' }}</td>
              <td class="tiny muted">{{ line.text ?? '—' }}</td>
              <td class="small">{{ lineVoiceName(line) }}</td>
              <td>
                <label class="speed-field">
                  <input
                    class="input mono speed-input"
                    type="number"
                    min="0.5"
                    max="2"
                    step="0.05"
                    :value="(line.speed ?? 1).toFixed(2)"
                    :disabled="busy || !line.track_id"
                    title="只调这一条，本地变速，不产生费用"
                    @change="retimeLine(line, $event)"
                  />
                  <span class="tiny dim">×</span>
                </label>
              </td>
              <td>
                <span
                  class="badge"
                  :class="
                    line.stale || line.voice_drift
                      ? 'badge-warn'
                      : line.source === 'skipped'
                        ? 'badge'
                        : 'badge-ok'
                  "
                  :title="line.stale_reason || ''"
                >
                  {{ lineStatusLabel(line) }}
                </span>
                <span
                  v-if="line.voice_legacy"
                  class="badge voice-legacy"
                  title="早期生成的音频，没有记录当时的音色参数，可能和现在的音色不一致"
                >
                  旧版本
                </span>
              </td>
              <td>
                <audio
                  v-if="line.media_id"
                  :src="mediaApi.contentUrl(line.media_id)"
                  controls
                  preload="metadata"
                />
                <span v-else class="tiny dim-2">无音频</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div class="two-col two-col--even">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">BGM / 音效</h2>
          <button type="button" class="btn btn-sm btn-ghost" @click="openTrackForm">挂载已有音频</button>
        </div>
        <div class="card-body col">
          <!-- 生成垫乐：写场景不写台词，一次出一轨环境声/音乐 -->
          <div class="bgm-gen">
            <div class="bgm-scope">
              <span class="label">作用范围</span>
              <select class="select bgm-scope-select" :value="bgmShotNo ?? ''" @change="onBgmScopePick">
                <option value="">整集垫乐（从 0 秒铺到尾）</option>
                <option v-for="shot in bgmShots" :key="shot.id" :value="shot.shot_no">
                  从第 {{ shot.shot_no }} 镜
                </option>
              </select>
              <template v-if="bgmShotNo !== null">
                <span class="tiny dim">到</span>
                <select
                  class="select bgm-scope-select"
                  :value="bgmShotTo ?? bgmShotNo"
                  @change="onBgmToPick"
                >
                  <option v-for="shot in bgmShotToOptions" :key="shot.id" :value="shot.shot_no">
                    第 {{ shot.shot_no }} 镜
                  </option>
                </select>
              </template>
              <span class="hint">
                <template v-if="bgmRange">
                  覆盖 {{ bgmScopeLabel }}<template v-if="bgmEstimate?.window"> · {{ formatDuration(bgmEstimate.window.duration_ms) }}</template>，
                  按这段长度裁剪；相邻几镜可以共用一段
                </template>
                <template v-else>整集一条，重新生成会整条替换</template>
              </span>
            </div>
            <div class="field grow">
              <div class="row-between">
                <label class="label" for="bgm-prompt">氛围描述</label>
                <button
                  v-if="bgmRange"
                  type="button"
                  class="btn btn-sm btn-ghost"
                  :disabled="bgmSuggestBusy"
                  @click="refreshBgmPrompt(true)"
                >
                  {{ bgmSuggestBusy ? '整理中…' : '按镜头重写' }}
                </button>
              </div>
              <textarea
                id="bgm-prompt"
                v-model="bgmPrompt"
                class="textarea bgm-prompt"
                rows="2"
                placeholder="雨夜巷口，雨点打在铁皮棚上，远处闷雷，低沉的弦乐缓慢铺底，没有人声。"
                @input="bgmPromptTouched = true"
              />
              <span class="hint">
                <template v-if="bgmRange">
                  {{
                    bgmPromptTouched
                      ? '已手改，切换镜头不会再覆盖；点「按镜头重写」可回到建议文案'
                      : '已按所选镜头的场景、音效和情绪生成，可直接改'
                  }}
                </template>
                <template v-else>写场景、乐器与情绪，别写台词——这一轨只做垫乐，人声仍走上面的逐句配音。</template>
              </span>
            </div>
            <div class="bgm-side">
              <label class="field" for="bgm-seconds">
                <span class="label">时长</span>
                <input
                  id="bgm-seconds"
                  v-model.number="bgmSeconds"
                  class="input mono"
                  type="number"
                  :min="bgmEstimate?.min_seconds ?? 3"
                  :max="bgmEstimate?.max_seconds ?? 120"
                  :disabled="bgmRange !== null"
                  step="5"
                />
              </label>
              <label class="field" for="bgm-volume">
                <span class="label">音量</span>
                <select id="bgm-volume" v-model.number="bgmVolumeDb" class="select">
                  <option :value="-18">轻柔 −18dB</option>
                  <option :value="-14">标准 −14dB</option>
                  <option :value="-10">稍响 −10dB</option>
                </select>
              </label>
              <button
                type="button"
                class="btn btn-primary btn-block"
                :disabled="bgmBusy || bgmEstimate?.can_generate === false"
                @click="generateBgm"
              >
                {{ bgmBusy ? '提交中…' : '生成氛围音' }}
              </button>
              <span v-if="bgmEstimate" class="tiny dim">
                约 {{ formatCents(bgmEstimate.estimate_cents) }} · {{ bgmEstimate.seconds }} 秒
              </span>
              <span v-else class="tiny dim">按生成时长计费</span>
              <span v-if="bgmRange" class="tiny dim-2">
                按这段实际生成长度结算，可能略高于估价
              </span>
            </div>
          </div>

          <EmptyState
            v-if="!tracks.length"
            compact
            icon="♬"
            title="还没有音轨"
            description="BGM / 环境音 / 音效会按时间区间混入成片。"
          />
          <ul v-else class="track-list">
            <li v-for="(t, i) in tracks" :key="t.id ?? i" class="track-item">
              <div class="track-head">
                <div class="grow">
                  <div class="row-wrap">
                    <span class="badge badge-accent">{{ TRACK_KIND_LABEL[t.kind] ?? t.kind }}</span>
                    <span v-if="trackShotScope(t)" class="badge">{{ trackShotScope(t) }}</span>
                    <span class="mono tiny dim">
                      {{ formatSeconds((t.start_ms ?? 0) / 1000) }} ~ {{ formatSeconds((t.end_ms ?? 0) / 1000) }}
                    </span>
                    <span class="mono tiny dim">{{ t.volume_db }}dB</span>
                    <span v-if="t.rights_declared" class="badge badge-ok">权利已声明</span>
                    <span v-else class="badge badge-warn">权利未声明</span>
                  </div>
                  <p v-if="t.text" class="tiny dim ellipsis track-prompt" :title="String(t.text)">
                    {{ t.text }}
                  </p>
                </div>
                <button type="button" class="btn btn-sm btn-danger" :disabled="busy" @click="removeTrack(t)">
                  移除
                </button>
              </div>
              <audio
                v-if="t.media_id"
                :src="mediaApi.contentUrl(t.media_id ?? '')"
                controls
                preload="none"
              />
            </li>
          </ul>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2 class="title-md">字幕</h2>
          <div class="row-wrap">
            <button type="button" class="btn btn-sm" :disabled="busy" @click="generateSubtitles">
              生成字幕
            </button>
            <button type="button" class="btn btn-sm btn-ghost" @click="exportSubtitle('srt')">导出 SRT</button>
            <button type="button" class="btn btn-sm btn-ghost" @click="exportSubtitle('vtt')">导出 VTT</button>
          </div>
        </div>
        <div class="card-body col" style="gap: var(--space-3)">
          <EmptyState
            v-if="!cues.length"
            compact
            icon="▭"
            title="还没有字幕条目"
            description="字幕依据确认台词与实际音频生成。"
          />
          <template v-else>
            <!-- FR-053：字幕人工校正，改完统一保存 -->
            <div v-if="cuesDirty" class="cue-bar">
              <span class="tiny">有未保存的字幕改动</span>
              <span class="row" style="gap: var(--space-2)">
                <button type="button" class="btn btn-sm btn-primary" :disabled="busy" @click="saveAllCues">
                  {{ busy ? '保存中…' : '保存全部字幕' }}
                </button>
                <button type="button" class="btn btn-sm btn-ghost" @click="discardCueDraft">
                  放弃改动
                </button>
              </span>
            </div>
            <ul class="cue-list">
              <li v-for="(cue, i) in cues" :key="cue.id" class="cue-item">
                <div class="cue-head">
                  <span class="mono tiny dim cue-idx">{{ cue.index ?? i + 1 }}</span>
                  <div class="cue-times">
                    <!-- 只读态给出人话时间；想看毫秒/微调再点开 -->
                    <button
                      v-if="cueTimeEditing !== cue.id"
                      type="button"
                      class="cue-time-pill"
                      :title="`${cueOf(cue).start_ms} → ${cueOf(cue).end_ms} 毫秒，点击微调`"
                      @click="toggleCueTime(cue)"
                    >
                      {{ formatCueTime(cueTime(cue, 'start')) }} → {{ formatCueTime(cueTime(cue, 'end')) }}
                    </button>
                    <template v-else>
                      <input
                        :value="cueTime(cue, 'start')"
                        class="input mono cue-sec"
                        type="number"
                        step="0.1"
                        min="0"
                        :aria-label="`第 ${i + 1} 条字幕开始时间（秒）`"
                        @focus="ensureCueDraft(cue)"
                        @input="setCueTime(cue, 'start', ($event.target as HTMLInputElement).value)"
                      />
                      <span class="dim-2 tiny">→</span>
                      <input
                        :value="cueTime(cue, 'end')"
                        class="input mono cue-sec"
                        type="number"
                        step="0.1"
                        min="0"
                        :aria-label="`第 ${i + 1} 条字幕结束时间（秒）`"
                        @focus="ensureCueDraft(cue)"
                        @input="setCueTime(cue, 'end', ($event.target as HTMLInputElement).value)"
                      />
                      <span class="tiny dim-2">秒</span>
                      <button type="button" class="btn btn-sm btn-ghost" @click="cueTimeEditing = null">
                        完成
                      </button>
                    </template>
                    <span class="tiny dim-2">{{ cueDuration(cue).toFixed(1) }} 秒</span>
                  </div>
                  <span v-if="cue.shot_no != null" class="badge mono">第 {{ cue.shot_no }} 镜</span>
                  <button
                    type="button"
                    class="btn btn-sm cue-save"
                    :disabled="!cueDraft[cue.id] || cueBusy === cue.id"
                    @click="saveCue(cue)"
                  >
                    {{ cueBusy === cue.id ? '…' : '保存' }}
                  </button>
                </div>
                <input
                  :value="cueOf(cue).text"
                  class="input cue-text"
                  :aria-label="`第 ${i + 1} 条字幕文本`"
                  @focus="ensureCueDraft(cue)"
                  @input="ensureCueDraft(cue); cueOf(cue).text = ($event.target as HTMLInputElement).value"
                />
              </li>
            </ul>
            <span class="hint">
              时间按秒显示，点时间可微调到 0.1 秒。改完可重新导出 SRT/VTT；已合成的成片需重新合成才会生效。
            </span>
          </template>
        </div>
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <h2 class="title-md">音频任务</h2>
        <span class="mono tiny dim">{{ jobs.length }} 个</span>
      </div>
      <div class="card-body col">
        <EmptyState v-if="!jobs.length" compact icon="⚙" title="暂无任务" description="提交配音或字幕生成后会出现在这里。" />
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

    <ModalDialog :open="showVoiceForm" title="新增音色" :width="520" @close="showVoiceForm = false">
      <div class="field">
        <label class="label" for="v-name">音色名称<span class="req">*</span></label>
        <input id="v-name" v-model="voiceForm.name" class="input" placeholder="例如：林晚-清冷女声" />
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="v-provider">供应商</label>
          <input id="v-provider" v-model="voiceForm.provider" class="input mono" />
        </div>
        <div class="field">
          <label class="label" for="v-lang">语言</label>
          <input id="v-lang" v-model="voiceForm.language" class="input mono" />
        </div>
      </div>
      <div class="field">
        <label class="label" for="v-gender">性别 / 音色描述</label>
        <input id="v-gender" v-model="voiceForm.gender" class="input" />
      </div>
      <div class="field">
        <label class="label" for="v-file">音色样本（可选，上传后作为参考音频）</label>
        <input
          id="v-file"
          type="file"
          accept="audio/*"
          class="input"
          @change="voiceFile = ($event.target as HTMLInputElement).files?.[0] ?? null"
        />
        <span class="hint">上传会走 /media/upload（kind=audio，rights_declared=true）。</span>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showVoiceForm = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitVoice">
          {{ busy ? '提交中…' : '保存音色' }}
        </button>
      </template>
    </ModalDialog>

    <ModalDialog :open="showTrackForm" title="挂载音轨" :width="560" @close="showTrackForm = false">
      <!-- FR-052：直接在本页上传音频，不必先去素材库 -->
      <div class="field">
        <label class="label" for="t-file">上传音频（可选）</label>
        <div class="row-wrap">
          <input
            id="t-file"
            class="input grow"
            type="file"
            accept="audio/*"
            :disabled="bgmUploading"
            @change="bgmFile = ($event.target as HTMLInputElement).files?.[0] ?? null"
          />
          <button
            type="button"
            class="btn btn-sm"
            :disabled="!bgmFile || bgmUploading"
            @click="uploadBgm"
          >
            {{ bgmUploading ? '上传中…' : '上传并选用' }}
          </button>
        </div>
        <span class="hint">
          走 /media/upload（kind=audio，自动声明权利）。上传成功后会自动填到下面的素材选择里。
        </span>
      </div>
      <div class="field">
        <label class="label" for="t-media">音频素材<span class="req">*</span></label>
        <select id="t-media" v-model="trackForm.media_id" class="select">
          <option value="">选择剧集音频素材…</option>
          <option v-for="media in audioMedia" :key="media.id" :value="media.id">
            {{ media.id }}（{{ formatDuration(media.duration_ms) }}）
          </option>
        </select>
        <span class="hint">本剧集上传的音频素材会列在这里。</span>
      </div>
      <!-- FR-052：挂载前先试听，避免配错曲子 -->
      <div v-if="trackForm.media_id" class="field">
        <label class="label">试听</label>
        <audio :src="mediaApi.contentUrl(trackForm.media_id)" controls />
      </div>
      <div class="field">
        <label class="label" for="t-kind">类型</label>
        <select id="t-kind" v-model="trackForm.kind" class="select">
          <option v-for="(label, key) in TRACK_KIND_LABEL" :key="key" :value="key">{{ label }}</option>
        </select>
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="t-start">开始（毫秒）</label>
          <input id="t-start" v-model.number="trackForm.start_ms" class="input mono" type="number" min="0" />
        </div>
        <div class="field">
          <label class="label" for="t-end">结束（毫秒）</label>
          <input id="t-end" v-model.number="trackForm.end_ms" class="input mono" type="number" min="0" />
        </div>
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="t-vol">音量（dB）</label>
          <input id="t-vol" v-model.number="trackForm.volume_db" class="input mono" type="number" min="-60" max="12" />
        </div>
        <div class="field">
          <label class="label" for="t-fadein">淡入（毫秒）</label>
          <input id="t-fadein" v-model.number="trackForm.fade_in_ms" class="input mono" type="number" min="0" />
        </div>
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="t-fadeout">淡出（毫秒）</label>
          <input id="t-fadeout" v-model.number="trackForm.fade_out_ms" class="input mono" type="number" min="0" />
        </div>
        <div class="field" style="justify-content: flex-end">
          <label class="checkbox">
            <input v-model="trackForm.rights_declared" type="checkbox" />
            <span class="small">我已声明该素材权利</span>
          </label>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showTrackForm = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitTrack">
          {{ busy ? '提交中…' : '挂载音轨' }}
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}
.voice-list,
.bind-list,
.bgm-scope {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  padding-bottom: var(--space-2);
}
.bgm-scope-select {
  /* 起止两个下拉并排放得下，「到」字不会被挤到下一行 */
  width: auto;
  flex: 1 1 150px;
  min-width: 132px;
  max-width: 232px;
}
.bgm-gen {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 168px;
  gap: var(--gap);
  align-items: start;
  padding-bottom: var(--gap);
  border-bottom: 1px dashed var(--line);
}
.bgm-prompt {
  min-height: 64px;
}
.bgm-side {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.track-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-2);
}
.track-prompt {
  margin-top: 2px;
  max-width: 100%;
}
@media (max-width: 760px) {
  .bgm-gen {
    grid-template-columns: minmax(0, 1fr);
  }
}
.track-list,
.cue-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 340px;
  overflow: auto;
}
.voice-item,
.bind-item,
.track-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  flex-wrap: wrap;
}
.voice-main {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.voice-preview {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.voice-preview audio {
  width: 100%;
  height: 32px;
}
.bind-select {
  width: 190px;
}
.bind-controls {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.speed-field {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.speed-field.disabled {
  opacity: 0.5;
}
.speed-input {
  width: 78px;
  text-align: right;
}
/* 「旧版本」只做提示，别和状态徽章抢注意力 */
.voice-legacy {
  margin-left: var(--space-1);
  opacity: 0.7;
  font-weight: 400;
}
.cue-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-xs);
  background: rgba(243, 237, 227, 0.025);
}
/* 字幕卡只有半栏宽，时间+文本挤一行会把文本压没：分成「信息行 + 文本行」 */
.cue-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.cue-item .mono {
  flex: 0 0 auto;
}
.over {
  color: var(--danger);
}

/* FR-053 字幕可编辑 */
.cue-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border: 1px solid var(--line-accent);
  border-radius: var(--radius-sm);
  background: var(--accent-soft);
}
.cue-idx {
  flex: 0 0 auto;
  width: 26px;
  text-align: right;
}
.cue-times {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 0 0 auto;
}
.cue-ms {
  width: 76px;
  height: 26px;
  padding: 0 6px;
  font-size: var(--fs-12);
}
/* 只读时间：等宽数字 + 可点击微调，替代原来裸露的毫秒输入框 */
.cue-time-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 8px;
  border: 1px solid var(--line);
  border-radius: var(--radius-xs);
  background: var(--panel-2);
  color: var(--text-1);
  font-family: var(--font-mono, monospace);
  font-size: var(--fs-12);
  font-variant-numeric: tabular-nums;
  line-height: 1.4;
  white-space: nowrap;
  cursor: pointer;
  appearance: none;
  transition: border-color 0.15s ease, color 0.15s ease;
}
.cue-time-pill:hover {
  border-color: var(--line-strong);
  color: var(--text-0);
}
.cue-sec {
  width: 68px;
  height: 26px;
  padding: 0 6px;
  font-size: var(--fs-12);
}
.cue-text {
  width: 100%;
  height: 28px;
  padding: 0 7px;
  font-size: var(--fs-12);
}
.cue-save {
  margin-left: auto;
}

/* FR-050：配音前置条件阻断提示 */
.voice-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  align-items: flex-start;
  padding: var(--space-3) var(--space-3);
  border: 1px solid var(--warn, #b45309);
  border-radius: 8px;
  background: color-mix(in srgb, var(--warn, #b45309) 8%, transparent);
}
.voice-block-head {
  font-size: var(--fs-13);
}
.voice-block-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  font-size: var(--fs-12);
}
.voice-block-list li {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

/* §12-4：TTS 失败降级入口 */
.degrade-block {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-3);
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
}
.degrade-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  flex-wrap: wrap;
  font-size: var(--fs-13);
}
.hidden-file {
  display: none;
}
.btn.disabled {
  opacity: 0.55;
  pointer-events: none;
}
</style>
