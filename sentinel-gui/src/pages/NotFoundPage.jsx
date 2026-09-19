import { Link } from 'react-router-dom'
import EmptyState from '../components/ui/EmptyState.jsx'
import { usePageTitle } from '../hooks/usePageTitle.js'

export default function NotFoundPage() {
  usePageTitle('Page not found')
  return (
    <main className="auth-page">
      <EmptyState
        icon="search"
        title="Page not found"
        description="That page doesn't exist, or it has moved."
        action={
          <Link to="/" className="button button--primary">
            Back to dashboard
          </Link>
        }
      />
    </main>
  )
}
