<script setup lang="ts">
/** 首页 / 项目列表（API.md §2、§15）。 */
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { storeToRefs } from 'pinia'
import { useProjectStore } from '@/stores/project'
import { useConfigStore } from '@/stores/config'
import { useToastStore } from '@/stores/toast'
import { formatCents } from '@/api/cost'
import { relativeTime } from '@/utils/format'
import EmptyState from '@/components/EmptyState.vue'
import ErrorState from '@/components/ErrorState.vue'
import SkeletonBlock from '@/components/SkeletonBlock.vue'
import ModalDialog from '@/components/ModalDialog.vue'
import StatTile from '@/components/StatTile.vue'
import type { Project } from '@/types'

const router = useRouter()
const store = useProjectStore()
const config = useConfigStore()
const toast = useToastStore()
const { projects, listLoading, listError, includeArchived, activeProjects, archivedProjects } =
  storeToRefs(store)

const showCreate = ref(false)
const creating = ref(false)
const formError = ref('')

const form = reactive({
  name: '',
  genre: '',
  aspect_ratio: '',
  default_language: '',
  style: '',
})

const totalCost = computed(() =>
  projects.value.reduce((sum, p) => sum + (p.total_cents ?? 0), 0),
)

function openCreate(): void {
  form.name = ''
  form.genre = ''
  form.aspect_ratio = config.aspectRatios[0] ?? '9:16'
  form.default_language = config.defaultLanguage
  form.style = ''
  formError.value = ''
  showCreate.value = true
}

async function submitCreate(): Promise<void> {
  if (!form.name.trim()) {
    formError.value = '项目名称不能为空'
    return
  }
  if (!form.genre.trim()) {
    formError.value = '题材不能为空'
    return
  }
  creating.value = true
  formError.value = ''
  try {
    const created = await store.createProject({
      name: form.name.trim(),
      genre: form.genre.trim(),
      aspect_ratio: form.aspect_ratio || config.aspectRatios[0] || '9:16',
      default_language: form.default_language || config.defaultLanguage,
      style: form.style.trim() || undefined,
    })
    showCreate.value = false
    toast.success('项目已创建', created.name)
    void router.push({ name: 'project-detail', params: { projectId: created.id } })
  } catch {
    // 错误已由 api 层 toast 展示（含 trace_id）
  } finally {
    creating.value = false
  }
}

function openProject(project: Project): void {
  void router.push({ name: 'project-detail', params: { projectId: project.id } })
}

async function toggleArchived(): Promise<void> {
  await store.loadList()
}

onMounted(async () => {
  await config.load()
  await store.loadList()
})
</script>

<template>
  <div class="page">
    <header class="page-head">
      <div>
        <h1 class="title-xl">项目</h1>
        <p class="muted small">
          每个项目是一季漫剧的容器，包含多个剧集（Episode）。从一句话创意到成片导出。
        </p>
      </div>
      <div class="row-wrap">
        <label class="checkbox">
          <input v-model="includeArchived" type="checkbox" @change="toggleArchived" />
          <span class="small">显示已归档</span>
        </label>
        <button type="button" class="btn btn-ghost" :disabled="listLoading" @click="store.loadList()">
          刷新
        </button>
        <button type="button" class="btn btn-primary" @click="openCreate">新建项目</button>
      </div>
    </header>

    <div class="stat-row">
      <StatTile label="进行中项目" :value="activeProjects.length" tone="accent" />
      <StatTile label="已归档" :value="archivedProjects.length" />
      <StatTile label="项目总成本" :value="formatCents(totalCost)" hint="本工作区累计" />
      <StatTile
        label="配额可用"
        :value="config.quota ? formatCents(config.quota.remaining_cents) : '—'"
        :tone="config.quota && config.quota.remaining_cents <= 0 ? 'danger' : 'ok'"
      />
    </div>

    <SkeletonBlock v-if="listLoading && !projects.length" variant="cards" :rows="6" />

    <ErrorState
      v-else-if="listError && !projects.length"
      :error="listError"
      context="加载项目列表"
      @retry="store.loadList()"
    />

    <EmptyState
      v-else-if="!projects.length"
      icon="▤"
      title="还没有项目"
      description="创建第一个项目，开始你的 AI 漫剧制作。"
    >
      <button type="button" class="btn btn-primary" @click="openCreate">新建项目</button>
    </EmptyState>

    <div v-else class="grid-cards">
      <article
        v-for="project in projects"
        :key="project.id"
        class="card card-hover project-card"
        @click="openProject(project)"
      >
        <div class="pc-head">
          <h2 class="pc-name ellipsis" :title="project.name">{{ project.name }}</h2>
          <span v-if="project.archived" class="badge">已归档</span>
          <span v-else class="badge badge-info">{{ project.aspect_ratio || '—' }}</span>
        </div>
        <div class="row-wrap pc-chips">
          <span v-if="project.genre" class="chip">{{ project.genre }}</span>
          <span v-if="project.default_language" class="chip mono">{{ project.default_language }}</span>
          <span v-if="project.style" class="chip ellipsis">{{ project.style }}</span>
        </div>
        <div class="pc-foot">
          <div class="pc-stat">
            <span class="tiny dim">剧集</span>
            <span class="mono">{{ project.episode_count ?? '—' }}</span>
          </div>
          <div class="pc-stat">
            <span class="tiny dim">成本</span>
            <span class="mono">{{ project.total_cents != null ? formatCents(project.total_cents) : '—' }}</span>
          </div>
          <div class="pc-stat">
            <span class="tiny dim">更新</span>
            <span class="mono">{{ relativeTime(project.updated_at ?? project.created_at) }}</span>
          </div>
        </div>
      </article>
    </div>

    <ModalDialog :open="showCreate" title="新建项目" :width="520" @close="showCreate = false">
      <div class="field">
        <label class="label" for="p-name">项目名称<span class="req">*</span></label>
        <input id="p-name" v-model="form.name" class="input" placeholder="例如：雨夜追迹" />
        <span class="hint">名称在工作区内唯一，冲突时后端返回 CONFLICT。</span>
      </div>
      <div class="field">
        <label class="label" for="p-genre">题材<span class="req">*</span></label>
        <input id="p-genre" v-model="form.genre" class="input" placeholder="例如：都市悬疑" />
      </div>
      <div class="form-row">
        <div class="field">
          <label class="label" for="p-ratio">画幅</label>
          <select id="p-ratio" v-model="form.aspect_ratio" class="select">
            <option v-for="ratio in config.aspectRatios" :key="ratio" :value="ratio">{{ ratio }}</option>
          </select>
          <span class="hint">可选项由后台配置下发</span>
        </div>
        <div class="field">
          <label class="label" for="p-lang">默认语言</label>
          <input id="p-lang" v-model="form.default_language" class="input mono" />
        </div>
      </div>
      <div class="field">
        <label class="label" for="p-style">画风备注（可选）</label>
        <input id="p-style" v-model="form.style" class="input" placeholder="例如：日系赛璐璐、冷色调" />
      </div>
      <p v-if="formError" class="field-error">{{ formError }}</p>
      <template #footer>
        <button type="button" class="btn btn-ghost" @click="showCreate = false">取消</button>
        <button type="button" class="btn btn-primary" :disabled="creating" @click="submitCreate">
          {{ creating ? '创建中…' : '创建项目' }}
        </button>
      </template>
    </ModalDialog>
  </div>
</template>

<style scoped>
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--gap);
}
.project-card {
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  cursor: pointer;
}
.pc-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.pc-name {
  font-size: var(--fs-16);
  font-weight: 650;
  letter-spacing: 0.01em;
  margin: 0;
  min-width: 0;
}
.pc-chips {
  min-height: 22px;
}
.pc-foot {
  display: flex;
  gap: var(--space-5);
  padding-top: 10px;
  border-top: 1px solid var(--line);
}
.pc-stat {
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
