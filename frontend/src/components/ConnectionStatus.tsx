import { useEffect, useRef, useState } from 'react'
import {
  AuthState,
  AuthStatus,
  cancelAuth,
  disconnectAuth,
  fetchAuthStatus,
  startAuth,
  validateAuth,
} from '../api'

const STATE_VIEW: Record<
  AuthState,
  { label: string; badge: string; message: string }
> = {
  not_configured: {
    label: '未配置',
    badge: 'muted',
    message: '尚未连接 Pixiv。',
  },
  authorizing: {
    label: '授权中',
    badge: 'info',
    message: '请在已打开的浏览器中完成操作。',
  },
  connected: {
    label: '已连接',
    badge: 'success',
    message: '授权凭据已安全保存并通过本次验证。',
  },
  expired_or_revoked: {
    label: '需要重新授权',
    badge: 'warning',
    message: '现有授权已失效。',
  },
  rate_limited: {
    label: '暂时限流',
    badge: 'warning',
    message: '请等待冷却时间结束后重试。',
  },
  temporarily_unavailable: {
    label: '暂时不可用',
    badge: 'error',
    message: '本地凭据后端或授权服务当前不可用。',
  },
  user_action_required: {
    label: '需要操作',
    badge: 'warning',
    message: '请按提示完成或重新开始授权。',
  },
}

const ERROR_MESSAGES: Record<string, string> = {
  credential_validation_required:
    '检测到本项目已有本机凭据，但尚未在本次启动中验证。',
  credential_backend_unavailable: 'Windows 凭据存储当前不可用。',
  pixiv_login_required:
    '没有可用的已保存登录；请点击“登录 Pixiv”完成一次授权。',
  authorization_cancelled: '上一次授权已取消。',
  authorization_timeout: '上一次授权已超时，请重新开始。',
  authorization_state_mismatch: '授权校验未通过，会话已安全终止。',
  authorization_failed: '授权未完成，请重新尝试。',
  authorization_rejected: '登录已失效，请重新连接。',
  authorization_rate_limited: '请求被限流，请等待冷却时间结束后重试。',
  authorization_invalid_result: '授权结果无效，请重新尝试。',
  authorization_temporarily_unavailable: '授权服务暂时不可用，请稍后重试。',
  browser_action_required: '浏览器未就绪或已关闭，请重新尝试。',
}

const CAPABILITY_MESSAGES: Record<string, string> = {
  demo_mode: '演示模式 · 不连接 Pixiv；如需真实登录，请用“启动 Pixiv 小说雷达.cmd”打开真实模式。',
  missing_dependency: '缺少 Python 登录依赖；请在 backend 运行 pip install -r requirements.txt。',
  missing_browser:
    '缺少 Chromium runtime；请在 backend 运行 .venv\\Scripts\\python.exe -m playwright install chromium。',
}

function remainingSeconds(
  expiresAt: string | null,
  now: number = Date.now(),
): number | null {
  if (!expiresAt) return null
  const ms = new Date(expiresAt).getTime() - now
  if (Number.isNaN(ms) || ms <= 0) return 0
  return Math.ceil(ms / 1000)
}

function formatRemaining(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return m > 0 ? `${m} 分 ${s} 秒` : `${s} 秒`
}

function ConnectionStatus() {
  const [status, setStatus] = useState<AuthStatus | null>(null)
  const [loadError, setLoadError] = useState(false)
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [nowTick, setNowTick] = useState(() => Date.now())
  const mounted = useRef(true)

  async function load() {
    if (mounted.current) setLoadError(false)
    try {
      const nextStatus = await fetchAuthStatus()
      if (mounted.current) setStatus(nextStatus)
    } catch {
      if (mounted.current) setLoadError(true)
    }
  }

  useEffect(() => {
    mounted.current = true
    void load()
    return () => {
      mounted.current = false
    }
  }, [])

  useEffect(() => {
    if (status?.state !== 'authorizing') return
    const timer = window.setInterval(() => {
      void load()
    }, 2000)
    return () => window.clearInterval(timer)
  }, [status?.state])

  // R9：冷却到期只恢复本地按钮；本地每秒重算剩余时间，绝不自动 POST。
  useEffect(() => {
    const until = status?.cooldown_until ?? null
    if (!until) return
    const target = new Date(until).getTime()
    if (Number.isNaN(target) || target <= Date.now()) return
    const timer = window.setInterval(() => {
      if (Date.now() >= target) window.clearInterval(timer)
      setNowTick(Date.now())
    }, 1000)
    return () => window.clearInterval(timer)
  }, [status?.cooldown_until])

  async function run(action: () => Promise<AuthStatus>) {
    setBusy(true)
    setActionError(null)
    try {
      const nextStatus = await action()
      if (mounted.current) setStatus(nextStatus)
    } catch {
      if (mounted.current) setActionError('操作未完成，请稍后重试。')
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function handleStart() {
    setBusy(true)
    setActionError(null)
    try {
      const result = await startAuth()
      if (mounted.current) setStatus(result.status)
    } catch {
      if (mounted.current) setActionError('登录组件暂时无法启动。')
      // 被共享冷却拒绝时刷新状态，让剩余时间来自后端而不是猜测。
      void load()
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  async function handleValidate() {
    setBusy(true)
    setActionError(null)
    try {
      const nextStatus = await validateAuth()
      if (mounted.current) setStatus(nextStatus)
    } catch {
      if (mounted.current) setActionError('验证已保存登录失败，请稍后重试。')
      void load()
    } finally {
      if (mounted.current) setBusy(false)
    }
  }

  function handleDisconnect() {
    if (!window.confirm('确认断开 Pixiv？只会删除本项目的固定凭据项。')) {
      return
    }
    void run(disconnectAuth)
  }

  if (status === null) {
    return (
      <div className="card" aria-busy={!loadError}>
        <h2 className="card__title">连接状态</h2>
        {loadError ? (
          <p className="error-text">
            无法读取登录状态。{' '}
            <button className="button button--secondary" onClick={() => void load()}>
              重试
            </button>
          </p>
        ) : (
          <p className="muted">正在检查本地登录状态…</p>
        )}
      </div>
    )
  }

  const view = STATE_VIEW[status.state]
  const errorMessage = status.error_code
    ? ERROR_MESSAGES[status.error_code]
    : null
  const cooldownRemaining = status.cooldown_until
    ? remainingSeconds(status.cooldown_until, nowTick)
    : null
  const cooldownActive =
    status.state === 'rate_limited' &&
    cooldownRemaining !== null &&
    cooldownRemaining > 0
  const startDisabled =
    busy ||
    !status.login_available ||
    status.state === 'authorizing' ||
    cooldownActive
  const validateDisabled =
    busy ||
    !status.validate_available ||
    status.state === 'authorizing' ||
    cooldownActive
  const capabilityMessage = status.unavailable_reason
    ? CAPABILITY_MESSAGES[status.unavailable_reason]
    : null

  return (
    <div className="card" aria-live="polite">
      <h2 className="card__title">连接状态</h2>
      <div className="connection-row">
        <div>
          <p className="connection-copy">
            <span className={`badge badge--${view.badge}`}>{view.label}</span>{' '}
            <span>{view.message}</span>
          </p>
          {errorMessage && <p className="muted">{errorMessage}</p>}
          {capabilityMessage && <p className="muted">{capabilityMessage}</p>}
          {status.state === 'not_configured' && status.login_available && (
            <p className="muted">
              登录将打开专用可见浏览器：账号、密码与两步验证只在 Pixiv 页面输入；
              应用不保存密码。若之前已登录过，可直接点“使用已保存登录”。
            </p>
          )}
          {status.state === 'authorizing' && (
            <p className="muted" aria-live="polite">
              请在已打开的浏览器中完成操作。
              {remainingSeconds(status.session_expires_at) !== null &&
                ` 授权窗口剩余约 ${formatRemaining(remainingSeconds(status.session_expires_at) ?? 0)}。`}
            </p>
          )}
          {status.state === 'rate_limited' && (
            <p className="muted" aria-live="polite">
              {cooldownActive
                ? `共享出站冷却剩余 ${formatRemaining(cooldownRemaining ?? 0)}；到期后可点“使用已保存登录”或重新授权，不会自动连接。`
                : '冷却已到期：可显式点“使用已保存登录”或重新授权恢复；不会自动连接。'}
            </p>
          )}
          {actionError && <p className="error-text">{actionError}</p>}
        </div>
        <div className="connection-actions">
          {status.state === 'authorizing' ? (
            <button
              className="button button--secondary"
              disabled={busy}
              onClick={() => void run(cancelAuth)}
            >
              取消授权
            </button>
          ) : (
            <>
              <button
                className="button button--primary"
                disabled={startDisabled}
                onClick={() => void handleStart()}
                aria-label="登录 Pixiv"
              >
                {status.login_available
                  ? status.can_disconnect
                    ? '重新授权'
                    : '登录 Pixiv'
                  : '登录不可用'}
              </button>
              {status.validate_available && (
                <button
                  className="button button--secondary"
                  disabled={validateDisabled}
                  onClick={() => void handleValidate()}
                  aria-label="使用已保存登录"
                >
                  使用已保存登录
                </button>
              )}
            </>
          )}
          {status.can_disconnect && (
            <button
              className="button button--danger"
              disabled={busy}
              onClick={handleDisconnect}
            >
              断开连接
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default ConnectionStatus
