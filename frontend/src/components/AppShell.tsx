import { ReactNode } from 'react'

interface Props {
  title: string
  subtitle: string
  badge?: ReactNode
  nav?: ReactNode
  children: ReactNode
}

function AppShell({ title, subtitle, badge, nav, children }: Props) {
  return (
    <div className="shell">
      <header className="shell__header">
        <div className="shell__brand">
          <h1 className="shell__title">{title}</h1>
          <span className="shell__subtitle">{subtitle}</span>
        </div>
        {badge}
        {nav && <nav className="view-tabs" aria-label="视图切换">{nav}</nav>}
      </header>
      {children}
    </div>
  )
}

export default AppShell
