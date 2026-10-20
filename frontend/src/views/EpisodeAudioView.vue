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
import { jobsApi } from '@/api/jobs'
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
import { formatDuration, formatSeconds } from '@/utils/format'
import GateCard from '@/components/GateCard.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ErrorState from '@/components/ErrorState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import { ApiError, toApiError } from '@/api/errors'
import type {
  AudioTrack,
  Character,
  Job,
  MediaAsset,
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
    const [v, c, t, tr, cu, media] = await Promise.all([
      projectId.value ? fetchVoices(projectId.value).catch(() => []) : Promise.resolve([]),
      projectId.value ? fetchCharacters(projectId.value).catch(() => []) : Promise.resolve([]),
      fetchTtsLines(episodeId.value).catch(() => []),
      fetchTracks(episodeId.value).catch(() => []),
      fetchSubtitles(episodeId.value).catch(() => []),
      mediaApi.episodeMedia(episodeId.value, 'audio').then((r) => (Array.isArray(r) ? r : r.items)).catch(() => []),
    ])
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
function lineIndexInShot(line: TtsLineStatus): number {
  const same = ttsLineStatus.value.filter((l) => l.shot_no === line.shot_no)
  return Math.max(0, same.findIndex((l) => l.text === line.text))
}

async function skipLines(shotNos: number[], reason = ''): Promise<void> {
  if (!episodeId.value || !shotNos.length) return
  degradeBusy.value = true
  try {
    await audioApi.ttsSkip(episodeId.value, { shot_nos: shotNos, reason })
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
    const res = await jobsApi.cancel(id)
    if (res.job) store.trackJob(res.job)
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

    <div class="two-col">
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
                  <span v-if="voice.gender" class="badge">{{ voice.gender }}</span>
                  <span v-if="voice.language" class="badge mono">{{ voice.language }}</span>
                </div>
                <span class="mono tiny dim">{{ voice.provider || '—' }} · {{ voice.id }}</span>
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
                <audio :src="audioApi.voicePreviewUrl(voice.id)" controls />
                <span class="mono tiny dim-2">{{ audioApi.voicePreviewUrl(voice.id) }}</span>
              </div>
            </li>
          </ul>
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2 class="title-md">角色音色绑定</h2>
          <span class="mono tiny dim">{{ characters.length }} 个角色</span>
        </div>
        <div class="card-body">
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
                    {{ voiceById[character.voice_id]?.name ?? character.voice_id }}
                  </span>
                  <span v-else class="badge badge-warn">未绑定</span>
                </div>
              </div>
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
            </li>
          </ul>
        </div>
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">台词配音（TTS）</h2>
          <span class="mono tiny dim">{{ ttsLines.length }} 条</span>
          <span
            v-if="ttsEstimate && ttsEstimate.line_count"
            class="mono tiny dim"
            title="本次将合成的台词条数与预估成本"
          >
            待合成 {{ ttsEstimate.line_count }} 句 · 预估 {{ formatCents(ttsEstimate.estimate_cents) }}
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
      <div class="card-body col" style="gap: 14px">
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
                      @click="skipLines([line.shot_no as number], '用户选择跳过该句配音')"
                    >
                      跳过
                    </button>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else-if="staleLines.length" class="hint">
          有 {{ staleLines.length }} 句台词的音频已过期（角色音色或语速变过），建议重新生成。
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
              <th style="width: 80px">状态</th>
              <th style="width: 110px">试听</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(line, i) in ttsLines" :key="line.media_id ?? i">
              <td class="mono">{{ line.shot_no != null ? `#${line.shot_no}` : '—' }}</td>
              <td class="small">{{ line.character ?? '—' }}</td>
              <td class="tiny muted">{{ line.text ?? '—' }}</td>
              <td class="tiny dim mono">{{ line.voice_id ?? '—' }}</td>
              <td>
                <span
                  class="badge"
                  :class="line.stale ? 'badge-warn' : line.source === 'skipped' ? 'badge' : 'badge-ok'"
                >
                  {{
                    line.stale
                      ? '已过期'
                      : line.source === 'skipped'
                        ? '已跳过'
                        : line.source === 'manual'
                          ? '用户上传'
                          : (line.status ?? '就绪')
                  }}
                </span>
              </td>
              <td>
                <audio v-if="line.media_id" :src="mediaApi.contentUrl(line.media_id)" controls preload="none" />
                <span v-else class="tiny dim-2">无音频</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">BGM / 音效</h2>
          <button type="button" class="btn btn-sm btn-primary" @click="openTrackForm">挂载音轨</button>
        </div>
        <div class="card-body">
          <EmptyState
            v-if="!tracks.length"
            compact
            icon="♬"
            title="还没有音轨"
            description="BGM / 环境音 / 音效会按时间区间混入成片。"
          />
          <ul v-else class="track-list">
            <li v-for="(t, i) in tracks" :key="t.id ?? i" class="track-item">
              <div class="grow">
                <div class="row-wrap">
                  <span class="badge badge-accent">{{ TRACK_KIND_LABEL[t.kind] ?? t.kind }}</span>
                  <span class="mono tiny dim">
                    {{ formatSeconds((t.start_ms ?? 0) / 1000) }} ~ {{ formatSeconds((t.end_ms ?? 0) / 1000) }}
                  </span>
                  <span class="mono tiny dim">{{ t.volume_db }}dB</span>
                  <span v-if="t.rights_declared" class="badge badge-ok">权利已声明</span>
                  <span v-else class="badge badge-warn">权利未声明</span>
                </div>
                <span class="mono tiny dim-2">{{ t.media_id }}</span>
              </div>
              <button type="button" class="btn btn-sm btn-danger" :disabled="busy" @click="removeTrack(t)">
                移除
              </button>
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
        <div class="card-body col" style="gap: 10px">
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
              <span class="row" style="gap: 6px">
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
                <span class="mono tiny dim cue-idx">{{ cue.index ?? i + 1 }}</span>
                <div class="cue-times">
                  <input
                    :value="cueOf(cue).start_ms"
                    class="input mono cue-ms"
                    type="number"
                    step="100"
                    min="0"
                    :aria-label="`第 ${i + 1} 条字幕开始时间`"
                    @focus="ensureCueDraft(cue)"
                    @input="ensureCueDraft(cue); cueOf(cue).start_ms = Number(($event.target as HTMLInputElement).value)"
                  />
                  <span class="dim-2 tiny">→</span>
                  <input
                    :value="cueOf(cue).end_ms"
                    class="input mono cue-ms"
                    type="number"
                    step="100"
                    min="0"
                    :aria-label="`第 ${i + 1} 条字幕结束时间`"
                    @focus="ensureCueDraft(cue)"
                    @input="ensureCueDraft(cue); cueOf(cue).end_ms = Number(($event.target as HTMLInputElement).value)"
                  />
                </div>
                <input
                  :value="cueOf(cue).text"
                  class="input cue-text"
                  :aria-label="`第 ${i + 1} 条字幕文本`"
                  @focus="ensureCueDraft(cue)"
                  @input="ensureCueDraft(cue); cueOf(cue).text = ($event.target as HTMLInputElement).value"
                />
                <span v-if="cue.shot_no != null" class="badge mono">#{{ cue.shot_no }}</span>
                <button
                  type="button"
                  class="btn btn-sm"
                  :disabled="!cueDraft[cue.id] || cueBusy === cue.id"
                  @click="saveCue(cue)"
                >
                  {{ cueBusy === cue.id ? '…' : '保存' }}
                </button>
              </li>
            </ul>
            <span class="hint">
              时间单位为毫秒（与后端一致）。改完可重新导出 SRT/VTT；已合成的成片需重新合成才会生效。
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
        <span class="hint">素材来自 GET /episodes/{id}/media?kind=audio。</span>
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
.stage {
  display: flex;
  flex-direction: column;
  gap: var(--gap);
}
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--gap);
  align-items: start;
}
@media (max-width: 1120px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.voice-list,
.bind-list,
.track-list,
.cue-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 340px;
  overflow: auto;
}
.voice-item,
.bind-item,
.track-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
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
  gap: 2px;
}
.voice-preview {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.voice-preview audio {
  width: 100%;
  height: 32px;
}
.bind-select {
  width: 190px;
}
.cue-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 9px;
  border-radius: var(--radius-xs);
  background: rgba(255, 255, 255, 0.025);
}
.cue-item .mono {
  flex: 0 0 auto;
  width: 132px;
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
  gap: 8px;
  padding: 7px 10px;
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
  gap: 5px;
  flex: 0 0 auto;
}
.cue-ms {
  width: 76px;
  height: 26px;
  padding: 0 6px;
  font-size: 12px;
}
.cue-text {
  flex: 1;
  height: 26px;
  padding: 0 7px;
  font-size: 12px;
}

/* FR-050：配音前置条件阻断提示 */
.voice-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  padding: 10px 12px;
  border: 1px solid var(--warn, #b45309);
  border-radius: 8px;
  background: color-mix(in srgb, var(--warn, #b45309) 8%, transparent);
}
.voice-block-head {
  font-size: 13px;
}
.voice-block-list {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}
.voice-block-list li {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

/* §12-4：TTS 失败降级入口 */
.degrade-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  border: 1px dashed var(--line-strong);
  border-radius: 8px;
}
.degrade-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 13px;
}
.hidden-file {
  display: none;
}
.btn.disabled {
  opacity: 0.55;
  pointer-events: none;
}
</style>
