<script setup lang="ts">
/**
 * 媒体缩略图：图片 / 视频 / 音频占位。
 * 地址优先用后端返回的 url，其次回落到 `/media/{id}/content`（API.md §11）。
 *
 * FR-032：图片支持点击放大（灯箱）与下载；`zoomable`/`downloadable` 默认开启图片，
 * 视频/音频只做播放，不放大。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { mediaApi } from '@/api/media'
import { downloadFile, reportError } from '@/api/client'

const props = withDefaults(
  defineProps<{
    mediaId?: string | null
    url?: string | null
    kind?: 'image' | 'video' | 'audio'
    /** 竖屏 9:16 比例容器。 */
    portrait?: boolean
    alt?: string
    /** 允许点击放大（仅图片有效）。 */
    zoomable?: boolean
    /** 允许下载（仅图片有效）。 */
    downloadable?: boolean
    /** 下载时的文件名（不含扩展名），默认用 mediaId。 */
    filename?: string | null
  }>(),
  {
    mediaId: null,
    url: null,
    kind: 'image',
    portrait: false,
    alt: '',
    zoomable: true,
    downloadable: true,
    filename: null,
  },
)

const failed = ref(false)
const zoomed = ref(false)
const downloading = ref(false)

const src = computed(() => props.url || (props.mediaId ? mediaApi.contentUrl(props.mediaId) : null))
const isImage = computed(() => props.kind === 'image')
const canZoom = computed(() => isImage.value && props.zoomable && Boolean(src.value) && !failed.value)
const canDownload = computed(
  () => isImage.value && props.downloadable && Boolean(props.mediaId) && !failed.value,
)

const downloadName = computed(() => `${props.filename || props.mediaId || 'image'}.png`)

function openZoom(): void {
  if (!canZoom.value) return
  zoomed.value = true
  if (typeof window !== 'undefined') window.addEventListener('keydown', onKey)
}

function closeZoom(): void {
  zoomed.value = false
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKey)
}

function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape') closeZoom()
}

/**
 * 从 `/media/{id}/content` 取原图另存。
 * 下载链路与展示走的**同一个**接口，避免出现「能看不能下」的地址不一致。
 */
async function download(): Promise<void> {
  if (!props.mediaId) return
  downloading.value = true
  try {
    await downloadFile(`/media/${props.mediaId}/content`, downloadName.value)
  } catch (err) {
    reportError(err)
  } finally {
    downloading.value = false
  }
}

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKey)
})

watch(
  () => [props.url, props.mediaId],
  () => {
    failed.value = false
  },
)
</script>

<template>
  <div class="thumb" :class="{ portrait }">
    <template v-if="src && !failed">
      <video v-if="kind === 'video'" :src="src" controls preload="metadata" />
      <audio v-else-if="kind === 'audio'" :src="src" controls />
      <img
        v-else
        :src="src"
        :alt="alt"
        loading="lazy"
        :class="{ clickable: canZoom }"
        @error="failed = true"
        @click="openZoom"
      />
    </template>
    <div v-else class="thumb-fallback">
      <span class="thumb-icon" aria-hidden="true">
        {{ kind === 'video' ? '▶' : kind === 'audio' ? '♪' : '▣' }}
      </span>
      <span class="thumb-note">{{ failed ? '加载失败' : '暂无素材' }}</span>
    </div>

    <!-- FR-032：放大 / 下载 -->
    <div v-if="canDownload || canZoom" class="thumb-actions">
      <button
        v-if="canZoom"
        type="button"
        class="thumb-btn"
        title="放大查看"
        @click.stop="openZoom"
      >
        ⤢
      </button>
      <button
        v-if="canDownload"
        type="button"
        class="thumb-btn"
        :disabled="downloading"
        title="下载原图"
        @click.stop="download"
      >
        {{ downloading ? '…' : '⤓' }}
      </button>
    </div>

    <!-- 灯箱：点击背景或按 Esc 关闭 -->
    <Teleport to="body">
      <div v-if="zoomed" class="lightbox" role="dialog" aria-modal="true" @click="closeZoom">
        <img :src="src ?? ''" :alt="alt" @click.stop />
        <button type="button" class="lightbox-close" title="关闭（Esc）" @click="closeZoom">×</button>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.thumb {
  position: relative;
  width: 100%;
  background: var(--bg-1);
  border-radius: var(--radius-sm);
  overflow: hidden;
  display: grid;
  place-items: center;
  min-height: 72px;
}
.thumb.portrait {
  aspect-ratio: 9 / 16;
}
.thumb img,
.thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.thumb.portrait img,
.thumb.portrait video {
  position: absolute;
  inset: 0;
}
.thumb img.clickable {
  cursor: zoom-in;
}
.thumb audio {
  width: calc(100% - 16px);
  height: 34px;
}
.thumb-fallback {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  color: var(--text-3);
  padding: 12px;
}
.thumb-icon {
  font-size: 18px;
}
.thumb-note {
  font-size: 11px;
}
.thumb-actions {
  position: absolute;
  right: 5px;
  bottom: 5px;
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity var(--dur, 0.15s) var(--ease, ease);
}
.thumb:hover .thumb-actions,
.thumb:focus-within .thumb-actions {
  opacity: 1;
}
.thumb-btn {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-xs, 4px);
  background: rgba(0, 0, 0, 0.62);
  color: #fff;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
}
.thumb-btn:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.82);
}
.thumb-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.lightbox {
  position: fixed;
  inset: 0;
  z-index: 9999;
  background: rgba(0, 0, 0, 0.88);
  display: grid;
  place-items: center;
  padding: 4vh 4vw;
  cursor: zoom-out;
}
.lightbox img {
  max-width: 100%;
  max-height: 92vh;
  object-fit: contain;
  border-radius: var(--radius-sm);
  cursor: default;
}
.lightbox-close {
  position: absolute;
  top: 16px;
  right: 20px;
  width: 34px;
  height: 34px;
  border: 1px solid rgba(255, 255, 255, 0.35);
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.5);
  color: #fff;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}
</style>
