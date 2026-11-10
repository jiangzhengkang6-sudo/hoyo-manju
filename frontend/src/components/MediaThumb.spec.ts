/**
 * FR-032：候选图放大 / 下载。
 *
 * 这两项能力做在共用的 `MediaThumb` 上，所有媒体面（候选图、角色参考图、成片）
 * 都靠它获得 —— 所以这里的行为一旦回退，影响面是全站，
 * 值得用组件测试钉住。
 */
import { mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { downloadFile, reportError } = vi.hoisted(() => ({
  downloadFile: vi.fn(async (_path: string, _filename: string) => undefined),
  reportError: vi.fn(),
}))

vi.mock('@/api/client', async (importOriginal) => {
  // 只替换下载/上报，保留 apiUrl 等纯函数（媒体地址依赖它拼 /api/v1）
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, downloadFile, reportError }
})

import MediaThumb from '@/components/MediaThumb.vue'

const MEDIA_ID = 'med_abc123'

function mountThumb(props: Record<string, unknown> = {}) {
  return mount(MediaThumb, {
    props: { mediaId: MEDIA_ID, alt: '候选图', ...props },
    attachTo: document.body,
  })
}

/** 灯箱 Teleport 到 body，只能在 body 上查。 */
function lightbox(): HTMLElement | null {
  return document.body.querySelector('.lightbox')
}

describe('MediaThumb (FR-032)', () => {
  beforeEach(() => {
    downloadFile.mockClear()
    reportError.mockClear()
    document.body.innerHTML = ''
  })

  it('图片默认回落到 /media/{id}/content', () => {
    const wrapper = mountThumb()
    const img = wrapper.find('img')
    expect(img.exists()).toBe(true)
    expect(img.attributes('src')).toBe(`/api/v1/media/${MEDIA_ID}/content`)
    wrapper.unmount()
  })

  it('显式 url 优先于接口回落地址', () => {
    const wrapper = mountThumb({ url: 'https://cdn.example.com/a.png' })
    expect(wrapper.find('img').attributes('src')).toBe('https://cdn.example.com/a.png')
    wrapper.unmount()
  })

  it('点击图片打开灯箱，点背景关闭', async () => {
    const wrapper = mountThumb()
    expect(lightbox()).toBeNull()

    await wrapper.find('img').trigger('click')
    const box = lightbox()
    expect(box).not.toBeNull()
    expect(box!.querySelector('img')?.getAttribute('src')).toBe(`/api/v1/media/${MEDIA_ID}/content`)

    // 点背景（.lightbox 本身，不是里面的 img）
    await (box as HTMLElement).click()
    expect(lightbox()).toBeNull()
    wrapper.unmount()
  })

  it('灯箱里点图片本身不关闭（便于查看细节）', async () => {
    const wrapper = mountThumb()
    await wrapper.find('img').trigger('click')
    const box = lightbox() as HTMLElement

    const inner = box.querySelector('img') as HTMLImageElement
    inner.click()
    await wrapper.vm.$nextTick()

    expect(lightbox()).not.toBeNull()
    wrapper.unmount()
  })

  it('Esc 关闭灯箱', async () => {
    const wrapper = mountThumb()
    await wrapper.find('img').trigger('click')
    expect(lightbox()).not.toBeNull()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()
    expect(lightbox()).toBeNull()
    wrapper.unmount()
  })

  it('关闭按钮能关闭灯箱', async () => {
    const wrapper = mountThumb()
    await wrapper.find('img').trigger('click')

    const close = document.body.querySelector('.lightbox-close') as HTMLElement
    expect(close).not.toBeNull()
    close.click()
    await wrapper.vm.$nextTick()
    expect(lightbox()).toBeNull()
    wrapper.unmount()
  })

  it('组件卸载后 Esc 不再报错（监听已移除）', async () => {
    const wrapper = mountThumb()
    await wrapper.find('img').trigger('click')
    wrapper.unmount()
    expect(() => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))).not.toThrow()
    expect(lightbox()).toBeNull()
  })

  it('下载走与展示同源的 /media/{id}/content，文件名带扩展名', async () => {
    const wrapper = mountThumb()
    const buttons = wrapper.findAll('.thumb-btn')
    // 第一个是放大，第二个是下载
    const dl = buttons.find((b) => b.text().includes('⤓'))
    expect(dl).toBeTruthy()

    await dl!.trigger('click')
    await wrapper.vm.$nextTick()

    expect(downloadFile).toHaveBeenCalledTimes(1)
    expect(downloadFile.mock.calls[0][0]).toBe(`/media/${MEDIA_ID}/content`)
    expect(downloadFile.mock.calls[0][1]).toBe(`${MEDIA_ID}.png`)
    wrapper.unmount()
  })

  it('可指定下载文件名', async () => {
    const wrapper = mountThumb({ filename: 'shot-003' })
    const dl = wrapper.findAll('.thumb-btn').find((b) => b.text().includes('⤓'))
    await dl!.trigger('click')
    await wrapper.vm.$nextTick()
    expect(downloadFile.mock.calls[0][1]).toBe('shot-003.png')
    wrapper.unmount()
  })

  it('下载失败时交给统一错误上报，不抛出', async () => {
    downloadFile.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountThumb()
    const dl = wrapper.findAll('.thumb-btn').find((b) => b.text().includes('⤓'))

    await dl!.trigger('click')
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()

    expect(reportError).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('zoomable=false 时不显示放大入口', () => {
    const wrapper = mountThumb({ zoomable: false })
    expect(wrapper.findAll('.thumb-btn').some((b) => b.text().includes('⤢'))).toBe(false)
    wrapper.unmount()
  })

  it('downloadable=false 时不显示下载入口', () => {
    const wrapper = mountThumb({ downloadable: false })
    expect(wrapper.findAll('.thumb-btn').some((b) => b.text().includes('⤓'))).toBe(false)
    wrapper.unmount()
  })

  it('视频与音频不提供放大/下载（只做播放）', () => {
    const video = mountThumb({ kind: 'video' })
    expect(video.find('video').exists()).toBe(true)
    expect(video.find('.thumb-actions').exists()).toBe(false)
    video.unmount()

    const audio = mountThumb({ kind: 'audio' })
    expect(audio.find('audio').exists()).toBe(true)
    expect(audio.find('.thumb-actions').exists()).toBe(false)
    audio.unmount()
  })

  it('没有 mediaId 时显示空态且不渲染图片', () => {
    const wrapper = mount(MediaThumb, { props: { mediaId: null }, attachTo: document.body })
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无素材')
    expect(wrapper.find('.thumb-actions').exists()).toBe(false)
    wrapper.unmount()
  })

  it('图片加载失败时显示失败态并撤掉操作入口', async () => {
    const wrapper = mountThumb()
    await wrapper.find('img').trigger('error')
    expect(wrapper.text()).toContain('加载失败')
    expect(wrapper.find('.thumb-actions').exists()).toBe(false)
    wrapper.unmount()
  })

  it('切换 mediaId 后失败态会重置（避免上一张的失败粘住）', async () => {
    const wrapper = mountThumb()
    await wrapper.find('img').trigger('error')
    expect(wrapper.text()).toContain('加载失败')

    await wrapper.setProps({ mediaId: 'med_other' })
    expect(wrapper.find('img').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('加载失败')
    wrapper.unmount()
  })
})

describe('MediaThumb 视频', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('视频地址带 /api/v1 前缀（少了会打到前端根路径，黑屏无法播放）', () => {
    const wrapper = mount(MediaThumb, {
      props: { mediaId: MEDIA_ID, kind: 'video' },
      attachTo: document.body,
    })
    const video = wrapper.find('video')
    expect(video.exists()).toBe(true)
    expect(video.attributes('src')).toBe(`/api/v1/media/${MEDIA_ID}/content`)
    wrapper.unmount()
  })

  it('列表里只给播放按钮，点开弹层才真正播', async () => {
    const wrapper = mount(MediaThumb, {
      props: { mediaId: MEDIA_ID, kind: 'video' },
      attachTo: document.body,
    })
    expect(document.body.querySelector('.video-lightbox')).toBeNull()

    await wrapper.find('.thumb-play').trigger('click')
    const box = document.body.querySelector('.video-lightbox') as HTMLElement | null
    expect(box).not.toBeNull()
    const player = box!.querySelector('video')
    expect(player?.getAttribute('src')).toBe(`/api/v1/media/${MEDIA_ID}/content`)
    expect(player?.hasAttribute('controls')).toBe(true)

    await box!.click()                       // 点背景关闭
    expect(document.body.querySelector('.video-lightbox')).toBeNull()
    wrapper.unmount()
  })

  it('视频加载失败时显示失败态', async () => {
    const wrapper = mount(MediaThumb, {
      props: { mediaId: MEDIA_ID, kind: 'video' },
      attachTo: document.body,
    })
    await wrapper.find('video').trigger('error')
    expect(wrapper.text()).toContain('加载失败')
    wrapper.unmount()
  })
})
