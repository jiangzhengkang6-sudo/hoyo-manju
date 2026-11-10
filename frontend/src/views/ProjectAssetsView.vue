<script setup lang="ts">
/**
 * 项目级「角色与画风资产库」（PRD §5.1-6）。
 *
 * 与 `WorkspaceAssetsView`（跨项目汇总、只读）分工不同：这里是**某个项目内部**的
 * 资产主控台，可新建/编辑/归档角色、生成参考图、新建/编辑画风、设为项目默认画风。
 *
 * 过滤、统计、表单校验等纯逻辑放在 `utils/projectAssets.ts`，本文件只负责请求编排与 DOM。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { charactersApi, fetchCharacters, fetchStyles, stylesApi } from '@/api/characters'
import { fetchVoices } from '@/api/audio'
import { reportError } from '@/api/client'
import { ApiError, toApiError } from '@/api/errors'
import { characterStatusLabel, characterStatusTone, isColorLike, normalizePalette } from '@/api/workspace'
import { useJobWatcher } from '@/composables/useJobWatcher'
import { useProjectStore } from '@/stores/project'
import { useToastStore } from '@/stores/toast'
import { relativeTime, truncate } from '@/utils/format'
import {
  assetCounts,
  characterReadiness,
  filterCharacters,
  filterStyles,
  formatPaletteInput,
  parsePaletteInput,
  showCharacters,
  showStyles,
  speedLabel,
  validateCharacterForm,
  validateStyleForm,
  visibleCount,
  voiceLabel,
  type ProjectAssetKind,
} from '@/utils/projectAssets'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import JobInline from '@/components/JobInline.vue'
import MediaThumb from '@/components/MediaThumb.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import StatTile from '@/components/StatTile.vue'
import type { Character, CharacterAssetKind, Job, StyleAsset, Voice } from '@/types'

const props = defineProps<{ projectId: string }>()

const store = useProjectStore()
const toast = useToastStore()

const KIND_TABS: { value: ProjectAssetKind; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'character', label: '角色' },
  { value: 'style', label: '画风' },
]

/** API.md §5：角色参考图的资产类型。 */
const ASSET_KIND_OPTIONS: { value: CharacterAssetKind; label: string }[] = [
  { value: 'setting', label: '设定图' },
  { value: 'front', label: '正面' },
  { value: 'half_body', label: '半身' },
  { value: 'expression', label: '表情' },
]

const characters = ref<Character[]>([])
const styles = ref<StyleAsset[]>([])
const voices = ref<Voice[]>([])
const kind = ref<ProjectAssetKind>('')
const search = ref('')
const loading = ref(false)
const loaded = ref(false)
const busy = ref(false)
const loadError = ref<ApiError | null>(null)

const projectName = computed(() => store.current?.name ?? '项目')
const defaultStyleId = computed(() => store.current?.default_style_id ?? null)

/* ---------------- 任务观察（生成参考图是异步任务） ---------------- */

const watchedIds = ref<string[]>([])
const watcher = useJobWatcher(() => watchedIds.value, { intervalMs: 2500 })
const jobList = computed<Job[]>(() =>
  watchedIds.value.map((id) => watcher.jobs.value[id]).filter((j): j is Job => Boolean(j)),
)

function trackJob(job: Job | null | undefined): void {
  if (!job?.id) return
  watcher.track(job)
  if (!watchedIds.value.includes(job.id)) watchedIds.value = [job.id, ...watchedIds.value]
}

/** 生成任务结束后需要重新拉取角色（主参考图变了）。 */
watch(
  () => jobList.value.filter((j) => j.status === 'SUCCEEDED').length,
  (now, before) => {
    if (now > (before ?? 0)) void loadAssets()
  },
)

/* ---------------- 加载 ---------------- */

async function loadAssets(): Promise<void> {
  loading.value = true
  loadError.value = null
  try {
    const [chars, stys] = await Promise.all([
      fetchCharacters(props.projectId),
      fetchStyles(props.projectId),
    ])
    characters.value = chars
    styles.value = stys
  } catch (err) {
    loadError.value = toApiError(err)
  } finally {
    loading.value = false
    loaded.value = true
  }
}

async function loadVoices(): Promise<void> {
  // 音色列表拿不到不影响主流程（只是标签退化成 id），所以不弹错
  voices.value = await fetchVoices(props.projectId).catch(() => [])
}

async function loadAll(): Promise<void> {
  await Promise.all([store.loadProject(props.projectId), loadAssets(), loadVoices()])
}

onMounted(loadAll)
watch(() => props.projectId, loadAll)

/* ---------------- 派生数据 ---------------- */

const visibleCharacters = computed(() => filterCharacters(characters.value, search.value))
const visibleStyles = computed(() => filterStyles(styles.value, search.value))
const counts = computed(() => assetCounts(characters.value, styles.value, defaultStyleId.value))
const shownCount = computed(() => visibleCount(kind.value, visibleCharacters.value, visibleStyles.value))
const filtering = computed(() => Boolean(search.value.trim() || kind.value))
const showChars = computed(() => showCharacters(kind.value))
const showStys = computed(() => showStyles(kind.value))

function resetFilters(): void {
  search.value = ''
  kind.value = ''
}

function paletteOf(style: StyleAsset): string[] {
  return normalizePalette(style.palette)
}
function paletteColors(style: StyleAsset): string[] {
  return paletteOf(style).filter(isColorLike)
}
function paletteTexts(style: StyleAsset): string[] {
  return paletteOf(style).filter((v) => !isColorLike(v))
}

/* ---------------- 角色表单 ---------------- */

const showCharacterForm = ref(false)
const editingCharacterId = ref<string | null>(null)
const characterForm = reactive({ name: '', identity: '', appearance: '', outfit: '' })
const characterFormError = ref('')

function openCreateCharacter(): void {
  editingCharacterId.value = null
  Object.assign(characterForm, { name: '', identity: '', appearance: '', outfit: '' })
  characterFormError.value = ''
  showCharacterForm.value = true
}

function openEditCharacter(character: Character): void {
  editingCharacterId.value = character.id
  Object.assign(characterForm, {
    name: character.name ?? '',
    identity: character.identity ?? '',
    appearance: character.appearance ?? '',
    outfit: character.outfit ?? '',
  })
  characterFormError.value = ''
  showCharacterForm.value = true
}

async function submitCharacter(): Promise<void> {
  const error = validateCharacterForm(characterForm.name)
  if (error) {
    characterFormError.value = error
    return
  }
  busy.value = true
  characterFormError.value = ''
  try {
    const body = {
      name: characterForm.name.trim(),
      identity: characterForm.identity.trim(),
      appearance: characterForm.appearance.trim(),
      outfit: characterForm.outfit.trim(),
    }
    if (editingCharacterId.value) {
      const updated = await charactersApi.patch(editingCharacterId.value, body)
      characters.value = characters.value.map((c) => (c.id === updated.id ? updated : c))
      toast.success('角色已更新', updated.name)
    } else {
      const created = await charactersApi.create(props.projectId, body)
      characters.value = [...characters.value, created]
      toast.success('角色已创建', created.name)
    }
    showCharacterForm.value = false
  } catch (err) {
    characterFormError.value = err instanceof ApiError ? err.message : '保存失败'
    reportError(err, '保存角色')
  } finally {
    busy.value = false
  }
}

async function archiveCharacter(character: Character): Promise<void> {
  if (!window.confirm(`确定归档角色「${character.name}」？归档后不再参与后续生成。`)) return
  busy.value = true
  try {
    await charactersApi.archive(character.id)
    characters.value = characters.value.filter((c) => c.id !== character.id)
    toast.info('角色已归档', character.name)
  } catch (err) {
    reportError(err, '归档角色')
  } finally {
    busy.value = false
  }
}

/* ---------------- 生成参考图 ---------------- */

const showGenerate = ref(false)
const generateTarget = ref<Character | null>(null)
const generateForm = reactive({
  asset_kinds: ['front'] as CharacterAssetKind[],
  count_per_kind: 2,
})

function openGenerate(character: Character): void {
  generateTarget.value = character
  generateForm.asset_kinds = ['front']
  generateForm.count_per_kind = 2
  showGenerate.value = true
}

function toggleAssetKind(value: CharacterAssetKind): void {
  const list = generateForm.asset_kinds
  generateForm.asset_kinds = list.includes(value)
    ? list.filter((k) => k !== value)
    : [...list, value]
}

async function submitGenerate(): Promise<void> {
  const target = generateTarget.value
  if (!target) return
  if (!generateForm.asset_kinds.length) {
    toast.warn('请至少选择一种资产类型')
    return
  }
  busy.value = true
  try {
    const job = await charactersApi.generate(target.id, {
      asset_kinds: [...generateForm.asset_kinds],
      count_per_kind: generateForm.count_per_kind,
    })
    trackJob(job)
    showGenerate.value = false
    toast.info('参考图生成任务已提交', job.id)
  } catch (err) {
    reportError(err, '提交参考图生成')
  } finally {
    busy.value = false
  }
}

/* ---------------- 画风表单 ---------------- */

const showStyleForm = ref(false)
const editingStyleId = ref<string | null>(null)
const styleForm = reactive({
  name: '',
  description: '',
  paletteText: '',
  camera_style: '',
  lighting_style: '',
})
const styleFormError = ref('')

function openCreateStyle(): void {
  editingStyleId.value = null
  Object.assign(styleForm, {
    name: '',
    description: '',
    paletteText: '',
    camera_style: '',
    lighting_style: '',
  })
  styleFormError.value = ''
  showStyleForm.value = true
}

function openEditStyle(style: StyleAsset): void {
  editingStyleId.value = style.id
  Object.assign(styleForm, {
    name: style.name ?? '',
    description: style.description ?? '',
    paletteText: formatPaletteInput(style.palette),
    camera_style: style.camera_style ?? '',
    lighting_style: style.lighting_style ?? '',
  })
  styleFormError.value = ''
  showStyleForm.value = true
}

async function submitStyle(): Promise<void> {
  const error = validateStyleForm(styleForm.name)
  if (error) {
    styleFormError.value = error
    return
  }
  busy.value = true
  styleFormError.value = ''
  try {
    const body = {
      name: styleForm.name.trim(),
      description: styleForm.description.trim(),
      palette: parsePaletteInput(styleForm.paletteText),
      camera_style: styleForm.camera_style.trim(),
      lighting_style: styleForm.lighting_style.trim(),
    }
    if (editingStyleId.value) {
      const updated = await stylesApi.patch(editingStyleId.value, body)
      styles.value = styles.value.map((s) => (s.id === updated.id ? updated : s))
      toast.success('画风已更新', updated.name)
    } else {
      const created = await stylesApi.create(props.projectId, body)
      styles.value = [...styles.value, created]
      toast.success('画风已创建', created.name)
    }
    showStyleForm.value = false
  } catch (err) {
    styleFormError.value = err instanceof ApiError ? err.message : '保存失败'
    reportError(err, '保存画风')
  } finally {
    busy.value = false
  }
}

/** 设为项目默认画风；后端会顺带把已有分镜标过期，必须如实告知影响面。 */
async function applyStyle(style: StyleAsset): Promise<void> {
  busy.value = true
  try {
    const res = await stylesApi.apply(style.id)
    const affected = res.affected_shots ?? 0
    styles.value = styles.value.map((s) => ({ ...s, is_default: s.id === style.id }))
    if (store.current) store.current.default_style_id = style.id
    toast.success(
      '画风已设为项目默认',
      affected ? `${affected} 个分镜已标记为需重新生成` : '暂无分镜受影响',
    )
  } catch (err) {
    reportError(err, '应用画风')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">角色与画风资产库</h1>
        <p class="muted small">
          「{{ projectName }}」的角色与画风主控台：新建、编辑、归档角色，生成参考图，维护项目画风。
          剧集内的角色阶段页负责逐镜确认，这里负责资产本身。
        </p>
      </div>
      <div class="row-wrap">
        <RouterLink class="btn btn-ghost" :to="{ name: 'project-detail', params: { projectId } }">
          返回项目
        </RouterLink>
        <RouterLink class="btn btn-ghost" :to="{ name: 'workspace-assets' }">
          跨项目资产库
        </RouterLink>
        <button type="button" class="btn" :disabled="loading" @click="loadAll">
          {{ loading ? '加载中…' : '刷新' }}
        </button>
      </div>
    </header>

    <section class="stat-row">
      <StatTile label="角色" :value="counts.characters" hint="项目内全部角色" />
      <StatTile label="画风" :value="counts.styles" hint="可切换的项目画风" />
      <StatTile
        label="已确认参考图"
        :value="counts.withReference"
        :tone="counts.withReference < counts.characters ? 'warn' : 'ok'"
        hint="角色主参考图"
      />
      <StatTile
        label="已绑定音色"
        :value="counts.withVoice"
        :tone="counts.withVoice < counts.characters ? 'warn' : 'ok'"
        hint="FR-050 配音前置条件"
      />
    </section>

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
          <label class="label" for="pa-search">名称搜索</label>
          <input
            id="pa-search"
            v-model="search"
            class="input"
            type="search"
            placeholder="角色名、身份、外貌、画风名或色板…"
          />
          <span class="hint">本地过滤，输入即生效</span>
        </div>
        <button
          type="button"
          class="btn btn-ghost tb-reset"
          :disabled="!filtering"
          @click="resetFilters"
        >
          重置筛选
        </button>
        <div class="row-wrap tb-actions">
          <button type="button" class="btn btn-sm" :disabled="busy" @click="openCreateStyle">
            新建画风
          </button>
          <button type="button" class="btn btn-sm btn-primary" :disabled="busy" @click="openCreateCharacter">
            新建角色
          </button>
        </div>
      </div>

      <div class="row-wrap tb-summary">
        <span class="badge">角色 {{ visibleCharacters.length }} · 画风 {{ visibleStyles.length }}</span>
        <span v-if="filtering" class="tiny dim">已过滤（共 {{ counts.characters }} 角色 / {{ counts.styles }} 画风）</span>
        <span v-if="loading" class="tiny dim">加载中…</span>
      </div>
    </section>

    <section v-if="jobList.length" class="card card-pad col">
      <div class="row-between sec-head">
        <span class="section-title">生成任务</span>
        <span class="mono tiny dim">{{ jobList.length }}</span>
      </div>
      <JobInline
        v-for="job in jobList"
        :key="job.id"
        :job="job"
        :busy="busy"
      />
    </section>

    <SkeletonBlock v-if="loading && !loaded" variant="cards" :rows="6" />

    <ErrorState v-else-if="loadError" :error="loadError" context="加载资产库" @retry="loadAll" />

    <EmptyState
      v-else-if="!shownCount"
      icon="◇"
      :title="filtering ? '没有匹配的资产' : '这个项目还没有角色或画风'"
      :description="
        filtering
          ? '换个关键词或类型再试试。'
          : '创建角色并生成参考图后，剧本与分镜生成会用到这些资产。'
      "
    >
      <button v-if="filtering" type="button" class="btn btn-primary" @click="resetFilters">
        重置筛选
      </button>
      <button v-else type="button" class="btn btn-primary" @click="openCreateCharacter">
        新建角色
      </button>
    </EmptyState>

    <template v-else>
      <!-- 角色 -->
      <section v-if="showChars && visibleCharacters.length" class="col">
        <div class="row-between sec-head">
          <span class="section-title">角色</span>
          <span class="mono tiny dim">{{ visibleCharacters.length }}</span>
        </div>

        <div class="grid-cards">
          <article v-for="character in visibleCharacters" :key="character.id" class="card character-card">
            <div class="cc-thumb">
              <MediaThumb :media-id="character.primary_media_id" :alt="character.name" />
            </div>

            <div class="cc-body">
              <div class="cc-head">
                <span class="cc-name ellipsis" :title="character.name">{{ character.name }}</span>
                <span class="badge" :class="characterStatusTone(character.status)">
                  {{ characterStatusLabel(character.status) }}
                </span>
              </div>

              <div class="row-wrap cc-badges">
                <span class="badge" :class="character.voice_id ? 'badge-info' : 'badge-warn'">
                  {{ voiceLabel(character, voices) }}
                </span>
                <span class="badge badge-muted">{{ speedLabel(character) }}</span>
                <span v-if="character.is_main === false" class="badge badge-muted">配角</span>
              </div>

              <p v-if="character.identity" class="cc-identity small" :title="character.identity">
                {{ character.identity }}
              </p>
              <p v-else class="cc-identity small dim-2">未填写身份</p>

              <p v-if="character.appearance" class="cc-appearance tiny dim">
                {{ truncate(character.appearance, 56) }}
              </p>

              <!-- 缺什么就说清楚，而不是让用户在生成时才撞上 -->
              <p
                v-if="!characterReadiness(character).ready"
                class="tiny warn-text"
              >
                待完善：{{ characterReadiness(character).missing.join('、') }}
              </p>

              <div class="row-wrap cc-actions">
                <button type="button" class="btn btn-sm" :disabled="busy" @click="openGenerate(character)">
                  生成参考图
                </button>
                <button type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="openEditCharacter(character)">
                  编辑
                </button>
                <button type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="archiveCharacter(character)">
                  归档
                </button>
              </div>

              <div class="cc-foot mono tiny dim-2">
                <span :title="character.id">{{ truncate(character.id, 18) }}</span>
                <span>{{ relativeTime(character.created_at) }}</span>
              </div>
            </div>
          </article>
        </div>
      </section>

      <!-- 画风 -->
      <section v-if="showStys && visibleStyles.length" class="col">
        <div class="row-between sec-head">
          <span class="section-title">画风</span>
          <span class="mono tiny dim">{{ visibleStyles.length }}</span>
        </div>

        <div class="grid-cards">
          <article v-for="style in visibleStyles" :key="style.id" class="card style-card">
            <div class="sc-head">
              <span class="sc-name ellipsis" :title="style.name ?? style.id">
                {{ style.name ?? '未命名画风' }}
              </span>
              <span v-if="style.is_default" class="badge badge-accent">项目默认</span>
            </div>

            <div class="row-wrap sc-badges">
              <span v-if="style.version != null" class="badge badge-muted mono">v{{ style.version }}</span>
              <span class="badge" :class="characterStatusTone(style.status)">
                {{ characterStatusLabel(style.status) }}
              </span>
            </div>

            <p v-if="style.description" class="sc-desc small">{{ style.description }}</p>

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

            <div class="row-wrap sc-actions">
              <button
                type="button"
                class="btn btn-sm"
                :disabled="busy || style.is_default"
                @click="applyStyle(style)"
              >
                {{ style.is_default ? '当前默认' : '设为项目默认' }}
              </button>
              <button type="button" class="btn btn-sm btn-ghost" :disabled="busy" @click="openEditStyle(style)">
                编辑
              </button>
            </div>

            <div class="sc-foot mono tiny dim-2">
              <span>{{ relativeTime(style.created_at) }}</span>
            </div>
          </article>
        </div>
      </section>

      <p v-if="showChars && !visibleCharacters.length && showStys && visibleStyles.length" class="tiny dim">
        当前筛选下没有匹配的角色。
      </p>
      <p v-if="showStys && !visibleStyles.length && showChars && visibleCharacters.length" class="tiny dim">
        当前筛选下没有匹配的画风。
      </p>
    </template>

    <!-- 新建 / 编辑角色 -->
    <ModalDialog
      :open="showCharacterForm"
      :title="editingCharacterId ? '编辑角色' : '新建角色'"
      @close="showCharacterForm = false"
    >
      <div class="col form-col">
        <div class="field">
          <label class="label" for="pa-c-name">角色名 *</label>
          <input id="pa-c-name" v-model="characterForm.name" class="input" maxlength="60" />
        </div>
        <div class="field">
          <label class="label" for="pa-c-identity">身份</label>
          <input id="pa-c-identity" v-model="characterForm.identity" class="input" placeholder="例如：夜班护士" />
        </div>
        <div class="field">
          <label class="label" for="pa-c-appearance">外貌</label>
          <textarea id="pa-c-appearance" v-model="characterForm.appearance" class="textarea" rows="3" />
        </div>
        <div class="field">
          <label class="label" for="pa-c-outfit">服装</label>
          <input id="pa-c-outfit" v-model="characterForm.outfit" class="input" />
        </div>
        <p v-if="characterFormError" class="hint warn-text">{{ characterFormError }}</p>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" :disabled="busy" @click="showCharacterForm = false">
          取消
        </button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitCharacter">
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 生成参考图 -->
    <ModalDialog
      :open="showGenerate"
      :title="`生成参考图 · ${generateTarget?.name ?? ''}`"
      @close="showGenerate = false"
    >
      <div class="col form-col">
        <div class="field">
          <span class="label">资产类型</span>
          <div class="row-wrap">
            <button
              v-for="opt in ASSET_KIND_OPTIONS"
              :key="opt.value"
              type="button"
              class="btn btn-sm"
              :class="generateForm.asset_kinds.includes(opt.value) ? 'btn-primary' : 'btn-ghost'"
              @click="toggleAssetKind(opt.value)"
            >
              {{ opt.label }}
            </button>
          </div>
          <span class="hint">至少选一种</span>
        </div>
        <div class="field">
          <label class="label" for="pa-g-count">每类数量</label>
          <input
            id="pa-g-count"
            v-model.number="generateForm.count_per_kind"
            class="input"
            type="number"
            min="1"
            max="8"
          />
        </div>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" :disabled="busy" @click="showGenerate = false">
          取消
        </button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitGenerate">
          {{ busy ? '提交中…' : '提交生成' }}
        </button>
      </template>
    </ModalDialog>

    <!-- 新建 / 编辑画风 -->
    <ModalDialog
      :open="showStyleForm"
      :title="editingStyleId ? '编辑画风' : '新建画风'"
      :width="600"
      @close="showStyleForm = false"
    >
      <div class="col form-col">
        <div class="field">
          <label class="label" for="pa-s-name">画风名 *</label>
          <input id="pa-s-name" v-model="styleForm.name" class="input" maxlength="120" />
        </div>
        <div class="field">
          <label class="label" for="pa-s-desc">描述</label>
          <textarea id="pa-s-desc" v-model="styleForm.description" class="textarea" rows="2" />
        </div>
        <div class="field">
          <label class="label" for="pa-s-palette">色板</label>
          <input
            id="pa-s-palette"
            v-model="styleForm.paletteText"
            class="input"
            placeholder="#1b2a41, #c9a227, 冷色调"
          />
          <span class="hint">逗号分隔；#hex 会渲染成色块，其余按文字显示</span>
        </div>
        <div class="field">
          <label class="label" for="pa-s-camera">镜头风格</label>
          <input id="pa-s-camera" v-model="styleForm.camera_style" class="input" />
        </div>
        <div class="field">
          <label class="label" for="pa-s-light">光线风格</label>
          <input id="pa-s-light" v-model="styleForm.lighting_style" class="input" />
        </div>
        <p v-if="styleFormError" class="hint warn-text">{{ styleFormError }}</p>
      </div>
      <template #footer>
        <button type="button" class="btn btn-ghost" :disabled="busy" @click="showStyleForm = false">
          取消
        </button>
        <button type="button" class="btn btn-primary" :disabled="busy" @click="submitStyle">
          {{ busy ? '保存中…' : '保存' }}
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
.sec-head {
  margin-bottom: 2px;
}
.form-col {
  gap: var(--space-3);
}
/* 与 WorkspaceView / ProjectDetailView 同款概览网格（各视图局部定义，base.css 无此工具类） */
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: var(--gap);
}
.warn-text {
  color: var(--warn);
}

/* 工具栏 */
.toolbar {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.toolbar-filters {
  display: flex;
  align-items: flex-end;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.tb-field {
  min-width: 240px;
  flex: 1 1 240px;
}
.tb-reset {
  margin-bottom: 1px;
}
.tb-actions {
  margin-left: auto;
  gap: var(--space-2);
}
.tb-summary {
  padding-top: 10px;
  border-top: 1px solid var(--line);
}

/* 分段控件 */
.seg {
  display: inline-flex;
  padding: var(--space-1);
  gap: var(--space-1);
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
  font-size: var(--fs-12);
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
  padding: var(--space-3);
  display: flex;
  gap: var(--space-3);
  align-items: flex-start;
}
.cc-thumb {
  width: 108px;
  flex: 0 0 auto;
}
.cc-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  flex: 1 1 auto;
}
.cc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.cc-name {
  font-size: var(--fs-16);
  font-weight: 650;
  min-width: 0;
}
.cc-badges {
  gap: var(--space-2);
}
.cc-identity,
.cc-appearance {
  margin: 0;
  line-height: 1.5;
}
.cc-actions {
  gap: var(--space-2);
  margin-top: 2px;
}
.cc-foot {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}

/* 画风卡片 */
.style-card {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.sc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.sc-name {
  font-size: var(--fs-16);
  font-weight: 650;
  min-width: 0;
}
.sc-badges {
  gap: var(--space-2);
}
.sc-desc {
  margin: 0;
  line-height: 1.55;
  color: var(--text-1);
}
.sc-palette {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.swatches {
  display: flex;
  gap: var(--space-2);
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
  gap: var(--space-2);
}
.sc-meta {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.sc-meta-row {
  display: flex;
  gap: var(--space-2);
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
.sc-actions {
  gap: var(--space-2);
}
.sc-foot {
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--line);
}
</style>
