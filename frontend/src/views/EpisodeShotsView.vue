<script setup lang="ts">
/**
 * 分镜图确认（API.md §6、§9）。
 * 生成候选图 → 对比选择 → 确认/驳回 → 批量确认 → 门控确认。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { reportError } from '@/api/client'
import { jobsApi } from '@/api/jobs'
import { fetchCandidates, shotsApi } from '@/api/shots'
import { CAMERA_MOVEMENTS, COMPOSITIONS, scriptApi } from '@/api/story'
import { formatDuration } from '@/utils/format'
import { ApiError } from '@/api/errors'
import GateCard from '@/components/GateCard.vue'
import ShotGrid from '@/components/ShotGrid.vue'
import CandidatePicker from '@/components/CandidatePicker.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import type { DialogueLine, Job, Shot, ShotCandidate, ShotPatch } from '@/types'

const store = useEpisodeStore()
const config = useConfigStore()
const toast = useToastStore()
const { shots, workflow, pendingGate, jobSnapshots } = storeToRefs(store)

const selectedNos = ref<number[]>([])
const activeNo = ref<number | null>(null)
const candidates = ref<ShotCandidate[]>([])
const candidatesLoading = ref(false)

const busy = ref(false)
const gateBusy = ref(false)
const savingShot = ref(false)
const notice = ref('')
const jobIds = ref<string[]>([])
const regenerateOpen = ref(false)

const candidatesPerShot = ref(2)
const regenerateInstruction = ref('')
const replanDownstream = ref(false)

const draft = reactive<Required<Pick<ShotPatch, 'duration_ms' | 'composition' | 'camera_movement'>> & {
  characters: string
  action: string
  emotion: string
  image_prompt: string
  negative_prompt: string
  narration: string
  sfx: string
  required: boolean
  /** FR-061 转场：'' 表示硬切 */
  transition_type: string
  transition_ms: number
}>({
  duration_ms: 4000,
  composition: '',
  camera_movement: '',
  characters: '',
  action: '',
  emotion: '',
  image_prompt: '',
  negative_prompt: '',
  narration: '',
  sfx: '',
  required: false,
  transition_type: '',
  transition_ms: 300,
})

const activeShot = computed(() => shots.value.find((s) => s.shot_no === activeNo.value) ?? null)

/* ------------------------------------------------------------------ *
 * FR-030：分镜表格视图 + 台词编辑
 * ------------------------------------------------------------------ */

/** 'grid' 卡片视图 / 'table' 表格视图。表格便于逐镜核对与批量改台词。 */
const viewMode = ref<'grid' | 'table'>('grid')

/** 台词草稿：key 是镜号，值是该镜的台词行（可增删改）。 */
const dialogueDraft = ref<Record<number, DialogueLine[]>>({})
const dialogueBusy = ref<number | null>(null)

function dialogueOf(shot: Shot): DialogueLine[] {
  return dialogueDraft.value[shot.shot_no] ?? (shot.dialogue ?? [])
}

/** 表格里改台词前，先把该镜的台词复制进草稿，避免直接改 store 里的原始对象。 */
function ensureDialogueDraft(shot: Shot): DialogueLine[] {
  if (!dialogueDraft.value[shot.shot_no]) {
    dialogueDraft.value = {
      ...dialogueDraft.value,
      [shot.shot_no]: (shot.dialogue ?? []).map((d) => ({ ...d })),
    }
  }
  return dialogueDraft.value[shot.shot_no]
}

function addDialogueLine(shot: Shot): void {
  const lines = ensureDialogueDraft(shot)
  lines.push({ character: (shot.characters ?? [])[0] ?? '', text: '', emotion: '' })
}

function removeDialogueLine(shot: Shot, index: number): void {
  ensureDialogueDraft(shot).splice(index, 1)
}

/** 台词行是否为空（全空的行不提交，避免存下无意义的占位）。 */
function isBlankLine(line: DialogueLine): boolean {
  return !line.character.trim() && !line.text.trim()
}

const dialogueDirty = computed(() => {
  for (const [no, lines] of Object.entries(dialogueDraft.value)) {
    const shot = shots.value.find((s) => s.shot_no === Number(no))
    if (!shot) continue
    const original = (shot.dialogue ?? []).filter((d) => !isBlankLine(d))
    const current = lines.filter((d) => !isBlankLine(d))
    if (JSON.stringify(original) !== JSON.stringify(current)) return true
  }
  return false
})

/** 保存某镜台词。后端会把该镜音频标为 STALE，提示需要重新配音。 */
async function saveDialogue(shot: Shot): Promise<void> {
  const lines = (dialogueDraft.value[shot.shot_no] ?? []).filter((d) => !isBlankLine(d))
  dialogueBusy.value = shot.shot_no
  try {
    const updated = await shotsApi.patch(shot.id, { dialogue: lines })
    store.patchLocalShot(updated)
    const next = { ...dialogueDraft.value }
    delete next[shot.shot_no]
    dialogueDraft.value = next
    toast.success('台词已保存', `#${shot.shot_no} 共 ${lines.length} 句；该镜配音将标记为需重做`)
  } catch {
    /* api 层已提示 */
  } finally {
    dialogueBusy.value = null
  }
}

/** 一次性保存所有改动过的镜头台词。 */
async function saveAllDialogue(): Promise<void> {
  const dirtyNos: number[] = []
  for (const [no, lines] of Object.entries(dialogueDraft.value)) {
    const shot = shots.value.find((s) => s.shot_no === Number(no))
    if (!shot) continue
    const original = (shot.dialogue ?? []).filter((d) => !isBlankLine(d))
    const current = lines.filter((d) => !isBlankLine(d))
    if (JSON.stringify(original) !== JSON.stringify(current)) dirtyNos.push(Number(no))
  }
  if (!dirtyNos.length) {
    toast.info('没有需要保存的台词改动')
    return
  }
  busy.value = true
  let ok = 0
  try {
    for (const no of dirtyNos) {
      const shot = shots.value.find((s) => s.shot_no === no)
      if (!shot) continue
      const lines = (dialogueDraft.value[no] ?? []).filter((d) => !isBlankLine(d))
      try {
        const updated = await shotsApi.patch(shot.id, { dialogue: lines })
        store.patchLocalShot(updated)
        ok += 1
      } catch {
        /* 单镜失败不阻断其余镜头，最后统一汇报 */
      }
    }
    const next = { ...dialogueDraft.value }
    for (const no of dirtyNos) delete next[no]
    dialogueDraft.value = next
    if (ok === dirtyNos.length) {
      toast.success('台词已全部保存', `${ok} 个镜头`)
    } else {
      toast.warn('部分台词保存失败', `成功 ${ok} / ${dirtyNos.length}，失败的可重试`)
    }
  } finally {
    busy.value = false
  }
}

function discardDialogueDraft(): void {
  dialogueDraft.value = {}
  toast.info('已放弃台词改动')
}
/** 首镜没有「上一镜」，转场对其无意义。 */
const isFirstShot = computed(() => (activeShot.value?.shot_no ?? 1) <= 1)

/** 转场类型 id → 中文名（清单来自 /config/limits）。 */
function transitionLabel(typeId: string): string {
  return config.transitionTypes.find((t) => t.id === typeId)?.label ?? typeId
}
const confirmedCount = computed(() => shots.value.filter((s) => s.confirmed_media_id).length)
const jobs = computed<Job[]>(() =>
  jobIds.value.map((id) => jobSnapshots.value[id]).filter((j): j is Job => Boolean(j)),
)

onMounted(async () => {
  await config.load()
  candidatesPerShot.value = config.candidateDefault
})

watch(
  () => config.candidateDefault,
  (value) => {
    candidatesPerShot.value = value
  },
)

function toggleSelect(shotNo: number): void {
  selectedNos.value = selectedNos.value.includes(shotNo)
    ? selectedNos.value.filter((n) => n !== shotNo)
    : [...selectedNos.value, shotNo].sort((a, b) => a - b)
}

function selectAll(): void {
  selectedNos.value = shots.value.map((s) => s.shot_no)
}

function clearSelection(): void {
  selectedNos.value = []
}

function selectUnconfirmed(): void {
  selectedNos.value = shots.value.filter((s) => !s.confirmed_media_id).map((s) => s.shot_no)
}

async function openShot(shot: Shot): Promise<void> {
  activeNo.value = shot.shot_no
  fillDraft(shot)
  candidatesLoading.value = true
  try {
    candidates.value = await fetchCandidates(shot.id)
  } catch {
    candidates.value = []
  } finally {
    candidatesLoading.value = false
  }
}

function fillDraft(shot: Shot): void {
  draft.duration_ms = shot.duration_ms ?? config.shotMinMs
  draft.composition = shot.composition ?? ''
  draft.camera_movement = shot.camera_movement ?? ''
  draft.characters = (shot.characters ?? []).join('、')
  draft.action = shot.action ?? ''
  draft.emotion = shot.emotion ?? ''
  draft.image_prompt = shot.image_prompt ?? ''
  draft.negative_prompt = shot.negative_prompt ?? ''
  draft.narration = shot.narration ?? ''
  draft.sfx = (shot.sfx ?? []).join('、')
  draft.required = Boolean(shot.required)
  draft.transition_type = shot.transition?.type ?? ''
  draft.transition_ms = shot.transition?.duration_ms ?? config.transitionDefaultMs
}

async function saveShot(): Promise<void> {
  const shot = activeShot.value
  if (!shot) return
  if (draft.duration_ms < config.shotMinMs || draft.duration_ms > config.shotMaxMs) {
    toast.warn('时长超出限制', `允许范围 ${config.shotMinMs} ~ ${config.shotMaxMs} ms（来自 /config/limits）`)
    return
  }
  savingShot.value = true
  const patch: ShotPatch = {
    duration_ms: draft.duration_ms,
    composition: draft.composition || undefined,
    camera_movement: draft.camera_movement || undefined,
    characters: draft.characters
      .split(/[、,，\n]/)
      .map((s) => s.trim())
      .filter(Boolean),
    action: draft.action,
    emotion: draft.emotion,
    image_prompt: draft.image_prompt,
    negative_prompt: draft.negative_prompt,
    narration: draft.narration,
    sfx: draft.sfx
      .split(/[、,，\n]/)
      .map((s) => s.trim())
      .filter(Boolean),
    required: draft.required,
    // FR-061 转场：留空即清除（后端会把 none 视为硬切）
    transition: draft.transition_type
      ? { type: draft.transition_type, duration_ms: draft.transition_ms }
      : null,
  }
  try {
    const updated = await shotsApi.patch(shot.id, patch)
    store.patchLocalShot(updated)
    toast.success('镜头已保存', `#${shot.shot_no}`)
  } catch {
    /* api 层已提示 */
  } finally {
    savingShot.value = false
  }
}

function track(job: Job): void {
  jobIds.value = [job.id, ...jobIds.value]
  store.trackJob(job)
}

async function generateImages(): Promise<void> {
  const nos = selectedNos.value.length ? selectedNos.value : activeNo.value ? [activeNo.value] : []
  if (!nos.length) {
    toast.warn('请先选择镜头', '勾选需要生成分镜图的镜头')
    return
  }
  if (candidatesPerShot.value < 1 || candidatesPerShot.value > config.candidateMax) {
    toast.warn('候选数超出限制', `允许 1 ~ ${config.candidateMax}（来自 /config/limits）`)
    return
  }
  busy.value = true
  try {
    const job = await shotsApi.generateImages(store.episodeId ?? '', {
      shot_nos: nos,
      candidates_per_shot: candidatesPerShot.value,
    })
    track(job)
    toast.info('分镜图生成任务已提交', `${nos.length} 个镜头 × ${candidatesPerShot.value} 张候选`)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function confirmBatch(): Promise<void> {
  const nos = selectedNos.value
  if (!nos.length) {
    toast.warn('请先选择镜头')
    return
  }
  busy.value = true
  try {
    const result = await shotsApi.confirmBatch(store.episodeId ?? '', nos)
    toast.success('批量确认已提交', `确认 ${result.confirmed?.length ?? nos.length} 个镜头`)
    clearSelection()
    await store.loadShots()
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

async function submitRegenerate(): Promise<void> {
  const nos = selectedNos.value.length ? selectedNos.value : activeNo.value ? [activeNo.value] : []
  if (!nos.length) {
    toast.warn('请先选择要重生成的镜头')
    return
  }
  busy.value = true
  try {
    const job = await scriptApi.regenerateShots(store.episodeId ?? '', {
      shot_nos: nos,
      replan_downstream: replanDownstream.value,
      instruction: regenerateInstruction.value.trim() || undefined,
    })
    track(job)
    regenerateOpen.value = false
    regenerateInstruction.value = ''
    toast.info('局部重生成任务已提交', `${nos.length} 个镜头`)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

/* ---------------- 候选操作 ---------------- */

async function selectCandidate(mediaId: string): Promise<void> {
  const shot = activeShot.value
  if (!shot) return
  try {
    const updated = await shotsApi.select(shot.id, mediaId)
    store.patchLocalShot(updated)
  } catch {
    /* api 层已提示 */
  }
}

async function confirmCandidate(mediaId: string): Promise<void> {
  const shot = activeShot.value
  if (!shot) return
  try {
    const updated = await shotsApi.confirm(shot.id, mediaId)
    store.patchLocalShot(updated)
    candidates.value = await fetchCandidates(shot.id).catch(() => candidates.value)
    toast.success('画面已确认', `#${shot.shot_no}`)
    await store.refreshLive()
  } catch {
    /* api 层已提示 */
  }
}

async function rejectCandidate(mediaId: string, reason: string): Promise<void> {
  const shot = activeShot.value
  if (!shot) return
  try {
    await shotsApi.reject(shot.id, mediaId, reason)
    candidates.value = await fetchCandidates(shot.id).catch(() => candidates.value)
    toast.info('候选已驳回', reason)
  } catch {
    /* api 层已提示 */
  }
}

async function restoreCandidate(mediaId: string): Promise<void> {
  const shot = activeShot.value
  if (!shot) return
  try {
    await shotsApi.restoreCandidate(shot.id, mediaId)
    candidates.value = await fetchCandidates(shot.id).catch(() => candidates.value)
    toast.success('候选已恢复')
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
    toast.success('分镜画面已确认')
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
      approve-text="确认全部分镜画面，进入视频生成"
      description="所有必需镜头都需要确认主图；确认后工作流进入视频成本确认阶段。"
      @approve="approve"
      @reject="reject"
      @edit="edit"
      @refresh="store.refreshLive()"
    >
      <div class="row-wrap tiny dim">
        <span>已确认 <span class="mono">{{ confirmedCount }}/{{ shots.length }}</span></span>
        <span>候选数上限 <span class="mono">{{ config.candidateMax }}</span></span>
        <span>时长范围 <span class="mono">{{ config.shotMinMs }}~{{ config.shotMaxMs }}ms</span></span>
      </div>
    </GateCard>

    <section class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">分镜表</h2>
          <span class="mono tiny dim">{{ shots.length }} 镜</span>
        </div>
        <div class="row-wrap">
          <label class="row tiny dim" for="cand-count">候选数</label>
          <input
            id="cand-count"
            v-model.number="candidatesPerShot"
            class="input mono cand-input"
            type="number"
            min="1"
            :max="config.candidateMax"
          />
          <button type="button" class="btn btn-sm btn-primary" :disabled="busy" @click="generateImages">
            生成分镜图
          </button>
          <button type="button" class="btn btn-sm" :disabled="busy" @click="confirmBatch">
            批量确认
          </button>
          <button type="button" class="btn btn-sm btn-ghost" @click="regenerateOpen = true">
            局部重生成
          </button>
        </div>
      </div>
      <div class="card-body col" style="gap: 14px">
        <div class="row-wrap">
          <button type="button" class="btn btn-sm btn-ghost" @click="selectAll">全选</button>
          <button type="button" class="btn btn-sm btn-ghost" @click="selectUnconfirmed">选未确认</button>
          <button type="button" class="btn btn-sm btn-ghost" @click="clearSelection">清空选择</button>
          <span class="tiny dim">已选 <span class="mono">{{ selectedNos.length }}</span> 个镜头</span>
          <!-- FR-030：卡片 / 表格两种视图 -->
          <span class="view-switch" role="group" aria-label="视图切换">
            <button
              type="button"
              class="btn btn-sm"
              :class="viewMode === 'grid' ? 'btn-primary' : 'btn-ghost'"
              @click="viewMode = 'grid'"
            >
              卡片
            </button>
            <button
              type="button"
              class="btn btn-sm"
              :class="viewMode === 'table' ? 'btn-primary' : 'btn-ghost'"
              @click="viewMode = 'table'"
            >
              表格
            </button>
          </span>
        </div>

        <ShotGrid
          v-if="viewMode === 'grid'"
          :shots="shots"
          :selected-nos="selectedNos"
          :active-no="activeNo"
          selectable
          @toggle="toggleSelect"
          @open="openShot"
        />

        <!-- FR-030 表格视图：逐镜核对 + 台词行内编辑 -->
        <template v-else>
          <div v-if="dialogueDirty" class="dialogue-bar">
            <span class="tiny">有未保存的台词改动</span>
            <span class="row" style="gap: 6px">
              <button
                type="button"
                class="btn btn-sm btn-primary"
                :disabled="busy"
                @click="saveAllDialogue"
              >
                {{ busy ? '保存中…' : '保存全部台词' }}
              </button>
              <button type="button" class="btn btn-sm btn-ghost" @click="discardDialogueDraft">
                放弃改动
              </button>
            </span>
          </div>

          <div class="shot-table-wrap">
            <table class="shot-table">
              <thead>
                <tr>
                  <th class="col-sel"></th>
                  <th class="col-no">镜号</th>
                  <th class="col-dur">时长</th>
                  <th class="col-role">角色</th>
                  <th class="col-act">动作</th>
                  <th class="col-cam">运镜/景别</th>
                  <th class="col-dlg">台词（可直接编辑）</th>
                  <th class="col-state">状态</th>
                  <th class="col-op"></th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="shot in shots"
                  :key="shot.id"
                  :class="{ 'is-active': shot.shot_no === activeNo, 'is-stale': shot.stale }"
                >
                  <td class="col-sel">
                    <input
                      type="checkbox"
                      :checked="selectedNos.includes(shot.shot_no)"
                      :aria-label="`选择镜头 ${shot.shot_no}`"
                      @change="toggleSelect(shot.shot_no)"
                    />
                  </td>
                  <td class="col-no mono">{{ String(shot.shot_no).padStart(3, '0') }}</td>
                  <td class="col-dur mono">{{ formatDuration(shot.duration_ms) }}</td>
                  <td class="col-role tiny">{{ (shot.characters ?? []).join('、') || '—' }}</td>
                  <td class="col-act tiny">{{ shot.action || '—' }}</td>
                  <td class="col-cam tiny">
                    {{ [shot.camera_movement, shot.composition].filter(Boolean).join(' / ') || '—' }}
                  </td>
                  <td class="col-dlg">
                    <div class="dlg-list">
                      <div
                        v-for="(line, i) in dialogueOf(shot)"
                        :key="i"
                        class="dlg-row"
                      >
                        <input
                          v-model="line.character"
                          class="input dlg-char"
                          placeholder="角色"
                          :aria-label="`镜头 ${shot.shot_no} 第 ${i + 1} 句角色`"
                          @focus="ensureDialogueDraft(shot)"
                        />
                        <input
                          v-model="line.text"
                          class="input dlg-text"
                          placeholder="台词内容"
                          :aria-label="`镜头 ${shot.shot_no} 第 ${i + 1} 句台词`"
                          @focus="ensureDialogueDraft(shot)"
                        />
                        <button
                          type="button"
                          class="btn btn-sm btn-ghost dlg-del"
                          title="删除这句"
                          @click="removeDialogueLine(shot, i)"
                        >
                          ×
                        </button>
                      </div>
                      <button
                        type="button"
                        class="btn btn-sm btn-ghost dlg-add"
                        @click="addDialogueLine(shot)"
                      >
                        ＋ 加一句
                      </button>
                    </div>
                  </td>
                  <td class="col-state">
                    <span v-if="shot.confirmed_media_id" class="badge badge-ok">已确认</span>
                    <span v-else class="badge badge-warn">待确认</span>
                    <span v-if="shot.stale" class="badge badge-warn" :title="shot.stale_reason ?? ''">
                      过期
                    </span>
                  </td>
                  <td class="col-op">
                    <span class="row" style="gap: 4px">
                      <button
                        type="button"
                        class="btn btn-sm btn-ghost"
                        @click="openShot(shot)"
                      >
                        详情
                      </button>
                      <button
                        type="button"
                        class="btn btn-sm"
                        :disabled="dialogueBusy === shot.shot_no"
                        @click="saveDialogue(shot)"
                      >
                        {{ dialogueBusy === shot.shot_no ? '…' : '存台词' }}
                      </button>
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <span class="hint">
            改台词会触发该镜配音标记为「需重做」（后端 `mark_stale_for_shot`），已生成的音频不会自动重配。
          </span>
        </template>
      </div>
    </section>

    <div class="two-col">
      <section v-if="activeShot" class="card">
        <div class="card-head">
          <div class="row">
            <h2 class="title-md">镜头 #{{ String(activeShot.shot_no).padStart(3, '0') }}</h2>
            <span class="badge mono">{{ formatDuration(activeShot.duration_ms) }}</span>
            <span v-if="activeShot.confirmed_media_id" class="badge badge-ok">画面已确认</span>
            <span v-else class="badge badge-warn">待确认</span>
            <span v-if="activeShot.stale" class="badge badge-warn">下游过期</span>
          </div>
          <button type="button" class="btn btn-sm btn-ghost" @click="activeNo = null">收起</button>
        </div>
        <div class="card-body col" style="gap: 14px">
          <CandidatePicker
            :shot="activeShot"
            :candidates="candidates"
            :busy="candidatesLoading"
            :selected-media-id="activeShot.selected_media_id ?? null"
            @select="selectCandidate"
            @confirm="confirmCandidate"
            @reject="rejectCandidate"
            @restore="restoreCandidate"
          />

          <hr class="divider" />

          <span class="section-title">镜头参数</span>
          <div class="form-row">
            <div class="field">
              <label class="label" for="d-dur">时长（毫秒）</label>
              <input
                id="d-dur"
                v-model.number="draft.duration_ms"
                class="input mono"
                type="number"
                :min="config.shotMinMs"
                :max="config.shotMaxMs"
                step="100"
              />
              <span class="hint">{{ config.shotMinMs }} ~ {{ config.shotMaxMs }} ms</span>
            </div>
            <div class="field">
              <label class="label" for="d-comp">构图</label>
              <select id="d-comp" v-model="draft.composition" class="select">
                <option value="">未设置</option>
                <option v-for="c in COMPOSITIONS" :key="c" :value="c">{{ c }}</option>
              </select>
            </div>
          </div>
          <div class="form-row">
            <div class="field">
              <label class="label" for="d-cam">镜头运动</label>
              <select id="d-cam" v-model="draft.camera_movement" class="select">
                <option value="">未设置</option>
                <option v-for="c in CAMERA_MOVEMENTS" :key="c" :value="c">{{ c }}</option>
              </select>
            </div>
            <div class="field">
              <label class="label" for="d-chars">出场角色（顿号分隔）</label>
              <input id="d-chars" v-model="draft.characters" class="input" />
            </div>
          </div>
          <!-- FR-061 转场：从上一镜切入本镜的方式；首镜设置会被忽略 -->
          <div class="form-row">
            <div class="field">
              <label class="label" for="d-trans">转场（从上一镜切入）</label>
              <select
                id="d-trans"
                v-model="draft.transition_type"
                class="select"
                :disabled="isFirstShot"
              >
                <option value="">无（硬切）</option>
                <option v-for="t in config.transitionTypes" :key="t.id" :value="t.id">
                  {{ t.label }}
                </option>
              </select>
              <span v-if="isFirstShot" class="hint">首镜没有上一镜，转场会被忽略</span>
            </div>
            <div v-if="draft.transition_type" class="field">
              <label class="label" for="d-trans-ms">转场时长（毫秒）</label>
              <input
                id="d-trans-ms"
                v-model.number="draft.transition_ms"
                class="input mono"
                type="number"
                :min="100"
                :max="config.transitionMaxMs"
                step="50"
              />
              <span class="hint">不得超过相邻两镜较短者的一半，且 ≤ {{ config.transitionMaxMs }}ms</span>
            </div>
          </div>
          <div class="field">
            <label class="label" for="d-action">画面动作</label>
            <textarea id="d-action" v-model="draft.action" class="textarea" rows="2" />
          </div>
          <div class="form-row">
            <div class="field">
              <label class="label" for="d-emo">情绪</label>
              <input id="d-emo" v-model="draft.emotion" class="input" />
            </div>
            <div class="field">
              <label class="label" for="d-sfx">音效（顿号分隔）</label>
              <input id="d-sfx" v-model="draft.sfx" class="input" />
            </div>
          </div>
          <div class="field">
            <label class="label" for="d-prompt">画面提示词</label>
            <textarea id="d-prompt" v-model="draft.image_prompt" class="textarea" rows="2" />
          </div>
          <div class="field">
            <label class="label" for="d-neg">负面提示词</label>
            <textarea id="d-neg" v-model="draft.negative_prompt" class="textarea" rows="2" />
          </div>
          <div class="field">
            <label class="label" for="d-nar">旁白</label>
            <textarea id="d-nar" v-model="draft.narration" class="textarea" rows="2" />
          </div>
          <div class="row-between">
            <label class="checkbox">
              <input v-model="draft.required" type="checkbox" />
              <span class="small">必需镜头（不可跳过视频）</span>
            </label>
            <button type="button" class="btn btn-primary" :disabled="savingShot" @click="saveShot">
              {{ savingShot ? '保存中…' : '保存镜头' }}
            </button>
          </div>
        </div>
      </section>

      <div class="col" style="gap: var(--gap)">
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
              description="勾选镜头后点击「生成分镜图」。"
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

        <section class="card">
          <div class="card-head">
            <h2 class="title-md">镜头状态</h2>
          </div>
          <div class="card-body">
            <EmptyState v-if="!shots.length" compact icon="▦" title="暂无分镜" />
            <ul v-else class="status-list">
              <li
                v-for="shot in shots"
                :key="shot.id"
                class="status-item"
                :class="{ active: shot.shot_no === activeNo }"
                @click="openShot(shot)"
              >
                <span class="mono tiny">#{{ String(shot.shot_no).padStart(3, '0') }}</span>
                <span class="badge" :class="shot.confirmed_media_id ? 'badge-ok' : 'badge-warn'">
                  {{ shot.confirmed_media_id ? '画面已确认' : '待确认' }}
                </span>
                <span v-if="shot.video_status" class="badge badge-info">视频 {{ shot.video_status }}</span>
                <span v-if="shot.audio_stale" class="badge badge-warn">音频过期</span>
                <span
                  v-if="shot.transition?.type && shot.shot_no > 1"
                  class="badge badge-muted"
                  :title="`转场：${transitionLabel(shot.transition.type)}`"
                >
                  ⤳ {{ transitionLabel(shot.transition.type) }}
                </span>
              </li>
            </ul>
          </div>
        </section>
      </div>
    </div>

    <section v-if="!activeShot" class="card card-pad">
      <ErrorState
        v-if="store.error"
        :error="store.error"
        context="加载分镜表"
        compact
        @retry="store.loadShots()"
      />
      <EmptyState
        v-else
        compact
        icon="▦"
        title="点击任意分镜查看候选图"
        description="选中镜头后可编辑参数、对比候选图并确认主图。"
      />
    </section>
    <ModalDialog :open="regenerateOpen" title="局部重生成分镜" :width="540" @close="regenerateOpen = false">
      <p class="small muted">
        将对已选镜头重新规划分镜脚本与画面提示词。未选择时默认只处理当前打开的镜头。
      </p>
      <div class="field">
        <label class="label">目标镜头</label>
        <div class="row-wrap">
          <span v-if="!selectedNos.length && !activeNo" class="tiny dim">请先在分镜表中勾选镜头</span>
          <span v-for="no in selectedNos" :key="no" class="chip mono">#{{ no }}</span>
          <span v-if="!selectedNos.length && activeNo" class="chip mono">#{{ activeNo }}</span>
        </div>
      </div>
      <div class="field">
        <label class="label" for="rg-inst">重做指令</label>
        <textarea
          id="rg-inst"
          v-model="regenerateInstruction"
          class="textarea"
          rows="3"
          placeholder="例如：这一镜改成雨夜俯拍，情绪更压抑"
        />
      </div>
      <label class="checkbox">
        <input v-model="replanDownstream" type="checkbox" />
        <span class="small">同时重规划下游分镜（replan_downstream）</span>
      </label>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="regenerateOpen = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitRegenerate">
          {{ busy ? '提交中…' : '提交重生成' }}
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
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
  gap: var(--gap);
  align-items: start;
}
@media (max-width: 1180px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
.cand-input {
  width: 76px;
}
.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.status-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 340px;
  overflow: auto;
}
.status-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: rgba(255, 255, 255, 0.02);
  cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.status-item:hover {
  border-color: var(--line-strong);
}
.status-item.active {
  border-color: var(--line-accent);
  background: var(--accent-soft);
}

/* FR-030 表格视图 */
.view-switch {
  display: inline-flex;
  gap: 3px;
  margin-left: auto;
}
.dialogue-bar {
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
.shot-table-wrap {
  overflow: auto;
  max-height: 620px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
}
.shot-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.shot-table th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg);
  text-align: left;
  font-weight: 600;
  color: var(--dim);
  padding: 7px 8px;
  border-bottom: 1px solid var(--line-strong);
  white-space: nowrap;
}
.shot-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--line);
  vertical-align: top;
}
.shot-table tr.is-active {
  background: var(--accent-soft);
}
.shot-table tr.is-stale {
  background: rgba(184, 134, 11, 0.07);
}
.col-sel {
  width: 30px;
}
.col-no {
  width: 52px;
}
.col-dur {
  width: 62px;
  white-space: nowrap;
}
.col-role,
.col-cam {
  width: 96px;
}
.col-act {
  width: 150px;
}
.col-state {
  width: 86px;
  white-space: nowrap;
}
.col-op {
  width: 108px;
}
.col-dlg {
  min-width: 280px;
}
.dlg-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.dlg-row {
  display: flex;
  align-items: center;
  gap: 4px;
}
.dlg-char {
  width: 68px;
  height: 25px;
  padding: 0 5px;
  font-size: 12px;
}
.dlg-text {
  flex: 1;
  height: 25px;
  padding: 0 5px;
  font-size: 12px;
}
.dlg-del {
  height: 25px;
  padding: 0 7px;
}
.dlg-add {
  align-self: flex-start;
  height: 24px;
  padding: 0 8px;
  font-size: 11px;
}
</style>
