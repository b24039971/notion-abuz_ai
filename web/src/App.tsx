import { useState, useEffect, useMemo, useCallback, useRef } from 'react'
import type { DashboardData, AccountInfo, AccountSummary, RefreshStatus, TokenStats } from './types'
import { fetchDashboardData, openProxy, openBestProxy, checkAuth, login, logout, triggerRefresh, fetchSettings, updateSettings, addAccount, fetchTokenStats } from './api'
import type { SearchSettings } from './api'
import { fmt, formatTokens, getQuotaStatusByUsage, getQuotaPct, avatarColor, avatarLetter, formatCheckedAt, formatTimestampMs, providerDisplay } from './utils'
import { AccountMenu } from './components/AccountMenu'
import { RegisterModal } from './components/RegisterModal'
import { HistoryDrawer } from './components/HistoryDrawer'
import { AdminModels } from './components/AdminModels'
import { IconUserPlus, IconHistory } from './components/Icons'

// --- Icons ---
const IconBarChart = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/>
  </svg>
)
const IconRefresh = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M21 2v6h-6"/><path d="M21 13a9 9 0 1 1-3-7.7L21 8"/>
  </svg>
)
const IconZap = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
  </svg>
)
const IconClock = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
  </svg>
)
const IconFlask = () => (
  <svg className="w-3 h-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M10 2v7.31" />
    <path d="M14 9.3V1.99" />
    <path d="M8.5 2h7" />
    <path d="M14 9.3a6.5 6.5 0 1 1-4 0" />
    <path d="M5.52 16h12.96" />
  </svg>
)
const IconActivity = () => (
  <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
  </svg>
)
const IconSettings = () => (
  <svg className="w-3.5 h-3.5 text-text-secondary" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
)

const IconPlus = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
  </svg>
)
const IconTrash = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
  </svg>
)

const IconTelegram = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="m22 2-7 20-4-9-9-4Z"/>
    <path d="M22 2 11 13"/>
  </svg>
)

const IconYoutube = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M2.5 17a24.12 24.12 0 0 1 0-10 2 2 0 0 1 1.4-1.4 49.56 49.56 0 0 1 16.2 0A2 2 0 0 1 21.5 7a24.12 24.12 0 0 1 0 10 2 2 0 0 1-1.4 1.4 49.55 49.55 0 0 1-16.2 0A2 2 0 0 1 2.5 17Z"/>
    <polygon points="10 15 15 12 10 9 10 15"/>
  </svg>
)

// --- Add Account Modal ---

function AddAccountModal({ onClose, onSuccess }: { onClose: () => void; onSuccess: () => void }) {
  const [token, setToken] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<{ name: string; email: string; space: string; plan_type: string } | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = token.trim()
    if (!trimmed) return
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const res = await addAccount(trimmed)
      if (res.error) {
        setError(res.error)
      } else if (res.account) {
        setResult(res.account)
        setTimeout(() => {
          onSuccess()
          onClose()
        }, 1500)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка запроса')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-lg bg-[#1a1a1a] border border-white/10 rounded-xl shadow-2xl p-6" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-[16px] font-semibold">Добавить аккаунт Notion</h2>
          <button onClick={onClose} className="text-text-muted hover:text-white bg-transparent border-none cursor-pointer text-lg px-1">×</button>
        </div>

        <div className="text-[12px] text-text-secondary mb-4 space-y-1.5">
          <p>Вставьте ваш <code className="bg-white/[.08] px-1 py-0.5 rounded text-[11px]">token_v2</code> cookie, система автоматически получит информацию об аккаунте.</p>
          <p className="text-text-muted">Как получить: Откройте <code className="bg-white/[.08] px-1 py-0.5 rounded text-[11px]">notion.so</code> → F12 → Application → Cookies → Скопируйте значение <code className="bg-white/[.08] px-1 py-0.5 rounded text-[11px]">token_v2</code></p>
        </div>

        <form onSubmit={handleSubmit}>
          <textarea
            ref={inputRef}
            value={token}
            onChange={e => setToken(e.target.value)}
            placeholder="Вставьте значение token_v2..."
            rows={3}
            className="w-full py-2.5 px-3 bg-transparent border border-white/10 rounded-lg text-[13px] text-text-primary outline-none focus:border-white/30 focus:ring-1 focus:ring-white/10 transition-all placeholder:text-white/25 resize-none font-mono"
          />
          {error && (
            <div className="text-err text-[12px] mt-2 px-1">{error}</div>
          )}
          {result && (
            <div className="mt-3 p-3 bg-[#0a3d0a]/50 border border-[#1b5e20]/50 rounded-lg text-[12px]">
              <div className="text-[#4ade80] font-medium mb-1.5">Успешно добавлено</div>
              <div className="space-y-0.5 text-text-secondary">
                <div>Пользователь: <span className="text-white">{result.name}</span> ({result.email})</div>
                <div>Рабочее пространство: <span className="text-white">{result.space}</span> · {result.plan_type}</div>
              </div>
            </div>
          )}
          <div className="flex gap-2.5 mt-4">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 py-2.5 bg-transparent hover:bg-white/5 text-text-secondary rounded-lg text-[13px] font-medium cursor-pointer transition-colors border border-white/10"
            >
              Отмена
            </button>
            <button
              type="submit"
              disabled={loading || !token.trim() || !!result}
              className="flex-1 py-2.5 bg-white hover:bg-white/90 text-black rounded-lg text-[13px] font-semibold cursor-pointer transition-colors border-none disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {loading ? 'Проверка...' : 'Добавить аккаунт'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// --- Login Page ---

function LoginPage({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!password.trim()) return
    setLoading(true)
    setError('')
    try {
      const result = await login(password)
      if (result.ok) {
        onSuccess()
        return
      }
      setError(result.error || 'Неверный пароль')
      setPassword('')
      inputRef.current?.focus()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка запроса')
      setPassword('')
      inputRef.current?.focus()
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="w-32 h-32 rounded-full border border-[#00ff66]/20 flex items-center justify-center overflow-hidden mb-5 logo-glow relative bg-[#040806]">
            <img src="/logo.png" alt="ВАЙБ КОДЕР" className="w-full h-full object-cover scale-[1.05]" />
          </div>
          <h1 className="text-2xl font-extrabold tracking-tight text-white cyber-text-glow-bright uppercase">ВАЙБ КОДЕР</h1>
          <p className="text-[11px] text-text-secondary font-mono uppercase tracking-widest text-[#00ff66] mt-1.5">notion-manager panel</p>
          <p className="text-[12px] text-text-muted mt-3 font-medium">Введите ключ администратора для доступа к Dashboard</p>
        </div>
        <form onSubmit={handleSubmit}>
          <div className="relative mb-4">
            <input
              ref={inputRef}
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="Ключ администратора"
              autoComplete="current-password"
              className="w-full py-2.5 px-4 bg-transparent border border-white/10 rounded-lg text-[14px] text-text-primary outline-none focus:border-white/30 focus:ring-1 focus:ring-white/10 transition-all placeholder:text-white/25"
            />
          </div>
          {error && (
            <div className="text-err text-[12px] mb-3 px-1">{error}</div>
          )}
          <button
            type="submit"
            disabled={loading || !password.trim()}
            className="w-full py-2.5 bg-white hover:bg-white/90 text-black rounded-lg text-[14px] font-semibold cursor-pointer transition-colors border-none disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {loading ? 'Проверка...' : 'Войти'}
          </button>
        </form>
      </div>
    </div>
  )
}

// --- Header ---

function Header({ query, onQuery, onLogout, authRequired }: {
  query: string; onQuery: (q: string) => void; onLogout: () => void; authRequired: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === '/') {
        // Don't hijack "/" when the user is already typing in another
        // input/textarea/contenteditable — otherwise modals like the
        // register form (proxy URL, credentials textarea, etc.) lose
        // focus mid-keystroke.
        const ae = document.activeElement as HTMLElement | null
        const inEditable =
          !!ae &&
          (ae.tagName === 'INPUT' ||
            ae.tagName === 'TEXTAREA' ||
            ae.tagName === 'SELECT' ||
            ae.isContentEditable)
        if (inEditable) return
        e.preventDefault()
        inputRef.current?.focus()
      }
      if (e.key === 'Escape' && document.activeElement === inputRef.current) {
        inputRef.current?.blur()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  return (
    <header className="sticky top-0 z-50 flex items-center justify-between px-6 py-2.5 border-b border-border bg-bg-secondary/80 backdrop-blur-xl">
      <div className="flex items-center gap-2.5">
        <div className="w-8 h-8 rounded-full border border-[#00ff66]/20 flex items-center justify-center overflow-hidden bg-black/40">
          <img src="/logo.png" alt="Вайб Кодер" className="w-full h-full object-cover scale-[1.1]" />
        </div>
        <span className="text-[15px] font-bold tracking-tight text-white cyber-text-glow uppercase">
          ВАЙБ КОДЕР
          <span className="text-[#00ff66] font-mono text-[10px] ml-2 tracking-widest font-normal lowercase bg-[#00ff66]/10 px-2 py-0.5 rounded border border-[#00ff66]/20">notion panel</span>
        </span>
      </div>
      <div className="flex items-center gap-3">
        <a
          href="https://t.me/abuz_ai"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-[11px] font-mono text-[#00ff66]/80 hover:text-[#00ff66] transition-all bg-[#00ff66]/5 hover:bg-[#00ff66]/10 px-2.5 py-1 rounded border border-[#00ff66]/15 hover:border-[#00ff66]/30 hover:shadow-[0_0_8px_rgba(0,255,102,0.15)]"
          style={{ textDecoration: 'none' }}
          title="Наш Telegram-канал"
        >
          <IconTelegram />
          <span>Telegram</span>
        </a>
        <a
          href="https://youtube.com/channel/UC15FjPfHK0F6TpUHJpCfINA?si=sPZ1eTUe7samELP3"
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 text-[11px] font-mono text-[#ff3333]/80 hover:text-[#ff3333] transition-all bg-[#ff3333]/5 hover:bg-[#ff3333]/10 px-2.5 py-1 rounded border border-[#ff3333]/15 hover:border-[#ff3333]/30 hover:shadow-[0_0_8px_rgba(255,51,51,0.15)]"
          style={{ textDecoration: 'none' }}
          title="Наш YouTube-канал"
        >
          <IconYoutube />
          <span>YouTube</span>
        </a>
        <div className="relative w-72">
          <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-text-muted" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="11" cy="11" r="8" /><path d="m21 21-4.35-4.35" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={e => onQuery(e.target.value)}
            placeholder="Поиск аккаунтов, email, тарифов..."
            className="w-full py-1.5 pl-8 pr-10 bg-bg-input border border-border rounded-md text-[13px] text-text-primary outline-none focus:border-white/20 transition-colors placeholder:text-text-muted"
          />
          <kbd className="absolute right-2.5 top-1/2 -translate-y-1/2 text-[11px] text-text-muted bg-bg-card border border-border rounded px-1.5 py-0.5">/</kbd>
        </div>
        {authRequired && (
          <button
            onClick={onLogout}
            className="text-[12px] text-text-secondary hover:text-text-primary cursor-pointer transition-colors bg-transparent border-none px-2 py-1"
            title="Выйти из системы"
          >
            Выйти
          </button>
        )}
      </div>
    </header>
  )
}

function StatCard({ label, value, sub, color, icon }: { label: string; value: string | number; sub: string; color?: string; icon?: React.ReactNode }) {
  return (
    <div className="px-6 py-5">
      <div className="text-[11px] text-text-secondary uppercase tracking-wider mb-1 flex items-center gap-1.5">
        {icon}
        <span>{label}</span>
      </div>
      <div className="text-2xl font-bold tracking-tight tabular-nums" style={color ? { color } : undefined}>{value}</div>
      <div className="text-[11px] text-text-muted mt-1 truncate">{sub}</div>
    </div>
  )
}

function hasPremiumAccess(account: AccountInfo): boolean {
  return !!account.has_premium || (account.premium_limit || 0) > 0 || (account.premium_balance || 0) > 0
}

function getSpaceQuota(account: AccountInfo) {
  const usage = account.space_usage ?? account.usage ?? 0
  const limit = account.space_limit ?? account.limit ?? 0
  const remaining = account.space_remaining ?? Math.max(limit - usage, 0)
  return { usage, limit, remaining }
}

function getUserQuota(account: AccountInfo) {
  const usage = account.user_usage ?? 0
  const limit = account.user_limit ?? 0
  const remaining = account.user_remaining ?? Math.max(limit - usage, 0)
  return { usage, limit, remaining }
}

function isSameQuota(a: { usage: number; limit: number }, b: { usage: number; limit: number }): boolean {
  return a.limit > 0 && a.limit === b.limit && a.usage === b.usage
}

function isResearchLimited(account: AccountInfo): boolean {
  return !hasPremiumAccess(account) && (account.research_usage ?? 0) >= 3
}

function mergeQuotaStatus(statuses: Array<'ok' | 'low' | 'exhausted'>): 'ok' | 'low' | 'exhausted' {
  if (statuses.includes('exhausted')) return 'exhausted'
  if (statuses.includes('low')) return 'low'
  return 'ok'
}

function OverviewBar({ label, usage, limit }: { label: string; usage: number; limit: number }) {
  const pct = getQuotaPct(usage, limit)
  const remaining = Math.max(limit - usage, 0)
  const status = getQuotaStatusByUsage(usage, limit)
  const fillClass = status === 'exhausted' ? 'bg-err opacity-40'
    : status === 'low' ? 'bg-warn' : 'bg-ok'
  const numColor = status === 'exhausted' ? 'text-err'
    : status === 'low' ? 'text-warn' : 'text-text-primary'

  return (
    <div>
      <div className="flex justify-between items-center mb-1.5">
        <span className="text-[10px] text-text-muted uppercase tracking-wider">{label}</span>
        <span className={`text-[11px] font-semibold tabular-nums ${numColor}`}>
          {fmt(remaining)} <span className="text-text-muted font-normal">/ {fmt(limit)} Осталось</span>
        </span>
      </div>
      <div className="h-[2px] bg-white/[.06] rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all duration-500 ${fillClass}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function TotalQuotaBar({ summary }: { summary?: AccountSummary | null }) {
  const totalSpaceUsage = summary?.total_space_usage ?? 0
  const totalSpaceLimit = summary?.total_space_limit ?? 0
  const totalUserUsage = summary?.total_user_usage ?? 0
  const totalUserLimit = summary?.total_user_limit ?? 0
  const totalPremiumBalance = summary?.total_premium_balance ?? 0
  const totalPremiumLimit = summary?.total_premium_limit ?? 0
  const sameBasicQuota = isSameQuota(
    { usage: totalSpaceUsage, limit: totalSpaceLimit },
    { usage: totalUserUsage, limit: totalUserLimit },
  )

  return (
    <div className="mb-5 space-y-3">
      <div className="flex justify-between items-center">
        <span className="text-[11px] text-text-secondary uppercase tracking-wider flex items-center gap-1.5"><IconBarChart /> Обзор лимитов Basic</span>
        {totalPremiumLimit > 0 && (
          <span className="text-[12px] text-text-muted tabular-nums">
            Premium Осталось <span className="text-[#7eb8ff] font-semibold">{fmt(totalPremiumBalance)}</span> / {fmt(totalPremiumLimit)}
          </span>
        )}
      </div>
      {sameBasicQuota ? (
        <OverviewBar label="Basic" usage={totalSpaceUsage} limit={totalSpaceLimit} />
      ) : (
        <>
          <OverviewBar label="Space" usage={totalSpaceUsage} limit={totalSpaceLimit} />
          <OverviewBar label="User" usage={totalUserUsage} limit={totalUserLimit} />
        </>
      )}
    </div>
  )
}

function QuotaBar({ label, labelClass, usage, limit, status }: { label: string; labelClass?: string; usage?: number; limit?: number; status?: 'ok' | 'low' | 'exhausted' }) {
  const pct = getQuotaPct(usage, limit)
  const resolvedStatus = status || getQuotaStatusByUsage(usage, limit)
  const fillClass = resolvedStatus === 'exhausted' ? 'bg-err opacity-40'
    : resolvedStatus === 'low' ? 'bg-warn' : 'bg-ok'
  const numColor = resolvedStatus === 'exhausted' ? 'text-err'
    : resolvedStatus === 'low' ? 'text-warn' : 'text-text-primary'

  return (
    <div className="mb-1.5">
      <div className="flex justify-between items-baseline mb-1">
        <span className={`text-[10px] ${labelClass || 'text-text-muted'}`}>{label}</span>
        <span className={`text-[11px] font-semibold tabular-nums ${numColor}`}>
          {fmt(usage || 0)} <span className="text-text-muted font-normal">/</span> {fmt(limit || 0)}
        </span>
      </div>
      <div className="h-[2px] bg-white/[.06] rounded-full overflow-hidden">
        <div className={`h-full rounded-full transition-all duration-500 ${fillClass}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

function Badge({ children, variant }: { children: React.ReactNode; variant: 'plan' | 'premium' | 'research' | 'warning' | 'model' }) {
  const cls: Record<string, string> = {
    plan: 'text-text-secondary',
    premium: 'text-[#7eb8ff]',
    research: 'text-research',
    warning: 'text-red-400 bg-red-500/10 px-1.5 rounded',
    model: 'text-text-secondary hover:text-white transition-colors cursor-pointer',
  }
  return (
    <span className={`inline-flex items-center gap-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap ${cls[variant] || ''}`}>
      {children}
    </span>
  )
}

function AccountCard({ account, onChanged }: { account: AccountInfo; onChanged: () => void }) {
  const [showModels, setShowModels] = useState(false)
  const spaceQuota = getSpaceQuota(account)
  const userQuota = getUserQuota(account)
  const sameBasicQuota = isSameQuota(spaceQuota, userQuota)
  const premium = hasPremiumAccess(account)
  const researchLimited = isResearchLimited(account)
  const noWorkspace = !!account.no_workspace
  const status = account.permanent || account.exhausted || noWorkspace
    ? 'exhausted'
    : mergeQuotaStatus([
      getQuotaStatusByUsage(spaceQuota.usage, spaceQuota.limit),
      getQuotaStatusByUsage(userQuota.usage, userQuota.limit),
    ])
  const modelCount = account.models?.length || 0

  const dotCls = status === 'exhausted' ? 'bg-err' : status === 'low' ? 'bg-err' : 'bg-ok'
  // no_workspace shares the exhausted card style so the operator
  // immediately sees the account is unhealthy. Click-through is blocked
  // because Notion's /ai SPA hangs indefinitely on these accounts (the
  // root-cause this fix is for).
  const cardBg = account.permanent ? 'bg-bg-exhausted border-white/[0.03] opacity-55'
    : account.exhausted || noWorkspace ? 'bg-bg-exhausted border-white/[0.03]'
    : 'bg-bg-card hover:bg-bg-card-hover border-white/[0.03] hover:border-white/[0.07]'

  const handleClick = () => {
    if (noWorkspace) {
      // Use a native alert — we don't have a toast infra and openProxy
      // would otherwise pop a tab that displays raw JSON 409 to the user.
      alert('У этого аккаунта нет доступной рабочей области Notion. Пожалуйста, зарегистрируйтесь заново или выберите другой аккаунт.')
      return
    }
    openProxy(account.email)
  }

  return (
    <div
      className={`rounded-lg p-4 border ${noWorkspace ? 'cursor-not-allowed' : 'cursor-pointer hover:-translate-y-0.5 hover:shadow-lg hover:shadow-black/30'} transition-all duration-200 ${cardBg}`}
      onClick={handleClick}
      title={noWorkspace ? 'У аккаунта нет доступной рабочей области, он исключен из пула' : undefined}
    >
      {/* Header */}
      <div className="flex items-center gap-2.5 mb-2.5">
        <div
          className="w-8 h-8 rounded-full flex items-center justify-center text-sm font-bold text-white shrink-0"
          style={{ background: avatarColor(account.name) }}
        >
          {avatarLetter(account.name)}
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-semibold truncate">
            {account.name || 'Unknown'}
            {account.space && <span className="text-text-secondary font-normal"> · {account.space}</span>}
          </div>
          <div className="text-[11px] text-text-secondary truncate">{account.email || '—'}</div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <div className={`w-2 h-2 rounded-full ${dotCls}`} />
          <AccountMenu account={account} onChanged={onChanged} />
        </div>
      </div>

      {/* Badges */}
      <div className="flex gap-3 flex-wrap mt-3 mb-2.5 items-center">
        <Badge variant="plan">{account.plan || 'unknown'}</Badge>
        {account.registered_via && (
          <Badge variant="plan">via {providerDisplay(account.registered_via)}</Badge>
        )}
        {premium && <Badge variant="premium">AI Premium</Badge>}
        {(account.research_usage != null && account.research_usage > 0) && (
          <Badge variant={researchLimited ? 'warning' : 'research'}>
            <IconFlask /> Research использовано {account.research_usage}{premium ? '' : '/3'}
          </Badge>
        )}
        {account.exhausted && !account.permanent && <Badge variant="warning">Basic blocked</Badge>}
        {account.permanent && <Badge variant="warning">Free cap</Badge>}
        {noWorkspace && <Badge variant="warning">Без рабочей области</Badge>}
        {modelCount > 0 && (
          <button
            onClick={e => { e.stopPropagation(); setShowModels(!showModels) }}
            className="cursor-pointer border-none bg-transparent p-0 text-[11px] text-text-secondary hover:text-white transition-colors"
          >
            {modelCount} models {showModels ? '▴' : '▾'}
          </button>
        )}
      </div>

      {/* Quotas */}
      {sameBasicQuota ? (
        <QuotaBar label="Basic" usage={spaceQuota.usage} limit={spaceQuota.limit} />
      ) : (
        <>
          <QuotaBar label="Space" usage={spaceQuota.usage} limit={spaceQuota.limit} />
          {userQuota.limit > 0 && <QuotaBar label="User" usage={userQuota.usage} limit={userQuota.limit} />}
        </>
      )}
      {premium && <QuotaBar label="Premium" labelClass="text-[#7eb8ff]" usage={account.premium_usage} limit={account.premium_limit} />}
      <div className="flex flex-wrap gap-3 mt-2 text-[10px] text-text-muted">
        <span>Basic Осталось {fmt(account.remaining || 0)}</span>
        {premium && <span>Premium Осталось {fmt(account.premium_balance || 0)}</span>}
      </div>

      {/* Models (expandable) */}
      {showModels && account.models && account.models.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5 mb-1">
          {account.models.map(m => (
            <span key={m.id} className="text-[10px] px-1.5 py-0.5 bg-white/[.06] rounded text-text-secondary">
              {m.name || m.id}
            </span>
          ))}
        </div>
      )}

      {/* Footer */}
      <div className="flex justify-between items-center mt-2 pt-2 border-t border-border">
        <span className="text-[10px] text-text-muted flex items-center gap-1 min-w-0">
          <IconClock />
          <span className="truncate">Проверено {formatCheckedAt(account.checked_at)} · Посл. AI {formatTimestampMs(account.last_usage_at)}</span>
        </span>
        {noWorkspace ? (
          <span className="text-[11px] text-err font-medium">Недоступен ⚠</span>
        ) : (
          <span className="text-[11px] text-text-secondary hover:text-white font-medium transition-colors">Открыть прокси →</span>
        )}
      </div>
    </div>
  )
}

export default function App() {
  const [authState, setAuthState] = useState<'checking' | 'login' | 'authenticated'>('checking')
  const [authRequired, setAuthRequired] = useState(false)
  const [data, setData] = useState<DashboardData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [quotaRefreshing, setQuotaRefreshing] = useState(false)
  const [refreshStatus, setRefreshStatus] = useState<RefreshStatus | null>(null)
  const [query, setQuery] = useState('')
  const [refreshTime, setRefreshTime] = useState('')
  const [page, setPage] = useState(0)
  const [settings, setSettings] = useState<SearchSettings | null>(null)
  const [tokenStats, setTokenStats] = useState<TokenStats | null>(null)
  const [apiKeyRevealed, setApiKeyRevealed] = useState(false)
  const [registerOpen, setRegisterOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [copiedField, setCopiedField] = useState<'key' | 'base' | null>(null)
  const [showAddModal, setShowAddModal] = useState(false)
  const copyToClipboard = (text: string, field: 'key' | 'base') => {
    navigator.clipboard.writeText(text)
    setCopiedField(field)
    setTimeout(() => setCopiedField(null), 1000)
  }
  // Local draft for the global Notion proxy input. Kept separate from
  // settings.notion_proxy so the user can type without each keystroke
  // hitting the API; we commit on blur/Enter and roll back on error.
  const [proxyDraft, setProxyDraft] = useState('')
  const [proxyError, setProxyError] = useState<string | null>(null)
  const [proxySaving, setProxySaving] = useState(false)
  const PAGE_SIZE = 20

  // Debounced query: typing in the search box shouldn't fire a request
  // on every keystroke; we wait 250ms after the user stops typing and
  // only then re-fetch. The debounced value is what actually goes to
  // the server.
  const [debouncedQuery, setDebouncedQuery] = useState('')
  useEffect(() => {
    const handle = setTimeout(() => setDebouncedQuery(query.trim()), 250)
    return () => clearTimeout(handle)
  }, [query])

  // Check auth on mount
  useEffect(() => {
    checkAuth().then(status => {
      setAuthRequired(status.required)
      if (!status.required || status.authenticated) {
        setAuthState('authenticated')
      } else {
        setAuthState('login')
        setLoading(false)
      }
    }).catch(() => {
      setAuthState('authenticated') // fallback: skip auth
    })
  }, [])

  // loadData fetches the *paginated* account list using the current
  // page + debounced query. The server filters/sorts/slices for us, so
  // `data.accounts` is already the visible page.
  const loadData = useCallback(async () => {
    try {
      const d = await fetchDashboardData({ page, pageSize: PAGE_SIZE, query: debouncedQuery })
      setData(d)
      setError(null)
      setRefreshTime(new Date().toLocaleTimeString('zh-CN'))
      if (d.refresh) {
        setRefreshStatus(d.refresh)
      }
    } catch (e: any) {
      setError(e.message || 'Unknown error')
    } finally {
      setLoading(false)
    }
  }, [page, debouncedQuery])

  useEffect(() => {
    if (authState === 'authenticated') loadData()
  }, [authState, loadData])

  // Settings + token stats are pool-wide and don't change with the
  // current page/query, so we only fetch them on auth — not on every
  // page navigation.
  useEffect(() => {
    if (authState !== 'authenticated') return
    fetchSettings()
      .then(s => {
        setSettings(s)
        setProxyDraft(s.notion_proxy ?? '')
      })
      .catch(() => {})
    fetchTokenStats().then(setTokenStats).catch(() => {})
  }, [authState])

  const handleLogout = async () => {
    await logout()
    setAuthState('login')
    setData(null)
  }

  const refresh = async () => {
    setRefreshing(true)
    await loadData()
    setRefreshing(false)
  }

  const handleQuotaRefresh = async () => {
    setQuotaRefreshing(true)
    try {
      await triggerRefresh()
      // Start polling immediately
      setRefreshStatus(prev => prev ? { ...prev, refreshing: true, done: 0 } : { refreshing: true, done: 0, total: 0 })
    } catch { /* ignore */ }
    setQuotaRefreshing(false)
  }

  const toggleSetting = async (key: 'enable_web_search' | 'enable_workspace_search' | 'ask_mode_default' | 'debug_logging') => {
    if (!settings) return
    const newVal = !settings[key]
    try {
      const updated = await updateSettings({ [key]: newVal })
      setSettings(updated)
    } catch { /* ignore */ }
  }

  // saveProxy commits the proxy input draft. We skip the round trip when
  // the value is unchanged (typical blur after focus). Backend rejects
  // unsupported schemes with HTTP 400 + JSON error; we surface the
  // message inline and roll the input back to the persisted value so a
  // typo doesn't get silently saved.
  const saveProxy = async () => {
    if (!settings) return
    const next = proxyDraft.trim()
    if (next === (settings.notion_proxy ?? '').trim()) {
      setProxyDraft(settings.notion_proxy ?? '')
      setProxyError(null)
      return
    }
    setProxySaving(true)
    setProxyError(null)
    try {
      const updated = await updateSettings({ notion_proxy: next })
      setSettings(updated)
      setProxyDraft(updated.notion_proxy ?? '')
    } catch (e: any) {
      setProxyError(e?.message || 'Ошибка сохранения')
      setProxyDraft(settings.notion_proxy ?? '')
    } finally {
      setProxySaving(false)
    }
  }

  // Auto-poll when backend is refreshing quotas
  useEffect(() => {
    if (!refreshStatus?.refreshing) return
    const interval = setInterval(async () => {
      await loadData()
    }, 3000)
    return () => clearInterval(interval)
  }, [refreshStatus?.refreshing, loadData])

  // Server-paginated: data.accounts is already the visible page slice
  // (filtered + sorted server-side). filtered_total tells us how many
  // entries match the current query across the whole pool, which is
  // what we need to render pagination controls.
  const accounts = data?.accounts || []
  const paged = accounts
  const filteredTotal = data?.filtered_total ?? data?.total ?? accounts.length
  const totalPages = Math.max(1, Math.ceil(filteredTotal / PAGE_SIZE))

  // Reset page when the (debounced) query changes so the user always
  // lands on the first page of new search results.
  useEffect(() => { setPage(0) }, [debouncedQuery])
  // Clamp `page` if the result set shrank below the current page.
  useEffect(() => {
    if (page > 0 && page >= totalPages) setPage(Math.max(0, totalPages - 1))
  }, [page, totalPages])

  const summary = useMemo(() => {
    if (!data) return null
    const s = data.summary
    // Note: backend's AvailableCount already excludes no_workspace, so
    // (total - available) lumps "exhausted" and "no workspace" together.
    // We split them out explicitly for the operator.
    const exhausted = data.total - data.available
    const availableRate = data.total > 0 ? Math.round((data.available / data.total) * 100) : 0
    const sameBasicQuota = isSameQuota(
      { usage: s?.total_space_usage ?? 0, limit: s?.total_space_limit ?? 0 },
      { usage: s?.total_user_usage ?? 0, limit: s?.total_user_limit ?? 0 },
    )
    return {
      exhausted,
      exhaustedOnly: s?.exhausted_only ?? 0,
      noWorkspace: s?.no_workspace ?? 0,
      availableRate,
      totalResearchUsage: s?.total_research_usage ?? 0,
      totalRemaining: s?.total_remaining ?? 0,
      totalSpaceRemaining: s?.total_space_remaining ?? 0,
      totalUserRemaining: s?.total_user_remaining ?? 0,
      totalPremiumBalance: s?.total_premium_balance ?? 0,
      totalPremiumLimit: s?.total_premium_limit ?? 0,
      premiumAccounts: s?.premium_accounts ?? 0,
      researchLimited: s?.research_limited ?? 0,
      sameBasicQuota,
    }
  }, [data])

  // Auth checking spinner
  if (authState === 'checking') {
    return (
      <div className="flex items-center justify-center h-screen gap-3 text-text-secondary text-sm">
        <div className="w-4 h-4 border-2 border-border border-t-notion-blue rounded-full animate-spin" />
      </div>
    )
  }

  // Login page
  if (authState === 'login') {
    return <LoginPage onSuccess={() => { setAuthState('authenticated'); setLoading(true) }} />
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-screen gap-3 text-text-secondary text-sm">
        <div className="w-4 h-4 border-2 border-border border-t-notion-blue rounded-full animate-spin" />
        Загрузка аккаунтов...
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="flex items-center justify-center h-screen text-err text-sm">
        Ошибка загрузки: {error}
      </div>
    )
  }

  return (
    <div className="min-h-screen">
      <Header query={query} onQuery={setQuery} onLogout={handleLogout} authRequired={authRequired} />

      <main className="max-w-[1280px] mx-auto px-6 py-6">
        {/* Summary */}
        {summary && (
          <div className="grid grid-cols-5 divide-x divide-white/[.05] mb-6 max-lg:grid-cols-3 max-md:grid-cols-2 max-md:divide-x-0 max-sm:grid-cols-1">
            <StatCard
              label="Всего аккаунтов" value={data!.total}
              sub={summary.noWorkspace > 0
                ? `${data!.available} Активно / ${summary.exhaustedOnly} Исчерпано / ${summary.noWorkspace} Без рабочей области`
                : `${data!.available} Активно / ${summary.exhausted} Исчерпано`}
            />
            <StatCard
              label="Активно" value={data!.available}
              sub={`Доля ${summary.availableRate}%`}
              color="var(--color-ok)"
            />
            <StatCard
              label="Basic Осталось" value={fmt(summary.totalRemaining)}
              sub={summary.sameBasicQuota
                ? 'Лимиты Space / User совпадают'
                : `Space ${fmt(summary.totalSpaceRemaining)} · User ${fmt(summary.totalUserRemaining)}`}
            />
            <StatCard
              label="Premium Осталось" value={fmt(summary.totalPremiumBalance)}
              sub={summary.totalPremiumLimit > 0
                ? `${summary.premiumAccounts} аккаунта premium · Использование Research ${summary.totalResearchUsage}`
                : `Нет premium лимитов · Research ограничен ${summary.researchLimited}`}
              color="var(--color-research, #9b51e0)"
            />
            <StatCard
              icon={<IconActivity />}
              label="Расход токенов"
              value={formatTokens(tokenStats?.total.total ?? 0)}
              sub={tokenStats
                ? `Сегодня ${formatTokens(tokenStats.today.total)} · Вход ${formatTokens(tokenStats.today.input)} · Выход ${formatTokens(tokenStats.today.output)}`
                : 'Расход токенов отсутствует'}
              color="var(--color-notion-blue)"
            />
          </div>
        )}

        {/* Total Quota Bar */}
        <TotalQuotaBar summary={data?.summary} />

        {/* Refresh Status Banner */}
        {refreshStatus?.refreshing && (
          <div className="bg-notion-blue/10 border border-notion-blue/20 rounded-lg p-3 mb-5 flex items-center gap-3">
            <div className="w-4 h-4 border-2 border-notion-blue/30 border-t-notion-blue rounded-full animate-spin shrink-0" />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-medium text-[#5c9ce6]">
                Обновление лимитов... {refreshStatus.done}/{refreshStatus.total}
              </div>
              <div className="h-1.5 bg-white/[.06] rounded-full overflow-hidden mt-1.5">
                <div
                  className="h-full bg-notion-blue rounded-full transition-all duration-500"
                  style={{ width: `${refreshStatus.total > 0 ? (refreshStatus.done / refreshStatus.total) * 100 : 0}%` }}
                />
              </div>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2.5 mb-5 flex-wrap">
          <button
            onClick={openBestProxy}
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-white hover:bg-white/90 text-[#111] rounded-md text-[13px] font-medium cursor-pointer transition-colors border-none"
          >
            <IconZap /> Открыть лучший аккаунт
          </button>
          <button
            onClick={handleQuotaRefresh}
            disabled={quotaRefreshing || refreshStatus?.refreshing}
            className={`inline-flex items-center gap-1.5 px-4 py-2 bg-bg-card hover:bg-bg-card-hover text-text-primary rounded-md text-[13px] font-medium cursor-pointer transition-colors border border-border disabled:opacity-50 disabled:cursor-not-allowed ${refreshStatus?.refreshing ? 'animate-pulse' : ''}`}
          >
            <IconRefresh /> Обновить лимиты
          </button>
          <button
            onClick={refresh}
            disabled={refreshing}
            className={`inline-flex items-center gap-1.5 px-4 py-2 bg-bg-card hover:bg-bg-card-hover text-text-primary rounded-md text-[13px] font-medium cursor-pointer transition-colors border border-border disabled:opacity-50 disabled:cursor-not-allowed ${refreshing ? 'animate-pulse' : ''}`}
          >
            <IconRefresh /> Обновить данные
          </button>
          <button
            onClick={() => setShowAddModal(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-bg-card hover:bg-bg-card-hover text-text-primary rounded-md text-[13px] font-medium cursor-pointer transition-colors border border-border"
          >
            <IconPlus /> Добавить аккаунт
          </button>
          <button
            onClick={() => setRegisterOpen(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-bg-card hover:bg-bg-card-hover text-text-primary rounded-md text-[13px] font-medium cursor-pointer transition-colors border border-border"
          >
            <IconUserPlus size={13} /> Регистрация аккаунтов
          </button>
          <button
            onClick={() => setHistoryOpen(true)}
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-bg-card hover:bg-bg-card-hover text-text-primary rounded-md text-[13px] font-medium cursor-pointer transition-colors border border-border"
          >
            <IconHistory size={13} /> История задач
          </button>
          {refreshTime && (
            <span className="text-[11px] text-text-muted">
              Обновлено в {refreshTime}
              {refreshStatus?.last_refresh_at && !refreshStatus.refreshing && (
                <> · Лимиты обновлены в {new Date(refreshStatus.last_refresh_at).toLocaleTimeString('zh-CN')}</>
              )}
            </span>
          )}
        </div>

        {/* Admin Models Configuration */}
        <AdminModels />

        {/* API Settings */}
        {settings && (() => {
          const apiKey = document.querySelector('meta[name="api-key"]')?.getAttribute('content') || ''
          const apiBase = `${window.location.origin}/v1`
          const maskedKey = apiKey ? apiKey.slice(0, 5) + '•'.repeat(Math.max(0, apiKey.length - 9)) + apiKey.slice(-4) : ''
          return (
            <div className="mb-6 px-4 py-3 bg-[#171717] border border-white/5 rounded-lg shadow-inner">
              <div className="flex items-center gap-6 flex-wrap">
                <span className="text-[12px] text-text-secondary font-medium flex items-center gap-2 shrink-0">
                  <IconSettings /> Настройки API
                </span>
                <div className="flex items-center gap-6 flex-wrap">
                  <div className="flex items-center gap-1.5">
                    <span className="text-[11px] text-text-muted">API Key</span>
                    <code
                      className={`text-[11px] bg-white/[.05] px-1.5 py-0.5 rounded cursor-pointer hover:bg-white/[.1] transition-colors font-mono ${copiedField === 'key' ? 'text-ok' : 'text-text-primary'}`}
                      onClick={() => copyToClipboard(apiKey, 'key')}
                      title="Нажмите для копирования"
                    >
                      {copiedField === 'key' ? '✓ Скопировано' : (apiKeyRevealed ? apiKey : maskedKey)}
                    </code>
                    <button
                      onClick={() => setApiKeyRevealed(!apiKeyRevealed)}
                      className="ml-3 text-text-muted hover:text-text-primary transition-colors bg-transparent border-none cursor-pointer px-0.5 flex items-center"
                      title={apiKeyRevealed ? 'Скрыть' : 'Показать'}
                    >
                      {apiKeyRevealed ? (
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><line x1="1" y1="1" x2="23" y2="23"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>
                        </svg>
                      ) : (
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
                        </svg>
                      )}
                    </button>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[11px] text-text-muted">Base URL</span>
                    <code
                      className={`text-[11px] bg-white/[.05] px-1.5 py-0.5 rounded cursor-pointer hover:bg-white/[.1] transition-colors font-mono ${copiedField === 'base' ? 'text-ok' : 'text-text-primary'}`}
                      onClick={() => copyToClipboard(apiBase, 'base')}
                      title="Нажмите для копирования"
                    >
                      {copiedField === 'base' ? '✓ Скопировано' : apiBase}
                    </code>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-[11px] text-text-muted">Глобальный прокси</span>
                    <span
                      className={`inline-block w-1.5 h-1.5 rounded-full ${proxyError ? 'bg-err' : settings.notion_proxy ? 'bg-ok' : 'bg-text-muted/60'}`}
                      title={proxyError ? proxyError : settings.notion_proxy ? 'Прокси включен' : 'Напрямую'}
                    />
                    <input
                      type="text"
                      value={proxyDraft}
                      onChange={e => { setProxyDraft(e.target.value); if (proxyError) setProxyError(null) }}
                      onBlur={saveProxy}
                      onKeyDown={e => {
                        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
                        if (e.key === 'Escape') {
                          setProxyDraft(settings.notion_proxy ?? '')
                          setProxyError(null)
                          ;(e.target as HTMLInputElement).blur()
                        }
                      }}
                      placeholder="Пусто = напрямую"
                      disabled={proxySaving}
                      className={`text-[11px] bg-white/[.05] px-1.5 py-0.5 rounded font-mono outline-none border w-[160px] focus:w-[280px] transition-[width,border-color] duration-150 ${proxyError ? 'border-err text-err' : 'border-transparent focus:border-white/20 text-text-primary'} placeholder:text-text-muted/60`}
                      title={proxyError || (settings.notion_proxy ? `Текущий: ${settings.notion_proxy}` : 'Текущий: Напрямую')}
                    />
                  </div>
                </div>
                <div className="flex items-center gap-5 ml-auto">
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <button
                      onClick={() => toggleSetting('enable_web_search')}
                      className={`relative w-7 h-4 rounded-full transition-colors duration-200 cursor-pointer border-none ${settings.enable_web_search ? 'bg-[#4dab9a]' : 'bg-white/10 border border-white/5'}`}
                    >
                      <span className={`absolute top-[2px] left-[2px] w-3 h-3 rounded-full transition-all duration-200 ${settings.enable_web_search ? 'bg-white shadow-sm translate-x-[12px]' : 'bg-white/40'}`} />
                    </button>
                    <span className="text-[12px] text-white font-medium">Поиск в сети</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <button
                      onClick={() => toggleSetting('enable_workspace_search')}
                      className={`relative w-7 h-4 rounded-full transition-colors duration-200 cursor-pointer border-none ${settings.enable_workspace_search ? 'bg-[#4dab9a]' : 'bg-white/10 border border-white/5'}`}
                    >
                      <span className={`absolute top-[2px] left-[2px] w-3 h-3 rounded-full transition-all duration-200 ${settings.enable_workspace_search ? 'bg-white shadow-sm translate-x-[12px]' : 'bg-white/40'}`} />
                    </button>
                    <span className="text-[12px] text-text-primary">Поиск в раб. пространстве</span>
                  </label>
                  <label
                    className="flex items-center gap-2 cursor-pointer select-none"
                    title="Если включено, все запросы по умолчанию идут в Режим ASK (только ответ, без записи). Единократное включение: добавьте -ask к модели, например claude-sonnet-4.6-ask"
                  >
                    <button
                      onClick={() => toggleSetting('ask_mode_default')}
                      className={`relative w-7 h-4 rounded-full transition-colors duration-200 cursor-pointer border-none ${settings.ask_mode_default ? 'bg-[#4dab9a]' : 'bg-white/10 border border-white/5'}`}
                    >
                      <span className={`absolute top-[2px] left-[2px] w-3 h-3 rounded-full transition-all duration-200 ${settings.ask_mode_default ? 'bg-white shadow-sm translate-x-[12px]' : 'bg-white/40'}`} />
                    </button>
                    <span className="text-[12px] text-text-primary">Режим ASK</span>
                  </label>
                  <label className="flex items-center gap-2 cursor-pointer select-none">
                    <button
                      onClick={() => toggleSetting('debug_logging')}
                      className={`relative w-7 h-4 rounded-full transition-colors duration-200 cursor-pointer border-none ${settings.debug_logging ? 'bg-[#4dab9a]' : 'bg-white/10 border border-white/5'}`}
                    >
                      <span className={`absolute top-[2px] left-[2px] w-3 h-3 rounded-full transition-all duration-200 ${settings.debug_logging ? 'bg-white shadow-sm translate-x-[12px]' : 'bg-white/40'}`} />
                    </button>
                    <span className="text-[12px] text-text-primary">Логи отладки</span>
                  </label>
                </div>
              </div>
            </div>
          )
        })()}

        {/* Section Title */}
        <div className="text-[12px] font-semibold text-text-secondary uppercase tracking-wider mb-3.5 flex items-center gap-1.5">
          <span>Пул аккаунтов</span>
          <span className="font-normal text-text-muted">({filteredTotal})</span>
        </div>

        {/* Grid */}
        {filteredTotal === 0 ? (
          <div className="text-center py-16 text-text-secondary text-sm">
            Совпадающих аккаунтов не найдено
          </div>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-2.5 mb-4">
            {paged.map(acc => (
              <AccountCard key={acc.email} account={acc} onChanged={loadData} />
            ))}
          </div>
        )}

        {/* Pagination */}
        {totalPages > 1 && (
          <div className="flex items-center justify-center gap-2 mb-10">
            <button
              onClick={() => setPage(0)}
              disabled={page === 0}
              className="px-2.5 py-1.5 bg-bg-card hover:bg-bg-card-hover text-text-secondary rounded-md text-[12px] cursor-pointer transition-colors border border-border disabled:opacity-30 disabled:cursor-not-allowed"
            >
              «
            </button>
            <button
              onClick={() => setPage(p => Math.max(0, p - 1))}
              disabled={page === 0}
              className="px-2.5 py-1.5 bg-bg-card hover:bg-bg-card-hover text-text-secondary rounded-md text-[12px] cursor-pointer transition-colors border border-border disabled:opacity-30 disabled:cursor-not-allowed"
            >
              ‹ Назад
            </button>
            <span className="text-[12px] text-text-secondary tabular-nums px-3">
              {page + 1} / {totalPages}
            </span>
            <button
              onClick={() => setPage(p => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
              className="px-2.5 py-1.5 bg-bg-card hover:bg-bg-card-hover text-text-secondary rounded-md text-[12px] cursor-pointer transition-colors border border-border disabled:opacity-30 disabled:cursor-not-allowed"
            >
              Вперед ›
            </button>
            <button
              onClick={() => setPage(totalPages - 1)}
              disabled={page >= totalPages - 1}
              className="px-2.5 py-1.5 bg-bg-card hover:bg-bg-card-hover text-text-secondary rounded-md text-[12px] cursor-pointer transition-colors border border-border disabled:opacity-30 disabled:cursor-not-allowed"
            >
              »
            </button>
          </div>
        )}
      </main>
      {showAddModal && <AddAccountModal onClose={() => setShowAddModal(false)} onSuccess={loadData} />}

      <RegisterModal
        open={registerOpen}
        onClose={() => setRegisterOpen(false)}
        onJobFinished={() => {
          // Immediate reload so newly-registered accounts show up. The
          // backend kicks off a per-account quota refresh in a goroutine
          // after each success; that lands a few seconds later, so we
          // schedule a second reload to pick up the freshly-cached
          // quota_info from disk.
          loadData()
          window.setTimeout(() => { loadData() }, 4000)
        }}
      />
      <HistoryDrawer
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        onRetryStarted={() => {
          // Reload account list once the retry finishes so newly succeeded
          // accounts surface in the dashboard. Best-effort: the drawer's
          // own poller picks up live counters in the meantime.
          window.setTimeout(() => { loadData() }, 4000)
        }}
      />
    </div>
  )
}
