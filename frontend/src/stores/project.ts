import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiError } from '@/api/errors'
import { asList, projectsApi } from '@/api/projects'
import { episodesApi } from '@/api/episodes'
import { fetchApprovals } from '@/api/settings'
import type {
  Approval,
  AuditEntry,
  CostSummary,
  Episode,
  EpisodeCreateInput,
  Project,
  ProjectCreateInput,
  ProjectPatch,
} from '@/types'

function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  return new ApiError({
    code: 'NETWORK_ERROR',
    message: err instanceof Error ? err.message : String(err),
    status: 0,
    retryable: true,
  })
}

/** 项目、项目下的剧集、成本与审计（API.md §2 / §3）。 */
export const useProjectStore = defineStore('project', () => {
  const projects = ref<Project[]>([])
  const includeArchived = ref(false)
  const listLoading = ref(false)
  const listError = ref<ApiError | null>(null)

  const current = ref<Project | null>(null)
  const episodes = ref<Episode[]>([])
  const cost = ref<CostSummary | null>(null)
  const audit = ref<AuditEntry[]>([])
  const approvals = ref<Approval[]>([])
  const detailLoading = ref(false)
  const detailError = ref<ApiError | null>(null)

  const activeProjects = computed(() => projects.value.filter((p) => !p.archived))
  const archivedProjects = computed(() => projects.value.filter((p) => p.archived))

  async function loadList(): Promise<void> {
    listLoading.value = true
    listError.value = null
    try {
      projects.value = asList<Project>(await projectsApi.list(includeArchived.value))
    } catch (err) {
      listError.value = toApiError(err)
      projects.value = []
    } finally {
      listLoading.value = false
    }
  }

  async function createProject(body: ProjectCreateInput): Promise<Project> {
    const project = await projectsApi.create(body)
    projects.value = [project, ...projects.value]
    return project
  }

  async function patchProject(projectId: string, body: ProjectPatch): Promise<Project> {
    const updated = await projectsApi.patch(projectId, body)
    projects.value = projects.value.map((p) => (p.id === updated.id ? updated : p))
    if (current.value?.id === updated.id) current.value = updated
    return updated
  }

  async function archiveProject(projectId: string): Promise<void> {
    const updated = await projectsApi.archive(projectId)
    projects.value = projects.value.map((p) => (p.id === updated.id ? updated : p))
    if (current.value?.id === updated.id) current.value = updated
  }

  async function restoreProject(projectId: string): Promise<void> {
    const updated = await projectsApi.restore(projectId)
    projects.value = projects.value.map((p) => (p.id === updated.id ? updated : p))
    if (current.value?.id === updated.id) current.value = updated
  }

  async function loadProject(projectId: string): Promise<void> {
    detailLoading.value = true
    detailError.value = null
    try {
      current.value = await projectsApi.get(projectId)
    } catch (err) {
      detailError.value = toApiError(err)
      current.value = null
    } finally {
      detailLoading.value = false
    }
  }

  async function loadEpisodes(projectId: string): Promise<void> {
    try {
      episodes.value = await episodesApi.list(projectId)
    } catch {
      episodes.value = []
    }
  }

  async function loadCost(projectId: string): Promise<void> {
    try {
      cost.value = await projectsApi.cost(projectId)
    } catch {
      cost.value = null
    }
  }

  async function loadAudit(projectId: string): Promise<void> {
    try {
      audit.value = asList<AuditEntry>(await projectsApi.audit(projectId))
    } catch {
      audit.value = []
    }
  }

  async function loadApprovals(episodeId: string): Promise<void> {
    try {
      approvals.value = await fetchApprovals(episodeId)
    } catch {
      approvals.value = []
    }
  }

  async function loadDetail(projectId: string): Promise<void> {
    await Promise.all([
      loadProject(projectId),
      loadEpisodes(projectId),
      loadCost(projectId),
      loadAudit(projectId),
    ])
  }

  async function createEpisode(projectId: string, body: EpisodeCreateInput): Promise<Episode> {
    const episode = await episodesApi.create(projectId, body)
    episodes.value = [...episodes.value, episode].sort(
      (a, b) => (a.episode_no ?? 0) - (b.episode_no ?? 0),
    )
    return episode
  }

  return {
    projects,
    includeArchived,
    listLoading,
    listError,
    current,
    episodes,
    cost,
    audit,
    approvals,
    detailLoading,
    detailError,
    activeProjects,
    archivedProjects,
    loadList,
    createProject,
    patchProject,
    archiveProject,
    restoreProject,
    loadProject,
    loadEpisodes,
    loadCost,
    loadAudit,
    loadApprovals,
    loadDetail,
    createEpisode,
  }
})
