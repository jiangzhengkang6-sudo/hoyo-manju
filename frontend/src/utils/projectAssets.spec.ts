/**
 * 项目级「角色与画风资产库」纯逻辑回归（PRD §5.1-6）。
 *
 * 这些函数决定「用户能不能找到资产」和「表单能不能提交」，
 * 是最容易在重构中悄悄退化、又最难靠肉眼发现的部分，所以钉住。
 */
import { describe, expect, it } from 'vitest'
import type { Character, StyleAsset, Voice } from '@/types'
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
} from './projectAssets'

function character(over: Partial<Character> = {}): Character {
  return { id: 'chr_1', name: '林晚', ...over }
}

function style(over: Partial<StyleAsset> = {}): StyleAsset {
  return { id: 'sty_1', name: '霓虹夜雨', ...over }
}

describe('parsePaletteInput / formatPaletteInput', () => {
  it('按逗号、分号、空白拆分（中英文标点都接受）', () => {
    expect(parsePaletteInput('#fff, #000; 冷色调')).toEqual(['#fff', '#000', '冷色调'])
    expect(parsePaletteInput('a，b；c')).toEqual(['a', 'b', 'c'])
  })

  it('丢掉空项与首尾空白', () => {
    expect(parsePaletteInput('  #fff ,,  , #000  ')).toEqual(['#fff', '#000'])
    expect(parsePaletteInput('')).toEqual([])
  })

  it('还原成可编辑文本，与解析互为逆运算', () => {
    expect(formatPaletteInput(['#fff', '#000'])).toBe('#fff, #000')
    expect(parsePaletteInput(formatPaletteInput(['a', 'b']))).toEqual(['a', 'b'])
  })

  it('色板是字符串或对象时也能还原（后端不保证类型）', () => {
    expect(formatPaletteInput('#fff #000')).toBe('#fff, #000')
    expect(formatPaletteInput(undefined)).toBe('')
  })
})

describe('filterCharacters', () => {
  const list = [
    character({ id: 'a', name: '林晚', identity: '夜班护士' }),
    character({ id: 'b', name: '陈默', identity: '侦探', appearance: '风衣' }),
    character({ id: 'c', name: 'Alice' }),
  ]

  it('空查询原样返回（不产生新数组语义差异）', () => {
    expect(filterCharacters(list, '')).toHaveLength(3)
    expect(filterCharacters(list, '   ')).toHaveLength(3)
  })

  it('大小写不敏感，且能按身份、外貌命中（用户不一定记得名字）', () => {
    expect(filterCharacters(list, 'alice').map((c) => c.id)).toEqual(['c'])
    expect(filterCharacters(list, '夜班').map((c) => c.id)).toEqual(['a'])
    expect(filterCharacters(list, '风衣').map((c) => c.id)).toEqual(['b'])
  })

  it('无命中返回空数组', () => {
    expect(filterCharacters(list, '不存在')).toEqual([])
  })

  it('字段缺失不会抛错', () => {
    expect(() => filterCharacters([{ id: 'x', name: 'x' }], 'x')).not.toThrow()
  })
})

describe('filterStyles', () => {
  const list = [
    style({ id: 's1', name: '霓虹夜雨', palette: ['#1b2a41', '#c9a227'] }),
    style({ id: 's2', name: '水墨', description: '留白与淡墨' }),
  ]

  it('可按名称、描述、色板命中', () => {
    expect(filterStyles(list, '霓虹').map((s) => s.id)).toEqual(['s1'])
    expect(filterStyles(list, '淡墨').map((s) => s.id)).toEqual(['s2'])
    expect(filterStyles(list, 'c9a227').map((s) => s.id)).toEqual(['s1'])
  })

  it('色板是字符串时同样可搜', () => {
    expect(filterStyles([style({ palette: '#ff0000 暖色' })], '暖色')).toHaveLength(1)
  })

  it('空查询返回全部', () => {
    expect(filterStyles(list, '')).toHaveLength(2)
  })
})

describe('assetCounts', () => {
  it('分别统计总量、有参考图、有音色，并带上默认画风', () => {
    const counts = assetCounts(
      [
        character({ id: 'a', primary_media_id: 'med_1', voice_id: 'voi_1' }),
        character({ id: 'b', primary_media_id: 'med_2' }),
        character({ id: 'c' }),
      ],
      [style(), style({ id: 's2' })],
      'sty_1',
    )
    expect(counts).toEqual({
      characters: 3,
      styles: 2,
      withReference: 2,
      withVoice: 1,
      defaultStyleId: 'sty_1',
    })
  })

  it('没有默认画风时归一成 null（而不是 undefined）', () => {
    expect(assetCounts([], [], undefined).defaultStyleId).toBeNull()
  })
})

describe('visibleCount / showCharacters / showStyles', () => {
  const chars = [character(), character({ id: 'c2' })]
  const stys = [style()]

  it('按分段给出应展示条数', () => {
    expect(visibleCount('', chars, stys)).toBe(3)
    expect(visibleCount('character', chars, stys)).toBe(2)
    expect(visibleCount('style', chars, stys)).toBe(1)
  })

  it('分段决定两个分区各自是否展示', () => {
    expect(showCharacters('')).toBe(true)
    expect(showCharacters('style')).toBe(false)
    expect(showStyles('character')).toBe(false)
    expect(showStyles('style')).toBe(true)
  })
})

describe('validateCharacterForm / validateStyleForm', () => {
  it('名字必填，且拒绝纯空白', () => {
    expect(validateCharacterForm('')).toBe('角色名不能为空')
    expect(validateCharacterForm('   ')).toBe('角色名不能为空')
    expect(validateCharacterForm('林晚')).toBeNull()
  })

  it('长度上限与后端 schema 一致', () => {
    expect(validateCharacterForm('x'.repeat(60))).toBeNull()
    expect(validateCharacterForm('x'.repeat(61))).toContain('60')
    expect(validateStyleForm('x'.repeat(120))).toBeNull()
    expect(validateStyleForm('x'.repeat(121))).toContain('120')
  })

  it('画风名同样必填', () => {
    expect(validateStyleForm('  ')).toBe('画风名不能为空')
    expect(validateStyleForm('霓虹夜雨')).toBeNull()
  })
})

describe('voiceLabel / speedLabel', () => {
  const voices: Voice[] = [
    { id: 'voi_1', name: '清冷女声' },
    { id: 'voi_2' },
  ]

  it('未绑定音色时明确说明（FR-050 的前置条件）', () => {
    expect(voiceLabel(character(), voices)).toBe('未绑定音色')
    expect(voiceLabel(character({ voice_id: null }), voices)).toBe('未绑定音色')
  })

  it('绑定后显示音色名；查不到名字时退回 id 而不是显示空白', () => {
    expect(voiceLabel(character({ voice_id: 'voi_1' }), voices)).toBe('音色 清冷女声')
    expect(voiceLabel(character({ voice_id: 'voi_2' }), voices)).toBe('音色 voi_2')
    expect(voiceLabel(character({ voice_id: 'voi_missing' }), voices)).toBe('音色 voi_missing')
  })

  it('语速缺省按后端默认 1.0 显示，固定两位小数', () => {
    expect(speedLabel(character())).toBe('语速 1.00×')
    expect(speedLabel(character({ voice_speed: 1.25 }))).toBe('语速 1.25×')
    expect(speedLabel(character({ voice_speed: null }))).toBe('语速 1.00×')
  })
})

describe('characterReadiness', () => {
  it('缺参考图与音色时逐条列出缺什么', () => {
    expect(characterReadiness(character())).toEqual({
      ready: false,
      missing: ['未确认参考图', '未绑定音色'],
    })
  })

  it('两项齐备即 ready', () => {
    expect(
      characterReadiness(character({ primary_media_id: 'med_1', voice_id: 'voi_1' })),
    ).toEqual({ ready: true, missing: [] })
  })

  it('只缺一项时只报那一项', () => {
    expect(characterReadiness(character({ primary_media_id: 'med_1' })).missing).toEqual([
      '未绑定音色',
    ])
  })
})
