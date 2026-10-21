<script setup lang="ts">
/**
 * 故事输入与剧本确认（API.md §4、§9）。
 * 流程：保存故事 → 触发 StoryAgent（异步 job）→ 校验剧本 → 门控确认/驳回/重做。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useEpisodeStore } from '@/stores/episode'
import { useToastStore } from '@/stores/toast'
import { reportError } from '@/api/client'
import { jobsApi } from '@/api/jobs'
import {
  CAMERA_MOVEMENTS,
  COMPOSITIONS,
  STORY_INPUT_TYPE_LABEL,
  STORY_TEXT_MAX,
  emptyStoryJson,
  fetchStoryVersions,
  scriptApi,
  storyApi,
} from '@/api/story'
import { formatDateTime, formatDuration } from '@/utils/format'
import { formatCents } from '@/api/cost'
import { useAutosave, clearDraft } from '@/composables/useAutosave'
import GateCard from '@/components/GateCard.vue'
import JobInline from '@/components/JobInline.vue'
import EmptyState from '@/components/EmptyState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiError, toApiError } from '@/api/errors'
import type {
  Job,
  ScriptDocument,
  ScriptValidationResult,
  StoryDocument,
  StoryEstimate,
  StoryInputType,
  StoryJson,
  StoryVersion,
} from '@/types'

/** 剧本枚举白名单（API.md §4.1），用于表格中高亮合法取值。 */
const COMPOSITION_SET = new Set<string>(COMPOSITIONS)
const CAMERA_SET = new Set<string>(CAMERA_MOVEMENTS)

const store = useEpisodeStore()
const toast = useToastStore()
const { workflow, pendingGate, logs, jobSnapshots } = storeToRefs(store)

const story = ref<StoryDocument | null>(null)
const versions = ref<StoryVersion[]>([])
const script = ref<ScriptDocument | null>(null)

const storyText = ref('')
const inputType = ref<StoryInputType>('idea')
const draft = ref('')
const validation = ref<ScriptValidationResult | null>(null)
const scriptTab = ref<'outline' | 'shots' | 'json'>('outline')

const storyJobId = ref<string | null>(null)
const storyJob = computed<Job | null>(() =>
  storyJobId.value ? (jobSnapshots.value[storyJobId.value] ?? null) : null,
)

const loadingStory = ref(false)
const loadingScript = ref(false)
const saving = ref(false)
const busy = ref(false)
const gateBusy = ref(false)
const scriptError = ref<ApiError | null>(null)
const notice = ref('')

const storyRemaining = computed(() => STORY_TEXT_MAX - storyText.value.length)
const overLimit = computed(() => storyRemaining.value < 0)

/* ------------------------------------------------------------------ *
 * FR-010：长文本必须显示字符数和预计生成成本
 * ------------------------------------------------------------------ */

const estimate = ref<StoryEstimate | null>(null)
const estimating = ref(false)
let estimateTimer: ReturnType<typeof setTimeout> | null = null

/** 后端与 `/story/generate` 共用同一份提示词，因此这里就是实扣口径。 */
async function fetchEstimate(): Promise<void> {
  const id = episodeId.value
  if (!id || !storyText.value.trim() || overLimit.value) {
    estimate.value = null
    return
  }
  estimating.value = true
  try {
    estimate.value = await storyApi.estimate(id, { source_text: storyText.value })
  } catch {
    // 预估失败不阻塞主流程：保留上一次结果，输入框照常可编辑
    estimate.value = null
  } finally {
    estimating.value = false
  }
}

/** 输入停止 600ms 后再请求，避免每敲一个字打一次接口。 */
watch(storyText, () => {
  if (estimateTimer) clearTimeout(estimateTimer)
  estimateTimer = setTimeout(() => void fetchEstimate(), 600)
})

const estimateLabel = computed<string>(() => {
  const e = estimate.value
  if (!e) return '—'
  if (!e.declared) return '供应商未声明单价'
  return formatCents(e.estimate_cents)
})

/* ------------------------------------------------------------------ *
 * FR-003：自动保存 + 本地草稿 + 冲突提示
 * ------------------------------------------------------------------ */

const episodeId = computed(() => store.episodeId ?? '')
const draftNotice = ref('')

const autosave = useAutosave<string>({
  episodeId,
  key: 'story-text',
  value: storyText,
  serverVersion: () => story.value?.version ?? null,
  isEmpty: (text) => !text.trim(),
  save: async (text) => {
    const doc = await storyApi.save(episodeId.value, {
      input_type: inputType.value,
      source_text: text,
    })
    story.value = doc
    await loadVersions()
    return doc.version ?? null
  },
})

/** 进入页面时若本地草稿与服务器版本不一致，提示用户取舍，不静默覆盖。 */
watch(
  () => autosave.conflict.value,
  (pending) => {
    if (!pending) {
      draftNotice.value = ''
      return
    }
    const at = new Date(pending.draft.savedAt).toISOString()
    draftNotice.value =
      `检测到本地草稿（保存于 ${formatDateTime(at)}），` +
      `但服务器已更新到 v${pending.serverVersion ?? '—'}。请选择保留哪一份。`
  },
  { immediate: true },
)

const AUTO_SAVE_LABEL: Record<string, string> = {
  idle: '自动保存已开启',
  pending: '待自动保存…',
  saving: '自动保存中…',
  saved: '已自动保存',
  error: '自动保存失败',
  offline: '离线，已存本地草稿',
}

async function acceptLocalDraft(): Promise<void> {
  await autosave.acceptDraft()
  draftNotice.value = ''
  toast.success('已采用本地草稿', '并已提交到服务器')
}

function discardLocalDraft(): void {
  autosave.discardDraft()
  draftNotice.value = ''
  toast.info('已丢弃本地草稿', '保留服务器上的版本')
}

const parsedDraft = computed<StoryJson | null>(() => {
  try {
    const parsed: unknown = JSON.parse(draft.value)
    if (parsed && typeof parsed === 'object') return parsed as StoryJson
    return null
  } catch {
    return null
  }
})
const jsonInvalid = computed(() => draft.value.trim().length > 0 && parsedDraft.value === null)
const storyJson = computed<StoryJson | null>(() => script.value?.story_json ?? parsedDraft.value)

/** 冲突目标一行文本（PRD FR-011 五项之一）。 */
const conflictText = computed<string>(() => {
  const c = story.value?.analysis?.conflict
  if (!c) return ''
  return [c.goal, c.obstacle, c.stakes].filter((v) => !!v).join(' ／ ')
})

async function loadStory(): Promise<void> {
  if (!episodeId.value) return
  loadingStory.value = true
  try {
    const doc = await storyApi.get(episodeId.value)
    story.value = doc
    // FR-003：先回填服务器内容，再看本地草稿是否更新（restore 会判定冲突）
    autosave.loadWithoutSaving(doc?.source_text ?? '')
    inputType.value = (doc?.input_type as StoryInputType) ?? 'idea'
    autosave.restore()
  } catch {
    story.value = null
  } finally {
    loadingStory.value = false
  }
}

async function loadVersions(): Promise<void> {
  if (!episodeId.value) return
  versions.value = await fetchStoryVersions(episodeId.value).catch(() => [])
}

async function loadScript(): Promise<void> {
  if (!episodeId.value) return
  loadingScript.value = true
  scriptError.value = null
  try {
    script.value = await scriptApi.get(episodeId.value)
    draft.value = JSON.stringify(script.value?.story_json ?? emptyStoryJson(), null, 2)
    validation.value = null
  } catch (err) {
    const apiErr = toApiError(err)
    // 剧本尚未生成时后端返回 NOT_FOUND，属于正常空态；其余（含未知异常）都必须报错，
    // 否则「加载失败」会被显示成「还没有剧本」。
    if (apiErr.status === 404 || apiErr.code === 'NOT_FOUND') {
      script.value = null
      draft.value = ''
    } else {
      scriptError.value = apiErr
    }
  } finally {
    loadingScript.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadStory(), loadVersions(), loadScript()])
})

async function saveStory(): Promise<void> {
  if (!episodeId.value) return
  if (overLimit.value) {
    toast.warn('故事文本过长', `最多 ${STORY_TEXT_MAX} 字，当前超出 ${-storyRemaining.value} 字`)
    return
  }
  if (!storyText.value.trim()) {
    toast.warn('故事内容为空', '请先写下你的创意或故事文本')
    return
  }
  saving.value = true
  try {
    const doc = await storyApi.save(episodeId.value, {
      input_type: inputType.value,
      source_text: storyText.value,
    })
    story.value = doc
    toast.success('故事已保存', `版本 ${doc.version ?? '—'}`)
    await loadVersions()
    // 手动保存成功后本地草稿已无意义，清掉以免下次进页面误报冲突
    clearDraft(episodeId.value, 'story-text')
    draftNotice.value = ''
  } catch {
    /* api 层已提示 */
  } finally {
    saving.value = false
  }
}

async function runJob(runner: () => Promise<Job>, label: string): Promise<void> {
  busy.value = true
  try {
    const job = await runner()
    storyJobId.value = job.id
    store.trackJob(job)
    toast.info(`${label}已提交`, `任务 ${job.id}`)
  } catch {
    /* api 层已提示 */
  } finally {
    busy.value = false
  }
}

/**
 * 生成前把「屏幕上这份文本」落到服务器。
 *
 * 之前这里直接调生成接口，依赖自动保存先跑完：用户输入后立刻点「生成剧本」时，
 * 保存防抖还没触发，后端读到的仍是上一次（甚至空的）内容 —— 结果是模型
 * 拿着空文本自由发挥，产出与用户创意毫无关系的剧本。
 * 返回 false 表示没同步成功，调用方应中止生成，而不是拿旧内容硬跑。
 */
async function syncStoryText(): Promise<boolean> {
  const text = storyText.value
  if (!episodeId.value || !text.trim()) return false
  if (text === (story.value?.source_text ?? '')) return true
  try {
    const doc = await storyApi.save(episodeId.value, {
      input_type: inputType.value,
      source_text: text,
    })
    story.value = doc
    clearDraft(episodeId.value, 'story-text')
    draftNotice.value = ''
    void loadVersions()
    return true
  } catch {
    return false
  }
}

function generateScript(): Promise<void> {
  return runJob(async () => {
    if (!(await syncStoryText())) {
      toast.warn('无法生成剧本', '故事内容未能保存到服务器，请检查网络后重试')
      throw new Error('story text not synced')
    }
    return storyApi.generate(episodeId.value)
  }, '剧本生成')
}

async function analyzeStory(): Promise<void> {
  // 后端 /story/analyze 是同步接口（模型不可用时用已有剧本兜底派生），
  // 返回 {analysis, story}，直接替换本地状态，不再走 job 轮询。
  if (busy.value) return
  busy.value = true
  try {
    // 同「生成剧本」：先把屏幕上的文本落库，避免分析的是上一次的内容
    await syncStoryText()
    const result = await storyApi.analyze(episodeId.value)
    if (result.story) {
      story.value = result.story
    } else if (story.value) {
      story.value = { ...story.value, analysis: result.analysis }
    }
    const analysis = result.analysis
    if (!analysis || !Object.keys(analysis).length) {
      toast.info('故事分析完成', '未提取到可用内容，请补充故事文本后重试')
    } else if (analysis.raw_summary?.derived_from_script) {
      toast.info('故事分析完成', '模型未返回结构化结果，已按现有剧本派生')
    } else {
      toast.success('故事分析完成', analysis.theme || '已更新分析面板')
    }
  } catch {
    /* api 层已提示（含 trace_id） */
  } finally {
    busy.value = false
  }
}

function useManualTemplate(): void {
  draft.value = JSON.stringify(emptyStoryJson(), null, 2)
  scriptTab.value = 'json'
  toast.info('已填入空白剧本骨架', '可直接编辑后保存')
}

/** 任务重试 / 取消（API.md §10）。 */
async function retryJob(id: string): Promise<void> {
  try {
    const job = await jobsApi.retry(id)
    storyJobId.value = job.id
    store.trackJob(job)
  } catch {
    /* api 层已提示 */
  }
}

async function cancelJob(id: string): Promise<void> {
  try {
    const res = await jobsApi.cancel(id)
    if (res.job) store.trackJob(res.job)
    if (res.note) toast.info('取消请求已提交', res.note)
  } catch {
    /* api 层已提示 */
  }
}

async function validateScript(): Promise<void> {
  const json = parsedDraft.value
  if (!episodeId.value || !json) {
    toast.warn('无法校验', '剧本 JSON 解析失败，请先修正语法')
    return
  }
  try {
    validation.value = await scriptApi.validate(episodeId.value, { story_json: json })
    if (validation.value.valid) toast.success('剧本校验通过')
    else toast.warn('剧本校验未通过', `${validation.value.errors.length} 个错误`)
  } catch {
    /* api 层已提示 */
  }
}

async function saveScript(): Promise<void> {
  const json = parsedDraft.value
  if (!episodeId.value || !json) {
    toast.warn('无法保存', '剧本 JSON 解析失败，请先修正语法')
    return
  }
  saving.value = true
  try {
    script.value = await scriptApi.save(episodeId.value, {
      story_json: json,
      base_version: script.value?.version ?? 0,
    })
    draft.value = JSON.stringify(script.value.story_json, null, 2)
    toast.success('剧本已保存', `版本 ${script.value.version}`)
    await store.refreshAll()
  } catch (err) {
    if (err instanceof ApiError && err.code === 'CHECKPOINT_STALE') {
      notice.value = '检查点已过期：剧本在别处被更新，请刷新后重新编辑。'
    }
    reportError(err, '保存剧本')
  } finally {
    saving.value = false
  }
}

function resetDraft(): void {
  draft.value = JSON.stringify(script.value?.story_json ?? emptyStoryJson(), null, 2)
  validation.value = null
  notice.value = ''
}

/* ---------------- 门控 ---------------- */

async function approve(): Promise<void> {
  gateBusy.value = true
  try {
    await store.resumeGate('approve')
    toast.success('剧本已确认', '工作流将继续推进')
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
    toast.info('已驳回', '工作流将按驳回决策处理')
  } catch (err) {
    handleGateError(err)
  } finally {
    gateBusy.value = false
  }
}

async function edit(instruction: string): Promise<void> {
  gateBusy.value = true
  try {
    await store.resumeGate('edit', { instruction }, script.value?.version)
    toast.info('已提交重做指令', instruction)
  } catch (err) {
    handleGateError(err)
  } finally {
    gateBusy.value = false
  }
}

function handleGateError(err: unknown): void {
  if (err instanceof ApiError && err.code === 'CHECKPOINT_STALE') {
    notice.value = '检查点已过期（CHECKPOINT_STALE），请点击「刷新状态」后重试。'
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
      approve-text="确认剧本，进入角色与画风"
      description="确认故事圣经、节拍表与分镜脚本后，工作流会继续到角色与画风阶段。"
      @approve="approve"
      @reject="reject"
      @edit="edit"
      @refresh="store.refreshLive()"
    >
      <div class="row-wrap tiny dim">
        <span>剧本版本 <span class="mono">{{ script?.version ?? '—' }}</span></span>
        <span>分镜数 <span class="mono">{{ storyJson?.shots?.length ?? 0 }}</span></span>
        <span>总时长 <span class="mono">{{ formatDuration((storyJson?.shots ?? []).reduce((s, x) => s + (x.duration_ms ?? 0), 0)) }}</span></span>
      </div>
    </GateCard>

    <div class="two-col">
      <section class="card">
        <div class="card-head">
          <h2 class="title-md">故事输入</h2>
          <span v-if="story?.version" class="badge mono">v{{ story.version }}</span>
        </div>
        <div class="card-body col" style="gap: 14px">
          <div class="field">
            <label class="label" for="story-type">输入方式</label>
            <select id="story-type" v-model="inputType" class="select">
              <option v-for="(label, key) in STORY_INPUT_TYPE_LABEL" :key="key" :value="key">
                {{ label }}
              </option>
            </select>
          </div>

          <div class="field">
            <label class="label" for="story-text">故事内容</label>
            <textarea
              id="story-text"
              v-model="storyText"
              class="textarea"
              rows="8"
              :class="{ invalid: overLimit }"
              placeholder="写下一句话创意或完整故事：主角、处境、冲突、结局走向…"
            />
            <div class="row-between">
              <span class="hint">长度上限 {{ STORY_TEXT_MAX }} 字（API.md §4）</span>
              <span class="row" style="gap: 8px">
                <!-- FR-003：自动保存状态，来自真实的写操作活动 -->
                <span class="autosave-state" :class="`is-${autosave.state.value}`">
                  {{ AUTO_SAVE_LABEL[autosave.state.value] ?? autosave.state.value }}
                </span>
                <span class="mono tiny" :class="overLimit ? 'over' : 'dim'">
                  {{ storyText.length }} / {{ STORY_TEXT_MAX }}
                </span>
              </span>
            </div>

            <!-- FR-003：本地草稿与服务器版本不一致时提示取舍，不静默覆盖 -->
            <div v-if="autosave.conflict.value" class="draft-conflict">
              <span class="draft-msg">{{ draftNotice }}</span>
              <span class="row" style="gap: 6px">
                <button type="button" class="btn btn-sm btn-primary" @click="acceptLocalDraft">
                  用本地草稿
                </button>
                <button type="button" class="btn btn-sm btn-ghost" @click="discardLocalDraft">
                  丢弃本地草稿
                </button>
              </span>
            </div>
            <!-- FR-010：长文本必须显示字符数与预计生成成本（与真实调用同一份提示词） -->
            <div v-if="storyText.trim() && !overLimit" class="estimate-bar">
              <template v-if="estimating && !estimate">
                <span class="dim tiny">正在估算成本…</span>
              </template>
              <template v-else-if="estimate">
                <span class="estimate-item">
                  <span class="dim">预计生成成本</span>
                  <b class="mono">{{ estimateLabel }}</b>
                </span>
                <span class="estimate-item">
                  <span class="dim">计费字符</span>
                  <span class="mono">{{ estimate.billing_chars }}</span>
                  <span class="dim tiny">
                    （输入 {{ estimate.source_chars }} ＋ 提示词 {{ estimate.prompt_chars }}）
                  </span>
                </span>
                <span v-if="estimate.declared" class="estimate-item dim tiny">
                  {{ estimate.billing_unit }} · 单价
                  {{ formatCents(estimate.unit_price_cents) }}
                </span>
                <span
                  v-if="estimate.long_text"
                  class="estimate-warn"
                  :title="`超过 ${estimate.long_text_threshold} 字视为长文本`"
                >
                  长文本，成本随长度上升
                </span>
                <span v-else-if="!estimate.declared" class="estimate-warn">
                  该供应商未声明单价，实际可能产生费用
                </span>
              </template>
            </div>
          </div>

          <div class="row-wrap">
            <button type="button" class="btn" :disabled="saving || overLimit" @click="saveStory">
              {{ saving ? '保存中…' : '保存故事' }}
            </button>
            <button
              type="button"
              class="btn btn-primary"
              :disabled="busy || !storyText.trim() || overLimit"
              @click="generateScript"
            >
              生成剧本
            </button>
            <button
              type="button"
              class="btn btn-ghost"
              :disabled="busy || !storyText.trim() || overLimit"
              @click="analyzeStory"
            >
              故事分析
            </button>
          </div>

          <JobInline
            v-if="storyJob"
            :job="storyJob"
            :busy="busy"
            @retry="retryJob"
            @cancel="cancelJob"
          />

          <div v-if="story?.analysis" class="analysis">
            <span class="section-title">故事分析</span>
            <dl class="kv">
              <template v-if="story.analysis.theme">
                <dt>主题</dt>
                <dd>{{ story.analysis.theme }}</dd>
              </template>
              <template v-if="story.analysis.characters?.length">
                <dt>角色</dt>
                <dd>{{ story.analysis.characters.join('、') }}</dd>
              </template>
              <template v-if="story.analysis.relationships?.length">
                <dt>人物关系</dt>
                <dd>
                  {{
                    story.analysis.relationships
                      .map((r) => `${r.from ?? r.from_ ?? '?'}→${r.to ?? '?'}${r.relation ? '·' + r.relation : ''}`)
                      .join('；')
                  }}
                </dd>
              </template>
              <template v-if="story.analysis.scenes?.length">
                <dt>场景</dt>
                <dd>{{ story.analysis.scenes.join('、') }}</dd>
              </template>
              <template v-if="story.analysis.beats?.length">
                <dt>节拍</dt>
                <dd>{{ story.analysis.beats.join(' / ') }}</dd>
              </template>
              <template v-if="conflictText">
                <dt>冲突目标</dt>
                <dd>{{ conflictText }}</dd>
              </template>
            </dl>
          </div>

          <SkeletonBlock v-if="loadingStory && !story" :rows="2" />
        </div>
      </section>

      <section class="card">
        <div class="card-head">
          <h2 class="title-md">故事版本</h2>
          <span class="mono tiny dim">{{ versions.length }} 个版本</span>
        </div>
        <div class="card-body">
          <EmptyState
            v-if="!versions.length"
            compact
            icon="◷"
            title="暂无版本记录"
            description="保存故事后，每次修改都会留下版本。"
          />
          <ul v-else class="version-list">
            <li v-for="v in versions" :key="v.version" class="version-item">
              <span class="badge mono">v{{ v.version }}</span>
              <span class="tiny dim">{{ STORY_INPUT_TYPE_LABEL[v.input_type ?? ''] ?? v.input_type }}</span>
              <span class="mono tiny dim-2 grow ellipsis">{{ formatDateTime(v.created_at) }}</span>
              <button
                type="button"
                class="btn btn-sm btn-ghost"
                @click="storyText = v.source_text ?? ''; inputType = v.input_type ?? 'idea'"
              >
                载入编辑区
              </button>
            </li>
          </ul>
        </div>
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <div class="row">
          <h2 class="title-md">剧本</h2>
          <span v-if="script" class="badge mono">v{{ script.version }}</span>
        </div>
        <div class="row-wrap">
          <div class="tabs">
            <button
              type="button"
              class="tab"
              :class="{ active: scriptTab === 'outline' }"
              @click="scriptTab = 'outline'"
            >
              结构视图
            </button>
            <button
              type="button"
              class="tab"
              :class="{ active: scriptTab === 'shots' }"
              @click="scriptTab = 'shots'"
            >
              分镜脚本
            </button>
            <button
              type="button"
              class="tab"
              :class="{ active: scriptTab === 'json' }"
              @click="scriptTab = 'json'"
            >
              JSON 编辑
            </button>
          </div>
          <button type="button" class="btn btn-sm btn-ghost" @click="loadScript">重新加载</button>
        </div>
      </div>

      <div class="card-body col" style="gap: 14px">
        <ErrorState v-if="scriptError" :error="scriptError" context="加载剧本" compact @retry="loadScript" />

        <SkeletonBlock v-else-if="loadingScript && !script" :rows="5" />

        <EmptyState
          v-else-if="!script"
          icon="✎"
          title="剧本还没有生成"
          description="保存故事后点击「生成剧本」，或直接使用手工录入模板。"
        >
          <button type="button" class="btn btn-primary btn-sm" :disabled="busy" @click="generateScript">
            生成剧本
          </button>
          <button type="button" class="btn btn-sm btn-ghost" @click="useManualTemplate">
            手工录入模板
          </button>
        </EmptyState>

        <template v-else>
          <!-- 结构视图 -->
          <div v-if="scriptTab === 'outline'" class="outline">
            <div class="outline-block">
              <span class="section-title">故事圣经</span>
              <dl class="kv">
                <dt>一句话梗概</dt>
                <dd>{{ script.story_json.story_bible?.logline || '—' }}</dd>
                <dt>主题</dt>
                <dd>{{ script.story_json.story_bible?.theme || '—' }}</dd>
                <dt>基调</dt>
                <dd>{{ script.story_json.story_bible?.tone || '—' }}</dd>
                <dt>世界观</dt>
                <dd>{{ script.story_json.story_bible?.setting || '—' }}</dd>
              </dl>
            </div>

            <div class="outline-block">
              <span class="section-title">冲突</span>
              <dl class="kv">
                <dt>目标</dt>
                <dd>{{ script.story_json.story_bible?.conflict?.goal || '—' }}</dd>
                <dt>阻碍</dt>
                <dd>{{ script.story_json.story_bible?.conflict?.obstacle || '—' }}</dd>
                <dt>代价</dt>
                <dd>{{ script.story_json.story_bible?.conflict?.stakes || '—' }}</dd>
              </dl>
            </div>

            <div class="outline-block">
              <span class="section-title">角色（{{ script.story_json.story_bible?.characters?.length ?? 0 }}）</span>
              <div v-if="script.story_json.story_bible?.characters?.length" class="char-grid">
                <div v-for="ch in script.story_json.story_bible.characters" :key="ch.name" class="char-mini">
                  <div class="row-between">
                    <strong>{{ ch.name }}</strong>
                    <span class="tiny dim">{{ ch.age_range || '—' }}</span>
                  </div>
                  <p class="tiny dim">{{ ch.identity || '—' }}</p>
                  <p class="tiny muted">{{ ch.appearance || '' }}</p>
                </div>
              </div>
              <p v-else class="tiny dim">剧本未包含角色。</p>
            </div>

            <div class="outline-block">
              <span class="section-title">场景（{{ script.story_json.story_bible?.scenes?.length ?? 0 }}）</span>
              <ul class="scene-list">
                <li v-for="scene in script.story_json.story_bible?.scenes ?? []" :key="scene.scene_no">
                  <span class="badge mono">#{{ scene.scene_no }}</span>
                  <span class="small">{{ scene.location || '—' }}</span>
                  <span class="tiny dim">{{ scene.time_of_day || '' }}</span>
                  <span class="tiny muted grow ellipsis">{{ scene.summary || '' }}</span>
                </li>
              </ul>
            </div>

            <div class="outline-block">
              <span class="section-title">节拍表（{{ script.story_json.beat_sheet?.length ?? 0 }}）</span>
              <ul class="scene-list">
                <li v-for="beat in script.story_json.beat_sheet ?? []" :key="beat.beat_no">
                  <span class="badge mono">B{{ beat.beat_no }}</span>
                  <span class="small">{{ beat.name || '节拍' }}</span>
                  <span class="tiny muted grow ellipsis">{{ beat.summary || '' }}</span>
                  <span class="tiny dim mono">镜头 {{ (beat.shots ?? []).join(',') || '—' }}</span>
                </li>
              </ul>
            </div>
          </div>

          <!-- 分镜脚本 -->
          <div v-else-if="scriptTab === 'shots'" class="shots-table-wrap">
            <table class="table">
              <thead>
                <tr>
                  <th style="width: 56px">镜号</th>
                  <th style="width: 80px">时长</th>
                  <th style="width: 90px">构图</th>
                  <th style="width: 80px">运镜</th>
                  <th style="width: 130px">角色</th>
                  <th>动作 / 台词</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="shot in script.story_json.shots ?? []" :key="shot.shot_no">
                  <td class="mono">#{{ String(shot.shot_no).padStart(3, '0') }}</td>
                  <td class="mono">{{ formatDuration(shot.duration_ms) }}</td>
                  <td>
                    <span class="chip" :class="{ 'chip-active': COMPOSITION_SET.has(shot.composition ?? '') }">
                      {{ shot.composition || '—' }}
                    </span>
                  </td>
                  <td>
                    <span class="chip" :class="{ 'chip-active': CAMERA_SET.has(shot.camera_movement ?? '') }">
                      {{ shot.camera_movement || '—' }}
                    </span>
                  </td>
                  <td class="tiny dim">{{ (shot.characters ?? []).join('、') || '—' }}</td>
                  <td>
                    <div class="pre-wrap tiny">{{ shot.action || '—' }}</div>
                    <div v-if="shot.dialogue?.length" class="dialogue">
                      <span v-for="(line, i) in shot.dialogue" :key="i" class="dialogue-line tiny">
                        <span class="dialogue-who">{{ line.character }}</span>：{{ line.text }}
                      </span>
                    </div>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="!script.story_json.shots?.length" class="tiny dim" style="padding: 12px">
              剧本暂无分镜，可在「JSON 编辑」中补充或重新生成。
            </p>
          </div>

          <!-- JSON 编辑 -->
          <div v-else class="json-editor">
            <textarea
              v-model="draft"
              class="textarea mono-input"
              rows="18"
              spellcheck="false"
              placeholder="剧本 JSON（StoryBible + BeatSheet + ShotScript）"
            />
            <div class="row-between">
              <span class="tiny" :class="jsonInvalid ? 'over' : 'dim'">
                {{ jsonInvalid ? 'JSON 语法错误，无法校验或保存' : '结构对齐 API.md §4.1' }}
              </span>
              <div class="row">
                <button type="button" class="btn btn-sm btn-ghost" @click="resetDraft">还原</button>
                <button
                  type="button"
                  class="btn btn-sm"
                  :disabled="jsonInvalid"
                  @click="validateScript"
                >
                  校验
                </button>
                <button
                  type="button"
                  class="btn btn-sm btn-primary"
                  :disabled="saving || jsonInvalid"
                  @click="saveScript"
                >
                  {{ saving ? '保存中…' : '保存剧本' }}
                </button>
              </div>
            </div>

            <div v-if="validation" class="validation" :class="validation.valid ? 'ok' : 'bad'">
              <div class="row-between">
                <span class="badge" :class="validation.valid ? 'badge-ok' : 'badge-danger'">
                  {{ validation.valid ? '校验通过' : '校验未通过' }}
                </span>
                <span class="mono tiny dim">
                  {{ validation.errors.length }} 错误 · {{ validation.warnings.length }} 警告
                </span>
              </div>
              <ul v-if="validation.errors.length" class="validation-list">
                <li v-for="(e, i) in validation.errors" :key="i">
                  <span class="mono tiny">{{ (e.loc ?? []).join('.') || '整体' }}</span>
                  <span class="tiny">{{ e.msg }}</span>
                </li>
              </ul>
              <ul v-if="validation.warnings.length" class="validation-list warn">
                <li v-for="(w, i) in validation.warnings" :key="i">
                  <span class="mono tiny">{{ (w.loc ?? []).join('.') || '整体' }}</span>
                  <span class="tiny">{{ w.msg }}</span>
                </li>
              </ul>
            </div>
          </div>
        </template>
      </div>
    </section>

    <section class="card">
      <div class="card-head">
        <h2 class="title-md">工作流日志</h2>
        <span class="mono tiny dim">{{ logs.length }} 条</span>
      </div>
      <div class="card-body">
        <EmptyState v-if="!logs.length" compact icon="≡" title="暂无节点日志" description="工作流执行后，关键节点会写入脱敏日志。" />
        <ul v-else class="log-list">
          <li v-for="(log, i) in logs" :key="i" class="log-item">
            <span class="badge mono">{{ log.node ?? '—' }}</span>
            <span
              class="badge"
              :class="log.level === 'ERROR' ? 'badge-danger' : log.level === 'WARNING' ? 'badge-warn' : 'badge-info'"
            >
              {{ log.level ?? 'INFO' }}
            </span>
            <span class="tiny muted grow pre-wrap">{{ log.message }}</span>
            <span class="mono tiny dim-2">{{ formatDateTime(log.created_at) }}</span>
          </li>
        </ul>
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
  grid-template-columns: 1.35fr 1fr;
  gap: var(--gap);
}
@media (max-width: 1080px) {
  .two-col {
    grid-template-columns: 1fr;
  }
}
.over {
  color: var(--danger);
}

/* FR-010 成本预估条 */
.estimate-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  margin-top: 6px;
  padding: 6px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm, 6px);
  background: var(--bg-soft, transparent);
  font-size: 12px;
}
.estimate-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.estimate-warn {
  margin-left: auto;
  padding: 1px 7px;
  border-radius: 999px;
  border: 1px solid var(--warn, #b8860b);
  color: var(--warn, #b8860b);
  font-size: 11px;
  white-space: nowrap;
}

/* FR-003 自动保存状态与草稿冲突 */
.autosave-state {
  font-size: 11px;
  color: var(--dim);
}
.autosave-state.is-saved {
  color: var(--ok, #34d399);
}
.autosave-state.is-error {
  color: var(--danger);
}
.autosave-state.is-offline {
  color: var(--warn, #b8860b);
}
.draft-conflict {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
  padding: 7px 10px;
  border: 1px solid var(--warn, #b8860b);
  border-radius: var(--radius-sm, 6px);
  background: rgba(184, 134, 11, 0.08);
}
.draft-msg {
  font-size: 12px;
  color: var(--text-1);
}
.analysis {
  border-top: 1px solid var(--line);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.version-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  max-height: 260px;
  overflow: auto;
}
.version-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
}
.tabs {
  display: flex;
  gap: 3px;
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 2px;
}
.tab {
  background: transparent;
  border: 0;
  padding: 5px 11px;
  border-radius: var(--radius-xs);
  font-size: 12.5px;
  color: var(--text-2);
  cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.tab:hover {
  color: var(--text-0);
}
.tab.active {
  background: var(--accent-soft);
  color: var(--accent);
}
.outline {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: var(--gap);
}
.outline-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-1);
}
.char-grid {
  display: grid;
  gap: 8px;
}
.char-mini {
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--panel-2);
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.scene-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.scene-list li {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border-radius: var(--radius-xs);
  background: rgba(255, 255, 255, 0.025);
}
.shots-table-wrap {
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  max-height: 520px;
}
.dialogue {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 5px;
  padding-left: 8px;
  border-left: 2px solid var(--line-accent);
}
.dialogue-who {
  color: var(--accent);
}
.json-editor {
  display: flex;
  flex-direction: column;
  gap: 10px;
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
  gap: 4px;
  max-height: 160px;
  overflow: auto;
}
.validation-list li {
  display: flex;
  gap: 8px;
}
.validation-list .mono {
  color: var(--warn);
  flex: 0 0 auto;
}
.validation-list.warn .mono {
  color: var(--text-2);
}
.log-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
  max-height: 300px;
  overflow: auto;
}
.log-item {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 6px 9px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.025);
  border: 1px solid var(--line);
}
</style>
