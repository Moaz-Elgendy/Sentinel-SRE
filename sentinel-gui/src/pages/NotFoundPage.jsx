import { Link } from 'react-router-dom'

export default function NotFoundPage() {
  return (
    <div className="empty-state">
      <h1>404</h1>
      <p>That page doesn't exist.</p>
      <Link to="/" className="button button--primary">
        Back to dashboard
      </Link>
    </div>
  )
}
