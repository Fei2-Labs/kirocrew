import { describe, it, expect } from 'vitest'
import { extractToolImagePaths, MAX_TOOL_IMAGE_THUMBNAILS } from '../utils/toolImagePaths'

const op = (paths: unknown, mode: unknown = 'Image') => JSON.stringify({ operations: [{ mode, image_paths: paths }] })

describe('extractToolImagePaths', () => {
  it('reads image_paths from an image-mode read operation, in order', () => {
    expect(extractToolImagePaths(op(['/a/one.png', '/b/two.jpg']))).toEqual(['/a/one.png', '/b/two.jpg'])
  })

  it('ignores an image operation outside operations[]', () => {
    expect(extractToolImagePaths(JSON.stringify({ mode: 'Image', image_paths: ['/x.png'] }))).toEqual([])
  })

  it('matches the mode case-insensitively', () => {
    expect(extractToolImagePaths(op(['/x.png'], 'image'))).toEqual(['/x.png'])
  })

  it('collects across several image operations and skips other modes', () => {
    const input = JSON.stringify({ operations: [
      { mode: 'Line', path: '/notes.md' },
      { mode: 'Image', image_paths: ['/a.png'] },
      { mode: 'Image', image_paths: ['/b.png', '/a.png'] },
    ] })
    expect(extractToolImagePaths(input)).toEqual(['/a.png', '/b.png'])
  })

  it('returns nothing for a non-image read or another tool', () => {
    expect(extractToolImagePaths(JSON.stringify({ operations: [{ mode: 'Line', path: '/a.png' }] }))).toEqual([])
    expect(extractToolImagePaths(JSON.stringify({ command: 'ls' }))).toEqual([])
    expect(extractToolImagePaths(JSON.stringify({ operations: [{ image_paths: ['/a.png'] }] }))).toEqual([])
  })

  it('returns nothing for empty or invalid input', () => {
    expect(extractToolImagePaths('')).toEqual([])
    expect(extractToolImagePaths('{not json')).toEqual([])
    expect(extractToolImagePaths('null')).toEqual([])
    expect(extractToolImagePaths(op('/a.png'))).toEqual([])
  })

  it('keeps Windows drive and UNC paths', () => {
    expect(extractToolImagePaths(op(['C:\\Users\\me\\shot.png', 'D:/pics/a.png', '\\\\host\\share\\b.png'])))
      .toEqual(['C:\\Users\\me\\shot.png', 'D:/pics/a.png', '\\\\host\\share\\b.png'])
  })

  it('drops relative paths, URLs, blanks and non-strings', () => {
    expect(extractToolImagePaths(op(['shots/a.png', './b.png', 'https://example.com/c.png', '  ', 7, null, '/ok.png'])))
      .toEqual(['/ok.png'])
  })

  it('drops traversal and credential-directory paths', () => {
    expect(extractToolImagePaths(op(['/home/me/../etc/x.png', '/home/me/.ssh/key.png', '/home/me/.aws/a.png', '/fine.png'])))
      .toEqual(['/fine.png'])
  })

  it('trims surrounding whitespace', () => {
    expect(extractToolImagePaths(op(['  /a.png  ']))).toEqual(['/a.png'])
  })

  it('caps the number of paths', () => {
    const many = Array.from({ length: MAX_TOOL_IMAGE_THUMBNAILS + 5 }, (_, i) => `/p${i}.png`)
    const got = extractToolImagePaths(op(many))
    expect(got).toHaveLength(MAX_TOOL_IMAGE_THUMBNAILS)
    expect(got[0]).toBe('/p0.png')
  })
})
