import { isSafePath } from './safePath'
import { WINDOWS_ABS_PATH_RE } from './urlTransform'

/** Most thumbnails one tool row draws. A read can name any number of images,
 *  and the strip is a glance view, not a gallery: past this many the row would
 *  wrap into a wall above the answer. The full list stays in the Input panel. */
export const MAX_TOOL_IMAGE_THUMBNAILS = 12

/** Absolute on-disk path: POSIX `/…`, Windows drive `C:\…` / `C:/…`, or UNC
 *  `\\host\…`. Relative paths are skipped rather than guessed against a
 *  directory: the tool resolved them against ITS working directory, which the
 *  dashboard does not know, and a wrong guess would preview a different file. */
function isAbsoluteDiskPath(p: string): boolean {
  return p.startsWith('/') || WINDOWS_ABS_PATH_RE.test(p) || p.startsWith('\\\\')
}

/** Image paths an image-mode `read` call names, in order, for the tool row's
 *  thumbnail strip.
 *
 *  `input` is the raw JSON of the tool's arguments. The recognised shape is the
 *  built-in read tool's: `{"operations":[{"mode":"Image","image_paths":[…]}]}`.
 *  Matching the argument shape
 *  rather than a tool name keeps a localized or simplified title from hiding the
 *  strip, and `mode: "Image"` is what tells an image read apart from any other
 *  tool that happens to carry a path list.
 *
 *  A path is dropped when it is empty, a URL, relative, or refused by
 *  `isSafePath` (traversal or a credential directory): the strip fetches each
 *  path from `/api/file-raw` on render, and must not do so for a path the
 *  side-panel open would refuse. Duplicates collapse to one thumbnail, and at
 *  most `MAX_TOOL_IMAGE_THUMBNAILS` paths are returned. Invalid JSON, or no
 *  image operation, returns an empty list. */
export function extractToolImagePaths(input: string): string[] {
  if (!input) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(input)
  } catch {
    return []
  }
  if (!parsed || typeof parsed !== 'object') return []
  const ops = (parsed as Record<string, unknown>).operations
  if (!Array.isArray(ops)) return []
  const out: string[] = []
  const seen = new Set<string>()
  for (const op of ops) {
    if (!op || typeof op !== 'object') continue
    const rec = op as Record<string, unknown>
    if (typeof rec.mode !== 'string' || rec.mode.toLowerCase() !== 'image') continue
    if (!Array.isArray(rec.image_paths)) continue
    for (const raw of rec.image_paths) {
      if (typeof raw !== 'string') continue
      const p = raw.trim()
      if (!p || /^[a-z][a-z0-9+.-]*:\/\//i.test(p)) continue
      if (!isAbsoluteDiskPath(p) || !isSafePath(p) || seen.has(p)) continue
      seen.add(p)
      out.push(p)
      if (out.length >= MAX_TOOL_IMAGE_THUMBNAILS) return out
    }
  }
  return out
}
