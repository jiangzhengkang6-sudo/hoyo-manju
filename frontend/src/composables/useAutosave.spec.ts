/**
 * FR-003 自动保存 / 本地草稿 / 冲突取舍 的自动化回归。
 *
 * 这些用例覆盖的是**真实语义**，不是「函数被调用过」：
 * 防抖是否真的合并了连续输入、失败时草稿有没有留下、
 * 服务器版本前进时是否**没有**静默覆盖用户内容。
 */
import { defineComponent, nextTick, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { clearDraft, useAutosave, type AutosaveState } from '@/composables/useAutosave'

const KEY = 'story-text'
const EP = 'epi_test_001'

/** useAutosave 用了 onBeforeUnmount，必须在组件上下文里跑。 */
function withAutosave<T>(factory: () => ReturnType<typeof useAutosave<T>>) {
  let api!: ReturnType<typeof useAutosave<T>>
  const wrapper = mount(
    defineComponent({
      setup() {
        api = factory()
        return () => null
      },
    }),
  )
  return { api, wrapper }
}

function draftRaw(episodeId = EP, key = KEY): string | null {
  return window.localStorage.getItem(`hoyomanju:draft:${episodeId}:${key}`)
}

function setOnline(online: boolean): void {
  Object.defineProperty(window.navigator, 'onLine', { configurable: true, value: online })
}

describe('useAutosave (FR-003)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setOnline(true)
  })

  afterEach(() => {
    vi.useRealTimers()
    setOnline(true)
  })

  it('连续输入只在静默期结束后提交一次', async () => {
    const value = ref('')
    const save = vi.fn(async () => 1)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 1000,
      }),
    )

    value.value = '第'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    value.value = '第一'
    await nextTick()
    await vi.advanceTimersByTimeAsync(400)
    value.value = '第一集'
    await nextTick()

    // 静默期内不应有任何提交
    expect(save).not.toHaveBeenCalled()
    expect(api.state.value).toBe<AutosaveState>('pending')

    await vi.advanceTimersByTimeAsync(1000)
    expect(save).toHaveBeenCalledTimes(1)
    expect(save).toHaveBeenCalledWith('第一集')
    expect(api.state.value).toBe<AutosaveState>('saved')
    expect(api.lastSavedAt.value).not.toBeNull()

    wrapper.unmount()
  })

  it('输入过程中草稿实时落本地，提交成功后清掉', async () => {
    const value = ref('')
    const save = vi.fn(async () => 1)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 500,
      }),
    )

    value.value = '未提交的内容'
    await nextTick()

    // 提交之前本地就该有草稿 —— 这是「意外关页不丢内容」的关键
    const raw = draftRaw()
    expect(raw).not.toBeNull()
    expect(JSON.parse(raw!)).toMatchObject({
      episodeId: EP,
      baseVersion: 1,
      value: '未提交的内容',
    })

    await vi.advanceTimersByTimeAsync(500)
    expect(api.state.value).toBe<AutosaveState>('saved')
    expect(draftRaw()).toBeNull()

    wrapper.unmount()
  })

  it('保存失败时保留草稿并置为 error，不把异常抛给调用方', async () => {
    const value = ref('')
    const save = vi.fn(async () => {
      throw new Error('网络不可达')
    })
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 300,
      }),
    )

    value.value = '会失败的内容'
    await nextTick()
    await vi.advanceTimersByTimeAsync(300)

    expect(api.state.value).toBe<AutosaveState>('error')
    expect(api.lastError.value).toBe('网络不可达')
    expect(draftRaw()).not.toBeNull()
    expect(JSON.parse(draftRaw()!).value).toBe('会失败的内容')

    wrapper.unmount()
  })

  it('离线时不发请求，只留草稿并置为 offline', async () => {
    setOnline(false)
    const value = ref('')
    const save = vi.fn(async () => 1)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 200,
      }),
    )

    value.value = '离线写的内容'
    await nextTick()
    await vi.advanceTimersByTimeAsync(200)

    expect(save).not.toHaveBeenCalled()
    expect(api.state.value).toBe<AutosaveState>('offline')
    expect(draftRaw()).not.toBeNull()

    wrapper.unmount()
  })

  it('网络恢复后自动重试离线期间的改动', async () => {
    setOnline(false)
    const value = ref('')
    const save = vi.fn(async () => 7)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 200,
      }),
    )

    value.value = '离线内容'
    await nextTick()
    await vi.advanceTimersByTimeAsync(200)
    expect(api.state.value).toBe<AutosaveState>('offline')

    setOnline(true)
    window.dispatchEvent(new Event('online'))
    await vi.advanceTimersByTimeAsync(0)

    expect(save).toHaveBeenCalledTimes(1)
    expect(api.state.value).toBe<AutosaveState>('saved')

    wrapper.unmount()
  })

  it('服务器版本一致时 restore 直接还原，且不触发一次多余提交', async () => {
    window.localStorage.setItem(
      `hoyomanju:draft:${EP}:${KEY}`,
      JSON.stringify({ savedAt: 1234, baseVersion: 3, episodeId: EP, value: '草稿内容' }),
    )
    const value = ref('服务器内容')
    const save = vi.fn(async () => 3)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 3,
        debounceMs: 100,
      }),
    )

    const restored = api.restore()
    expect(restored).toBe(true)
    expect(value.value).toBe('草稿内容')
    expect(api.conflict.value).toBeNull()

    // 回填不能被当成用户编辑
    await nextTick()
    await vi.advanceTimersByTimeAsync(500)
    expect(save).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('服务器版本已前进时标记冲突，并且不覆盖当前内容', async () => {
    window.localStorage.setItem(
      `hoyomanju:draft:${EP}:${KEY}`,
      JSON.stringify({ savedAt: 1, baseVersion: 2, episodeId: EP, value: '我的本地草稿' }),
    )
    const value = ref('别人改过的最新内容')
    const save = vi.fn(async () => 5)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 5,
        debounceMs: 100,
      }),
    )

    const restored = api.restore()
    expect(restored).toBe(false)
    expect(api.conflict.value).not.toBeNull()
    expect(api.conflict.value!.serverVersion).toBe(5)
    expect(api.conflict.value!.draft.value).toBe('我的本地草稿')
    // 关键：不静默覆盖
    expect(value.value).toBe('别人改过的最新内容')

    wrapper.unmount()
  })

  it('acceptDraft 用本地草稿覆盖并立即提交', async () => {
    window.localStorage.setItem(
      `hoyomanju:draft:${EP}:${KEY}`,
      JSON.stringify({ savedAt: 1, baseVersion: 2, episodeId: EP, value: '采用本地草稿' }),
    )
    const value = ref('服务器内容')
    const save = vi.fn(async () => 9)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 5,
        debounceMs: 100,
      }),
    )

    api.restore()
    expect(api.conflict.value).not.toBeNull()

    await api.acceptDraft()
    expect(value.value).toBe('采用本地草稿')
    expect(save).toHaveBeenCalledWith('采用本地草稿')
    expect(api.conflict.value).toBeNull()
    expect(draftRaw()).toBeNull()

    wrapper.unmount()
  })

  it('discardDraft 保留服务器内容并清掉本地草稿', async () => {
    window.localStorage.setItem(
      `hoyomanju:draft:${EP}:${KEY}`,
      JSON.stringify({ savedAt: 1, baseVersion: 2, episodeId: EP, value: '要丢弃的草稿' }),
    )
    const value = ref('服务器内容')
    const save = vi.fn(async () => 5)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 5,
        debounceMs: 100,
      }),
    )

    api.restore()
    api.discardDraft()

    expect(value.value).toBe('服务器内容')
    expect(api.conflict.value).toBeNull()
    expect(draftRaw()).toBeNull()
    await vi.advanceTimersByTimeAsync(500)
    expect(save).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('isEmpty 判空的内容不提交，直接回到 idle', async () => {
    const value = ref('')
    const save = vi.fn(async () => 1)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 100,
        isEmpty: (v) => v.trim() === '',
      }),
    )

    value.value = '   '
    await nextTick()
    await vi.advanceTimersByTimeAsync(300)

    expect(save).not.toHaveBeenCalled()
    expect(api.state.value).toBe<AutosaveState>('idle')

    wrapper.unmount()
  })

  it('关闭自动保存时仍写本地草稿但不提交', async () => {
    const value = ref('')
    const save = vi.fn(async () => 1)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        enabled: false,
        debounceMs: 100,
      }),
    )

    expect(api.autoSaveOn.value).toBe(false)
    value.value = '草稿而已'
    await nextTick()
    await vi.advanceTimersByTimeAsync(500)

    expect(save).not.toHaveBeenCalled()
    expect(draftRaw()).not.toBeNull()

    wrapper.unmount()
  })

  it('草稿按剧集隔离，不会串味', async () => {
    const episodeId = ref<string | null>(EP)
    const value = ref('')
    const save = vi.fn(async () => 1)
    const { wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId,
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 50,
      }),
    )

    value.value = 'A 集内容'
    await nextTick()
    expect(draftRaw('epi_test_001')).not.toBeNull()
    expect(draftRaw('epi_test_002')).toBeNull()

    // 切到另一集：不应把 A 集草稿读出来
    episodeId.value = 'epi_test_002'
    await nextTick()
    const other = window.localStorage.getItem(`hoyomanju:draft:epi_test_002:${KEY}`)
    expect(other).toBeNull()

    wrapper.unmount()
  })

  it('episodeId 为空时既不提交也不写草稿', async () => {
    const value = ref('无归属内容')
    const save = vi.fn(async () => 1)
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref<string | null>(null),
        key: KEY,
        value,
        save,
        serverVersion: () => 1,
        debounceMs: 100,
      }),
    )

    await nextTick()
    await vi.advanceTimersByTimeAsync(300)

    expect(save).not.toHaveBeenCalled()
    expect(api.persistLocal).toBeTypeOf('function')
    expect(window.localStorage.length).toBe(0)

    wrapper.unmount()
  })

  it('clearDraft 能清掉指定草稿', () => {
    window.localStorage.setItem(
      `hoyomanju:draft:${EP}:${KEY}`,
      JSON.stringify({ savedAt: 1, baseVersion: 1, episodeId: EP, value: 'x' }),
    )
    expect(draftRaw()).not.toBeNull()
    clearDraft(EP, KEY)
    expect(draftRaw()).toBeNull()
  })

  it('损坏的草稿不会导致崩溃，按「无草稿」处理', () => {
    window.localStorage.setItem(`hoyomanju:draft:${EP}:${KEY}`, '{不是合法 JSON')
    const value = ref('服务器内容')
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save: vi.fn(async () => 1),
        serverVersion: () => 1,
      }),
    )

    expect(() => api.restore()).not.toThrow()
    expect(api.restore()).toBe(false)
    expect(value.value).toBe('服务器内容')

    wrapper.unmount()
  })

  it('草稿的 episodeId 与当前剧集不符时不采用', () => {
    // 键是 A 集的，但内容标记为 B 集（模拟手工篡改/搬迁）
    window.localStorage.setItem(
      `hoyomanju:draft:${EP}:${KEY}`,
      JSON.stringify({ savedAt: 1, baseVersion: 1, episodeId: 'epi_other', value: '别人的草稿' }),
    )
    const value = ref('服务器内容')
    const { api, wrapper } = withAutosave(() =>
      useAutosave<string>({
        episodeId: ref(EP),
        key: KEY,
        value,
        save: vi.fn(async () => 1),
        serverVersion: () => 1,
      }),
    )

    expect(api.restore()).toBe(false)
    expect(value.value).toBe('服务器内容')

    wrapper.unmount()
  })
})
