import { SearchX } from 'lucide-react'
import { Link } from 'react-router-dom'
import { EmptyState } from '@/components/sentinel/States'
import { Button } from '@/components/ui/button'
import { usePageTitle } from '@/hooks/usePageTitle'

export default function NotFoundPage() {
  usePageTitle('Page not found')
  return (
    <main className="grid min-h-svh place-items-center">
      <EmptyState
        icon={SearchX}
        title="Page not found"
        description="That page doesn't exist, or it has moved."
        action={
          <Button asChild>
            <Link to="/">Back to command center</Link>
          </Button>
        }
      />
    </main>
  )
}
