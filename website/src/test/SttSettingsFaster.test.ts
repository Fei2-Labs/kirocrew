// The faster-whisper provider's presence in the STT settings UI.
//
// The maps under test are the UI's whole contract with the backend's provider and
// install-step vocabularies: a provider missing from `PROVIDER_LABEL_KEY` renders
// as a bare id in the dropdown, a step missing from `STEP_LABEL_KEY` renders as an
// empty progress label, and a provider missing from `WHISPER_MODEL_PROVIDERS` gets
// no model picker at all despite the backend reading `stt.model` for it. All three
// are silent failures, which is why they are pinned rather than left to a
// screenshot.
//
// They are exported from the shipping module for the same reason
// `useMeetingSession`'s pure helpers are: so the test binds to the real values
// instead of a copy that can drift.

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'
import {
  PROVIDER_LABEL_KEY,
  STEP_LABEL_KEY,
  WHISPER_MODEL_PROVIDERS,
} from '../pages/settings/SttSettings'
import EN_MANUAL from '../i18n/locales/en.manual.json'

const manualStt = (EN_MANUAL as { pages: { settings: { sttSettings: Record<string, string> } } })
  .pages.settings.sttSettings

describe('provider labels', () => {
  it('labels every provider the backend can advertise', () => {
    // `_VALID_STT_PROVIDERS` in the config loader, mirrored by hand because it is
    // Python. An id absent here falls back to the raw string, so the dropdown would
    // read "faster". Keeping `apple` listed matters as much as `faster`: this list
    // is the mirror, so an omission here is the failure it is meant to catch.
    expect(Object.keys(PROVIDER_LABEL_KEY).sort()).toEqual(
      ['apple', 'faster', 'mlx', 'transcribe', 'whisper'],
    )
  })

  it('has a catalog string behind each label key', () => {
    for (const [provider, key] of Object.entries(PROVIDER_LABEL_KEY)) {
      const leaf = key.replace('pages.settings.sttSettings.', '')
      expect(manualStt[leaf], provider).toBeTruthy()
    }
  })
})

describe('the model picker gate', () => {
  it('covers the providers that name models by Whisper size', () => {
    // Both read `stt.model`, so both need the picker. Before this, selecting
    // `faster` showed no model control while the backend still used the field.
    expect(WHISPER_MODEL_PROVIDERS).toContain('whisper')
    expect(WHISPER_MODEL_PROVIDERS).toContain('faster')
  })

  it('excludes providers that do not', () => {
    // `mlx` takes a HuggingFace repo id in `mlx_model` — a different control.
    // `transcribe` runs server-side and has no local model at all.
    expect(WHISPER_MODEL_PROVIDERS).not.toContain('mlx')
    expect(WHISPER_MODEL_PROVIDERS).not.toContain('transcribe')
  })
})

describe('install progress steps', () => {
  it('labels the faster-whisper install step', () => {
    // The backend emits `installing_faster`; an unmapped step renders blank.
    expect(STEP_LABEL_KEY.installing_faster).toBe(
      'pages.settings.sttSettings.step_installing_faster',
    )
  })

  it('has a catalog string behind each step key', () => {
    for (const [step, key] of Object.entries(STEP_LABEL_KEY)) {
      const leaf = key.replace('pages.settings.sttSettings.', '')
      expect(manualStt[leaf], step).toBeTruthy()
    }
  })
})

describe('the faster-whisper install copy', () => {
  it('promises no SEPARATE ffmpeg install, not the absence of ffmpeg', () => {
    // The distinction is the whole accuracy of this string. FFmpeg is not gone --
    // it arrives inside PyAV's wheel and faster-whisper decodes through it
    // in-process. What is true, and what makes this provider worth choosing on a
    // machine where the CLI toolchain is the hard part, is that there is no
    // separate ffmpeg to install. Claiming "no ffmpeg needed" would tell the user
    // something false about what lands on their machine.
    const blurb = manualStt.installs_faster_whisper_no_ffmpeg_needed
    expect(blurb).toBeTruthy()
    expect(blurb.toLowerCase()).toContain('ffmpeg')
    expect(blurb.toLowerCase()).toContain('separate')
    expect(blurb).toMatch(/PyAV/i)
  })

  it('warns about the platform with no CTranslate2 wheel', () => {
    expect(manualStt.installs_faster_whisper_no_ffmpeg_needed).toContain('Windows on ARM')
  })

  it('has a button label', () => {
    expect(manualStt.install_faster_whisper).toBeTruthy()
  })
})

describe('the unsupported-platform notice', () => {
  // Pre-click gating. Without a served `faster_unsupported` flag the card could only
  // discover the platform is unsupported by pressing Install and reading a 400 --
  // the same dead end on every press, which is the failure this notice removes.
  it('exists in the catalog', () => {
    expect(manualStt.faster_unsupported_windows_arm).toBeTruthy()
  })

  it('names the platform and the reason, not just a failure', () => {
    const notice = manualStt.faster_unsupported_windows_arm
    expect(notice).toContain('Windows on ARM')
    // The reason matters: it is what tells the user retrying cannot help.
    expect(notice).toContain('CTranslate2')
  })

  it('names both working alternatives', () => {
    // A bare refusal leaves the user stuck. These two are the providers that do
    // work here, and they are the same pair cli_doctor prints.
    const notice = manualStt.faster_unsupported_windows_arm
    expect(notice.toLowerCase()).toContain('whisper')
    expect(notice).toMatch(/transcribe/i)
  })

  it('is translated into every shipped catalog', () => {
    // Read the catalogs exactly as the runtime composes them — including English's
    // generated (`en.json`) + manual (`en.manual.json`) merge. Globbing the locale
    // JSON directly would flag `en.json`, which is regenerated wholesale from source
    // scanning and legitimately never carries a hand-authored key.
    const seen: string[] = []
    for (const [code, bundle] of Object.entries(RUNTIME_CATALOGS)) {
      const root = (bundle as { translation: unknown }).translation as {
        pages?: { settings?: { sttSettings?: Record<string, string> } }
      }
      const value = root.pages?.settings?.sttSettings?.faster_unsupported_windows_arm
      // A missing key renders the dotted path into the UI, and this notice is the
      // only thing standing between a win-arm user and an unexplained dead end.
      expect(value, code).toBeTruthy()
      seen.push(code)
    }
    // Guard the guard: an empty catalog map would make the loop vacuously pass.
    expect(seen.length).toBeGreaterThanOrEqual(SUPPORTED_LANGUAGES.length)
  })
})
