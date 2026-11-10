<script setup lang="ts">
/**
 * 角色与画风确认（API.md §5、§9）。
 * 角色卡 CRUD → 生成参考图（异步 job）→ 确认/替换主参考图 → 门控确认。
 */
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { reportError } from '@/api/client'
import { cancelJob as cancelJobRequest, jobsApi, fetchJobs } from '@/api/jobs'
import { audioApi, fetchVoices } from '@/api/audio'
import {
  ASSET_KINDS,
  ASSET_KIND_LABEL,
  charactersApi,
  fetchCharacterAssets,
  fetchCharacterList,
  fetchStyles,
  stylesApi,
} from '@/api/characters'
import { relativeTime } from '@/utils/format'
import { ApiError, toApiError } from '@/api/errors'
import GateCard from '@/components/GateCard.vue'
import StageHead from '@/components/StageHead.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ErrorState from '@/components/ErrorState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import MediaThumb from '@/components/MediaThumb.vue'
import type {
  Character,
  CharacterAsset,
  CharacterAssetKind,
  CharacterInput,
  Job,
  StyleAsset,
  Voice,
} from '@/types'

/** 角色表单的本地形态（CharacterInput 带索引签名，不适合直接做表单模型）。 */
type CharacterForm = {
  name: string
  identity: string
  age_range: string
  appearance: string
  outfit: string
  personality: string
  speech_style: string
  forbidden_changes: string[]
}

const store = useEpisodeStore()
const config = useConfigStore()
const toast = useToastStore()
const { episode, workflow, pendingGate, jobSnapshots } = storeToRefs(store)

const characters = ref<Character[]>([])
const styles = ref<StyleAsset[]>([])
const loading = ref(false)
const error = ref<ApiError | null>(null)
/** 被「只看本集」挡掉的其他剧集角色数。 */
const hiddenCount = ref(0)
/** 是否连其他剧集的角色一起看。 */
const showAllCharacters = ref(false)

const activeCharacter = ref<Character | null>(null)
const assets = ref<CharacterAsset[]>([])
const assetsLoading = ref(false)

const showCharacterForm = ref(false)
const editingId = ref<string | null>(null)
const showGenerate = ref(false)
const showReplace = ref(false)
const showStyleForm = ref(false)

const busy = ref(false)
const gateBusy = ref(false)
/** 「从剧本同步」进行中（剧本抓取是同步接口，但可能建几十个角色，仍要给反馈）。 */
const syncBusy = ref(false)
const notice = ref('')
const jobIds = ref<string[]>([])
const affectedShots = ref<number[]>([])

const characterForm = reactive<CharacterForm>({
  name: '',
  identity: '',
  age_range: '',
  appearance: '',
  outfit: '',
  personality: '',
  speech_style: '',
  forbidden_changes: [],
})

const forbiddenText = ref('')

const generateForm = reactive({
  asset_kinds: ['front'] as CharacterAssetKind[],
  count_per_kind: 1,
})

const replaceForm = reactive({
  media_id: '',
  impact: 'future_only' as 'future_only' | 'mark_stale',
})

const styleForm = reactive({
  name: '',
  description: '',
  palette: '',
  camera_style: '',
  lighting_style: '',
  negative_prompt: '',
})

const projectId = computed(() => episode.value?.project_id ?? '')

const jobs = computed<Job[]>(() =>
  jobIds.value.map((id) => jobSnapshots.value[id]).filter((j): j is Job => Boolean(j)),
)

const activeAssets = computed(() =>
  [...assets.value].sort((a, b) => (b.version ?? 0) - (a.version ?? 0)),
)

async function loadCharacters(): Promise<void> {
  if (!projectId.value) return
  loading.value = true
  error.value = null
  try {
    // 角色卡是项目级资产库，但同项目下可能有多集互不相干的故事：
    // 默认只显示本集剧本里出现的角色（+ 手工新增的公共角色）。
    const page = await fetchCharacterList(projectId.value, {
      episodeId: store.episodeId ?? undefined,
      includeAll: showAllCharacters.value,
    })
    characters.value = page.items
    hiddenCount.value = page.hidden
  } catch (err) {
    error.value = toApiError(err)
  } finally {
    loading.value = false
  }
}

/** 切换「只看本集 / 看全部角色」。 */
async function toggleAllCharacters(): Promise<void> {
  showAllCharacters.value = !showAllCharacters.value
  await loadCharacters()
}

async function loadStyles(): Promise<void> {
  if (!projectId.value) return
  styles.value = await fetchStyles(projectId.value).catch(() => [])
}

/* ---------------- 剧本 → 角色卡（生成剧本时自动抓，这里可手动重抓） ---------------- */

/**
 * 从本集最新剧本重新抓取角色卡。
 * 默认不覆盖手改过的字段（后端按 `_user_edited` 判定），`force` 才会整体覆盖。
 */
async function syncFromScript(force = false): Promise<void> {
  const id = store.episodeId
  if (!id || syncBusy.value) return
  syncBusy.value = true
  try {
    const result = await charactersApi.syncFromEpisode(id, force)
    // 重新按「本集」口径拉一遍，hidden 计数也要跟着更新
    await loadCharacters()
    if (result.created + result.updated > 0) {
      toast.success(
        '已从剧本抓取角色',
        `新增 ${result.created} 个 · 更新 ${result.updated} 个 · 共 ${result.total} 个`,
      )
    } else {
      toast.info(
        '角色卡已是最新',
        `剧本 v${result.script_version ?? '—'} 的 ${result.total} 个角色都在，未做改动`,
      )
    }
  } catch (err) {
    const apiErr = toApiError(err)
    if (apiErr.status === 404) {
      // 还没生成剧本是正常前置缺失，不要报成「加载失败」
      toast.warn('还没有剧本', '请先在「故事与剧本」生成剧本，再抓取角色')
    } else {
      toast.error('抓取角色失败', apiErr.message)
    }
  } finally {
    syncBusy.value = false
  }
}

/* ---------------- FR-050 音色绑定 / FR-051 语速 ---------------- */

const voices = ref<Voice[]>([])
/** 每个角色的音色草稿（未保存前只改本地），key 为角色 id。 */
const voiceDraft = reactive<Record<string, { voice_id: string; speed: number }>>({})
const voiceBusy = ref<string | null>(null)
/** 正在试听的音色 id（试听要走一次供应商合成，可能慢，需要反馈）。 */
const previewingId = ref<string | null>(null)
const previewUrl = ref<string | null>(null)
const previewAudio = ref<HTMLAudioElement | null>(null)

async function loadVoices(): Promise<void> {
  if (!projectId.value) return
  voices.value = await fetchVoices(projectId.value).catch(() => [])
}

/** 取角色的音色草稿，没有就按当前绑定值初始化。 */
function voiceOf(character: Character): { voice_id: string; speed: number } {
  const existing = voiceDraft[character.id]
  if (existing) return existing
  const created = {
    voice_id: character.voice_id ?? '',
    speed: character.voice_speed ?? 1.0,
  }
  voiceDraft[character.id] = created
  return created
}

function voiceName(voiceId?: string | null): string {
  if (!voiceId) return '未绑定'
  return voices.value.find((v) => v.id === voiceId)?.name ?? voiceId
}

/** 草稿与已保存值是否不同（用来禁用保存按钮）。 */
function voiceDirty(character: Character): boolean {
  const draft = voiceOf(character)
  return (
    draft.voice_id !== (character.voice_id ?? '') ||
    Number(draft.speed) !== Number(character.voice_speed ?? 1.0)
  )
}

async function bindVoiceFor(character: Character): Promise<void> {
  const draft = voiceOf(character)
  if (!draft.voice_id) {
    toast.warn('请先选择一个音色')
    return
  }
  const speed = Number(draft.speed)
  if (!Number.isFinite(speed) || speed < 0.5 || speed > 2.0) {
    toast.warn('语速需在 0.5 ~ 2.0 之间')
    return
  }
  voiceBusy.value = character.id
  try {
    const updated = await charactersApi.bindVoice(character.id, draft.voice_id, speed)
    // 就地替换，避免整页重载
    const idx = characters.value.findIndex((c) => c.id === character.id)
    if (idx >= 0) characters.value[idx] = { ...characters.value[idx], ...updated }
    delete voiceDraft[character.id]
    toast.success(`已为「${character.name}」绑定音色`)
  } catch (err) {
    reportError(err)
  } finally {
    voiceBusy.value = null
  }
}

async function previewVoice(voiceId: string): Promise<void> {
  if (!voiceId) {
    toast.warn('请先选择一个音色')
    return
  }
  previewingId.value = voiceId
  try {
    const res = await audioApi.previewVoice(voiceId)
    previewUrl.value = res.url
    await nextTick()
    await previewAudio.value?.play().catch(() => undefined)
  } catch (err) {
    reportError(err)
  } finally {
    previewingId.value = null
  }
}

async function loadRecentJobs(): Promise<void> {
  if (!projectId.value) return
  try {
    // 角色生成任务是项目级的（不带 episode_id），刷新页面后也要能看到历史
    const page = await fetchJobs({
      project_id: projectId.value,
      type: 'CHARACTER_ASSET_GENERATION',
      page_size: 10,
    })
    if (page.items.length) jobIds.value = page.items.map((job) => job.id)
  } catch {
    /* 历史任务拉不到不影响主流程 */
  }
}

onMounted(async () => {
  await config.load()
  await Promise.all([loadCharacters(), loadStyles(), loadVoices(), loadRecentJobs()])
})

watch(projectId, async (next) => {
  if (!next) return
  await Promise.all([loadCharacters(), loadStyles(), loadVoices(), loadRecentJobs()])
})

/**
 * 参考图生成成功的瞬间刷新角色卡与素材。
 *
 * 之前任务跑到 100% 后卡片仍是「暂无素材」，必须整页刷新；而刷新后任务面板也空了，
 * 用户既看不到图、也找不到任务 —— 生成的东西像是「凭空消失」。
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
    const keepId = activeCharacter.value?.id ?? null
    await loadCharacters()
    if (!keepId) return
    const refreshed = characters.value.find((c) => c.id === keepId)
    if (refreshed) await openCharacter(refreshed)
  },
)

async function openCharacter(character: Character): Promise<void> {
  activeCharacter.value = character
  assetsLoading.value = true
  try {
    assets.value = await fetchCharacterAssets(character.id)
  } catch {
    assets.value = []
  } finally {
    assetsLoading.value = false
  }
}

function openCreateCharacter(): void {
  editingId.value = null
  Object.assign(characterForm, {
    name: '',
    identity: '',
    age_range: '',
    appearance: '',
    outfit: '',
    personality: '',
    speech_style: '',
    forbidden_changes: [],
  })
  forbiddenText.value = ''
  showCharacterForm.value = true
}

function openEditCharacter(character: Character): void {
  editingId.value = character.id
  Object.assign(characterForm, {
    name: character.name,
    identity: character.identity ?? '',
    age_range: character.age_range ?? '',
    appearance: character.appearance ?? '',
    outfit: character.outfit ?? '',
    personality: character.personality ?? '',
    speech_style: character.speech_style ?? '',
    forbidden_changes: character.forbidden_changes ?? [],
  })
  forbiddenText.value = (character.forbidden_changes ?? []).join('、')
  showCharacterForm.value = true
}

async function submitCharacter(): Promise<void> {
  if (!projectId.value) return
  if (!characterForm.name.trim()) {
    toast.warn('角色名不能为空', '角色名在项目内唯一')
    return
  }
  busy.value = true
  const payload: CharacterInput = {
    name: characterForm.name.trim(),
    identity: characterForm.identity,
    age_range: characterForm.age_range,
    appearance: characterForm.appearance,
    outfit: characterForm.outfit,
    personality: characterForm.personality,
    speech_style: characterForm.speech_style,
    forbidden_changes: forbiddenText.value
      .split(/[、,，\n]/)
      .map((s) => s.trim())
      .filter(Boolean),
    // 在某一集里新建的角色只属于这一集，避免同名角色串到其他剧集
    episode_id: store.episodeId ?? null,
  }
  try {
    if (editingId.value) {
      const updated = await charactersApi.patch(editingId.value, payload)
      characters.value = characters.value.map((c) => (c.id === updated.id ? updated : c))
      toast.success('角色已更新', updated.name)
    } else {
      const created = await charactersApi.create(projectId.value, payload)
      characters.value = [...characters.value, created]
      toast.success('角色已创建', created.name)
    }
    showCharacterForm.value = false
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

function openGenerate(): void {
  generateForm.asset_kinds = ['front']
  generateForm.count_per_kind = 1
  showGenerate.value = true
}

async function submitGenerate(): Promise<void> {
  if (!activeCharacter.value) return
  if (!generateForm.asset_kinds.length) {
    toast.warn('请至少选择一种资产类型')
    return
  }
  busy.value = true
  try {
    const job = await charactersApi.generate(activeCharacter.value.id, {
      asset_kinds: [...generateForm.asset_kinds],
      count_per_kind: generateForm.count_per_kind,
    })
    jobIds.value = [job.id, ...jobIds.value]
    store.trackJob(job)
    showGenerate.value = false
    toast.info('参考图生成任务已提交', job.id)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function confirmPrimary(mediaId: string): Promise<void> {
  if (!activeCharacter.value) return
  busy.value = true
  try {
    const updated = await charactersApi.confirm(activeCharacter.value.id, mediaId)
    characters.value = characters.value.map((c) => (c.id === updated.id ? updated : c))
    activeCharacter.value = updated
    toast.success('主参考图已确认')
    await store.refreshLive()
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

function openReplace(mediaId: string): void {
  replaceForm.media_id = mediaId
  replaceForm.impact = 'future_only'
  affectedShots.value = []
  showReplace.value = true
}

async function submitReplace(): Promise<void> {
  if (!activeCharacter.value || !replaceForm.media_id) return
  busy.value = true
  try {
    const result = await charactersApi.replacePrimary(
      activeCharacter.value.id,
      replaceForm.media_id,
      replaceForm.impact,
    )
    affectedShots.value = result.affected_shot_nos ?? (result.affected_shots ?? []).map((s) => s.shot_no)
    toast.success('主参考图已替换', `影响 ${affectedShots.value.length} 个分镜`)
    await store.refreshLive()
    await store.loadShots()
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function archiveCharacter(character: Character): Promise<void> {
  busy.value = true
  try {
    await charactersApi.archive(character.id)
    characters.value = characters.value.filter((c) => c.id !== character.id)
    if (activeCharacter.value?.id === character.id) activeCharacter.value = null
    toast.success('角色已归档', character.name)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function submitStyle(): Promise<void> {
  if (!projectId.value) return
  if (!styleForm.name.trim()) {
    toast.warn('画风名称不能为空')
    return
  }
  busy.value = true
  try {
    const created = await stylesApi.create(projectId.value, {
      name: styleForm.name.trim(),
      description: styleForm.description.trim(),
      palette: styleForm.palette
        .split(/[、,，\s]+/)
        .map((s) => s.trim())
        .filter(Boolean),
      camera_style: styleForm.camera_style.trim(),
      lighting_style: styleForm.lighting_style.trim(),
      negative_prompt: styleForm.negative_prompt.trim(),
    })
    styles.value = [...styles.value, created]
    showStyleForm.value = false
    toast.success('画风已创建', created.name)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function applyStyle(style: StyleAsset): Promise<void> {
  busy.value = true
  try {
    await stylesApi.apply(style.id)
    styles.value = styles.value.map((s) => ({ ...s, is_default: s.id === style.id }))
    toast.success('画风已应用到项目', style.name ?? style.id)
    await store.refreshLive()
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
    toast.success('角色与画风已确认')
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

/** 从当前阶段启动工作流（已有剧本会被复用，不会重新生成）。 */
async function startWorkflow(): Promise<void> {
  if (gateBusy.value) return
  gateBusy.value = true
  try {
    const run = await store.startWorkflow()
    if (run?.pending_gate === 'gate_script_confirmation') {
      toast.info('工作流已启动', '先到「故事与剧本」确认剧本，再回到这里确认角色')
    } else {
      toast.success('工作流已启动', run?.pending_gate ? '已停在待确认门控' : '')
    }
  } catch (err) {
    const apiErr = toApiError(err)
    toast.error('启动工作流失败', apiErr.message)
  } finally {
    gateBusy.value = false
  }
}

/** 音色性别说人话，别把 female / male 直接拼进下拉项。 */
const GENDER_LABELS: Record<string, string> = { female: '女声', male: '男声', neutral: '中性' }
function genderLabel(gender: string): string {
  return GENDER_LABELS[gender.toLowerCase()] ?? ''
}
</script>

<template>
  <div class="stage">
    <StageHead
      index="02"
      title="角色与画风"
      description="角色参考图与画风决定整部剧的画面一致性；先定角色卡，再批量出图。"
    />
    <!-- 工作流没启动时，页面上的门控按钮全是禁用的；这里给出可执行的下一步，
         而不是让用户对着灰色的「确认角色与画风」猜原因。 -->
    <div v-if="!workflow" class="card notice-banner">
      <div>
        <strong>工作流还没启动</strong>
        <p class="tiny dim">
          启动后会接着当前剧本往下走（<b>不会重新生成剧本、不再扣一次模型费</b>），
          并停在「剧本确认」门控；确认剧本后才进入角色与画风。
        </p>
      </div>
      <button type="button" class="btn btn-primary btn-sm" :disabled="gateBusy" @click="startWorkflow">
        {{ gateBusy ? '启动中…' : '启动工作流' }}
      </button>
    </div>
    <GateCard
      :gate="pendingGate"
      :workflow="workflow"
      :busy="gateBusy"
      :notice="notice"
      approve-text="确认角色与画风，进入分镜图"
      description="确认角色主参考图与项目画风后，工作流会继续到分镜画面阶段。"
      @approve="approve"
      @reject="reject"
      @edit="edit"
      @refresh="store.refreshLive()"
    >
      <div class="row-wrap tiny dim">
        <span>角色 <span class="mono">{{ characters.length }}</span></span>
        <span>已确认主图 <span class="mono">{{ characters.filter((c) => c.primary_media_id).length }}</span></span>
        <span>画风 <span class="mono">{{ styles.length }}</span></span>
      </div>
    </GateCard>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">角色卡</h2>
          <div class="row">
            <button
              type="button"
              class="btn btn-sm btn-ghost"
              :disabled="syncBusy"
              title="从本集最新剧本的人物表抓取角色；不会覆盖你手改过的字段"
              @click="syncFromScript()"
            >
              {{ syncBusy ? '抓取中…' : '从剧本同步' }}
            </button>
            <button type="button" class="btn btn-sm btn-ghost" @click="loadCharacters">刷新</button>
            <button type="button" class="btn btn-sm btn-primary" @click="openCreateCharacter">
              新增角色
            </button>
          </div>
        </div>
        <div class="card-body">
          <p v-if="hiddenCount || showAllCharacters" class="tiny dim char-scope">
            <span v-if="showAllCharacters">正在显示项目内全部角色（含其他剧集的故事）。</span>
            <span v-else>已隐藏 {{ hiddenCount }} 个「其他剧集」的角色。</span>
            <button type="button" class="btn btn-sm btn-ghost" @click="toggleAllCharacters">
              {{ showAllCharacters ? '只看本集' : '显示全部' }}
            </button>
          </p>
          <ErrorState v-if="error" :error="error" context="加载角色" compact @retry="loadCharacters" />
          <SkeletonBlock v-else-if="loading && !characters.length" variant="cards" :rows="3" />
          <EmptyState
            v-else-if="!characters.length"
            icon="☺"
            title="还没有角色"
            description="生成剧本后会从人物表自动抓取角色；也可以手动新增。"
          >
            <button
              type="button"
              class="btn btn-primary btn-sm"
              :disabled="syncBusy"
              @click="syncFromScript()"
            >
              从剧本同步
            </button>
            <button type="button" class="btn btn-ghost btn-sm" @click="openCreateCharacter">
              新增角色
            </button>
          </EmptyState>
          <div v-else class="char-list">
            <article
              v-for="character in characters"
              :key="character.id"
              class="char-card"
              :class="{ active: activeCharacter?.id === character.id }"
              @click="openCharacter(character)"
            >
              <div class="char-thumb">
                <MediaThumb :media-id="character.primary_media_id" portrait />
                <span v-if="character.primary_media_id" class="char-ok">主图已确认</span>
              </div>
              <div class="char-body">
                <div class="row-between">
                  <strong class="ellipsis">{{ character.name }}</strong>
                  <span class="tiny dim mono">{{ character.age_range || '—' }}</span>
                </div>
                <p class="tiny dim ellipsis">{{ character.identity || '未填写身份' }}</p>
                <div class="row-wrap">
                  <span v-if="character.source === 'script'" class="badge badge-info">剧本抓取</span>
                  <span v-else class="badge badge-muted">手动创建</span>
                  <span v-if="character.user_edited" class="badge badge-warn">已手改</span>
                  <span v-if="character.voice_id" class="badge badge-info">
                    音色 {{ voiceName(character.voice_id) }}
                  </span>
                  <span v-else class="badge badge-warn">未绑定音色</span>
                  <span v-if="character.voice_id" class="mono tiny dim">
                    语速 {{ (character.voice_speed ?? 1).toFixed(2) }}×
                  </span>
                </div>
                <!-- FR-050 绑定音色 + FR-051 语速；绑定前可试听 -->
                <div class="voice-picker" @click.stop>
                  <select
                    v-model="voiceOf(character).voice_id"
                    class="voice-select"
                    :aria-label="`为 ${character.name} 选择音色`"
                  >
                    <option value="">（未绑定）</option>
                    <option v-for="voice in voices" :key="voice.id" :value="voice.id">
                      {{ voice.name }}{{ voice.gender ? ` · ${genderLabel(voice.gender)}` : '' }}
                    </option>
                  </select>
                  <label class="voice-speed">
                    <span class="tiny dim">语速</span>
                    <input
                      v-model.number="voiceOf(character).speed"
                      type="number"
                      min="0.5"
                      max="2"
                      step="0.05"
                      class="voice-speed-input"
                    />
                    <span class="tiny dim">×</span>
                  </label>
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    :disabled="!voiceOf(character).voice_id || previewingId === voiceOf(character).voice_id"
                    @click="previewVoice(voiceOf(character).voice_id)"
                  >
                    {{ previewingId === voiceOf(character).voice_id ? '合成中…' : '试听' }}
                  </button>
                  <button
                    type="button"
                    class="btn btn-sm btn-primary"
                    :disabled="voiceBusy === character.id || !voiceDirty(character)"
                    @click="bindVoiceFor(character)"
                  >
                    {{ voiceBusy === character.id ? '绑定中…' : '保存音色' }}
                  </button>
                </div>
                <div class="row-wrap" @click.stop>
                  <button type="button" class="btn btn-sm btn-ghost" @click="openEditCharacter(character)">
                    编辑
                  </button>
                  <button
                    type="button"
                    class="btn btn-sm btn-ghost"
                    :disabled="busy"
                    @click="archiveCharacter(character)"
                  >
                    归档
                  </button>
                </div>
              </div>
            </article>
          </div>
        </div>
      </section>

      <div class="col" style="gap: var(--gap)">
        <section class="card">
          <div class="card-head">
            <h2 class="title-md">画风资产</h2>
            <button type="button" class="btn btn-sm btn-primary" @click="showStyleForm = true">
              新增画风
            </button>
          </div>
          <div class="card-body">
            <EmptyState
              v-if="!styles.length"
              compact
              icon="◐"
              title="还没有画风"
              description="画风决定整体色彩与镜头语言，会作为图像生成的默认风格。"
            />
            <ul v-else class="style-list">
              <li
                v-for="style in styles"
                :key="style.id"
                class="style-item"
                :class="{ default: style.is_default }"
              >
                <div class="row-between">
                  <strong class="small">{{ style.name ?? style.id }}</strong>
                  <span v-if="style.is_default" class="badge badge-ok">项目默认</span>
                </div>
                <p class="tiny dim">{{ style.description || '无描述' }}</p>
                <div class="row-wrap">
                  <span v-if="style.camera_style" class="chip tiny">{{ style.camera_style }}</span>
                  <span v-if="style.lighting_style" class="chip tiny">{{ style.lighting_style }}</span>
                </div>
                <button
                  type="button"
                  class="btn btn-sm btn-ghost"
                  :disabled="busy || style.is_default"
                  @click="applyStyle(style)"
                >
                  {{ style.is_default ? '已应用' : '应用到项目' }}
                </button>
              </li>
            </ul>
          </div>
        </section>

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">生成任务</h2>
            <span class="mono tiny dim">{{ jobs.length }} 个</span>
          </div>
          <div class="card-body col">
            <EmptyState
              v-if="!jobs.length"
              compact
              icon="⚙"
              title="暂无生成任务"
              description="选择角色后点击「生成参考图」。"
            />
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

    <!-- 角色详情 -->
    <section v-if="activeCharacter" class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">{{ activeCharacter.name }}</h2>
          <span class="badge mono">{{ activeCharacter.id }}</span>
        </div>
        <div class="row">
          <button type="button" class="btn btn-sm btn-primary" @click="openGenerate">
            生成参考图
          </button>
          <button type="button" class="btn btn-sm btn-ghost" @click="activeCharacter = null">收起</button>
        </div>
      </div>
      <div class="card-body col" style="gap: var(--space-4)">
        <dl class="kv">
          <dt>身份</dt>
          <dd>{{ activeCharacter.identity || '—' }}</dd>
          <dt>外貌</dt>
          <dd>{{ activeCharacter.appearance || '—' }}</dd>
          <dt>服饰</dt>
          <dd>{{ activeCharacter.outfit || '—' }}</dd>
          <dt>性格</dt>
          <dd>{{ activeCharacter.personality || '—' }}</dd>
          <dt>说话风格</dt>
          <dd>{{ activeCharacter.speech_style || '—' }}</dd>
          <dt>禁用变化</dt>
          <dd>{{ (activeCharacter.forbidden_changes ?? []).join('、') || '—' }}</dd>
        </dl>

        <div class="row-between">
          <span class="section-title">参考图资产</span>
          <button
            type="button"
            class="btn btn-sm btn-ghost"
            :disabled="assetsLoading"
            @click="openCharacter(activeCharacter)"
          >
            {{ assetsLoading ? '加载中…' : '刷新资产' }}
          </button>
        </div>

        <SkeletonBlock v-if="assetsLoading && !assets.length" variant="portrait" :rows="4" />
        <EmptyState
          v-else-if="!activeAssets.length"
          compact
          icon="▣"
          title="还没有参考图"
          description="点击「生成参考图」，选择需要生成的资产类型。"
        />
        <div v-else class="asset-grid">
          <figure
            v-for="asset in activeAssets"
            :key="asset.media_id"
            class="asset-item"
            :class="{ primary: asset.media_id === activeCharacter.primary_media_id }"
          >
            <MediaThumb :media-id="asset.media_id" :url="asset.url" portrait />
            <figcaption class="asset-cap">
              <div class="row-between">
                <span class="badge tiny">{{ ASSET_KIND_LABEL[asset.asset_kind ?? ''] ?? asset.asset_kind ?? '—' }}</span>
                <span class="mono tiny dim">v{{ asset.version ?? '—' }}</span>
              </div>
              <span class="tiny dim-2">{{ relativeTime(asset.created_at) }}</span>
              <span v-if="asset.media_id === activeCharacter.primary_media_id" class="badge badge-ok">当前主图</span>
              <div class="row" v-if="asset.media_id !== activeCharacter.primary_media_id">
                <button
                  type="button"
                  class="btn btn-sm"
                  :disabled="busy"
                  @click="confirmPrimary(asset.media_id)"
                >
                  设为主图
                </button>
                <button
                  type="button"
                  class="btn btn-sm btn-ghost"
                  :disabled="busy"
                  @click="openReplace(asset.media_id)"
                >
                  替换主图
                </button>
              </div>
            </figcaption>
          </figure>
        </div>
      </div>
    </section>

    <!-- 角色表单 -->
    <ModalDialog
      :open="showCharacterForm"
      :title="editingId ? '编辑角色' : '新增角色'"
      :width="600"
      @close="showCharacterForm = false"
    >
      <div class="form-row">
        <div class="field">
          <label class="label" for="c-name">角色名<span class="req">*</span></label>
          <input id="c-name" v-model="characterForm.name" class="input" placeholder="例如：林晚" />
          <span class="hint">角色名在项目内唯一</span>
        </div>
        <div class="field">
          <label class="label" for="c-age">年龄段</label>
          <input id="c-age" v-model="characterForm.age_range" class="input" placeholder="例如：20-24" />
        </div>
      </div>
      <div class="field">
        <label class="label" for="c-identity">身份</label>
        <input id="c-identity" v-model="characterForm.identity" class="input" placeholder="例如：夜班法医" />
      </div>
      <div class="field">
        <label class="label" for="c-appearance">外貌</label>
        <textarea id="c-appearance" v-model="characterForm.appearance" class="textarea" rows="2" />
      </div>
      <div class="field">
        <label class="label" for="c-outfit">服饰</label>
        <textarea id="c-outfit" v-model="characterForm.outfit" class="textarea" rows="2" />
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="c-personality">性格</label>
          <input id="c-personality" v-model="characterForm.personality" class="input" />
        </div>
        <div class="field">
          <label class="label" for="c-speech">说话风格</label>
          <input id="c-speech" v-model="characterForm.speech_style" class="input" />
        </div>
      </div>
      <div class="field">
        <label class="label" for="c-forbidden">禁用变化（顿号或逗号分隔）</label>
        <input id="c-forbidden" v-model="forbiddenText" class="input" placeholder="例如：不能改发型、不能换服装" />
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showCharacterForm = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitCharacter">
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 生成参考图 -->
    <ModalDialog :open="showGenerate" title="生成参考图" :width="520" @close="showGenerate = false">
      <div class="field">
        <label class="label">资产类型</label>
        <div class="row-wrap">
          <label v-for="kind in ASSET_KINDS" :key="kind" class="checkbox">
            <input v-model="generateForm.asset_kinds" type="checkbox" :value="kind" />
            <span>{{ ASSET_KIND_LABEL[kind] }}</span>
          </label>
        </div>
      </div>
      <div class="field">
        <label class="label" for="g-count">每种类型生成数量</label>
        <input
          id="g-count"
          v-model.number="generateForm.count_per_kind"
          class="input mono"
          type="number"
          min="1"
          :max="config.candidateMax"
        />
        <span class="hint">
          单次最多生成 {{ config.candidateMax }} 张（后台配置）
        </span>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showGenerate = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitGenerate">
          {{ busy ? '提交中…' : '提交生成任务' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 替换主图 -->
    <ModalDialog :open="showReplace" title="替换主参考图" :width="520" @close="showReplace = false">
      <p class="small muted">
        替换主图会影响下游分镜图一致性。请选择影响范围：
      </p>
      <div class="field">
        <label class="label" for="r-impact">影响范围</label>
        <select id="r-impact" v-model="replaceForm.impact" class="select">
          <option value="future_only">仅影响后续生成（future_only）</option>
          <option value="mark_stale">把已生成的分镜标记为过期（mark_stale）</option>
        </select>
        <span class="hint">替换主图会改变角色一致性，mark_stale 会触发下游重生成。</span>
      </div>
      <div v-if="affectedShots.length" class="affected">
        <span class="tiny dim">受影响分镜：</span>
        <div class="row-wrap">
          <span v-for="no in affectedShots" :key="no" class="chip mono">#{{ no }}</span>
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showReplace = false">关闭</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitReplace">
          {{ busy ? '提交中…' : '确认替换' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 画风表单 -->
    <ModalDialog :open="showStyleForm" title="新增画风" :width="560" @close="showStyleForm = false">
      <div class="field">
        <label class="label" for="s-name">名称<span class="req">*</span></label>
        <input id="s-name" v-model="styleForm.name" class="input" placeholder="例如：冷调赛璐璐" />
      </div>
      <div class="field">
        <label class="label" for="s-desc">描述</label>
        <textarea id="s-desc" v-model="styleForm.description" class="textarea" rows="2" />
      </div>
      <div class="field">
        <label class="label" for="s-palette">色板（空格或逗号分隔）</label>
        <input id="s-palette" v-model="styleForm.palette" class="input mono" placeholder="#1B2A41 var(--accent)" />
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="s-camera">镜头语言</label>
          <input id="s-camera" v-model="styleForm.camera_style" class="input" />
        </div>
        <div class="field">
          <label class="label" for="s-light">光线风格</label>
          <input id="s-light" v-model="styleForm.lighting_style" class="input" />
        </div>
      </div>
      <div class="field">
        <label class="label" for="s-neg">负面提示词</label>
        <textarea id="s-neg" v-model="styleForm.negative_prompt" class="textarea" rows="2" />
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showStyleForm = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitStyle">
          {{ busy ? '保存中…' : '创建画风' }}
        </button>
      </template>
    </ModalDialog>

    <!-- FR-050 试听：绑定前先听一下音色。后端按需合成并缓存，不重复计费。 -->
    <audio
      v-if="previewUrl"
      ref="previewAudio"
      :src="previewUrl"
      class="voice-preview-audio"
      controls
      preload="auto"
    />
  </div>
</template>

<style scoped>
/* 「工作流未启动」提示条：左侧说明 + 右侧启动按钮 */
.notice-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
}
/* 「已隐藏 N 个其他剧集角色」行 */
.char-scope {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 8px;
}
.notice-banner p {
  margin: 4px 0 0;
  line-height: 1.6;
}
.notice-banner b {
  color: var(--text-0);
}
.char-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: var(--gap);
}
.char-card {
  display: flex;
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel-2);
  cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.char-card:hover {
  border-color: var(--line-strong);
  transform: translateY(-1px);
}
.char-card.active {
  border-color: var(--line-accent);
  box-shadow: 0 0 0 1px var(--line-accent);
}
.char-thumb {
  position: relative;
  width: 76px;
  flex: 0 0 auto;
}
.char-ok {
  position: absolute;
  left: 3px;
  bottom: 3px;
  font-size: var(--fs-11);
  padding: 1px 5px;
  border-radius: var(--radius-pill);
  background: var(--ok-soft);
  border: 1px solid rgba(111, 191, 147, 0.45);
  color: var(--ok);
}
.char-body {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.style-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.style-item {
  padding: var(--space-3) var(--space-3);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.style-item.default {
  border-color: rgba(111, 191, 147, 0.35);
}
.asset-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(148px, 1fr));
  gap: var(--space-3);
}
.asset-item {
  margin: 0;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--panel-2);
  overflow: hidden;
}
.asset-item.primary {
  border-color: rgba(111, 191, 147, 0.5);
}
.asset-cap {
  padding: var(--space-2) var(--space-3) 10px;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}
.affected {
  padding: var(--space-3);
  border: 1px dashed var(--line-strong);
  border-radius: var(--radius-sm);
}

/* FR-050 音色绑定 + FR-051 语速 */
.voice-picker {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.voice-select {
  flex: 1;
  min-width: 120px;
  height: 26px;
  padding: 0 6px;
  font-size: var(--fs-12);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  color: inherit;
}
.voice-speed {
  display: flex;
  align-items: center;
  gap: var(--space-1);
}
.voice-speed-input {
  width: 62px;
  height: 26px;
  padding: 0 6px;
  font-size: var(--fs-12);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  color: inherit;
}
.voice-preview-audio {
  width: 100%;
  max-width: 360px;
  height: 34px;
}
</style>
