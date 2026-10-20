/**
 * PRD §5.1-6 项目级「角色与画风资产库」视图层回归。
 *
 * 这是本项目第一份 `.vue` 视图测试（`views/*.vue` 此前 0% 覆盖）。
 * 纯逻辑已经在 `utils/projectAssets.spec.ts` 钉过，这里只钉「接线是否正确」：
 * 数据来了能不能渲染出来、按钮点了有没有打到对的接口、被拦截时有没有说清原因。
 *
 * 这些正是视图层最容易悄悄坏掉的地方 —— 逻辑对、接线错，页面照样是坏的。
 */
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  fetchCharacters: vi.fn(),
  fetchStyles: vi.fn(),
  fetchVoices: vi.fn(),
  createCharacter: vi.fn(),
  patchCharacter: vi.fn(),
  archiveCharacter: vi.fn(),
  generateCharacter: vi.fn(),
  createStyle: vi.fn(),
  patchStyle: vi.fn(),
  applyStyle: vi.fn(),
  reportError: vi.fn(),
  loadProject: vi.fn(async () => undefined),
  // 组件里读的是 `store.current?.name` / `store.current.default_style_id`，给个带 .value 的对象即可
  project: { value: { id: 'prj_1', name: '测试项目', default_style_id: 'sty_default' } },
}))

vi.mock('@/api/client', () => ({
  reportError: mocks.reportError,
  // toast store 在创建时订阅错误通道，必须提供
  onToast: () => () => undefined,
  pushToast: () => undefined,
}))

vi.mock('@/api/characters', () => ({
  fetchCharacters: mocks.fetchCharacters,
  fetchStyles: mocks.fetchStyles,
  charactersApi: {
    create: mocks.createCharacter,
    patch: mocks.patchCharacter,
    archive: mocks.archiveCharacter,
    generate: mocks.generateCharacter,
  },
  stylesApi: {
    create: mocks.createStyle,
    patch: mocks.patchStyle,
    apply: mocks.applyStyle,
  },
}))

vi.mock('@/api/audio', () => ({ fetchVoices: mocks.fetchVoices }))

vi.mock('@/stores/project', () => ({
  useProjectStore: () => ({ current: mocks.project.value, loadProject: mocks.loadProject }),
}))

import ProjectAssetsView from '@/views/ProjectAssetsView.vue'
import { useToastStore } from '@/stores/toast'

const CHARACTER = {
  id: 'chr_1',
  name: '林晚',
  identity: '夜班护士',
  appearance: '及肩黑发，白大褂，总在凌晨三点出现',
  primary_media_id: 'med_1',
  voice_id: 'voi_1',
  voice_speed: 1.25,
  status: 'CONFIRMED',
  is_main: true,
  created_at: '2026-10-13T10:00:00Z',
}

const CHARACTER_MISSING = {
  id: 'chr_2',
  name: '陈默',
  identity: '侦探',
  primary_media_id: null,
  voice_id: null,
  voice_speed: 1,
  status: 'DRAFT',
  is_main: false,
  created_at: '2026-10-13T10:05:00Z',
}

const STYLE = {
  id: 'sty_1',
  name: '霓虹夜雨',
  description: '高对比冷调',
  palette: ['#1b2a41', '#c9a227', '冷色调'],
  camera_style: '低角度',
  lighting_style: '霓虹反射',
  status: 'CONFIRMED',
  version: 2,
  is_default: false,
  created_at: '2026-10-13T10:10:00Z',
}

function mountView() {
  return mount(ProjectAssetsView, {
    props: { projectId: 'prj_1' },
    attachTo: document.body,
    global: {
      stubs: {
        RouterLink: { template: '<a><slot /></a>' },
        // 真实 ModalDialog 会 Teleport 到 body，内容就跑到 wrapper 之外了。
        // 本文件测的是视图的接线（弹窗里的表单有没有打到对的接口），不是弹窗自身的机制，
        // 所以用内联替身把内容留在 wrapper 里。
        ModalDialog: {
          props: ['open', 'title'],
          template:
            '<div v-if="open" class="modal-stub"><h3>{{ title }}</h3>' +
            '<slot /><footer><slot name="footer" /></footer></div>',
        },
      },
    },
  })
}

/** 按可见文字找按钮。 */
function buttonByText(wrapper: VueWrapper, text: string) {
  const hit = wrapper.findAll('button').find((b) => (b.text() || '').trim().includes(text))
  if (!hit) throw new Error(`找不到按钮：${text}｜现有：${wrapper.findAll('button').map((b) => b.text().trim()).join(' / ')}`)
  return hit
}

function textOf(wrapper: VueWrapper): string {
  return wrapper.text()
}

async function mountWithData() {
  mocks.fetchCharacters.mockResolvedValue([CHARACTER, CHARACTER_MISSING])
  mocks.fetchStyles.mockResolvedValue([STYLE])
  mocks.fetchVoices.mockResolvedValue([{ id: 'voi_1', name: '清冷女声' }])
  const wrapper = mountView()
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  mocks.fetchCharacters.mockReset().mockResolvedValue([])
  mocks.fetchStyles.mockReset().mockResolvedValue([])
  mocks.fetchVoices.mockReset().mockResolvedValue([])
  mocks.createCharacter.mockReset()
  mocks.patchCharacter.mockReset()
  mocks.archiveCharacter.mockReset()
  mocks.generateCharacter.mockReset()
  mocks.createStyle.mockReset()
  mocks.patchStyle.mockReset()
  mocks.applyStyle.mockReset()
  mocks.reportError.mockReset()
  mocks.loadProject.mockClear()
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('加载与渲染', () => {
  it('拉取角色/画风/音色，并把角色与画风渲染出来', async () => {
    const wrapper = await mountWithData()

    expect(mocks.fetchCharacters).toHaveBeenCalledWith('prj_1')
    expect(mocks.fetchStyles).toHaveBeenCalledWith('prj_1')
    expect(mocks.fetchVoices).toHaveBeenCalledWith('prj_1')
    expect(mocks.loadProject).toHaveBeenCalledWith('prj_1')

    const text = textOf(wrapper)
    expect(text).toContain('角色与画风资产库')
    expect(text).toContain('林晚')
    expect(text).toContain('陈默')
    expect(text).toContain('霓虹夜雨')
    expect(text).toContain('高对比冷调')
  })

  it('音色与语速按真实字段展示（未绑定要明说，语速缺省按 1.00×）', async () => {
    const wrapper = await mountWithData()
    const text = textOf(wrapper)

    expect(text).toContain('音色 清冷女声')
    expect(text).toContain('语速 1.25×')
    expect(text).toContain('未绑定音色')
    expect(text).toContain('语速 1.00×')
  })

  it('色板里的 #hex 渲染成色块，其余按文字显示', async () => {
    const wrapper = await mountWithData()
    const swatches = wrapper.findAll('.swatch')
    expect(swatches).toHaveLength(2)
    // jsdom 会把 #hex 规范化成 rgb(...)，所以按 rgb 断言（颜色值本身没变）
    const styles = swatches.map((s) => s.attributes('style') ?? '')
    expect(styles[0]).toContain('rgb(27, 42, 65)') // #1b2a41
    expect(styles[1]).toContain('rgb(201, 162, 39)') // #c9a227
    expect(textOf(wrapper)).toContain('冷色调')
  })

  it('缺参考图/音色的角色给出「待完善」而不是等生成时才报错', async () => {
    const wrapper = await mountWithData()
    expect(textOf(wrapper)).toContain('待完善：未确认参考图、未绑定音色')
    // 齐备的角色不该出现该提示
    expect(textOf(wrapper).match(/待完善/g)).toHaveLength(1)
  })

  it('概览计数区分「总量」与「真的可用」', async () => {
    const wrapper = await mountWithData()
    const tiles = wrapper.findAll('.stat-tile')
    expect(tiles).toHaveLength(4)
    // StatTile 会把 label + value + hint 一起渲染，按顺序断言
    expect(tiles[0].text()).toContain('角色')
    expect(tiles[0].text()).toContain('2')
    expect(tiles[1].text()).toContain('画风')
    expect(tiles[1].text()).toContain('1')
    expect(tiles[2].text()).toContain('已确认参考图')
    expect(tiles[2].text()).toContain('1')
    expect(tiles[3].text()).toContain('已绑定音色')
    expect(tiles[3].text()).toContain('1')
  })
})

describe('筛选', () => {
  it('搜索按名称/身份/外貌命中，并提示已过滤', async () => {
    const wrapper = await mountWithData()
    await wrapper.find('#pa-search').setValue('护士')
    await flushPromises()

    expect(textOf(wrapper)).toContain('林晚')
    expect(textOf(wrapper)).not.toContain('陈默')
    expect(textOf(wrapper)).toContain('已过滤')
  })

  it('分段切到「画风」后角色区不再渲染', async () => {
    const wrapper = await mountWithData()
    await buttonByText(wrapper, '画风').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.style-card')).toHaveLength(1)
    expect(wrapper.findAll('.character-card')).toHaveLength(0)
  })

  it('无命中时给空态而不是空白，重置后恢复', async () => {
    const wrapper = await mountWithData()
    await wrapper.find('#pa-search').setValue('不存在的关键词')
    await flushPromises()

    expect(textOf(wrapper)).toContain('没有匹配的资产')
    expect(wrapper.findAll('.character-card')).toHaveLength(0)

    await buttonByText(wrapper, '重置筛选').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.character-card')).toHaveLength(2)
  })

  it('项目里一个资产都没有时给引导性空态', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(textOf(wrapper)).toContain('这个项目还没有角色或画风')
  })
})

describe('失败与错误上报', () => {
  it('拉取失败渲染 ErrorState 而不是假装空数据', async () => {
    mocks.fetchCharacters.mockRejectedValue(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()

    // 不该退化成「没有资产」的空态
    expect(textOf(wrapper)).not.toContain('这个项目还没有角色或画风')
    expect(wrapper.find('.error').exists() || textOf(wrapper).includes('加载资产库')).toBe(true)
  })

  it('音色列表失败不阻断页面（只是标签退化成 id）', async () => {
    mocks.fetchCharacters.mockResolvedValue([CHARACTER])
    mocks.fetchStyles.mockResolvedValue([])
    mocks.fetchVoices.mockRejectedValue(new Error('voices down'))
    const wrapper = mountView()
    await flushPromises()

    expect(textOf(wrapper)).toContain('林晚')
    expect(textOf(wrapper)).toContain('音色 voi_1')
  })
})

describe('设为项目默认画风', () => {
  it('调用 apply 并如实告知受影响的分镜数（而不是只说「已应用」）', async () => {
    mocks.applyStyle.mockResolvedValue({ project_id: 'prj_1', style_id: 'sty_1', affected_shots: 7 })
    const wrapper = await mountWithData()
    const toast = useToastStore()

    await buttonByText(wrapper, '设为项目默认').trigger('click')
    await flushPromises()

    expect(mocks.applyStyle).toHaveBeenCalledWith('sty_1')
    const item = toast.items.find((t) => t.title === '画风已设为项目默认')
    expect(item).toBeTruthy()
    expect(item?.message).toContain('7 个分镜')
  })

  it('没有分镜受影响时也不谎报影响面', async () => {
    mocks.applyStyle.mockResolvedValue({ project_id: 'prj_1', style_id: 'sty_1', affected_shots: 0 })
    const wrapper = await mountWithData()
    const toast = useToastStore()

    await buttonByText(wrapper, '设为项目默认').trigger('click')
    await flushPromises()

    const item = toast.items.find((t) => t.title === '画风已设为项目默认')
    expect(item?.message).toContain('暂无分镜受影响')
  })

  it('已经是默认的画风按钮禁用且显示「当前默认」', async () => {
    mocks.fetchCharacters.mockResolvedValue([])
    mocks.fetchStyles.mockResolvedValue([{ ...STYLE, is_default: true }])
    const wrapper = mountView()
    await flushPromises()

    const btn = buttonByText(wrapper, '当前默认')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('apply 失败走统一上报，不静默吞掉', async () => {
    mocks.applyStyle.mockRejectedValue(new Error('nope'))
    const wrapper = await mountWithData()

    await buttonByText(wrapper, '设为项目默认').trigger('click')
    await flushPromises()

    expect(mocks.reportError).toHaveBeenCalled()
  })
})

describe('新建角色', () => {
  it('名字为空时本地拦截，不打接口', async () => {
    const wrapper = await mountWithData()
    await buttonByText(wrapper, '新建角色').trigger('click')
    await flushPromises()

    // 弹窗里的「保存」
    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(mocks.createCharacter).not.toHaveBeenCalled()
    expect(textOf(wrapper)).toContain('角色名不能为空')
  })

  it('填好后提交，成功则关弹窗并加入列表', async () => {
    mocks.createCharacter.mockResolvedValue({
      id: 'chr_new',
      name: '苏晴',
      identity: '记者',
      status: 'DRAFT',
      created_at: '2026-10-13T11:00:00Z',
    })
    const wrapper = await mountWithData()
    const toast = useToastStore()

    await buttonByText(wrapper, '新建角色').trigger('click')
    await flushPromises()
    await wrapper.find('#pa-c-name').setValue('苏晴')
    await wrapper.find('#pa-c-identity').setValue('记者')
    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(mocks.createCharacter).toHaveBeenCalledWith('prj_1', {
      name: '苏晴',
      identity: '记者',
      appearance: '',
      outfit: '',
    })
    expect(textOf(wrapper)).toContain('苏晴')
    expect(toast.items.some((t) => t.title === '角色已创建')).toBe(true)
  })

  it('后端报错时把消息留在表单里，弹窗不关', async () => {
    const { ApiError } = await import('@/api/errors')
    mocks.createCharacter.mockRejectedValue(
      new ApiError({ code: 'CONFLICT', message: '角色已存在：林晚', status: 409, retryable: false }),
    )
    const wrapper = await mountWithData()

    await buttonByText(wrapper, '新建角色').trigger('click')
    await flushPromises()
    await wrapper.find('#pa-c-name').setValue('林晚')
    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(textOf(wrapper)).toContain('角色已存在：林晚')
    expect(wrapper.find('#pa-c-name').exists()).toBe(true)
  })
})

describe('归档角色', () => {
  it('确认后调用归档并从列表移除（归档是软删，界面必须立刻反映）', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    mocks.archiveCharacter.mockResolvedValue({ id: 'chr_2' })
    const wrapper = await mountWithData()

    const card = wrapper
      .findAll('.character-card')
      .find((c) => c.text().includes('陈默'))!
    await card.findAll('button').find((b) => b.text().includes('归档'))!.trigger('click')
    await flushPromises()

    expect(mocks.archiveCharacter).toHaveBeenCalledWith('chr_2')
    expect(textOf(wrapper)).not.toContain('陈默')
  })

  it('用户取消时不调接口', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const wrapper = await mountWithData()

    const card = wrapper
      .findAll('.character-card')
      .find((c) => c.text().includes('陈默'))!
    await card.findAll('button').find((b) => b.text().includes('归档'))!.trigger('click')
    await flushPromises()

    expect(mocks.archiveCharacter).not.toHaveBeenCalled()
    expect(textOf(wrapper)).toContain('陈默')
  })
})

describe('生成参考图', () => {
  it('默认选正面、每种 2 张，提交后进入任务观察', async () => {
    mocks.generateCharacter.mockResolvedValue({ id: 'job_1', status: 'QUEUED', progress: 0 })
    const wrapper = await mountWithData()

    const card = wrapper
      .findAll('.character-card')
      .find((c) => c.text().includes('林晚'))!
    await card.findAll('button').find((b) => b.text().includes('生成参考图'))!.trigger('click')
    await flushPromises()
    await buttonByText(wrapper, '提交生成').trigger('click')
    await flushPromises()

    expect(mocks.generateCharacter).toHaveBeenCalledWith('chr_1', {
      asset_kinds: ['front'],
      count_per_kind: 2,
    })
    // 提交后出现任务区，用户能看到进度
    expect(textOf(wrapper)).toContain('生成任务')
  })
})

describe('新建画风', () => {
  it('色板文本被解析成数组提交（逗号/分号/空白都算分隔）', async () => {
    mocks.createStyle.mockResolvedValue({
      id: 'sty_new',
      name: '水墨',
      palette: ['#f5f5f0', '留白'],
      status: 'CONFIRMED',
      version: 1,
      created_at: '2026-10-13T12:00:00Z',
    })
    const wrapper = await mountWithData()

    await buttonByText(wrapper, '新建画风').trigger('click')
    await flushPromises()
    await wrapper.find('#pa-s-name').setValue('水墨')
    await wrapper.find('#pa-s-palette').setValue('#f5f5f0, 留白')
    await buttonByText(wrapper, '保存').trigger('click')
    await flushPromises()

    expect(mocks.createStyle).toHaveBeenCalledWith('prj_1', {
      name: '水墨',
      description: '',
      palette: ['#f5f5f0', '留白'],
      camera_style: '',
      lighting_style: '',
    })
    expect(textOf(wrapper)).toContain('水墨')
  })
})
