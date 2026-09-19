import { Link } from 'react-router-dom'
import Icon from './Icon.jsx'

/**
 * breadcrumb: [{ to, label }] — shown above the title for detail pages so the
 * way back to the list is always one click away.
 * titleExtra: badges/controls that sit on the same line as the title.
 */
export default function PageHeader({ title, subtitle, breadcrumb, titleExtra, actions }) {
  return (
    <header className="page-header">
      <div className="page-header__main">
        {breadcrumb && (
          <nav className="breadcrumb" aria-label="Breadcrumb">
            {breadcrumb.map((crumb) => (
              <Link key={crumb.to} to={crumb.to} className="cluster" style={{ gap: 4 }}>
                <Icon name="arrowLeft" size={12} />
                {crumb.label}
              </Link>
            ))}
          </nav>
        )}
        <div className="page-header__title-row">
          <h1>{title}</h1>
          {titleExtra}
        </div>
        {subtitle && <div className="page-header__subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  )
}
