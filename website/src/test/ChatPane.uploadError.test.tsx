import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* #5707: api.uploadFiles does NOT throw on a non-2xx — it resolves
 * { paths: [], error }. Before the fix ChatPane's mutation had only an
 * onSuccess reading res.paths, so a server refusal (bad type, signature
 * mismatch, over-cap) was silent: the spinner stopped with no attachment
 * and no message. ChatPane must now surface res.error the way ChatPage
 * does, reusing the existing pages.chatPage.upload_failed_error string. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
    chatSlotAgent: vi.fn().mockResolvedValue(undefined),
  },
  SEARCH_MIN_CHARS: 2,
  ApiError: class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [{ name: 'default' }, { name: 'reviewer' }], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'

function makeStore(slotKey: string) {
  return configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: slotKey, messages: 0, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
}

function renderPane(slotKey: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = makeStore(slotKey)
  return Object.assign(render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={slotKey} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>,
  ), { store })
}

function pickFile(input: HTMLElement, file: File) {
  Object.defineProperty(input, 'files', { value: [file], configurable: true })
  fireEvent.change(input)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ChatPane — upload error surface (#5707)', () => {
  it('renders the server refusal message when uploadFiles resolves { paths: [], error }', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: [], error: 'unsupported file type' })
    renderPane('pane-upload-refused')
    const fileInput = await screen.findByLabelText(/attach files/i)
    pickFile(fileInput, new File(['x'], 'evil.exe', { type: 'application/octet-stream' }))
    // pages.chatPage.upload_failed_error === "Upload failed: {{error}}"
    await waitFor(() => expect(screen.getByText(/Upload failed: unsupported file type/)).toBeInTheDocument())
  })

  it('shows no error banner and no message when the upload succeeds', async () => {
    ;(api.uploadFiles as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ paths: ['/tmp/ok.png'] })
    renderPane('pane-upload-ok')
    const fileInput = await screen.findByLabelText(/attach files/i)
    pickFile(fileInput, new File(['x'], 'ok.png', { type: 'image/png' }))
    await waitFor(() => expect(api.uploadFiles).toHaveBeenCalledTimes(1))
    expect(screen.queryByText(/Upload failed/)).not.toBeInTheDocument()
  })

  it('reports an oversized file (client refusal) without calling the server', async () => {
    renderPane('pane-upload-big')
    const fileInput = await screen.findByLabelText(/attach files/i)
    const big = new File(['x'], 'huge.mp4', { type: 'video/mp4' })
    Object.defineProperty(big, 'size', { value: 60 * 1024 * 1024 })
    Object.defineProperty(fileInput, 'files', { value: [big], configurable: true })
    fireEvent.change(fileInput)
    // pages.chatPage.file_too_large === "File too large: {{name}} (max 50 MB)"
    await waitFor(() => expect(screen.getByText(/File too large: huge\.mp4/)).toBeInTheDocument())
    expect(api.uploadFiles).not.toHaveBeenCalled()
  })

  it('reports too many files (client refusal) without calling the server', async () => {
    renderPane('pane-upload-many')
    const fileInput = await screen.findByLabelText(/attach files/i)
    const files = Array.from({ length: 21 }, (_, i) => new File(['x'], `f${i}.png`, { type: 'image/png' }))
    Object.defineProperty(fileInput, 'files', { value: files, configurable: true })
    fireEvent.change(fileInput)
    await waitFor(() => expect(screen.getByText(/Too many files/)).toBeInTheDocument())
    expect(api.uploadFiles).not.toHaveBeenCalled()
  })
})
