import { Navigate, Route, Routes } from 'react-router-dom'
import AppLayout from './components/layout/AppLayout.jsx'
import ProtectedRoute from './components/layout/ProtectedRoute.jsx'
import { AuthProvider } from './context/AuthContext.jsx'
import ActionHistoryPage from './pages/ActionHistoryPage.jsx'
import DashboardPage from './pages/DashboardPage.jsx'
import DemoChaosPage from './pages/DemoChaosPage.jsx'
import EnvironmentPage from './pages/EnvironmentPage.jsx'
import IncidentDetailPage from './pages/IncidentDetailPage.jsx'
import IncidentsListPage from './pages/IncidentsListPage.jsx'
import LoginPage from './pages/LoginPage.jsx'
import NotFoundPage from './pages/NotFoundPage.jsx'
import PerformancePage from './pages/PerformancePage.jsx'

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route path="/login" element={<LoginPage />} />

        <Route element={<ProtectedRoute />}>
          <Route element={<AppLayout />}>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/incidents" element={<IncidentsListPage />} />
            <Route path="/incidents/:incidentId" element={<IncidentDetailPage />} />
            <Route path="/environment" element={<EnvironmentPage />} />
            <Route path="/actions" element={<ActionHistoryPage />} />
            <Route path="/performance" element={<PerformancePage />} />
            <Route path="/demo" element={<DemoChaosPage />} />
          </Route>
        </Route>

        <Route path="/404" element={<NotFoundPage />} />
        <Route path="*" element={<Navigate to="/404" replace />} />
      </Routes>
    </AuthProvider>
  )
}
