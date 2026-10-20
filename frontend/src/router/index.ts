import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/** 路由表严格对齐 docs/API.md §15 的页面清单。 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    name: 'projects',
    component: () => import('@/views/ProjectsView.vue'),
    meta: { title: '项目' },
  },
  {
    // PRD §5.1-1：工作区落地页。独立成一个导航项，`/` 仍然是项目列表。
    path: '/workspace',
    name: 'workspace',
    component: () => import('@/views/WorkspaceView.vue'),
    meta: { title: '工作区' },
  },
  {
    // PRD §5.1-6：项目级资产库（跨项目汇总角色与画风）。
    path: '/workspace/assets',
    name: 'workspace-assets',
    component: () => import('@/views/WorkspaceAssetsView.vue'),
    meta: { title: '资产库' },
  },
  {
    // PRD §5.1-6：项目级「角色与画风资产库」。
    // 与 `/workspace/assets`（跨项目汇总、只读）不同，这里是单项目内的资产主控台。
    path: '/projects/:projectId/assets',
    name: 'project-assets',
    component: () => import('@/views/ProjectAssetsView.vue'),
    props: true,
    meta: { title: '角色与画风资产库' },
  },
  {
    path: '/projects/:projectId',
    name: 'project-detail',
    component: () => import('@/views/ProjectDetailView.vue'),
    props: true,
    meta: { title: '项目详情' },
  },
  {
    path: '/episodes/:episodeId',
    component: () => import('@/layouts/WorkbenchLayout.vue'),
    props: true,
    children: [
      { path: '', redirect: (to) => ({ name: 'episode-story', params: to.params }) },
      {
        path: 'story',
        name: 'episode-story',
        component: () => import('@/views/EpisodeStoryView.vue'),
        meta: { title: '故事与剧本', stage: 'story' },
      },
      {
        path: 'characters',
        name: 'episode-characters',
        component: () => import('@/views/EpisodeCharactersView.vue'),
        meta: { title: '角色与画风', stage: 'characters' },
      },
      {
        path: 'shots',
        name: 'episode-shots',
        component: () => import('@/views/EpisodeShotsView.vue'),
        meta: { title: '分镜画面', stage: 'shots' },
      },
      {
        path: 'video',
        name: 'episode-video',
        component: () => import('@/views/EpisodeVideoView.vue'),
        meta: { title: '视频生成', stage: 'video' },
      },
      {
        path: 'audio',
        name: 'episode-audio',
        component: () => import('@/views/EpisodeAudioView.vue'),
        meta: { title: '音频与字幕', stage: 'audio' },
      },
      {
        path: 'render',
        name: 'episode-render',
        component: () => import('@/views/EpisodeRenderView.vue'),
        meta: { title: '预览与导出', stage: 'render' },
      },
    ],
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('@/views/JobsView.vue'),
    meta: { title: '任务中心' },
  },
  {
    path: '/cost',
    name: 'cost',
    component: () => import('@/views/CostView.vue'),
    meta: { title: '成本与记录' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '制作台'
  document.title = `hoyo漫剧 · ${title}`
})

export default router
