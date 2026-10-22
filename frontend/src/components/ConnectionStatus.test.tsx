import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ConnectionStatus from './ConnectionStatus'

const BASE_STATUS = {
  state: 'not_configured',
  login_available: false,
  provider_mode: 'unavailable',
  can_disconnect: false,
  last_success_at: null,
  error_code: null,
  cooldown_until: null,
  session_expires_at: null,
  validate_available: false,
  unavailable_reason: 'demo_mode',
}

function response(data: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => data }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

it('shows an honest disabled state when the real provider is unavailable', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response(BASE_STATUS)))
  render(<ConnectionStatus />)
  expect(await screen.findByText('未配置')).toBeInTheDocument()
  expect(screen.getByText(/演示模式 · 不连接 Pixiv/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '登录 Pixiv' })).toBeDisabled()
})

it('explains missing chromium instead of claiming the component is uninstalled', async () => {
  const status = {
    ...BASE_STATUS,
    validate_available: true,
    unavailable_reason: 'missing_browser',
  }
  vi.stubGlobal('fetch', vi.fn(async () => response(status)))
  render(<ConnectionStatus />)
  expect(await screen.findByText(/缺少 Chromium runtime/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '使用已保存登录' })).toBeEnabled()
  expect(screen.getByRole('button', { name: '登录 Pixiv' })).toBeDisabled()
})

it('validates saved login only from an explicit click', async () => {
  const available = {
    ...BASE_STATUS,
    login_available: true,
    validate_available: true,
    unavailable_reason: null,
    provider_mode: 'fake_oauth',
  }
  const connected = { ...available, state: 'connected', can_disconnect: true }
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    if (String(input).endsWith('/api/auth/validate')) return response(connected)
    return response(available)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<ConnectionStatus />)
  fireEvent.click(
    await screen.findByRole('button', { name: '使用已保存登录' }),
  )
  expect(await screen.findByText('已连接')).toBeInTheDocument()
  const validateCall = fetchMock.mock.calls.find(([url]) =>
    String(url).endsWith('/api/auth/validate'),
  )
  expect(validateCall?.[1]).toMatchObject({
    method: 'POST',
    body: JSON.stringify({ user_initiated: true }),
  })
})

it('starts only from an explicit click and sends user_initiated=true', async () => {
  const available = { ...BASE_STATUS, login_available: true, provider_mode: 'fake_oauth' }
  const authorizing = { ...available, state: 'authorizing', session_expires_at: '2026-08-31T01:05:00+00:00' }
  const fetchMock = vi.fn(async (...args: [RequestInfo | URL, RequestInit?]) => {
    const input = args[0]
    if (String(input).endsWith('/api/auth/start')) {
      return response({
        status: authorizing,
        launch: { mode: 'fake_oauth', browser_opened: false },
      })
    }
    return response(available)
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<ConnectionStatus />)
  fireEvent.click(await screen.findByRole('button', { name: '登录 Pixiv' }))
  expect(await screen.findByText('授权中')).toBeInTheDocument()
  const startCall = fetchMock.mock.calls.find(([url]) =>
    String(url).endsWith('/api/auth/start'),
  )
  expect(startCall).toBeDefined()
  expect(startCall?.[1]).toMatchObject({
    method: 'POST',
    body: JSON.stringify({ user_initiated: true }),
  })
})

it('disconnects only after confirmation', async () => {
  const connected = {
    ...BASE_STATUS,
    state: 'connected',
    login_available: true,
    provider_mode: 'fake_oauth',
    can_disconnect: true,
  }
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).endsWith('/api/auth/disconnect')) return response(BASE_STATUS)
    return response(connected)
  })
  vi.stubGlobal('fetch', fetchMock)
  const confirmMock = vi.spyOn(window, 'confirm').mockReturnValue(false)
  render(<ConnectionStatus />)
  const disconnect = await screen.findByRole('button', { name: '断开连接' })
  fireEvent.click(disconnect)
  expect(confirmMock).toHaveBeenCalledOnce()
  expect(fetchMock).toHaveBeenCalledTimes(1)

  confirmMock.mockReturnValue(true)
  fireEvent.click(disconnect)
  await waitFor(() => {
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
  expect(await screen.findByText('未配置')).toBeInTheDocument()
})

it('keeps auth status failure local to its card', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('offline') }))
  render(<ConnectionStatus />)
  expect(await screen.findByText(/无法读取登录状态/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
})

it('shows supervised browser instructions before login', async () => {
  const available = {
    ...BASE_STATUS,
    login_available: true,
    provider_mode: 'supervised_browser',
  }
  vi.stubGlobal('fetch', vi.fn(async () => response(available)))
  render(<ConnectionStatus />)
  expect(await screen.findByText(/只在 Pixiv 页面输入/)).toBeInTheDocument()
  expect(screen.getByText(/应用不保存密码/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '登录 Pixiv' })).toBeEnabled()
})

it('shows cancel and remaining time while authorizing, without any code/token', async () => {
  const authorizing = {
    ...BASE_STATUS,
    state: 'authorizing',
    login_available: true,
    provider_mode: 'supervised_browser',
    session_expires_at: new Date(Date.now() + 4 * 60 * 1000).toISOString(),
  }
  vi.stubGlobal('fetch', vi.fn(async () => response(authorizing)))
  render(<ConnectionStatus />)
  expect(await screen.findByText('授权中')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '取消授权' })).toBeEnabled()
  expect(screen.getByText(/授权窗口剩余约 4 分/)).toBeInTheDocument()
  // 不展示 URL、code、state、session 或 token 信息
  expect(screen.queryByText(/http|code|state|token/i)).toBeNull()
})

it('maps browser action required to a plain message', async () => {
  const status = {
    ...BASE_STATUS,
    state: 'user_action_required',
    error_code: 'browser_action_required',
  }
  vi.stubGlobal('fetch', vi.fn(async () => response(status)))
  render(<ConnectionStatus />)
  expect(await screen.findByText(/浏览器未就绪或已关闭/)).toBeInTheDocument()
})

it('maps timeout to a plain message', async () => {
  const status = {
    ...BASE_STATUS,
    state: 'user_action_required',
    error_code: 'authorization_timeout',
  }
  vi.stubGlobal('fetch', vi.fn(async () => response(status)))
  render(<ConnectionStatus />)
  expect(await screen.findByText(/上一次授权已超时/)).toBeInTheDocument()
})

it('re-enables explicit actions after the cooldown expires, without auto-posting', async () => {
  vi.useFakeTimers()
  try {
    vi.setSystemTime(new Date('2026-09-15T00:00:00Z'))
    let posts = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
        if (init?.method === 'POST') posts += 1
        return response({
          ...BASE_STATUS,
          state: 'rate_limited',
          login_available: true,
          validate_available: true,
          provider_mode: 'supervised_browser',
          error_code: 'authorization_rate_limited',
          cooldown_until: '2026-09-15T00:00:02Z',
        })
      }),
    )
    await act(async () => {
      render(<ConnectionStatus />)
    })
    expect(
      screen.getByRole('button', { name: '使用已保存登录' }),
    ).toBeDisabled()
    expect(screen.getByText(/冷却剩余 2 秒/)).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(posts).toBe(0)
    expect(
      screen.getByRole('button', { name: '使用已保存登录' }),
    ).toBeEnabled()
    expect(screen.getByText(/冷却已到期/)).toBeInTheDocument()
  } finally {
    vi.useRealTimers()
  }
})
