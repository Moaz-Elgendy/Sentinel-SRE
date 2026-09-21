import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { ConfigLayout } from './components/config/ConfigShell.jsx'
import AppLayout from './components/layout/AppLayout.jsx'
import ProtectedRoute from './components/layout/ProtectedRoute.jsx'
import { SkeletonRows } from './components/sentinel/States.jsx'
import { AuthProvider } from './context/AuthContext.jsx'
import DashboardPage from './pages/DashboardPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import NotFoundPage from './pages/NotFoundPage.jsx'

// The command center and login load with the app; everything else is split into
// its own chunk so an SRE opening Sentinel during an incident downloads only
// what they look at.
const IncidentsListPage = lazy(() => import('./pages/IncidentsListPage.jsx'))
const IncidentDetailPage = lazy(() => import('./pages/IncidentDetailPage.jsx'))
const SentinelLogsPage = lazy(() => import('./pages/SentinelLogsPage.jsx'))
const ActionHistoryPage = lazy(() => import('./pages/ActionHistoryPage.jsx'))
const EnvironmentPage = lazy(() => import('./pages/EnvironmentPage.jsx'))
const PerformancePage = lazy(() => import('./pages/PerformancePage.jsx'))
const PoliciesPage = lazy(() => import('./pages/PoliciesPage.jsx'))
const RcaConfigPage = lazy(() => import('./pages/RcaConfigPage.jsx'))
const RemediationConfigPage = lazy(() => import('./pages/RemediationConfigPage.jsx'))
const AiConfigPage = lazy(() => import('./pages/AiConfigPage.jsx'))
const MonitoringConfigPage = lazy(() => import('./pages/MonitoringConfigPage.jsx'))
const ConfigHistoryPage = lazy(() => import('./pages/ConfigHistoryPage.jsx'))
const DemoChaosPage = lazy(() => import('./pages/DemoChaosPage.jsx'))

export default function App() {
  return (
    <AuthProvider>
      <Suspense fallback={<SkeletonRows rows={6} className="p-6" />}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />

          <Route element={<ProtectedRoute />}>
            <Route element={<AppLayout />}>
              <Route path="/" element={<DashboardPage />} />
              <Route path="/incidents" element={<IncidentsListPage />} />
              <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
              <Route path="/logs" element={<SentinelLogsPage />} />
              <Route path="/actions" element={<ActionHistoryPage />} />
              <Route path="/environment" element={<EnvironmentPage />} />
              <Route path="/performance" element={<PerformancePage />} />
              {/* Sentinel Live was merged into the command center; keep old links working. */}
              <Route path="/live" element={<Navigate to="/" replace />} />
              <Route element={<ConfigLayout />}>
                <Route path="/policies" element={<PoliciesPage />} />
                <Route path="/rca-config" element={<RcaConfigPage />} />
                <Route path="/remediation-config" element={<RemediationConfigPage />} />
                <Route path="/ai-config" element={<AiConfigPage />} />
                <Route path="/monitoring-config" element={<MonitoringConfigPage />} />
                <Route path="/config-history" element={<ConfigHistoryPage />} />
              </Route>
              <Route path="/demo" element={<DemoChaosPage />} />
            </Route>
          </Route>

          <Route path="/404" element={<NotFoundPage />} />
          <Route path="*" element={<Navigate to="/404" replace />} />
        </Routes>
      </Suspense>
    </AuthProvider>
  )
}
