import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { createAudioSample } from '../hooks/mic'

/**
 * Hold-to-talk mode: the WeChat-style swap where the mic becomes a mode switch
 * and the textarea is replaced by a press-and-hold target.
 *
 * The gesture's own state machine is covered in
 * `src/hooks/useTouchPushToTalk.test.ts`. What is covered HERE is the wiring
 * ChatInput owns: which surface is mounted, and — the part a reviewer caught —
 * that a draft arriving mid-capture does not tear the hold target out from under
 * the finger.
 */

vi.mock('../components/Strands', () => ({
  __esModule: true,
  default: () => <div data-testid="strands-stub" />,
  strandsSupported: () => true,
}))

const sampleRef = { current: createAudioSample() }

/** The full voice wiring ChatPage supplies; hold mode needs start/stop/cancel. */
const voiceProps = {
  onVoiceToggle: vi.fn(),
  onVoiceCancel: vi.fn(),
  onVoicePrewarm: vi.fn(),
  onVoiceStart: vi.fn(),
  onVoiceStop: vi.fn(),
  voiceSampleRef: sampleRef,
}

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  connected: true,
}

/** Coarse pointer is what gates the feature — `isTouchDevice()` reads matchMedia. */
function stubTouch(coarse: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: coarse && /coarse|hover: none/.test(q),
    media: q,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }))
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  stubTouch(true)
})

describe('ChatInput — hold-to-talk mode', () => {
  it('offers the mode switch on touch and swaps the textarea for the hold target', () => {
    renderWithProviders(<ChatInput {...base} {...voiceProps} />)

    const toSpeech = screen.getByRole('button', { name: 'Switch to voice' })
    expect(screen.queryByTestId('hold-to-talk')).toBeNull()

    fireEvent.click(toSpeech)
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
    // The textarea stays MOUNTED (sr-only) so value, caret and IME state survive.
    expect(screen.getByLabelText('Message input')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch to keyboard' })).toBeTruthy()
  })

  it('does not offer the mode switch on a fine pointer', () => {
    stubTouch(false)
    renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    expect(screen.queryByRole('button', { name: 'Switch to voice' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Voice input' })).toBeTruthy()
  })

  // With a draft there is no mode to switch into, so the mic must go back to being
  // a record button. Disabling it instead removed dictating ONTO existing text for
  // every touch user — including anyone who never opened hold mode, since the mic is
  // the only voice entry point there.
  it('suspends hold mode with a draft and reverts the mic to a record control', () => {
    localStorage.setItem('mc-voice-mode', '1')
    renderWithProviders(<ChatInput {...base} {...voiceProps} value="a typed draft" />)

    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
    const mic = screen.getByRole('button', { name: 'Voice input' })
    expect((mic as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(mic)
    expect(voiceProps.onVoiceToggle).toHaveBeenCalled()
  })

  // The regression a reviewer caught on the streaming path: `onPartial` writes each
  // hypothesis into the composer WHILE the finger is still down, so suspending on
  // that draft would unmount the hold target mid-gesture — taking the pointer
  // listeners with it, so the release and the slide-up land on nothing while
  // capture keeps running.
  it('keeps the hold target mounted when a streaming partial lands mid-capture', () => {
    localStorage.setItem('mc-voice-mode', '1')
    const startFn = vi.fn(() => Promise.resolve())
    const { rerender } = renderWithProviders(
      <ChatInput {...base} {...voiceProps} onVoiceStart={startFn} voiceCaptureActive />,
    )
    const bar = screen.getByTestId('hold-to-talk') as HTMLButtonElement
    expect(bar).toBeTruthy()

    // A real pointer gesture initiates capture — this is what sets touchPtt.owns.
    fireEvent.pointerDown(bar, { pointerId: 1, pointerType: 'touch', isPrimary: true, clientY: 400 })

    // Re-render with the capture now gated (voiceRecording) and a streaming partial.
    rerender(
      <ChatInput {...base} {...voiceProps} onVoiceStart={startFn} voiceRecording voiceCaptureActive value="arm auto merge on" />,
    )
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Switch to keyboard' })).toBeTruthy()

    // Release the gesture.
    fireEvent.pointerUp(bar, { pointerId: 1, pointerType: 'touch', isPrimary: true, clientY: 400 })

    // Capture ends — only now may the draft reclaim the textarea.
    rerender(<ChatInput {...base} {...voiceProps} onVoiceStart={startFn} value="arm auto merge on" />)
    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
  })

  // `voiceRecording` is ownership-gated (`owned && recording`) and ownership lands
  // only after the streaming handshake, so during that window capture is real and
  // the gated flag still reads false. The hold target must not vanish there — that
  // is the mid-gesture unmount hazard, and it would also make the hook's commit
  // veto answer "no capture" for audio that exists.
  it('keeps the hold target mounted on ungated capture, before ownership lands', () => {
    localStorage.setItem('mc-voice-mode', '1')
    const startFn = vi.fn(() => Promise.resolve())
    // Hold mode must be established FIRST. Rendering straight into a draft with
    // capture live is the *other* scenario — dictation started from the mic over a
    // draft — and asserting the bar there is what locked in an unstoppable mic.
    const { rerender } = renderWithProviders(
      <ChatInput {...base} {...voiceProps} onVoiceStart={startFn} voiceRecording={false} voiceCaptureActive />,
    )
    const bar = screen.getByTestId('hold-to-talk') as HTMLButtonElement
    expect(bar).toBeTruthy()

    // A real pointer gesture initiates capture — this is what sets touchPtt.owns.
    fireEvent.pointerDown(bar, { pointerId: 1, pointerType: 'touch', isPrimary: true, clientY: 400 })

    // Ownership has not landed yet (`voiceRecording` still false) while a partial
    // already fills the composer: the bar must survive on the touch ownership.
    rerender(
      <ChatInput {...base} {...voiceProps} onVoiceStart={startFn} voiceRecording={false} voiceCaptureActive value="a streaming partial" />,
    )
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
  })

  it('does not promote a draft composer into hold mode when dictation starts from the mic', () => {
    localStorage.setItem('mc-voice-mode', '1')
    // The saved preference is on, but a draft is already present, so the mic is a
    // record button and the bar was never mounted. Capture starting on that route
    // must NOT swap in a bar that renders `settling` (disabled) beside a disabled
    // mode switch — that leaves a live microphone with nothing able to stop it.
    renderWithProviders(
      <ChatInput {...base} {...voiceProps} voiceRecording voiceCaptureActive value="a draft the user typed" />,
    )

    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
    const stop = screen.getByRole('button', { name: 'Stop recording' })
    expect((stop as HTMLButtonElement).disabled).toBe(false)
  })

  // The scenario from the issue: a coarse-pointer device with a hardware keyboard
  // (e.g. iPad with a keyboard case). Hold mode is on and the composer is empty,
  // so the bar is mounted. The user starts dictation with the KEYBOARD push-to-talk
  // binding. A streaming partial lands, giving the composer a draft. Without the
  // ownership gate, voiceHoldMode stayed true because `holdTarget !== null`. With
  // the fix, `touchPtt.owns` is false because no pointer gesture was fired, and
  // voiceHoldMode becomes false once the draft arrives.
  it('does not promote composer into hold mode when keyboard PTT starts capture on a touch device', () => {
    localStorage.setItem('mc-voice-mode', '1')
    // Hold mode on, composer empty — bar is mounted.
    const { rerender } = renderWithProviders(
      <ChatInput {...base} {...voiceProps} />,
    )
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()

    // Keyboard binding starts capture (voiceCaptureActive true), no pointer gesture
    // on the hold target. A streaming partial arrives in the composer value.
    rerender(
      <ChatInput {...base} {...voiceProps} voiceCaptureActive voiceRecording value="a streaming partial from keyboard" />,
    )

    // touchPtt.owns is false (no gesture), so the draft suspends hold mode.
    // The hold target should NOT remain mounted.
    expect(screen.queryByTestId('hold-to-talk')).toBeNull()
  })

  // The drain-window settling hint ("Finishing...") should only appear for captures
  // the touch gesture owns. If capture was started by the keyboard binding (no
  // pointer gesture), the bar must not show settling even when recording is true.
  it('shows settling hint only for touch-owned captures, not keyboard-started ones', () => {
    localStorage.setItem('mc-voice-mode', '1')
    // Hold mode on, composer empty. Keyboard starts capture (no pointer gesture).
    const { rerender } = renderWithProviders(
      <ChatInput {...base} {...voiceProps} />,
    )
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()

    // Keyboard-initiated capture: voiceRecording=true but no pointer gesture was
    // fired. The composer is still empty so hold mode stays on.
    rerender(
      <ChatInput {...base} {...voiceProps} voiceCaptureActive voiceRecording />,
    )

    // The bar should exist (hold mode still on — no draft), but should NOT show
    // "settling" or "Finishing" because the touch hook does not own this capture.
    const bar = screen.getByTestId('hold-to-talk')
    expect(bar.textContent).not.toContain('Finishing')
  })

  // Label, icon, action and disabled state all come from one predicate, because
  // deriving them separately is how a control comes to say one thing and do
  // another. The path that caught it: a streaming partial lands mid-capture, so the
  // composer has a draft while capture is still live — the label read "Switch to
  // keyboard" (hold mode is still on, capture overrides the draft) while the click
  // stopped the recording instead.
  it('keeps the mic label and its action in agreement during streaming capture', () => {
    localStorage.setItem('mc-voice-mode', '1')
    const startFn = vi.fn(() => Promise.resolve())
    // Establish hold mode before the draft exists.
    const { rerender } = renderWithProviders(
      <ChatInput {...base} {...voiceProps} onVoiceStart={startFn} voiceCaptureActive />,
    )
    const bar = screen.getByTestId('hold-to-talk') as HTMLButtonElement

    // A real pointer gesture sets touchPtt.owns.
    fireEvent.pointerDown(bar, { pointerId: 1, pointerType: 'touch', isPrimary: true, clientY: 400 })

    // Now capture is fully live and a streaming partial arrives.
    rerender(
      <ChatInput {...base} {...voiceProps} onVoiceStart={startFn} voiceRecording voiceCaptureActive value="a streaming partial" />,
    )

    // Hold mode survives the draft while touch-owned capture is live, so this is still a switch.
    const mic = screen.getByRole('button', { name: 'Switch to keyboard' })
    // ...and a mode cannot be changed mid-capture, so the switch is disabled rather
    // than silently doing the other job.
    expect((mic as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByRole('button', { name: 'Stop recording' })).toBeNull()
  })

  it('disables the voice controls while ANOTHER session is still transcribing', () => {
    localStorage.setItem('mc-voice-mode', '1')
    // The gated flag is false — this slot owns nothing — but `startVoice` refuses
    // on the global one, so an enabled bar would invite a press and capture
    // nothing. Same shape as the capture split: an ownership-gated flag being
    // asked a question that is global.
    renderWithProviders(
      <ChatInput {...base} {...voiceProps} voiceTranscribing={false} voiceTranscribeActive />,
    )

    const bar = screen.getByTestId('hold-to-talk') as HTMLButtonElement
    expect(bar.disabled).toBe(true)
    expect(bar.textContent).toContain('Transcribing')

    // ...but the way OUT of voice mode must stay open. As a mode switch the mic
    // starts no capture, so a transcription in another session is no reason to
    // disable it — doing that strands this slot in voice mode with no way to type.
    const mic = screen.getByRole('button', { name: 'Switch to keyboard' }) as HTMLButtonElement
    expect(mic.disabled).toBe(false)
  })

  it('remembers the mode across mounts', () => {
    const first = renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    fireEvent.click(screen.getByRole('button', { name: 'Switch to voice' }))
    expect(localStorage.getItem('mc-voice-mode')).toBe('1')
    first.unmount()

    renderWithProviders(<ChatInput {...base} {...voiceProps} />)
    expect(screen.getByTestId('hold-to-talk')).toBeTruthy()
  })
})
